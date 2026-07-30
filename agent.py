#!/usr/bin/env python3
"""Pranav's GTM Agent - conversational competitive-intel CLI.

The master agent owns the conversation and the decisions:
which lanes to research, when to go deeper, when to verify, when to write.
It never scrapes; it dispatches researchers and synthesizes.

Two runtimes, same formation:
    api (default)  Anthropic API. Needs ANTHROPIC_API_KEY with web search enabled.
    cli            Local `claude` CLI subprocess - bills your Claude subscription,
                   no API key. One-time setup:
                   npm install -g @anthropic-ai/claude-code && claude login

Usage:
    python agent.py                        # interactive (api runtime)
    python agent.py "Deel"                 # competitor pre-loaded
    python agent.py --runtime cli "Deel"   # keyless, via claude CLI
    python agent.py --checkpoints "Deel"   # guided walk: pause at each lane
"""

from __future__ import annotations

import json
import os
import re
import sys

import anthropic

import research
import runtime_cli
from llm import MODEL, client, load_prompt
from schemas import ClaimsLedger

MASTER_TOOLS = [
    {
        "name": "dispatch_research",
        "description": (
            "Send parallel researchers into source lanes for one competitor. "
            "Choose only lanes worth running (skip lanes unlikely to pay off for this "
            "competitor, or re-run one lane with a sharper focus for deep-dives). "
            "Lanes: ads (multi-source ad ladder: Meta/Google/TikTok/LinkedIn libraries + "
            "third-party ad-spy + landing pages + martech pixel stack + traffic/hiring "
            "signals; per-ad taxonomy + longevity), "
            "positioning (site messaging, archive drift, answer-engine share of voice), "
            "pricing (packaging + tiers), social (LinkedIn/X/YouTube themes + community "
            "buyer demand), news (launches, press, funding, pivots). "
            "Returns per-lane summaries, coverage gaps, failures, and new claim IDs."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["competitor", "domain", "lanes"],
            "properties": {
                "competitor": {"type": "string"},
                "domain": {"type": "string", "description": "Primary website domain, e.g. gusto.com. Empty string if unknown."},
                "lanes": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["lane", "focus"],
                        "properties": {
                            "lane": {"type": "string", "enum": ["ads", "positioning", "pricing", "social", "news"]},
                            "focus": {"type": "string", "description": "Sharpened instruction for this researcher, '' for default sweep."},
                        },
                    },
                },
            },
        },
    },
    {
        "name": "verify_claims",
        "description": (
            "Run the adversarial verifier over unverified claims for a competitor. "
            "It attempts to refute each claim against its source; confirms, downgrades, or "
            "flags. Always run before writing a brief. Pass claim_ids to scope, or [] for all unverified."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["competitor", "claim_ids"],
            "properties": {
                "competitor": {"type": "string"},
                "claim_ids": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "write_brief",
        "description": (
            "Produce the final deliverables for a competitor: the markdown brief and the "
            "claims-ledger JSON, written to outputs/. Only call after verification. "
            "Pass your synthesis notes: the 3-5 strategic reads you want the brief to land, "
            "including your view of the Rippling exploit angles."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["competitor", "notes"],
            "properties": {
                "competitor": {"type": "string"},
                "notes": {"type": "string"},
            },
        },
    },
    {
        "name": "view_ledger",
        "description": "Inspect the current claims ledger (compact). Use before deciding whether a follow-up needs new research or is already answerable.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["competitor"],
            "properties": {
                "competitor": {"type": "string", "description": "'' for all competitors this session"},
                "category": {"type": "string", "description": "Optional category filter, '' for all."},
            },
        },
    },
    # Server-side recon for the master itself: resolving "levy.co vs levy.com",
    # checking a competitor is actually in Rippling's space, etc.
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 4},
]


class Session:
    """One conversation: ledger + master loop. `on_event` receives structured
    events (dict with "type") so any frontend — this CLI or the web UI in
    server.py — can render the formation's activity. Default handler prints
    the classic CLI lines."""

    def __init__(self, runtime: str = "api", on_event=None) -> None:
        self.runtime = runtime
        self.ledger = ClaimsLedger()
        self.client = client() if runtime == "api" else None  # cli mode: no key, no SDK client
        self.messages: list = []          # api runtime: native message history
        self.transcript: list = []        # cli runtime: (role, text) protocol log
        self.emit = on_event or self._print_event
        self.system = [{
            "type": "text",
            "text": load_prompt("master"),
            "cache_control": {"type": "ephemeral"},
        }]

    _CHECKPOINT_RE = re.compile(r"\s*\[\[checkpoint:(\d+)\]\]\s*")

    def _strip_checkpoint(self, text: str) -> str:
        """Checkpoint markers drive the UI tracker; emit the event, hide the tag."""
        m = self._CHECKPOINT_RE.search(text)
        if m:
            self.emit({"type": "checkpoint", "n": int(m.group(1)), "total": 6})
            text = self._CHECKPOINT_RE.sub(" ", text, count=1).strip()
        return text

    @staticmethod
    def _print_event(ev: dict) -> None:
        t = ev["type"]
        if t == "status":
            print(f"\n  >> {ev['text']}")
        elif t == "lane":
            line = f"     {ev['lane']}: {ev['claims']} claims [{ev['status']}]"
            if ev.get("fail"):
                line += f" | fail: {ev['fail'][:300]}"
            print(line)
        elif t == "verify":
            print(f"     {ev['judged']} claims judged: {ev['breakdown']}")
        elif t == "brief":
            print("\n" + "=" * 72 + f"\n{ev['text']}\n" + "=" * 72)
            print(f"\n  saved: {ev['brief_path']}\n         {ev['claims_path']}")
        elif t == "assistant_delta":
            print(ev["text"], end="", flush=True)
        elif t == "assistant_final":
            print()  # deltas already streamed; close the line
        elif t == "assistant":
            print(ev["text"] + "\n")
        elif t == "checkpoint":
            print(f"\n  ── checkpoint {ev['n']}/{ev['total']} ──")
        elif t == "error":
            print(f"\n[{ev['text']}]")

    # ---- tool execution -------------------------------------------------
    def execute(self, name: str, args: dict) -> str:
        if name == "dispatch_research":
            lanes = [spec["lane"] for spec in args["lanes"]]
            self.emit({"type": "status", "phase": "dispatch",
                       "text": f"dispatching researchers: {', '.join(lanes)} -> {args['competitor']}",
                       "lanes": lanes, "competitor": args["competitor"]})
            report = research.dispatch(self.ledger, args["competitor"], args.get("domain", ""), args["lanes"])
            for lane, r in report["lanes"].items():
                status = "ok" if r["claim_ids"] else ("dry" if not r["failures"] else "degraded")
                self.emit({"type": "lane", "lane": lane, "claims": len(r["claim_ids"]),
                           "status": status, "fail": r["failures"][0] if r["failures"] else "",
                           "summary": r.get("summary", ""), "secs": r.get("secs", 0)})
            return json.dumps(report)
        if name == "verify_claims":
            self.emit({"type": "status", "phase": "verify", "text": "adversarial verify pass..."})
            out = research.verify(self.ledger, args["competitor"], args.get("claim_ids") or None)
            self.emit({"type": "verify", "judged": out.get("verified", 0),
                       "breakdown": out.get("breakdown", {}), "flagged": out.get("flagged", [])})
            return json.dumps(out)
        if name == "write_brief":
            # Hard gate: nothing lands unverified. If the master under-scoped
            # verification (or skipped it), verify the remainder here - this is
            # enforced in code, not left to prompt compliance.
            pending = [c.id for c in self.ledger.for_competitor(args["competitor"])
                       if c.verified == "unverified"]
            if pending:
                self.emit({"type": "status", "phase": "verify",
                           "text": f"auto-verifying {len(pending)} unjudged claims before the brief..."})
                out = research.verify(self.ledger, args["competitor"], pending)
                self.emit({"type": "verify", "judged": out.get("verified", 0),
                           "breakdown": out.get("breakdown", {}), "flagged": out.get("flagged", [])})
            self.emit({"type": "status", "phase": "brief", "text": "writing brief..."})
            out = research.write_brief(self.ledger, args["competitor"], args["notes"])
            self.emit({"type": "brief", "text": out["brief_text"],
                       "brief_path": out["brief_path"], "claims_path": out["claims_path"],
                       "claims_used": out["claims_used"], "competitor": args["competitor"]})
            # keep the master's context lean - it gets paths + stats, not the full text
            return json.dumps({k: out[k] for k in ("brief_path", "claims_path", "claims_used")}
                              | {"brief_head": out["brief_text"][:600]})
        if name == "view_ledger":
            self.emit({"type": "status", "phase": "ledger", "text": "inspecting claims ledger"})
            return self.ledger.compact_view(args.get("competitor") or None, args.get("category") or None)
        return json.dumps({"error": f"unknown tool {name}"})

    # ---- one master turn ------------------------------------------------
    def turn(self, user_text: str) -> None:
        if self.runtime == "cli":
            return self.turn_cli(user_text)
        self.messages.append({"role": "user", "content": user_text})
        while True:
            try:
                turn_text = []
                with self.client.messages.stream(
                    model=MODEL,
                    max_tokens=8192,
                    system=self.system,
                    thinking={"type": "adaptive"},
                    output_config={"effort": "high"},
                    tools=MASTER_TOOLS,
                    messages=self.messages,
                ) as stream:
                    for text in stream.text_stream:
                        turn_text.append(text)
                        self.emit({"type": "assistant_delta", "text": text})
                    response = stream.get_final_message()
            except anthropic.AuthenticationError:
                self.emit({"type": "error", "text": (
                    "API key rejected (invalid or expired). Fix ANTHROPIC_API_KEY "
                    "(or .env), or restart keyless: python agent.py --runtime cli")})
                return
            except anthropic.RateLimitError:
                self.emit({"type": "error", "text": "rate limited - retry in a minute"})
                self.messages.pop() if self.messages[-1]["role"] == "user" else None
                return
            except anthropic.APIConnectionError:
                self.emit({"type": "error", "text": "network error - check connection and retry"})
                return

            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "pause_turn":
                continue  # server tool loop paused; resume
            if response.stop_reason == "refusal":
                self.emit({"type": "error", "text": "request declined by safety system - rephrase and try again"})
                return
            if response.stop_reason != "tool_use":
                self.emit({"type": "assistant_final",
                           "text": self._strip_checkpoint("".join(turn_text))})
                return

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                try:
                    out = self.execute(block.name, block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
                except Exception as e:
                    # tool failures go BACK to the master so it can adapt (retry,
                    # skip the lane, or ask the user) instead of crashing the CLI
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"{type(e).__name__}: {e}", "is_error": True})
            self.messages.append({"role": "user", "content": results})

    # ---- one master turn, cli runtime -----------------------------------
    # Same master prompt, same decisions, same tools - transported as a JSON
    # action protocol over `claude -p` subprocess calls instead of native
    # tool_use blocks. Still a real agentic loop: model decides, tool runs,
    # result returns, model decides again.
    def turn_cli(self, user_text: str, max_actions: int = 14) -> None:
        self.transcript.append(("user", user_text))
        system = self.system[0]["text"]
        for _ in range(max_actions):
            self.emit({"type": "status", "phase": "thinking", "text": "master deciding next move..."})
            act = runtime_cli.master_call(system, self.transcript)
            name = act.get("action", "reply")
            args = act.get("args", {}) or {}
            if name == "reply":
                text = args.get("text", "").strip() or "[no reply text]"
                text = self._strip_checkpoint(text)
                self.emit({"type": "assistant", "text": text})
                self.transcript.append(("assistant", text))
                return
            self.transcript.append(("assistant", json.dumps(act)))
            try:
                out = self.execute(name, args)
            except Exception as e:
                out = json.dumps({"error": f"{type(e).__name__}: {e}"})
            self.transcript.append(("tool_result", out))
        self.emit({"type": "assistant", "text": "[action limit reached this turn - ask me to continue]"})
        self.transcript.append(("assistant", "[action limit reached mid-task]"))


BANNER = """Pranav's GTM Agent  |  model: {model}  |  runtime: {runtime}
Give me a competitor name or domain (e.g. "Gusto", "deel.com").
Follow-ups work mid-session: "dig deeper on their pricing", "run this again for Justworks".
Commands: /quit
"""


def main() -> None:
    argv = sys.argv[1:]
    runtime = os.environ.get("AGENT_RUNTIME", "api")
    if "--runtime" in argv:
        i = argv.index("--runtime")
        runtime = argv[i + 1] if i + 1 < len(argv) else runtime
        del argv[i:i + 2]
    checkpoints = "--checkpoints" in argv
    if checkpoints:
        argv.remove("--checkpoints")
    if runtime not in ("api", "cli"):
        sys.exit(f"unknown runtime {runtime!r} (use: api | cli)")
    os.environ["AGENT_RUNTIME"] = runtime  # subagents in llm.py follow the same switch
    if runtime == "cli" and not runtime_cli.available():
        sys.exit("claude CLI not found. Setup: npm install -g @anthropic-ai/claude-code && claude login\n"
                 "Or use the API runtime: export ANTHROPIC_API_KEY=... && python agent.py")
    if runtime == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("No ANTHROPIC_API_KEY found. Either:\n"
                 "  cp .env.example .env      # then put your key in .env (auto-loaded)\n"
                 "  export ANTHROPIC_API_KEY=sk-ant-...\n"
                 "or run keyless on a Claude subscription:\n"
                 "  python agent.py --runtime cli")

    print(BANNER.format(model=MODEL, runtime=runtime))
    session = Session(runtime)
    cp_mark = "\n\n[[ui: checkpoint mode ON]]" if checkpoints else ""
    if argv:
        first = " ".join(argv)
        print(f"you> analyze {first}")
        session.turn(f"Analyze this competitor: {first}{cp_mark}")
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return
        if not user:
            continue
        if user in ("/quit", "/q", "exit"):
            print("bye")
            return
        session.turn(user + cp_mark)


if __name__ == "__main__":
    main()
