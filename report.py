"""Claims-ledger HTML report: the human-readable twin of the claims JSON.

The JSON stays the machine-readable deliverable (evals consume it); this
renders the same content as a self-contained, Notion-style document - grouped
by category, filterable by evidence grade and verification state, every quote
and source link visible. Zero dependencies, works as a static file.
"""

from __future__ import annotations

import html as h
import json
from collections import Counter

CATEGORY_ORDER = ["messaging", "positioning", "product", "pricing",
                  "ads", "social", "news", "customers", "other"]

EV_LABEL = {
    "measured": "measured — read directly from a fetched page",
    "stated_by_competitor": "their own claim — attributed, not independent fact",
    "proxy": "proxy — third-party or search-snippet sourced",
    "inferred": "inferred — analyst read, basis stated in the claim",
}


def render(payload: dict) -> str:
    comp = payload.get("competitor", "?")
    claims = payload.get("claims", [])
    run = payload.get("run", {})
    ver = Counter(c.get("verified", "?") for c in claims)
    ev = Counter(c.get("evidence", "?") for c in claims)

    by_cat: dict[str, list[dict]] = {}
    for c in claims:
        by_cat.setdefault(c.get("category", "other"), []).append(c)

    sections = []
    for cat in CATEGORY_ORDER:
        rows = by_cat.get(cat)
        if not rows:
            continue
        cards = "\n".join(_card(c) for c in rows)
        sections.append(
            f'<section><h2>{h.escape(cat)} <span class="count">{len(rows)}</span></h2>{cards}</section>')

    corrections = ""
    if run.get("post_eval_corrections"):
        items = "".join(f"<li>{h.escape(x)}</li>" for x in run["post_eval_corrections"])
        corrections = (f'<section><h2>post-eval corrections <span class="count">'
                       f'{len(run["post_eval_corrections"])}</span></h2>'
                       f'<p class="note">The eval harness catching things is the audit trail of it working.</p>'
                       f'<ul class="corr">{items}</ul></section>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h.escape(comp)} — claims ledger</title>
<style>
  :root {{ --ink:#1f2328; --dim:#6a737d; --faint:#98a1ab; --line:#e6e8eb; --soft:#f6f7f8;
          --ok:#0e7a55; --okbg:#e7f5ef; --warn:#8a5a00; --warnbg:#fdf3dc;
          --bad:#b42318; --badbg:#fdecea; --blue:#0b6bcb; --bluebg:#e8f1fc; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --ink:#e6e8eb; --dim:#9aa4b0; --faint:#6b7480; --line:#2a2f36; --soft:#171a1f;
             --ok:#4cc596; --okbg:#0d2b20; --warn:#e2b93d; --warnbg:#2c2410;
             --bad:#f08578; --badbg:#331512; --blue:#6cb0f5; --bluebg:#10233a; }}
    body {{ background:#0f1114; }}
  }}
  * {{ box-sizing:border-box; margin:0; }}
  body {{ font:15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         color:var(--ink); max-width:860px; margin:0 auto; padding:48px 28px 90px; }}
  h1 {{ font-size:30px; letter-spacing:-.4px; }}
  .sub {{ color:var(--dim); margin-top:6px; }}
  .statbar {{ display:flex; flex-wrap:wrap; gap:8px; margin:18px 0 6px; }}
  .stat {{ border:1px solid var(--line); border-radius:999px; padding:5px 13px; font-size:12.5px; color:var(--dim); }}
  .stat b {{ color:var(--ink); }}
  .filters {{ position:sticky; top:0; background:inherit; backdrop-filter:blur(6px);
             display:flex; flex-wrap:wrap; gap:6px; padding:14px 0; border-bottom:1px solid var(--line);
             margin-bottom:10px; z-index:5; background-color:rgba(255,255,255,.85); }}
  @media (prefers-color-scheme: dark) {{ .filters {{ background-color:rgba(15,17,20,.85); }} }}
  .filters button {{ border:1px solid var(--line); background:none; color:var(--dim); border-radius:999px;
                    padding:5px 13px; font:inherit; font-size:12.5px; cursor:pointer; }}
  .filters button.on {{ background:var(--bluebg); color:var(--blue); border-color:var(--blue); }}
  section {{ margin-top:34px; }}
  h2 {{ font-size:19px; padding-bottom:8px; border-bottom:1px solid var(--line); margin-bottom:14px;
       text-transform:capitalize; }}
  .count {{ color:var(--faint); font-weight:400; font-size:14px; }}
  .claim {{ border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-bottom:12px; }}
  .claim.hidden {{ display:none; }}
  .head {{ display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-bottom:8px; }}
  .cid {{ font-family:ui-monospace, Menlo, monospace; font-size:12px; color:var(--blue);
         background:var(--bluebg); border-radius:6px; padding:2px 8px; }}
  .pill {{ font-size:11.5px; border-radius:999px; padding:2px 10px; }}
  .p-ok {{ color:var(--ok); background:var(--okbg); }}
  .p-warn {{ color:var(--warn); background:var(--warnbg); }}
  .p-bad {{ color:var(--bad); background:var(--badbg); }}
  .p-dim {{ color:var(--dim); background:var(--soft); }}
  .text {{ font-size:15px; }}
  blockquote {{ margin:10px 0 8px; padding:8px 14px; border-left:3px solid var(--line);
               color:var(--dim); font-style:italic; background:var(--soft); border-radius:0 8px 8px 0; }}
  .src {{ font-size:12.5px; color:var(--faint); }}
  .src a {{ color:var(--blue); text-decoration:none; }}
  .vnote {{ margin-top:8px; font-size:12.5px; color:var(--dim); background:var(--soft);
           border-radius:8px; padding:7px 11px; }}
  .vnote b {{ font-weight:600; }}
  .note {{ color:var(--dim); font-size:13px; margin-bottom:10px; }}
  .corr {{ padding-left:20px; color:var(--dim); font-size:13.5px; }}
  .corr li {{ margin-bottom:8px; }}
  footer {{ margin-top:40px; color:var(--faint); font-size:12.5px; border-top:1px solid var(--line); padding-top:14px; }}
</style></head><body>
<h1>{h.escape(comp)} — claims ledger</h1>
<div class="sub">Every fact behind the brief, with its verbatim quote, source, and how hard it survived verification.
Generated {h.escape(str(payload.get("generated_at", "")))[:10]} · model {h.escape(str(run.get("model", "")))}</div>
<div class="statbar">
  <span class="stat"><b>{len(claims)}</b> claims</span>
  <span class="stat"><b>{ver.get("confirmed", 0)}</b> confirmed</span>
  <span class="stat"><b>{ver.get("plausible", 0)}</b> plausible</span>
  <span class="stat"><b>{ver.get("unsupported", 0) + ver.get("contradicted", 0)}</b> flagged</span>
  <span class="stat"><b>{ev.get("measured", 0)}</b> measured · <b>{ev.get("proxy", 0)}</b> proxy ·
      <b>{ev.get("stated_by_competitor", 0)}</b> self-claimed · <b>{ev.get("inferred", 0)}</b> inferred</span>
</div>
<div class="filters" id="filters">
  <button class="on" data-f="all">all</button>
  <button data-f="v:confirmed">confirmed</button>
  <button data-f="v:plausible">plausible</button>
  <button data-f="v:flagged">flagged</button>
  <button data-f="e:measured">measured</button>
  <button data-f="e:proxy">proxy</button>
  <button data-f="e:stated_by_competitor">self-claimed</button>
  <button data-f="e:inferred">inferred</button>
</div>
{"".join(sections)}
{corrections}
<footer>Machine-readable twin: the claims JSON in the same folder. Grades — measured: read from a fetched page ·
self-claimed: the competitor says so · proxy: third-party sourced · inferred: analyst read with basis stated.</footer>
<script>
document.getElementById('filters').addEventListener('click', e => {{
  const b = e.target.closest('button'); if (!b) return;
  document.querySelectorAll('#filters button').forEach(x => x.classList.toggle('on', x === b));
  const f = b.dataset.f;
  document.querySelectorAll('.claim').forEach(c => {{
    let show = f === 'all'
      || (f === 'v:flagged' && ['unsupported','contradicted'].includes(c.dataset.v))
      || (f.startsWith('v:') && c.dataset.v === f.slice(2))
      || (f.startsWith('e:') && c.dataset.e === f.slice(2));
    c.classList.toggle('hidden', !show);
  }});
  document.querySelectorAll('section').forEach(s => {{
    const any = s.querySelectorAll('.claim:not(.hidden)').length || !s.querySelector('.claim');
    s.style.display = any ? '' : 'none';
  }});
}});
</script>
</body></html>"""


def _card(c: dict) -> str:
    ev, conf, v = c.get("evidence", "?"), c.get("confidence", "?"), c.get("verified", "?")
    vcls = {"confirmed": "p-ok", "plausible": "p-warn",
            "unsupported": "p-bad", "contradicted": "p-bad"}.get(v, "p-dim")
    ecls = {"measured": "p-ok", "stated_by_competitor": "p-dim",
            "proxy": "p-warn", "inferred": "p-dim"}.get(ev, "p-dim")
    quote = (f"<blockquote>&ldquo;{h.escape(c['quote'])}&rdquo;</blockquote>"
             if c.get("quote") else "")
    vnote = (f'<div class="vnote"><b>verifier:</b> {h.escape(c["verify_note"])}</div>'
             if c.get("verify_note") else "")
    title = h.escape(c.get("source_title", "") or c.get("source_url", ""))
    return f"""<div class="claim" data-v="{h.escape(v)}" data-e="{h.escape(ev)}">
<div class="head"><span class="cid">{h.escape(c.get("id", "?"))}</span>
<span class="pill {vcls}">{h.escape(v)}</span>
<span class="pill {ecls}" title="{h.escape(EV_LABEL.get(ev, ev))}">{h.escape(ev.replace("_", " "))}</span>
<span class="pill p-dim">confidence: {h.escape(conf)}</span></div>
<div class="text">{h.escape(c.get("claim", ""))}</div>
{quote}
<div class="src"><a href="{h.escape(c.get("source_url", "#"))}" target="_blank">{title}</a>
 · observed {h.escape(c.get("observed_at", ""))} · lane: {h.escape(c.get("lane", ""))}</div>
{vnote}
</div>"""


def write_report(claims_json_path) -> str:
    """Render the HTML twin next to a claims JSON file; returns the html path."""
    from pathlib import Path
    p = Path(claims_json_path)
    payload = json.loads(p.read_text())
    out = p.with_suffix(".html")
    out.write_text(render(payload))
    return str(out)
