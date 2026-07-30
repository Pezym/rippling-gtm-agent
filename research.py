"""Research formation: parallel lane researchers + adversarial verifier +
brief writer. This is the execution layer the master agent dispatches into.

Shape (the formation):
  master (agent.py)  -> intake, routing, synthesis, one voice to the user
  lane researchers   -> parallel workers, each owns one source lane
  verifier           -> adversarial reviewer; tries to knock claims down
  brief writer       -> turns the verified ledger into the deliverable
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path

import diffing
from llm import load_prompt, run_subagent, web_tools
from schemas import (
    RESEARCH_RESULT_SCHEMA, VERIFY_RESULT_SCHEMA,
    ClaimsLedger, slugify, today,
)

OUTPUT_DIR = Path(__file__).parent / "outputs"

# Speed/quality split: researchers are extraction jobs - a faster model with a
# tight playbook loses little; judgment stages (master, verifier, brief) stay
# on the flagship. Override per role via env.
LANE_MODEL = os.environ.get("LANE_MODEL", "claude-sonnet-5")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL")  # None -> flagship default

LANE_BUDGETS = {
    #  lane          searches  fetches   (defaults tuned for wall-clock; the
    "ads":          (11,        8),    #  master can re-run a lane with sharper
    "positioning":  (5,         8),    #  focus when depth beats speed)
    "pricing":      (4,         5),
    "social":       (6,         5),
    "news":         (6,         5),
}

# Verifier wall-time scales with claim count; chunk and run reviewers in
# parallel so verify ~= one chunk's latency instead of the whole ledger's.
VERIFY_CHUNK = 24

# Per-competitor stage timings (seconds). Collected across dispatch/verify/
# write_brief and embedded into the run's claims JSON as run.timings, where
# the eval harness checks them against latency budgets.
TIMINGS: dict[str, dict] = defaultdict(dict)


def run_lane(lane: str, competitor: str, domain: str, focus: str) -> dict:
    """One researcher: server-side web_search + web_fetch, structured claims out."""
    searches, fetches = LANE_BUDGETS[lane]
    system = load_prompt(f"lane_{lane}") + "\n\n" + load_prompt("evidence_protocol")
    # Seed URL templates in the conversation so web_fetch is allowed to hit them.
    seeds = _seed_urls(lane, competitor, domain)
    task = (
        f"Competitor: {competitor}\nPrimary domain: {domain or 'unknown - discover it'}\n"
        f"Today's date: {today()}\n"
        f"Orchestrator focus note: {focus or 'none'}\n\n"
        f"Candidate URLs you may fetch directly (verify relevance first):\n{seeds}\n\n"
        "Run your lane now. Return only the structured result."
    )
    result = run_subagent(
        system, task,
        tools=web_tools(searches, fetches),
        output_schema=RESEARCH_RESULT_SCHEMA,
        effort="medium",
        max_tokens=14000,
        model=LANE_MODEL,
    )
    if "_error" in result:
        return {"lane": lane, "summary": "", "coverage": "failed",
                "failures": [result["_error"]], "claims": []}
    result["lane"] = lane
    return result


def _seed_urls(lane: str, competitor: str, domain: str) -> str:
    """Ad libraries and archives are behind stable URL patterns; seeding them in
    the task text makes them fetchable (web_fetch only follows in-conversation
    URLs). Researchers are told these may 404 / render empty - that itself is a
    finding to report, not an error."""
    q = competitor.replace(" ", "%20")
    base = [f"https://{domain}"] if domain else []
    dom_signals = [f"https://builtwith.com/{domain}",
                   f"https://www.similarweb.com/website/{domain}/"] if domain else []
    per_lane = {
        # Ads lane gets the widest seed set on purpose — multiple independent
        # windows into the same spend so no single wall blinds the researcher:
        # four official ad libraries, martech/pixel + traffic signals, LinkedIn,
        # and the two email archives. See prompts/lane_ads.md for the ladder.
        "ads": [
            f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=US&q={q}&search_type=keyword_unordered",
            f"https://adstransparency.google.com/?region=US&query={q}",
            f"https://library.tiktok.com/ads?region=US&query={q}",
            f"https://www.linkedin.com/ad-library/search?keyword={q}",
            f"https://www.linkedin.com/company/{slugify(competitor)}/posts/",
            *dom_signals,
            f"https://milled.com/search?q={q}",
            f"https://reallygoodemails.com/search?q={q}",
        ],
        "positioning": base + ([f"https://{domain}/pricing", f"https://{domain}/customers",
                                f"https://web.archive.org/web/2025/https://{domain}"] if domain else []),
        "pricing": [f"https://{domain}/pricing"] if domain else [],
        "social": [f"https://www.linkedin.com/company/{slugify(competitor)}/",
                   f"https://www.youtube.com/@{slugify(competitor)}"],
        "news": [f"https://{domain}/blog", f"https://{domain}/press"] if domain else [],
    }
    return "\n".join(base + per_lane.get(lane, [])) or "(none - discover via search)"


def dispatch(ledger: ClaimsLedger, competitor: str, domain: str, lanes: list[dict]) -> dict:
    """Run requested lanes in parallel; fold claims into the ledger; return a
    compact per-lane report for the master's context window."""
    report: dict = {"competitor": competitor, "lanes": {}}
    t_dispatch = time.monotonic()
    lane_secs: dict[str, float] = {}

    def _timed_lane(spec: dict) -> tuple[str, dict, float]:
        t0 = time.monotonic()
        try:
            res = run_lane(spec["lane"], competitor, domain, spec.get("focus", ""))
        except Exception as e:  # a researcher crashing must not kill the run
            res = {"summary": "", "coverage": "crashed",
                   "failures": [f"{type(e).__name__}: {e}"], "claims": []}
        return spec["lane"], res, time.monotonic() - t0

    with cf.ThreadPoolExecutor(max_workers=len(lanes)) as pool:
        futures = [pool.submit(_timed_lane, spec) for spec in lanes]
        for fut in cf.as_completed(futures):
            lane, res, secs = fut.result()
            lane_secs[lane] = round(secs, 1)
            ids = []
            for raw in res.get("claims", []):
                try:
                    ids.append(ledger.add(competitor, lane, raw).id)
                except TypeError:
                    continue  # malformed claim row; schema should prevent this
            report["lanes"][lane] = {
                "summary": res.get("summary", ""),
                "coverage": res.get("coverage", ""),
                "failures": res.get("failures", []),
                "claim_ids": ids,
                "secs": lane_secs[lane],
            }
    t = TIMINGS[slugify(competitor)]
    t.setdefault("lanes", {}).update(lane_secs)
    t["dispatch_wall"] = round(t.get("dispatch_wall", 0) + (time.monotonic() - t_dispatch), 1)

    # Honesty signal: every lane failing with the SAME error is a pipeline/
    # tooling bug (deterministic), not a source wall - the master must report
    # it as such instead of narrating an "outage" or improvising a brief.
    # A genuinely dry sweep cannot trip this: dry lanes report partial
    # coverage with differing failure texts, not one identical hard error.
    rows = list(report["lanes"].values())
    if rows and all(not r["claim_ids"] and r["failures"] for r in rows):
        prefixes = {r["failures"][0][:80] for r in rows}
        if len(prefixes) == 1:
            report["systemic_failure"] = (
                "every lane failed with the same tool/request error - this is a "
                "pipeline bug, not a source wall: " + rows[0]["failures"][0][:300])
    return report


def verify(ledger: ClaimsLedger, competitor: str, claim_ids: list[str] | None = None) -> dict:
    """Adversarial verify pass: a reviewer tries to REFUTE each claim before it
    can land in the brief. Downgrades, flags, or confirms."""
    targets = [
        c for c in ledger.for_competitor(competitor)
        if (not claim_ids or c.id in claim_ids) and c.verified == "unverified"
    ]
    if not targets:
        return {"verified": 0, "note": "nothing unverified"}

    def _verify_chunk(chunk: list) -> dict:
        block = "\n".join(
            f"{c.id} | claim: {c.claim} | quote: \"{c.quote}\" | url: {c.source_url} "
            f"| evidence: {c.evidence} | confidence: {c.confidence}"
            for c in chunk
        )
        return run_subagent(
            load_prompt("verifier"),
            f"Competitor: {competitor}\nToday: {today()}\n\nClaims under review:\n{block}",
            tools=web_tools(5, 5),
            output_schema=VERIFY_RESULT_SCHEMA,
            effort="high",
            max_tokens=12000,
            model=JUDGE_MODEL,
        )

    t_verify = time.monotonic()
    chunks = [targets[i:i + VERIFY_CHUNK] for i in range(0, len(targets), VERIFY_CHUNK)]
    verdicts, errors = [], []
    with cf.ThreadPoolExecutor(max_workers=min(len(chunks), 4)) as pool:
        for res in pool.map(_verify_chunk, chunks):
            if "_error" in res:
                errors.append(res["_error"])
            else:
                verdicts.extend(res.get("verdicts", []))

    # Retry round: chunks can fail under load (rate limits, transient errors).
    # Re-chunk whatever is still unjudged and try once more at lower concurrency
    # before reporting a gap - unverified claims are a quality hole.
    judged = {v["claim_id"] for v in verdicts}
    missed = [c for c in targets if c.id not in judged]
    if missed and errors:
        retry_chunks = [missed[i:i + VERIFY_CHUNK] for i in range(0, len(missed), VERIFY_CHUNK)]
        with cf.ThreadPoolExecutor(max_workers=min(len(retry_chunks), 2)) as pool:
            for res in pool.map(_verify_chunk, retry_chunks):
                if "_error" in res:
                    errors.append(res["_error"])
                else:
                    verdicts.extend(res.get("verdicts", []))
        chunks += retry_chunks

    tt = TIMINGS[slugify(competitor)]
    tt["verify_wall"] = round(tt.get("verify_wall", 0) + (time.monotonic() - t_verify), 1)
    tt["verify_chunks"] = tt.get("verify_chunks", 0) + len(chunks)
    if not verdicts:
        return {"verified": 0, "note": "; ".join(errors) or "verifier returned nothing"}

    summary = {"confirmed": 0, "plausible": 0, "unsupported": 0, "contradicted": 0}
    for v in verdicts:
        c = ledger.get(v["claim_id"])
        if not c:
            continue
        c.verified = v["verdict"]
        c.confidence = v["adjusted_confidence"]
        c.verify_note = v["note"]
        summary[v["verdict"]] = summary.get(v["verdict"], 0) + 1
    flagged = [f"{v['claim_id']}: {v['note']}" for v in verdicts
               if v["verdict"] in ("unsupported", "contradicted")]
    out = {"verified": len(verdicts), "breakdown": summary, "flagged": flagged}
    if errors:
        out["note"] = f"{len(errors)} verifier chunk(s) errored; their claims stay unverified"
    return out


def write_brief(ledger: ClaimsLedger, competitor: str, master_notes: str) -> dict:
    """Brief writer: verified ledger -> markdown deliverable + claims JSON."""
    claims = ledger.for_competitor(competitor)
    ledger_block = "\n".join(
        f"[{c.id}] ({c.category} | {c.evidence} | confidence:{c.confidence} | verified:{c.verified}) "
        f"{c.claim}\n    quote: \"{c.quote}\"\n    source: {c.source_title} <{c.source_url}> ({c.observed_at})"
        for c in claims
    )
    # Run-over-run memory: if this competitor has a prior dated run in
    # outputs/, hand the writer a deterministic evidence diff to narrate.
    claim_rows = [asdict(c) for c in claims]
    t_brief = time.monotonic()
    brief = run_subagent(
        load_prompt("brief_writer"),
        f"Competitor: {competitor}\nToday: {today()}\n\n"
        f"Orchestrator synthesis notes:\n{master_notes}\n\n"
        f"Verified claims ledger ({len(claims)} claims):\n{ledger_block}"
        + diffing.diff_block(competitor, claim_rows),
        effort="high",
        max_tokens=20000,
    )
    timings = TIMINGS.pop(slugify(competitor), {})
    timings["brief_wall"] = round(time.monotonic() - t_brief, 1)
    timings["formation_wall"] = round(timings.get("dispatch_wall", 0)
                                      + timings.get("verify_wall", 0)
                                      + timings["brief_wall"], 1)
    OUTPUT_DIR.mkdir(exist_ok=True)
    slug, day = slugify(competitor), date.today().isoformat()
    brief_path = OUTPUT_DIR / f"{slug}-brief-{day}.md"
    claims_path = OUTPUT_DIR / f"{slug}-claims-{day}.json"
    brief_path.write_text(brief if isinstance(brief, str) else str(brief))
    claims_path.write_text(ledger.to_json(competitor, {
        "date": day, "model": __import__("llm").MODEL,
        "lane_model": LANE_MODEL,
        "timings": timings,
        "claim_states": {c.id: c.verified for c in claims},
    }))
    # human-readable twin of the claims JSON (see report.py)
    import report
    report_path = report.write_report(claims_path)
    return {"brief_path": str(brief_path), "claims_path": str(claims_path),
            "report_path": report_path, "claims_used": len(claims), "brief_text": brief}
