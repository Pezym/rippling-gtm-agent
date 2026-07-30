"""Keyless runtime: run every model call through the local `claude` CLI
instead of the Anthropic API.

Why this exists: reviewers with a Claude subscription (Claude Code) but no
API key can run the full agent. `claude -p` inherits the CLI's login, so
calls bill against the subscription — zero marginal cost, no ANTHROPIC_API_KEY.

Tradeoffs vs the API runtime, stated honestly:
  - No server-enforced tool budgets: WebSearch/WebFetch caps become prompt
    instructions, not hard limits.
  - No schema-validated structured output: we ask for bare JSON and parse
    defensively, with one retry.
  - No effort/thinking control from here; the CLI decides.
The formation, prompts, ledger, verifier, and evals are identical either way.

Setup (one-time): `npm install -g @anthropic-ai/claude-code && claude login`
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

CLI = os.environ.get("CLAUDE_CLI", "claude")
# CLI model aliases (opus/sonnet/haiku) and full model IDs both work.
CLI_MODEL = os.environ.get("AGENT_MODEL", "claude-opus-4-8")

# Web-only, read-only: researchers may search and fetch, never touch the
# machine. Non-allowed tools are auto-denied in -p (non-interactive) mode;
# the disallow list is belt and braces.
_ALLOWED = ["WebSearch", "WebFetch"]
_DISALLOWED = ["Bash", "Edit", "Write", "NotebookEdit", "Read", "Glob", "Grep", "Task"]


def available() -> str | None:
    """Return the CLI path if usable, else None."""
    return shutil.which(CLI)


def _base_cmd(allow_web: bool, model: str | None = None) -> list[str]:
    cmd = [
        CLI, "-p",
        "--model", model or CLI_MODEL,
        "--no-session-persistence",
        # Don't load the user's personal settings/hooks/skills into subagents —
        # researcher behavior must come from our prompts only.
        "--setting-sources", "project",
        "--disallowedTools", ",".join(_DISALLOWED),
    ]
    if allow_web:
        cmd += ["--allowedTools", ",".join(_ALLOWED)]
    return cmd


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", text, flags=re.S)
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            out = json.loads(text[start:end + 1])
            return out if isinstance(out, dict) else None
        except json.JSONDecodeError:
            pass
    return None


def _budget_note(tools: list | None) -> str:
    """API mode enforces tool budgets server-side; here they become instructions."""
    if not tools:
        return ""
    parts = []
    for t in tools:
        if isinstance(t, dict) and "max_uses" in t:
            parts.append(f"{t.get('name', 'tool')} <= {t['max_uses']} uses")
    return ("\n\nTool budget for this task (hard cap, stop when reached): "
            + ", ".join(parts)) if parts else ""


def run_subagent_cli(
    system: str,
    user_content: str,
    *,
    tools: list | None = None,
    output_schema: dict | None = None,
    timeout: int = 1200,
    model: str | None = None,
) -> dict | str:
    """CLI-runtime twin of llm.run_subagent: same contract, same return shape."""
    sys_text = system + _budget_note(tools)
    prompt = user_content
    if output_schema:
        prompt += (
            "\n\nOUTPUT FORMAT: respond with ONLY a single JSON object (no prose, "
            "no markdown fences) that validates against this JSON Schema:\n"
            + json.dumps(output_schema)
        )
    cmd = _base_cmd(allow_web=tools is not None, model=model) + ["--system-prompt", sys_text]

    for attempt in (1, 2):
        try:
            res = subprocess.run(cmd, input=prompt, capture_output=True,
                                 text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"_error": f"cli timeout after {timeout}s"} if output_schema \
                else f"[cli timeout after {timeout}s]"
        except FileNotFoundError:
            return {"_error": "claude CLI not found - npm install -g @anthropic-ai/claude-code"} \
                if output_schema else "[claude CLI not found]"
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip()[:300]
            if attempt == 1 and ("rate" in err.lower() or "overloaded" in err.lower()):
                time.sleep(20)
                continue
            return {"_error": f"cli exit {res.returncode}: {err}"} if output_schema \
                else f"[cli exit {res.returncode}: {err}]"
        if not output_schema:
            return res.stdout.strip()
        parsed = _parse_json(res.stdout)
        if parsed is not None:
            return parsed
        # one retry, asking again with the parse failure made explicit
        prompt = (user_content + "\n\nYour previous output was not parseable JSON. "
                  "Respond with ONLY the JSON object this time.\n\nSchema:\n"
                  + json.dumps(output_schema))
    return {"_error": "unparseable structured output from cli", "_raw": res.stdout[:2000]}


# ---------------------------------------------------------------------------
# Master protocol: in API mode the master is a native tool-use loop. In CLI
# mode the same master prompt drives a hand-rolled JSON action protocol -
# still a real agentic loop (model decides -> tool executes -> result returns
# -> model decides again), just transported over subprocess calls.
# ---------------------------------------------------------------------------

MASTER_PROTOCOL = """
# Runtime protocol (headless CLI mode)

You drive the formation through JSON actions. Respond with EXACTLY ONE JSON
object per turn and nothing else:

  {"action": "dispatch_research", "args": {"competitor": "...", "domain": "...",
      "lanes": [{"lane": "ads|positioning|pricing|social|news", "focus": "..."}]}}
  {"action": "verify_claims",  "args": {"competitor": "...", "claim_ids": []}}
  {"action": "write_brief",    "args": {"competitor": "...", "notes": "..."}}
  {"action": "view_ledger",    "args": {"competitor": "", "category": ""}}
  {"action": "reply",          "args": {"text": "...your message to the user..."}}

Rules: one action per response. After each non-reply action you receive the
result and choose the next action. "reply" ends your turn and speaks to the
user - use it for clarifying questions, progress-summaries-with-a-question,
and final summaries. You also have WebSearch available directly inside this
very response for quick recon (resolving domains, disambiguating names)
before you choose an action.

CRITICAL: output EXACTLY ONE JSON object and nothing else. Never repeat,
quote, or echo the transcript, prior actions, or tool results back - the
runtime already has them. A full-audit turn is not complete until write_brief
has run; do not end with "reply" mid-formation unless you are asking the user
a genuine question.
"""


def _parse_action(text: str) -> dict | None:
    """Master responses must be one action object. Models occasionally echo
    the transcript; scan for the LAST parseable {"action": ...} object so an
    echoed history followed by the real action still resolves."""
    direct = _parse_json(text)
    if direct and "action" in direct:
        return direct
    best = None
    for m in re.finditer(r'\{"action"', text):
        depth, i = 0, m.start()
        for j in range(m.start(), len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        cand = json.loads(text[i:j + 1])
                        if isinstance(cand, dict) and "action" in cand:
                            best = cand
                    except json.JSONDecodeError:
                        pass
                    break
    return best


def master_call(system: str, transcript: list[tuple[str, str]], timeout: int = 600) -> dict:
    """One master decision: transcript in, one parsed action out.

    Long tool results are truncated in the rendered transcript (the ledger is
    the real memory), and unparseable/echoed output gets ONE corrective retry
    before degrading to a reply - an echoed transcript must never silently end
    a formation turn."""
    def render() -> str:
        parts = []
        for role, text in transcript:
            if role == "tool_result" and len(text) > 3000:
                text = text[:3000] + " ...[truncated - full data lives in the ledger]"
            parts.append(f"[{role}]\n{text}")
        return "\n\n".join(parts) + "\n\n[system note]\nRespond with your next JSON action."

    cmd = _base_cmd(allow_web=True) + ["--system-prompt", system + MASTER_PROTOCOL]
    past_actions = {t.strip() for r, t in transcript if r == "assistant" and t.strip().startswith("{")}
    prompt = render()
    for attempt in (1, 2):
        try:
            res = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"action": "reply", "args": {"text": "[master call timed out - say 'continue' to resume]"}}
        except FileNotFoundError:
            return {"action": "reply", "args": {"text": "[claude CLI not found - npm install -g @anthropic-ai/claude-code, then claude login]"}}
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip()[:300]
            return {"action": "reply", "args": {"text": f"[master call failed: {err}]"}}
        parsed = _parse_action(res.stdout)
        # An action we've already executed verbatim = the model echoed history.
        if parsed and json.dumps(parsed) not in past_actions:
            return parsed
        if attempt == 1:
            prompt = render() + (
                "\n\n[system note]\nYour previous output was not a single NEW JSON action "
                "(it was prose, an echo of the transcript, or a repeat of an already-executed "
                "action). Respond now with exactly ONE new JSON action object and nothing else.")
    text = res.stdout.strip()
    looks_like_protocol_junk = text.startswith("{") or "[tool_result]" in text or "[assistant]" in text
    return {"action": "reply", "args": {"text": (
        "[protocol hiccup - the model twice failed to produce a valid next action; "
        "say 'continue' to resume]" if looks_like_protocol_junk else text or "[empty response]")}}
