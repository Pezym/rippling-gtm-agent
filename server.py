#!/usr/bin/env python3
"""Web chat UI for the competitive-intel agent.

One FastAPI process wraps the same Session that powers the CLI: the chat pane
is the master's voice, the activity rail renders the formation's structured
events (dispatch, per-lane results, verify, brief) as they happen.

Run:
    ./.venv/bin/python server.py                 # api runtime (ANTHROPIC_API_KEY)
    AGENT_RUNTIME=cli ./.venv/bin/python server.py   # keyless via claude CLI
    open http://localhost:7788
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import markdown as md
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

RUNTIME = os.environ.get("AGENT_RUNTIME", "api")
os.environ["AGENT_RUNTIME"] = RUNTIME

from agent import Session  # noqa: E402  (env must be set before llm import chain)
from llm import MODEL      # noqa: E402
from schemas import Claim  # noqa: E402

ROOT = Path(__file__).parent
app = FastAPI(title="Pranav's GTM Agent")

# Named sessions, ChatGPT-style: "main" is the fresh scratch thread; each
# audited brand gets its own resumable thread seeded with the prior
# conversation AND the brand's claims ledger from outputs/, so follow-ups,
# re-verification, and re-briefs work with full context.
_sessions: dict[str, Session] = {}
_busy = threading.Lock()


def _resume_session(thread: str) -> Session:
    s = Session(RUNTIME, on_event=lambda ev: None)
    if thread == "main":
        return s
    runs = _runs_from_outputs().get(thread, [])
    name = runs[-1]["name"] if runs else next(
        (q["competitor"] for q in _load_history()
         if q.get("competitor") and _slugify(q["competitor"]) == thread), thread)
    # 1) ledger: latest run's claims, original IDs preserved
    n_claims = 0
    if runs:
        cj = ROOT / "outputs" / Path(runs[-1]["claims_url"]).name
        try:
            rows = json.loads(cj.read_text()).get("claims", [])
            fields = {f for f in Claim.__dataclass_fields__}
            for r in rows:
                s.ledger.claims.append(Claim(**{k: v for k, v in r.items() if k in fields}))
            s.ledger._counter = max((int(r["id"][1:]) for r in rows if r.get("id", "").startswith("C")),
                                    default=0)
            n_claims = len(rows)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    # 2) conversation: prior exchanges for this brand
    exchanges = [q for q in _load_history()
                 if q.get("competitor") and _slugify(q["competitor"]) == thread]
    note = (f"[Thread resumed: the {name} audit. {n_claims} claims from the last run are "
            f"loaded in the ledger; the prior conversation follows. Continue naturally - "
            f"answer follow-ups from the ledger, deepen or re-brief on request.]")
    if s.runtime == "cli":
        s.transcript.append(("system", note))
        for q in exchanges:
            s.transcript.append(("user", q["q"]))
            s.transcript.append(("assistant", q.get("a") or "[reply not recorded]"))
    else:
        s.messages.append({"role": "user", "content": note})
        s.messages.append({"role": "assistant", "content": f"Understood - resuming the {name} thread."})
        for q in exchanges:
            s.messages.append({"role": "user", "content": q["q"]})
            s.messages.append({"role": "assistant", "content": q.get("a") or "[reply not recorded]"})
    return s


def _get_session(thread: str) -> Session:
    if thread not in _sessions:
        _sessions[thread] = _resume_session(thread)
    return _sessions[thread]

# ---- audit history: every question asked, attributed to the competitor the
# formation was working on when it was asked. Survives restarts (.history.json,
# gitignored); company run-cards are derived fresh from outputs/ each request.
HISTORY_PATH = ROOT / ".history.json"
# "Clear audit history" hides runs produced before the cleared-at instant and
# wipes the question log. Brief/claims files themselves are never deleted -
# they are deliverables (and in git); clearing is a view-level reset.
CLEARED_PATH = ROOT / ".history_cleared"
_current_competitor: str | None = None


def _cleared_at() -> float:
    try:
        return float(CLEARED_PATH.read_text().strip())
    except (OSError, ValueError):
        return 0.0


def _load_history() -> list[dict]:
    try:
        return json.loads(HISTORY_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _log_question(question: str, competitor: str | None, answer: str = "") -> None:
    hist = _load_history()
    hist.append({"q": question, "competitor": competitor, "a": answer[:20000],
                 "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    HISTORY_PATH.write_text(json.dumps(hist, indent=1, ensure_ascii=False))


def _runs_from_outputs() -> dict[str, list[dict]]:
    """slug -> runs, read from the dated brief/claims files on disk."""
    runs: dict[str, list[dict]] = {}
    cutoff = _cleared_at()
    for cj in sorted((ROOT / "outputs").glob("*-claims-*.json")):
        m = re.match(r"(.+)-claims-(\d{4}-\d{2}-\d{2})\.json$", cj.name)
        if not m or cj.stat().st_mtime < cutoff:
            continue
        slug, day = m.groups()
        try:
            data = json.loads(cj.read_text())
            name, n = data.get("competitor", slug), data.get("claim_count", 0)
        except (OSError, json.JSONDecodeError):
            name, n = slug, 0
        brief = cj.with_name(f"{slug}-brief-{day}.md")
        rpt = cj.with_suffix(".html")
        runs.setdefault(slug, []).append({
            "name": name, "date": day, "claims": n,
            "brief_url": f"/outputs/{brief.name}" if brief.exists() else None,
            "claims_url": f"/outputs/{cj.name}",
            "report_url": f"/outputs/{rpt.name}" if rpt.exists() else None,
        })
    return runs


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def render_md(text: str) -> str:
    return md.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])


class ChatIn(BaseModel):
    message: str
    checkpoints: bool = False
    thread: str = "main"


@app.get("/")
def index():
    return FileResponse(ROOT / "ui" / "index.html")


@app.get("/api/state")
def state(thread: str = "main"):
    s = _sessions.get(thread)
    n = len(s.ledger.claims) if s else 0
    competitors = sorted({c.competitor for c in s.ledger.claims}) if s else []
    return {"runtime": RUNTIME, "model": MODEL, "claims": n, "competitors": competitors}


@app.post("/api/reset")
def reset(thread: str = "main"):
    global _current_competitor
    _sessions.pop(thread, None)
    if thread == "main":
        _current_competitor = None
    return {"ok": True}


@app.post("/api/history/clear")
def clear_history():
    import time
    HISTORY_PATH.write_text("[]")
    CLEARED_PATH.write_text(str(time.time()))
    return {"ok": True}


@app.get("/api/history")
def history():
    """Companies audited (from outputs/) + every question asked about each."""
    runs = _runs_from_outputs()
    questions = _load_history()
    companies: dict[str, dict] = {}
    for slug, rs in runs.items():
        companies[slug] = {"slug": slug, "name": rs[-1]["name"], "runs": rs, "questions": []}
    general = []
    for q in questions:
        comp = q.get("competitor")
        row = {"q": q["q"], "ts": q["ts"],
               "a_html": render_md(q["a"]) if q.get("a") else ""}
        if comp:
            slug = _slugify(comp)
            companies.setdefault(slug, {"slug": slug, "name": comp, "runs": [], "questions": []})
            companies[slug]["questions"].append(row)
        else:
            general.append(row)
    ordered = sorted(companies.values(),
                     key=lambda c: max([r["date"] for r in c["runs"]]
                                       + [q["ts"][:10] for q in c["questions"]] + [""]),
                     reverse=True)
    return {"companies": ordered, "general": general}


@app.post("/api/chat")
def chat(body: ChatIn):
    """One master turn, streamed as SSE events. Single user, single flight:
    a second message while the formation is running gets a busy event."""
    if not _busy.acquire(blocking=False):
        def busy():
            yield sse({"type": "error", "text": "formation is mid-run - wait for this turn to finish"})
            yield sse({"type": "done"})
        return StreamingResponse(busy(), media_type="text/event-stream")

    session = _get_session(body.thread)
    # In a brand thread, every exchange belongs to that brand by default.
    thread_brand = None
    if body.thread != "main":
        runs = _runs_from_outputs().get(body.thread, [])
        thread_brand = runs[-1]["name"] if runs else body.thread

    q: queue.Queue = queue.Queue()
    turn_replies: list[str] = []  # agent's spoken text this turn, for the brand transcript

    def on_event(ev: dict) -> None:
        global _current_competitor
        if ev.get("competitor"):  # dispatch/brief events carry the target
            _current_competitor = ev["competitor"]
        if ev["type"] in ("assistant", "assistant_final") and ev.get("text"):
            turn_replies.append(ev["text"])
        # enrich text-bearing events with rendered markdown for the UI
        if ev["type"] in ("assistant", "assistant_final") and ev.get("text"):
            ev = ev | {"html": render_md(ev["text"])}
        if ev["type"] == "brief":
            slug_html = Path(ev["claims_path"]).with_suffix(".html").name
            ev = ev | {"html": render_md(ev["text"]),
                       "brief_url": "/outputs/" + Path(ev["brief_path"]).name,
                       "claims_url": "/outputs/" + Path(ev["claims_path"]).name,
                       "report_url": "/outputs/" + slug_html}
            ev.pop("text")  # full brief goes via html; keep the event lean
        q.put(ev)

    # The checkpoint flag rides inside the message as a marker the master's
    # doctrine understands; the UI shows only the user's own words, and the
    # history log stores the clean text.
    augmented = body.message + ("\n\n[[ui: checkpoint mode ON]]" if body.checkpoints else "")

    def run_turn() -> None:
        session.emit = on_event
        try:
            session.turn(augmented)
        except Exception as e:  # a crashed turn must not wedge the UI
            q.put({"type": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
            try:  # brand threads attribute to their brand; main follows the formation
                _log_question(body.message, thread_brand or _current_competitor,
                              "\n\n".join(turn_replies))
            except OSError:
                pass
            q.put({"type": "done"})

    threading.Thread(target=run_turn, daemon=True).start()

    def stream():
        try:
            while True:
                ev = q.get()
                yield sse(ev)
                if ev["type"] == "done":
                    break
        finally:
            _busy.release()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


(ROOT / "outputs").mkdir(exist_ok=True)
app.mount("/outputs", StaticFiles(directory=ROOT / "outputs"), name="outputs")


if __name__ == "__main__":
    import uvicorn
    print(f"Pranav's GTM Agent · runtime={RUNTIME} · model={MODEL} · http://localhost:7788")
    uvicorn.run(app, host="127.0.0.1", port=7788, log_level="warning")
