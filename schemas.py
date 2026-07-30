"""JSON schemas for structured outputs, plus the claims ledger.

Evidence protocol (the ledger's grading contract):
  - measured              -> read directly from a fetched page, quote + URL attached
  - stated_by_competitor  -> the competitor asserts it about themselves ("10,000 customers");
                             recorded as their claim, never treated as independent fact
  - proxy                 -> from a search snippet / third-party summary, page not fetched
  - inferred              -> analyst inference from other evidence; quote may be empty
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone

LANES = ["ads", "positioning", "pricing", "social", "news"]

CLAIM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "claim", "category", "quote", "source_url", "source_title",
        "source_type", "evidence", "confidence", "observed_at",
    ],
    "properties": {
        "claim": {"type": "string", "description": "One factual statement, specific and self-contained."},
        "category": {"type": "string", "enum": [
            "messaging", "positioning", "product", "pricing",
            "ads", "social", "news", "customers", "other",
        ]},
        "quote": {"type": "string", "description": "Verbatim supporting text from the source ('' only for inferred claims)."},
        "source_url": {"type": "string"},
        "source_title": {"type": "string"},
        "source_type": {"type": "string", "enum": [
            "competitor_site", "ad_library", "social", "press",
            "review_site", "blog", "search_snippet", "archive", "other",
        ]},
        "evidence": {"type": "string", "enum": ["measured", "stated_by_competitor", "proxy", "inferred"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "observed_at": {"type": "string", "description": "ISO date this was observed (today's date for live reads)."},
    },
}

RESEARCH_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lane", "summary", "coverage", "failures", "claims"],
    "properties": {
        "lane": {"type": "string", "enum": LANES},
        "summary": {"type": "string", "description": "Compressed telegraphic summary for the orchestrator (<=120 words)."},
        "coverage": {"type": "string", "description": "What was actually reachable vs not, one line."},
        "failures": {"type": "array", "items": {"type": "string"},
                     "description": "Sources that failed or were unreachable, with the fallback used."},
        "claims": {"type": "array", "items": CLAIM_SCHEMA,
                   "description": "HARD CAP 18: return the 10-18 highest-value claims, never more. Fold minor observations into the claim text of load-bearing ones - a tight ledger beats an exhaustive one. (Cap enforced here in prose: structured outputs rejects array-size constraint keywords.)"},
    },
}

VERIFY_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim_id", "verdict", "adjusted_confidence", "note"],
                "properties": {
                    "claim_id": {"type": "string"},
                    "verdict": {"type": "string", "enum": [
                        "confirmed", "plausible", "unsupported", "contradicted",
                    ]},
                    "adjusted_confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "note": {"type": "string"},
                },
            },
        },
    },
}

JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "evidence_grounding", "specificity", "recency",
        "rippling_actionability", "overall", "rationale", "weaknesses",
    ],
    "properties": {
        "evidence_grounding": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "specificity": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "recency": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "rippling_actionability": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "overall": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "rationale": {"type": "string"},
        "weaknesses": {"type": "array", "items": {"type": "string"}},
    },
}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@dataclass
class Claim:
    id: str
    competitor: str
    lane: str
    claim: str
    category: str
    quote: str
    source_url: str
    source_title: str
    source_type: str
    evidence: str
    confidence: str
    observed_at: str
    verified: str = "unverified"   # unverified | confirmed | plausible | unsupported | contradicted
    verify_note: str = ""


@dataclass
class ClaimsLedger:
    """Append-only in-session claims store (one per conversation)."""

    claims: list[Claim] = field(default_factory=list)
    _counter: int = 0

    def add(self, competitor: str, lane: str, raw: dict) -> Claim:
        self._counter += 1
        c = Claim(id=f"C{self._counter:03d}", competitor=competitor, lane=lane, **raw)
        self.claims.append(c)
        return c

    def for_competitor(self, competitor: str) -> list[Claim]:
        slug = slugify(competitor)
        return [c for c in self.claims if slugify(c.competitor) == slug]

    def get(self, claim_id: str) -> Claim | None:
        return next((c for c in self.claims if c.id == claim_id), None)

    def compact_view(self, competitor: str | None = None, category: str | None = None) -> str:
        rows = self.for_competitor(competitor) if competitor else self.claims
        if category:
            rows = [c for c in rows if c.category == category]
        lines = [
            f"{c.id} [{c.category}/{c.evidence}/{c.confidence}/{c.verified}] {c.claim} <{c.source_url}>"
            for c in rows
        ]
        return "\n".join(lines) if lines else "(ledger empty)"

    def to_json(self, competitor: str, run_meta: dict) -> str:
        payload = {
            "competitor": competitor,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run": run_meta,
            "claim_count": len(self.for_competitor(competitor)),
            "claims": [asdict(c) for c in self.for_competitor(competitor)],
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)


def today() -> str:
    return date.today().isoformat()
