"""Shared Claude API plumbing: model config, prompt loading, and a
pause_turn/refusal-aware request helper used by every subagent.

Model choice: claude-opus-4-8 everywhere. Opus-tier reasoning matters for
synthesis and adversarial verification; researchers run at lower effort
instead of a smaller model so the whole system stays on one cache-friendly
model. Override with AGENT_MODEL.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import anthropic

def _load_dotenv() -> None:
    """`cp .env.example .env`, put the key in, done. Real shell env vars win
    (setdefault); no python-dotenv dependency needed for KEY=VALUE lines."""
    try:
        lines = (Path(__file__).parent / ".env").read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

MODEL = os.environ.get("AGENT_MODEL", "claude-opus-4-8")
# "api" (Anthropic API, needs ANTHROPIC_API_KEY) or "cli" (local `claude` CLI,
# bills the reviewer's Claude subscription - no key). See runtime_cli.py.
RUNTIME = os.environ.get("AGENT_RUNTIME", "api")
PROMPT_DIR = Path(__file__).parent / "prompts"

# One retry layer beyond the SDK's built-in retries, for overload spikes.
_RETRYABLE_PAUSES = 3


def load_prompt(name: str, **subs: str) -> str:
    text = (PROMPT_DIR / f"{name}.md").read_text()
    for key, val in subs.items():
        text = text.replace("{{" + key + "}}", val)
    return text


def client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def run_subagent(
    system: str,
    user_content: str,
    *,
    tools: list | None = None,
    output_schema: dict | None = None,
    effort: str = "medium",
    max_tokens: int = 16000,
    max_pause_resumes: int = 6,
    model: str | None = None,
) -> dict | str:
    """Run one self-contained subagent conversation to completion.

    Handles the three stop reasons that matter for a server-tool loop:
      - pause_turn: server tool loop hit its iteration limit -> resume
      - refusal:    surface as a structured failure, never crash the run
      - max_tokens: return what we have, flagged

    Returns the parsed JSON object when output_schema is given, else final text.
    """
    if os.environ.get("AGENT_RUNTIME", RUNTIME) == "cli":
        import runtime_cli
        return runtime_cli.run_subagent_cli(
            system, user_content, tools=tools, output_schema=output_schema, model=model)
    c = client()
    kwargs: dict = {
        "model": model or MODEL,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
    }
    if tools:
        kwargs["tools"] = tools
    if output_schema:
        kwargs["output_config"]["format"] = {"type": "json_schema", "schema": output_schema}
    kwargs["_user_content"] = user_content

    # Feature availability differs across API orgs, models and API versions
    # (structured-output format, adaptive thinking, effort, server web tools,
    # model ids). A 400 here is a compatibility gap, not a source wall - walk
    # the degradation ladder instead of failing the lane.
    def _attempt(kw):
        return _run_conversation(c, kw, output_schema, max_pause_resumes)

    try:
        return _attempt(kwargs)
    except anthropic.BadRequestError as e:
        msg = str(e)
        if "web_search" in msg or "web_fetch" in msg:
            hint = ("this API key/org does not have server-side web search enabled - "
                    "enable it in the Anthropic console, or run keyless: "
                    "python agent.py --runtime cli")
            return {"_error": f"web tools unavailable: {hint}"} if output_schema else f"[{hint}]"
        for degraded in _degradations(kwargs, output_schema, msg):
            try:
                return _attempt(degraded)
            except anthropic.BadRequestError as e2:
                msg = str(e2)
                continue
        return ({"_error": f"invalid request after all fallbacks: {msg[:500]}"}
                if output_schema else f"[invalid request after all fallbacks: {msg[:500]}]")


def _degradations(kwargs: dict, output_schema: dict | None, err_msg: str):
    """Progressively simpler request shapes, most-likely culprit first. When
    schema enforcement is dropped, the schema moves into the prompt and the
    reply is parsed defensively instead."""
    import copy

    def prompt_json(kw):
        if output_schema:
            kw["_parse_text_json"] = True
            kw["_user_content"] = (kwargs["_user_content"]
                + "\n\nOUTPUT FORMAT: respond with ONLY a single JSON object (no prose, no "
                  "markdown fences) valid against this JSON Schema:\n" + json.dumps(output_schema))
        return kw

    # 1. drop structured-output format (schema moves into the prompt)
    if output_schema:
        kw = copy.deepcopy(kwargs)
        kw["output_config"].pop("format", None)
        yield prompt_json(kw)
    # 2. drop thinking + effort too (older API surfaces)
    kw = copy.deepcopy(kwargs)
    kw.pop("thinking", None)
    kw.pop("output_config", None)
    yield prompt_json(kw)
    # 3. fall back to the flagship model with the minimal shape
    kw = copy.deepcopy(kw)
    kw["model"] = MODEL
    yield prompt_json(kw)


def _run_conversation(c, kwargs: dict, output_schema: dict | None, max_pause_resumes: int):
    """One subagent conversation to completion for a given request shape."""
    kwargs = dict(kwargs)
    parse_text_json = kwargs.pop("_parse_text_json", False)
    user_content = kwargs.pop("_user_content")
    messages: list = [{"role": "user", "content": user_content}]
    resumes = 0
    while True:
        response = _create_with_backoff(c, messages=messages, **kwargs)
        if response.stop_reason == "pause_turn" and resumes < max_pause_resumes:
            messages.append({"role": "assistant", "content": response.content})
            resumes += 1
            continue
        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            reason = getattr(detail, "explanation", "") or "request declined by safety system"
            return {"_error": f"refusal: {reason}"} if output_schema else f"[refused: {reason}]"
        text = "".join(b.text for b in response.content if b.type == "text")
        if output_schema:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                if parse_text_json:
                    import runtime_cli
                    parsed = runtime_cli._parse_json(text)
                    if parsed is not None:
                        return parsed
                return {"_error": f"unparseable structured output (stop_reason={response.stop_reason})",
                        "_raw": text[:2000]}
        if response.stop_reason == "max_tokens":
            text += "\n[truncated: hit max_tokens]"
        return text


def _create_with_backoff(c: anthropic.Anthropic, **kwargs):
    delay = 5.0
    for attempt in range(4):
        try:
            return c.messages.create(**kwargs)
        except anthropic.RateLimitError:
            if attempt == 3:
                raise
            time.sleep(delay)
            delay *= 2
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 and attempt < 3:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable")


def web_tools(search_uses: int, fetch_uses: int, fetch_token_cap: int = 25000) -> list[dict]:
    return [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": search_uses},
        {"type": "web_fetch_20260209", "name": "web_fetch",
         "max_uses": fetch_uses, "max_content_tokens": fetch_token_cap},
    ]
