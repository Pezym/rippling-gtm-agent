"""Run-over-run diffing: compare today's claims ledger against the most
recent prior run for the same competitor and produce a compact, deterministic
change report the brief writer can narrate.

The claims JSONs in outputs/ are the longitudinal memory - dated, append-only
per run, committed to git - version-controlled intel: a diff between runs is
a changelog for the data.
Claim IDs and wording regenerate every run, so we match on stable evidence:
source URLs and verbatim-quote fingerprints.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from schemas import slugify

OUTPUT_DIR = Path(__file__).parent / "outputs"


def prior_run(competitor: str) -> tuple[str, dict] | None:
    """Most recent prior-dated claims JSON for this competitor, or None."""
    slug = slugify(competitor)
    today = date.today().isoformat()
    runs = []
    for p in OUTPUT_DIR.glob(f"{slug}-claims-*.json"):
        m = re.search(r"(\d{4}-\d{2}-\d{2})\.json$", p.name)
        if m and m.group(1) < today:
            runs.append((m.group(1), p))
    if not runs:
        return None
    day, path = max(runs)
    try:
        return day, json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _url_key(url: str) -> str:
    """Normalize a source URL to scheme-less host+path, no trailing slash."""
    url = re.sub(r"^https?://(www\.)?", "", url.strip().lower())
    return url.split("#")[0].split("?")[0].rstrip("/")


def _quote_prints(claims: list[dict]) -> dict[str, dict]:
    """Fingerprint quoted claims by their quote's first 10 normalized words."""
    out = {}
    for c in claims:
        q = re.sub(r"\s+", " ", c.get("quote", "").strip().lower())
        if len(q.split()) >= 4:
            out[" ".join(q.split()[:10])] = c
    return out


def compute_diff(prior_date: str, old: dict, new_claims: list[dict]) -> dict:
    """Deterministic evidence diff. No LLM; the brief writer interprets it."""
    old_claims = old.get("claims", [])
    old_urls = {_url_key(c.get("source_url", "")) for c in old_claims} - {""}
    new_urls = {_url_key(c.get("source_url", "")) for c in new_claims} - {""}
    old_q, new_q = _quote_prints(old_claims), _quote_prints(new_claims)

    def sample(keys, table, n=12):
        rows = []
        for k in list(keys)[:n]:
            c = table[k]
            rows.append({"claim": c.get("claim", "")[:200], "quote": c.get("quote", "")[:160],
                         "source_url": c.get("source_url", "")})
        return rows

    return {
        "prior_run_date": prior_date,
        "prior_claims": len(old_claims),
        "current_claims": len(new_claims),
        "sources_new": sorted(new_urls - old_urls)[:20],
        "sources_no_longer_cited": sorted(old_urls - new_urls)[:20],
        "sources_persisting": len(old_urls & new_urls),
        "quotes_new": sample(set(new_q) - set(old_q), new_q),
        "quotes_gone": sample(set(old_q) - set(new_q), old_q),
        "note": ("Evidence-level diff between dated runs. 'gone' means this run's "
                 "researchers no longer surfaced that quote - page changed, message "
                 "retired, or lane focus shifted; interpret, don't overclaim."),
    }


def diff_block(competitor: str, new_claims: list[dict]) -> str:
    """Ready-to-embed prompt block for the brief writer; '' when first run."""
    prior = prior_run(competitor)
    if not prior:
        return ""
    day, old = prior
    return (f"\n\nPRIOR RUN COMPARISON (this competitor was last analyzed {day}; "
            "narrate what changed under section 6 as 'Since the last run'):\n"
            + json.dumps(compute_diff(day, old, new_claims), indent=1))
