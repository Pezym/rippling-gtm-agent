#!/usr/bin/env python3
"""Eval harness for agent outputs. Three layers, cheap to expensive:

  1. Structural checks (free, deterministic): claims JSON validates, required
     brief sections present, every factual line cites a claim ID, cited IDs exist.
  2. Grounding spot-check (network, no LLM): sample N measured claims, re-fetch
     the source URL, fuzzy-match the quote against page text.
  3. LLM judge (API cost): rubric-scored review of the brief against the ledger.

Usage:
  python evals/run_evals.py outputs/gusto-brief-2026-07-27.md outputs/gusto-claims-2026-07-27.json
  python evals/run_evals.py <brief> <claims> --grounding 5 --judge
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REQUIRED_SECTIONS = [
    "TL;DR", "Messaging angles", "position their product", "Channel & creative",
    "Pricing", "changed recently", "opportunities for Rippling", "Coverage",
]

VALID_ENUMS = {
    "evidence": {"measured", "stated_by_competitor", "proxy", "inferred"},
    "confidence": {"high", "medium", "low"},
    "verified": {"unverified", "confirmed", "plausible", "unsupported", "contradicted"},
}


def structural(brief: str, claims: dict) -> list[tuple[bool, str]]:
    checks: list[tuple[bool, str]] = []
    rows = claims.get("claims", [])
    checks.append((len(rows) >= 10, f"claim volume: {len(rows)} (want >=10)"))

    bad = [c.get("id", "?") for c in rows
           if not c.get("claim") or not c.get("source_url", "").startswith("http")
           or c.get("evidence") not in VALID_ENUMS["evidence"]
           or c.get("confidence") not in VALID_ENUMS["confidence"]
           or c.get("verified") not in VALID_ENUMS["verified"]]
    checks.append((not bad, f"claim field validity: {'ok' if not bad else 'bad rows ' + ','.join(bad[:5])}"))

    quoteless = [c["id"] for c in rows if not c.get("quote") and c.get("evidence") != "inferred"]
    checks.append((not quoteless, f"non-inferred claims all quoted: {'ok' if not quoteless else ','.join(quoteless[:5])}"))

    for section in REQUIRED_SECTIONS:
        checks.append((section.lower() in brief.lower(), f"brief section present: {section}"))

    cited = set(re.findall(r"\[C(\d{3})\]", brief))
    known = {c["id"][1:] for c in rows if c.get("id", "").startswith("C")}
    dangling = cited - known
    checks.append((len(cited) >= 15, f"citation density: {len(cited)} distinct claim IDs cited (want >=15)"))
    checks.append((not dangling, f"no dangling citations: {'ok' if not dangling else 'C' + ', C'.join(sorted(dangling)[:5])}"))

    rip = _section(brief, "opportunities for Rippling")
    rip_cites = len(re.findall(r"\[C\d{3}\]", rip))
    checks.append((rip_cites >= 5, f"Rippling section evidence-anchored: {rip_cites} citations (want >=5)"))

    verified_frac = sum(1 for c in rows if c.get("verified") in ("confirmed", "plausible")) / max(len(rows), 1)
    checks.append((verified_frac >= 0.6, f"verification pass-rate: {verified_frac:.0%} confirmed+plausible (want >=60%)"))
    return checks


# Latency budgets (seconds). Formation stages are timed at source
# (research.TIMINGS -> run.timings in the claims JSON); a run that blows a
# budget fails the eval so slowness regressions surface like quality ones.
LATENCY_BUDGETS = {
    "slowest_lane": 300,     # any single researcher
    "verify_wall": 300,      # whole verification stage (parallel chunks)
    "brief_wall": 300,       # brief generation
    "formation_wall": 750,   # dispatch + verify + brief, excluding master chat
}


def latency(claims: dict) -> list[tuple[bool, str]]:
    t = claims.get("run", {}).get("timings") or {}
    if not t:
        return [(True, "latency: no timings recorded (run predates instrumentation) - skipped")]
    checks = []
    lanes = t.get("lanes", {})
    if lanes:
        worst_lane, worst = max(lanes.items(), key=lambda kv: kv[1])
        checks.append((worst <= LATENCY_BUDGETS["slowest_lane"],
                       f"latency slowest lane: {worst_lane} {worst:.0f}s (budget {LATENCY_BUDGETS['slowest_lane']}s)"))
    for key in ("verify_wall", "brief_wall", "formation_wall"):
        if key in t:
            checks.append((t[key] <= LATENCY_BUDGETS[key],
                           f"latency {key}: {t[key]:.0f}s (budget {LATENCY_BUDGETS[key]}s)"))
    return checks or [(True, "latency: timings present but empty - skipped")]


def _section(brief: str, marker: str) -> str:
    m = re.search(rf"^##.*{re.escape(marker)}.*$", brief, re.M | re.I)
    if not m:
        return ""
    rest = brief[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def grounding(claims: dict, sample_n: int) -> list[tuple[bool, str]]:
    """Re-fetch a sample of measured claims and look for the quote on the page.
    HTML noise means we fuzzy-match: >=60% of the quote's 5-word shingles present."""
    rows = [c for c in claims.get("claims", []) if c.get("evidence") == "measured" and c.get("quote")]
    if not rows:
        return [(False, "grounding: no measured+quoted claims to check")]
    sample = random.sample(rows, min(sample_n, len(rows)))
    out = []
    for c in sample:
        try:
            # Full browser UA: sites like deel.com serve an empty shell to
            # non-browser UAs, which false-fails grounding on real quotes.
            req = urllib.request.Request(c["source_url"], headers={"User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")})
            html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
            import html as html_mod
            text = html_mod.unescape(re.sub(r"<[^>]+>", " ", html))
            # Normalize typographic quotes/dashes: pages serve ’“”– where
            # claims store '"- and shingle equality would false-fail.
            trans = str.maketrans({"’": "'", "‘": "'", "“": '"',
                                   "”": '"', "–": "-", "—": "-"})
            text = re.sub(r"\s+", " ", text.translate(trans)).lower()
            text = re.sub(r"\s+([,.;:!?])", r"\1", text)   # tag-strip leaves ' ,' artifacts
            quote_norm = re.sub(r"\s+([,.;:!?])", r"\1",
                                re.sub(r"\s+", " ", c["quote"].translate(trans).lower()))
            words = quote_norm.split()
            shingles = [" ".join(words[i:i + 5]) for i in range(0, max(len(words) - 4, 1))]
            hit = sum(1 for s in shingles if s in text) / len(shingles)
            out.append((hit >= 0.6, f"grounding {c['id']}: {hit:.0%} quote match at {c['source_url'][:60]}"))
        except Exception as e:
            out.append((False, f"grounding {c['id']}: fetch failed ({type(e).__name__}) - may be JS-rendered"))
    return out


def judge(brief: str, claims: dict) -> dict:
    from llm import run_subagent
    from schemas import JUDGE_SCHEMA
    ledger = "\n".join(
        f"[{c['id']}] ({c['evidence']}/{c['confidence']}/{c['verified']}) {c['claim']} <{c['source_url']}>"
        for c in claims.get("claims", [])
    )
    system = (
        "You are a demanding VP of Growth Marketing at Rippling reviewing a competitive brief "
        "produced by an automated agent. Score 1-5 on each dimension. 3 = usable with edits; "
        "4 = would circulate to the team; 5 = better than most human analyst output. Judge:\n"
        "- evidence_grounding: are statements cited, are citations honest to their evidence grade, "
        "is competitor self-claim kept attributed?\n"
        "- specificity: exact wording/prices/dates/segments vs generic filler?\n"
        "- recency: does it capture what changed recently with dates?\n"
        "- rippling_actionability: is section 7 campaign-ready and anchored, with honest "
        "'where they beat us' coverage - or generic advice?\n"
        "Be harsh on uncited assertions, vague opportunities, and weakness-only assessments."
    )
    return run_subagent(system, f"BRIEF:\n{brief}\n\nCLAIMS LEDGER:\n{ledger}",
                        output_schema=JUDGE_SCHEMA, effort="high", max_tokens=4000)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("brief_path")
    ap.add_argument("claims_path")
    ap.add_argument("--grounding", type=int, default=0, metavar="N", help="re-fetch N measured claims")
    ap.add_argument("--judge", action="store_true", help="run the LLM judge (API cost)")
    args = ap.parse_args()

    brief = Path(args.brief_path).read_text()
    claims = json.loads(Path(args.claims_path).read_text())

    results = structural(brief, claims)
    results += latency(claims)
    if args.grounding:
        results += grounding(claims, args.grounding)

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n== deterministic checks: {passed}/{len(results)} pass ==")
    for ok, msg in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {msg}")

    if args.judge:
        print("\n== LLM judge ==")
        verdict = judge(brief, claims)
        if "_error" in verdict:
            print(f"  judge failed: {verdict['_error']}")
        else:
            for k in ("evidence_grounding", "specificity", "recency", "rippling_actionability", "overall"):
                print(f"  {k}: {verdict[k]}/5")
            print(f"  rationale: {verdict['rationale']}")
            for w in verdict["weaknesses"]:
                print(f"  weakness: {w}")

    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
