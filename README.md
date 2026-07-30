# Pranav's GTM Agent — competitive-intel formation

A conversational agent that takes any competitor name or domain in Rippling's space and produces an evidence-grounded analysis of their public marketing strategy: a markdown brief plus a structured claims-ledger JSON with sources, quotes, confidence grades, and timestamps.

Built to answer one question fast: what is this competitor's marketing actually doing, and where is it beatable? Public sources only — websites, ad libraries, social, press, review sites. Nothing behind logins.

**Design rationale, machine-readable:** [`DESIGN.json`](DESIGN.json) — every architecture decision with its tradeoff, tool/model choices, the eval system's full logic flow, and a map from the five questions any evaluation of an agent like this asks to the evidence in this repo.

## Run it

```bash
git clone https://github.com/Pezym/rippling-gtm-agent && cd rippling-gtm-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**Runtime A — Anthropic API** (needs a key with web search enabled):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python agent.py            # interactive
python agent.py "Gusto"    # pre-loaded
```

**Runtime B — keyless, via the Claude Code CLI** (bills your Claude subscription instead; for reviewers without an API key):

```bash
npm install -g @anthropic-ai/claude-code && claude login   # one-time
python agent.py --runtime cli "Gusto"
# add --checkpoints for the guided lane-by-lane walk in the terminal
```

**Web chat UI** (same Session, same formation — a browser chat instead of the terminal):

```bash
python server.py                        # api runtime
AGENT_RUNTIME=cli python server.py      # keyless
# open http://localhost:7788
```

The chat pane is the master's voice; the formation's activity renders inline as it happens — dispatch cards with per-lane claim counts and coverage failures, adversarial-verify breakdown pills, and an expandable brief card with download links. Clarifying questions ("ADP is three motions — which one?") and follow-ups ("dig deeper on pricing", "now run Gusto") work exactly as in the CLI, because it is the same master loop behind an SSE stream (`server.py` + `ui/index.html`, no build step).

Same formation, prompts, ledger, verifier, and evals in both. CLI-runtime tradeoffs, stated honestly: tool budgets become prompt instructions instead of server-enforced caps, structured outputs are parsed defensively instead of schema-validated at the API layer, and the master's tool loop runs over a JSON action protocol instead of native `tool_use` blocks (`runtime_cli.py` — still a real decide→execute→observe loop, transported over subprocess calls).

Talk to it: `analyze Deel` → clarifying question or straight to work → live progress → brief + JSON land in `outputs/`. Follow-ups work: `dig deeper on their pricing`, `now run Justworks`, `what did the ads lane actually fail on?`.

A full single-competitor run lands in ~8–11 minutes wall (≈6–8 min of formation work — every run writes its own per-stage timing receipt into the claims JSON). Two research rounds and 90–100 claims is typical.

## Architecture

One orchestrator, disposable specialists, an adversarial gate, and a writer — a formation, not a pipeline:

```
                        ┌─────────────────────────────┐
 user ⟷ CLI ⟷ REPL ⟷   │  MASTER (orchestrator)       │  decides lanes, routes failures,
                        │  agent.py · Opus 4.8, high   │  goes deeper, synthesizes, talks
                        └──────┬───────────┬──────────┘
                    dispatch_research      │ verify_claims / write_brief / view_ledger
                               │           │
              ┌────────┬───────┼────────┬──┴─────┐            ┌──────────────────┐
              ▼        ▼       ▼        ▼        ▼            │  CLAIMS LEDGER    │
           [ads] [positioning][pricing][social][news]  ──────▶│  append-only,     │
            parallel researchers · web_search + web_fetch     │  session state    │
            (server-side tools, per-lane budgets)             └───────┬──────────┘
                                                                      ▼
                                                   VERIFIER (adversarial: tries to refute
                                                   every claim before it can land) 
                                                                      ▼
                                                   BRIEF WRITER → outputs/*.md + *.json
```

**Why this shape, not a linear script:**

- **The master makes real decisions.** Which lanes to run for *this* competitor, what focus to give each researcher, whether a dry lane means retry / reroute / ask the user, when one more research round is worth the money, when to stop. The loop is a hand-written tool loop (`agent.py`), so every decision point is visible and debuggable.
- **Failure is routed, not swallowed — and resolved silently.** Meta's ad library frequently renders empty to automated fetches. Researchers auto-descend a multi-tier source ladder (official libraries → ad-spy aggregators → landing pages → pixel/traffic/hiring signals), keep whatever partial read exists, and the gap is disclosed exactly once in the brief's §8 — the user hears findings, never plumbing. Tool crashes return `is_error` results to the master rather than killing the run.
- **Nothing lands unverified.** Every claim carries an evidence grade (`measured` / `stated_by_competitor` / `proxy` / `inferred`), a verbatim quote, URL, and timestamp. Before a brief is written, an adversarial verifier re-reads (and spot re-fetches) claims trying to knock them down — overreaching quotes get downgraded, competitor self-praise stays attributed, contradictions get flagged. The brief cites claim IDs inline, so every sentence is auditable back to a URL.
- **Follow-ups are cheap.** The ledger is session state. "Dig deeper on pricing" checks what's already known, dispatches one focused lane, verifies only the new claims. "Run it for Gusto" starts a second competitor in the same session.
- **Re-runs remember.** The dated claims JSONs in `outputs/` (committed to git) are longitudinal memory. On a repeat run of the same competitor, `diffing.py` computes a deterministic evidence diff against the latest prior run — sources that appeared, verbatim quotes that vanished — and the brief narrates it as "Since the last run" in §6. The "what's changed recently" question gets sharper every time the agent runs.

## Tool & model choices

| Choice | Why |
|---|---|
| Anthropic API, hand-written tool loop | The agentic loop *is* the product; a framework would hide the decisions worth seeing. ~600 lines total, no framework dependency to audit. |
| Server-side `web_search` + `web_fetch` | Zero scraping infra for reviewers to install, no API keys beyond Anthropic's, built-in dynamic filtering. The tradeoff — some JS-heavy surfaces (ad libraries) don't render — is handled as first-class coverage reporting + search-based fallbacks, not ignored. |
| Opus for judgment, Sonnet for extraction | Master, verifier, brief writer and judge run the flagship; the five lane researchers run `claude-sonnet-5` with an 18-claim cap — extraction goes fast, judgment stays heavy. Override via `AGENT_MODEL` / `LANE_MODEL` / `JUDGE_MODEL`. |
| Structured outputs (`output_config.format`) | Researcher/verifier/judge results are schema-validated JSON at the API layer — no brittle "please return JSON" parsing. |
| Prompt caching | Master system prompt and per-lane playbooks carry `cache_control`; multi-turn sessions reuse the prefix. |
| Parallel lanes (threads) | Five researchers are independent; wall-clock ≈ slowest lane instead of the sum. |

## The skill system inside

The agent runs on a skill library I've built and trained into my environment over time — marketing analysis, orchestration, and GTM engineering — distilled into `prompts/`. Four disciplines carry the weight:

1. **Orchestration doctrine** — the formation itself: goal-first intake → decompose into parallel workers → adversarial verify before anything lands → one voice reporting, with failure-as-decision routing. `prompts/master.md`.
2. **Marketing research playbooks** — page-by-page site extraction with honest-assessment rules; the creative-is-the-targeting ads lens (identity-trigger keywords → which ICPs they're buying, format mix and refresh cadence as spend signals, long-running headlines as proven messages); comparison discipline and packaging-as-positioning. `prompts/lane_*.md`, `prompts/brief_writer.md`.
3. **Evidence protocol** — claims-ledger discipline (append-only, exact wording, provenance, review state), the `measured / stated_by_competitor / proxy / inferred` grading contract, the category-narrative teardown (arc, claimed onlyness, proof pattern, archive-diff messaging drift), and the onlyness test for positioning claims. `schemas.py`, `prompts/evidence_protocol.md`, `prompts/verifier.md`, `prompts/lane_positioning.md`.
4. **Context compression** — researcher→master summaries are deliberately telegraphic ("compressed, information-dense, fragments fine"), keeping the master's context window lean across multi-competitor sessions; the user-facing brief switches back to full prose. The compression boundary is explicit in the prompts.

Security note baked into every prompt: fetched pages are treated as data, never instructions — every researcher and the master carry an injection-resistance clause.

### GTM-engineering patterns folded in

1. **Claude-as-subprocess runtime** → `runtime_cli.py`: the entire agent runs keyless against a Claude subscription.
2. **Ad taxonomy + longevity buckets** → the ads lane emits per-ad structured rows and treats run-duration (<7d test / 7–30d scaling / >30d declared bet) as the public library's one performance proxy, paired with impressions where exposed.
3. **Version-controlled intel + run diffing** → `diffing.py` over the dated claims JSONs already in git: a diff between runs is a changelog for the competitor's marketing.
4. **Declared-strategy vs actual-demand pairing + answer-engine share of voice** → the social lane collects buyer talk, the positioning lane checks who owns "X vs Rippling" answer surfaces, and §7 attacks the gap between what competitors say and what buyers ask.

## Sample output

`outputs/gusto-brief-2026-07-27.md` + `outputs/gusto-claims-2026-07-27.json` — a full Gusto run: 78 claims (56 confirmed / 22 plausible / 0 flagged by the adversarial verifier), 15/15 structural eval pass, LLM-judge 5/5. The sample was produced by this exact formation and these exact prompts executed on the Claude Code agent runtime (same model family, same tools); `agent.py` is the identical architecture packaged standalone for reviewers.

## Eval system

`evals/run_evals.py` — three layers, cheap to expensive:

1. **Structural (free, deterministic):** claims JSON field/enum validity, quote presence on non-inferred claims, all 8 brief sections present, citation density (≥15 distinct claim IDs), zero dangling citations, the Rippling section anchored by ≥5 citations, verification pass-rate ≥60%.
2. **Grounding spot-check (network only):** re-fetches a sample of `measured` claims and fuzzy-matches the stored quote against live page text — catches hallucinated quotes.
3. **LLM judge (API cost):** a "demanding Rippling VP of Growth" rubric scoring evidence-grounding, specificity, recency, and Rippling-actionability 1–5, with named weaknesses.

```bash
python evals/run_evals.py outputs/<slug>-brief-<date>.md outputs/<slug>-claims-<date>.json
python evals/run_evals.py <brief> <claims> --grounding 5 --judge   # full pass
```

Exit code is non-zero on any deterministic failure, so it slots into CI.

## Tradeoffs I chose (and would revisit with more time)

- **No headless browser.** Playwright would open the JS-rendered ad libraries properly. I chose reviewer-runnable simplicity + graceful degradation; the architecture takes a browser tool as just another lane budget line.
- **Session-scoped ledger.** Claims die with the process; the dated JSON exports survive and now power run-over-run diffing (`diffing.py`). A SQLite ledger would go further (querying across competitors, per-claim history) — the `observed_at` field and append-only design anticipate exactly that.
- **Verifier samples under budget.** It prioritizes load-bearing claims rather than re-fetching everything; a paranoid mode would re-verify 100%.
- **Threads, not asyncio.** Five concurrent calls don't need an event loop; readability won.

## Repo map

```
agent.py        master loop + CLI (the conversation and the decisions)
server.py       web chat: FastAPI + SSE wrapper around the same Session
ui/index.html   the chat frontend (single file, no build step)
research.py     lane dispatch, verifier, brief writer (the formation's hands)
llm.py          API plumbing: pause_turn/refusal/backoff handling, caching, budgets
runtime_cli.py  keyless runtime: every model call via `claude -p` subprocess
diffing.py      run-over-run evidence diff (dated claims JSONs = longitudinal memory)
schemas.py      claims ledger + JSON schemas for structured outputs
prompts/        the distilled skills — master, 5 lanes, evidence protocol, verifier, writer
evals/          three-layer eval harness
outputs/        generated briefs + claims ledgers (sample committed)
```
