#!/usr/bin/env python3
"""Latency harness tests - zero model calls, zero network.

A stub `claude` binary with a fixed sleep stands in for the model, so these
tests measure the FORMATION's plumbing, not the model:

  1. dispatch runs the five lanes in parallel  -> wall ~= one lane, not five
  2. verify runs its chunks in parallel        -> wall ~= one chunk
  3. stage timings are recorded and land in the claims-JSON run meta

Run:  ./.venv/bin/python tests/test_latency.py
Exit code is non-zero on any failure (CI-ready).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

STUB_SLEEP = 1.5  # seconds each fake model call takes

STUB = f"""#!/usr/bin/env python3
import json, sys, time
argv = " ".join(sys.argv[1:])
prompt = sys.stdin.read()
time.sleep({STUB_SLEEP})
if "Runtime protocol" in argv:
    print(json.dumps({{"action": "reply", "args": {{"text": "stub"}}}})); sys.exit(0)
if '"verdicts"' in prompt:
    import re
    ids = re.findall(r"^(C\\d+) \\|", prompt, re.M)
    print(json.dumps({{"verdicts": [
        {{"claim_id": i, "verdict": "confirmed", "adjusted_confidence": "high", "note": "stub"}}
        for i in ids]}})); sys.exit(0)
if "OUTPUT FORMAT" in prompt:
    print(json.dumps({{"lane": "pricing", "summary": "s", "coverage": "c", "failures": [],
        "claims": [{{"claim": "stub claim", "category": "pricing", "quote": "stub quote",
        "source_url": "https://example.com/x", "source_title": "t", "source_type": "competitor_site",
        "evidence": "measured", "confidence": "high", "observed_at": "2026-07-28"}}]}})); sys.exit(0)
print("# Stub Brief\\nbody")
"""


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="latency-test-"))
    stub = tmp / "claude"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    os.environ["CLAUDE_CLI"] = str(stub)
    os.environ["AGENT_RUNTIME"] = "cli"

    import research
    from schemas import ClaimsLedger

    failures: list[str] = []

    def check(ok: bool, msg: str) -> None:
        print(("PASS  " if ok else "FAIL  ") + msg)
        if not ok:
            failures.append(msg)

    # 1. parallel dispatch: 5 lanes of ~1.5s each must land well under 5x serial
    ledger = ClaimsLedger()
    lanes = [{"lane": ln, "focus": ""} for ln in research.LANE_BUDGETS]
    t0 = time.monotonic()
    report = research.dispatch(ledger, "LatencyCo", "example.com", lanes)
    wall = time.monotonic() - t0
    serial = STUB_SLEEP * len(lanes)
    check(wall < serial * 0.55,
          f"dispatch parallelism: 5 lanes wall {wall:.1f}s < 55% of serial {serial:.1f}s")
    check(all("secs" in r for r in report["lanes"].values()),
          "per-lane timings recorded in dispatch report")

    # 2. parallel verify: 3 chunks of ~1.5s each ~= one chunk, not three
    big = ClaimsLedger()
    for i in range(research.VERIFY_CHUNK * 3):
        big.add("LatencyCo", "pricing", {
            "claim": f"c{i}", "category": "pricing", "quote": f"q{i}",
            "source_url": "https://example.com", "source_title": "t",
            "source_type": "competitor_site", "evidence": "measured",
            "confidence": "high", "observed_at": "2026-07-28"})
    t0 = time.monotonic()
    out = research.verify(big, "LatencyCo")
    vwall = time.monotonic() - t0
    check(vwall < STUB_SLEEP * 3 * 0.75,
          f"verify parallelism: 3 chunks wall {vwall:.1f}s < 75% of serial {STUB_SLEEP * 3:.1f}s")
    check(out["verified"] == research.VERIFY_CHUNK * 3,
          f"verify coverage: {out['verified']}/{research.VERIFY_CHUNK * 3} claims judged")

    # 3. timings flow into the claims JSON run meta
    res = research.write_brief(big, "LatencyCo", "notes")
    meta = json.loads(Path(res["claims_path"]).read_text())["run"]
    t = meta.get("timings", {})
    check(bool(t.get("lanes")) and "verify_wall" in t and "brief_wall" in t
          and "formation_wall" in t,
          f"timings persisted to run meta: {sorted(t.keys())}")

    # cleanup test artifacts from outputs/
    for f in Path(res["claims_path"]).parent.glob("latencyco-*"):
        f.unlink()

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
