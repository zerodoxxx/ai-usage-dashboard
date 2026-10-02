# How It Works — AI Tools Usage & Cost Visualizer

## Overview

This project is a local-only web dashboard that reads telemetry files already written to your disk by AI coding tools. Built-in adapters support **OpenAI Codex**, **Claude Code**, and **Google Antigravity (AGY)**.

No API keys, no cloud connections, no subscriptions. Everything runs locally.

---

## The Tools Being Tracked

### 1. OpenAI Codex (`~/.codex/`)

Codex saves detailed session rollout logs in JSONL format. Every time a model makes an API call, Codex appends a structured record to a file like:

```
~/.codex/sessions/2026/09/08/rollout-<uuid>.jsonl
```

Each file contains lines with token usage records:

```json
{
  "type": "token_usage_record",
  "payload": {
    "thread_id": "<uuid>",
    "usage": {
      "input_tokens": 25991,
      "cached_input_tokens": 25600,
      "cache_write_input_tokens": 0,
      "output_tokens": 117,
      "reasoning_output_tokens": 0,
      "total_tokens": 26108
    }
  }
}
```

Codex also maintains `~/.codex/state_5.sqlite` for thread metadata and cumulative totals. Codex does not provide a built-in export of usage to an arbitrary SQLite database. This dashboard uses Codex's supported `Stop`, `SubagentStop`, and `Interrupt` hooks to publish usage into the shared dashboard database. The hook parses only the current transcript named in its payload, along with matching thread metadata; it does not scan conversation history on each dashboard refresh.

The shared database is the existing `~/.gemini/antigravity-cli/token_usage.db`. After the hook is installed and trusted, run `python scripts/codex_usage_writer.py --backfill` once to import existing Codex history. Once that succeeds, dashboard reads for Codex come from SQLite; before capture is enabled, the parser continues to read the local rollout files. The database records a provider key (`codex` or `antigravity`) and model for each row, and prefixes stored session IDs with the provider, so matching native IDs remain distinct. Set `AI_USAGE_DB_PATH` to use a different location. Setup and schema details are in [Shared usage database](docs/SHARED_USAGE_DB.md).

Hook changes may not hot-reload into an already-open Codex Desktop session. In a normal terminal Codex session, use `/hooks` to inspect and trust the exact installed definitions, then reopen Codex Desktop or start a fresh session before relying on captures.

The `Interrupt` hook has a short time limit and may not finish parsing a large transcript. A later `Stop` or a backfill can capture completed work; a turn that remains interrupted can have partial usage until its transcript is complete.

**Key distinction:** `cached_input_tokens` are tokens served from OpenAI's prompt cache (much cheaper). `uncached_input_tokens = input_tokens - cached_input_tokens`.

---

### 2. Google Antigravity / AGY (`~/.gemini/antigravity-cli/`)

AGY stores conversation state differently. It uses:
- **SQLite DBs** (`conversations/<uuid>.db`) — protobuf-serialized step blobs, no native token columns
- **Conversation summaries DB** (`conversation_summaries.db`) — session titles, step counts, timestamps
- **Brain transcripts** (`brain/<session-uuid>/.system_generated/logs/transcript.jsonl`) — step-by-step text records

Because transcript-based AGY sessions don't store raw token counts locally (quota is tracked server-side by Google), **their token counts are estimated** using a standard heuristic. When `token_usage.db` contains exact local token records, those records are marked reported instead:

```
input_tokens  ≈ total_input_chars  // 4
output_tokens ≈ (output_chars + thinking_chars) // 4
```

For multi-turn sessions (where prompt caching is very effective), a **45% cache hit rate** is assumed for input tokens. This is a conservative estimate based on typical coding session patterns. Single-turn sessions assume **0% cache** (no prior context to reuse). Both rules live in `src/parsers/agy.py` as `_AGY_CACHE_HIT_RATE_MULTI_TURN` with a rationale comment.

Transcript-based AGY sessions are marked estimated in the API (`estimated: true`, `token_source: "estimated"`); `token_usage.db` sessions carry explicit reported provenance. The database is also Codex's shared usage store, with rows separated by provider and model. A model containing both estimated and reported AGY sources is marked `mixed`. The dashboard renders approximate rows with a `~` prefix and a provenance badge, plus a footnote under the per-model table.

### 3. Claude Code (`~/.claude/`)

Claude Code stores one JSONL transcript per session below `~/.claude/projects` (including delegated sessions under `subagents/`). Assistant records include the model, timestamp, and API usage fields. The adapter reads base input, cache reads, cache writes, output, and optional reasoning tokens, and deduplicates repeated records that share the same message ID. Per-message events are retained internally so rolling time ranges include only calls that occurred inside the selected window. The dashboard continues to read those transcripts directly; a Claude writer and backfill also publish into the shared database through the `claude-code` provider contract described in [Shared usage database](docs/SHARED_USAGE_DB.md).

Active model is read from `~/.gemini/antigravity-cli/settings.json`.

---

## How the Parser Pipeline Works

```
Codex rollout logs ── one-time --backfill ──┐
Codex Stop / SubagentStop / Interrupt hooks ─┴──▶ ~/.gemini/antigravity-cli/token_usage.db
                                                       │ provider + model + namespaced session ID
                                                       ├──▶ src/parsers/codex.py (read after capture enabled)
                                                       └──▶ src/parsers/agy.py (Antigravity provider rows)

Codex rollout logs ───────────────────────────────▶ codex parser fallback (before capture is enabled)
AGY transcripts + summaries ─────────────────────▶ AGY parser (estimates when exact rows are absent)
Claude Code transcripts ──────────────────────────▶ Claude parser
                                                       │
                                                       ▼
                                            src/parsers/aggregator.py
                                              model, timeline, cost,
                                              sessions, analytics
                                                       │
                                                       ▼
                                      src/app.py → Browser Dashboard
```

---

## Performance: Refresh and Parse Caching

Before Codex capture is enabled, its rollout parser and the Claude Code parser each keep a process-local cache of parsed transcript files, capped at 1,024 entries per parser. Entries are keyed by resolved file path and a filesystem stat signature. An unchanged file can reuse its parsed result during the current server process; cache entries do not persist across restarts. A changed file is parsed in full, and unsuccessful or unstable reads are not cached. After the one-time Codex backfill enables capture, Codex usage is read from the shared SQLite database instead of rescanning its rollout history.

AGY does not use this parsed-file cache. It scans transcript files on each usage refresh, and reads `token_usage.db` events with one ordered query grouped by session instead of one query per session.

The launch script checks that parser modules import and built-in providers are registered; it does not read provider histories. With Codex capture enabled, the first usage request no longer needs to calculate Codex totals from the full rollout history. The dashboard still reads AGY and Claude data and rebuilds aggregates for the selected time range. When all tools are requested, the Codex, Claude Code, and AGY parsers run concurrently.

Before Codex capture is enabled, refresh time still depends on source size and changed files. A local benchmark before the SQLite capture change measured about 6.73 seconds cold and 2.66 seconds warm (2.53× faster with parsed-file caching; pricing-network time excluded). Those figures describe the earlier file-backed path; the database-only path has not yet been benchmarked.

## How Time Windows Stay Accurate

Each parsed session retains internal per-call usage events. Codex events come from imported rollout records in SQLite after capture is enabled, or directly from rollout files before that; hooks publish a replacement snapshot for the current session rather than adding cumulative totals as new calls. AGY transcripts do not expose token counts directly, so the parser estimates the session total and allocates it across model-response events according to their character weights. The API strips these internal records from its response, but the aggregator uses them when applying `month`, `30d`, `7d`, and `24h` windows.

Segments sharing the same tool and session ID are merged before aggregation, and model totals are built from each event's model rather than a single session-level label. A session that genuinely used multiple models is exposed as `mixed`. Metadata-only AGY conversations and user-only transcripts do not create synthetic API calls, tokens, or cost.

That means a long-running conversation is counted by the calls that actually occurred in the selected window, even when the session itself was created much earlier. Older or incomplete records fall back to the best session-level timestamp available. Preset ranges use exact instants, while calendar-month and custom-date boundaries use the dashboard's DST-aware local timezone. Set `AI_USAGE_TIMEZONE` to an IANA zone such as `America/New_York` to override the system timezone.

---

## How the Cost Engine Works

File: `src/pricing.py`

For each model, rates are expressed in USD per 1,000,000 tokens:
- `uncached_input` — tokens not served from cache
- `cached_input` — tokens served from prompt cache (typically 90–95% cheaper)
- `output` — generated completion tokens (including reasoning/thinking)
- `cache_write` — optional prompt-cache creation tokens, when the provider bills them

Model rates are pulled from LiteLLM's public JSON price list at
`https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json`.
The refresh is TTL-based (24 hours by default), thread-safe, and persists a
last-known-good snapshot at `$AI_USAGE_PRICING_CACHE`,
`$XDG_CACHE_HOME/ai-usage-dashboard/litellm-pricing.json`, or
`~/.cache/ai-usage-dashboard/litellm-pricing.json`. Only models seen in local
usage have rates applied from LiteLLM; the bundled `MODEL_PRICING` table remains
as an offline fallback. Offline startup uses the bundled catalog or that snapshot
and reports `stale`/`error` metadata rather than failing usage collection. Local
transcripts do not identify Batch, Flex, Fast, long-context, or regional-processing
tiers, so Standard short-context rates are used for estimates. The server has one
process-global active catalog; the default cache path should be used for normal
operation. Custom cache paths are supported for tests or explicitly switching the
active storage snapshot, and the payload always reactivates the matching rates
before returning its metadata.

**Cost formula (with caching):**
```
cost = (uncached_input × uncached_rate
      + cached_input  × cached_rate
      + output        × output_rate
      + cache_writes  × applicable_write_rate) / 1,000,000
```

**Cost formula (without caching):**
```
cost = ((uncached_input + cached_input + cache_writes) × uncached_rate
      + output × output_rate) / 1,000,000
```

**Net savings = cost_without_caching − cost_with_caching**

Claude 5-minute and 1-hour writes are retained separately and priced at 1.25× and 2× the regular input rate. Expensive writes can therefore produce negative net savings instead of being hidden by a zero clamp. Cache-write tokens are included in `total_input` and `total_tokens`; cache-read percentage uses only regular plus cache-read input as its denominator. AGY's local estimator already includes its source cache-write count in `input_tokens`, so the adapter preserves that source diagnostic without adding the same tokens a second time.

---

## The Frontend: How the Dashboard Updates

1. **On load:** `dashboard.js` calls `GET /api/usage?tool=all&time_range=all`. The current API includes the matching pricing catalog and its `__meta__` provenance/freshness data in that response; older servers can use the separate `/api/pricing` endpoint as a fallback.
2. **The API response** contains: `summary` (odometer values), `models` (per-model table rows), `timeline` (chart data), `sessions` (recent activity list), and `analytics` (derived insights for the selected window)
3. **Odometers** (`odometer.js`): Each number is broken into digit characters. CSS 3D `translateY` shifts a vertical strip of 0–9 digits to land on the right number. Digits animate with staggered delays and `cubic-bezier(0.2, 0.9, 0.3, 1)` easing — right-to-left, like a real counter.
4. **Charts** (Chart.js): Token breakdown, daily cost/token/call trend, cost by tool, blended cost per 1M tokens, cache-efficiency trend, hourly activity, and a weekday/hour heatmap
5. **Auto-refresh:** A configurable `setInterval` (10s / 30s / 60s) re-calls `GET /api/usage`. Each user-initiated action (tool switch, manual refresh) creates a new `AbortController`, cancelling any in-flight request before starting a fresh one.
6. **Session search:** Client-side filtering on `state.allSessions` — no additional server calls.
7. **Time filtering:** The header time selector requests one of `all`, `month`, `30d`, `7d`, `24h`, or `custom`. The server slices per-call events where available, then rebuilds the summary, model, timeline, session, and analytics results together.

---

## Directory Reference

| Path | What It Is |
|:-----|:-----------|
| `run.py` | CLI entry point — starts uvicorn, optionally opens browser |
| `src/app.py` | FastAPI app — 4 endpoints: `/`, `/api/usage`, `/api/pricing`, `/api/health` |
| `src/litellm_pricing.py` | LiteLLM pricing feed parser, exact key candidates, and model index |
| `src/pricing.py` | Provider catalog, LiteLLM refresh/cache, and cost calculator |
| `src/parsers/contracts.py` | Provider-neutral token, event, session, and cost contracts |
| `src/parsers/source_registry.py` | Provider adapter registry with canonical-key and alias lookup |
| `src/usage_store.py` | Shared provider-aware SQLite storage and normalized session reads/writes |
| `scripts/codex_usage_writer.py` | Codex completion-hook publisher and explicit one-time backfill |
| `src/parsers/codex.py` | Reads Codex from the shared SQLite store after capture, with rollout-file fallback |
| `src/parsers/agy.py` | Reads AGY transcripts + DBs, estimates tokens, returns structured metrics dict |
| `src/parsers/claude.py` | Reads Claude Code session JSONL and normalizes API usage |
| `src/parsers/aggregator.py` | Runs registered adapters, prices normalized sessions, slices time windows, and derives analytics |
| `src/static/js/odometer.js` | `RollingOdometer` class — zero-dependency vertical digit animation |
| `src/static/js/utils.js` | Shared formatting, provenance, escaping, and toast helpers |
| `src/static/js/api.js` | Usage/pricing requests, cancellation, and cache-busting |
| `src/static/js/dashboard.js` | Frontend orchestrator: state, polling, and filters |
| `src/static/js/charts.js` | Chart.js visualizations and activity heatmap |
| `src/static/js/tables.js` | Model/session tables and search filtering |
| `src/static/js/analytics.js` | Analytics and period-comparison renderer |
| `src/static/css/dashboard.css` | Dark-mode styles — frosted glass cards, badge colours, table layout |
| `src/templates/index.html` | Static HTML scaffold — odometer containers, chart canvases, tables |
| `test_parsers.py` | Tests pricing engine + both parsers against live local data |
| `test_server.py` | Spins up a test server, hits all endpoints, checks response correctness |

---

## Extending It

### Add a new AI tool
1. Create `src/parsers/<toolname>.py` implementing the `UsageSource` protocol and return normalized `UsageSession` objects from `extract_sessions()`.
2. Register the adapter in `DEFAULT_SOURCE_REGISTRY`; the aggregator automatically includes it in `tool=all` and resolves its aliases.
3. If the tool has known prices, register its provider-scoped models in `PricingCatalog`. Unknown models remain explicitly unpriced rather than receiving another provider's fallback rate.
4. Add an `<option>` to the `<select id="tool-select">` in `index.html` when it should be selectable in the current UI.

New adapters should own only provider-specific discovery and decoding. Token normalization, cost enrichment, time slicing, model/timeline aggregation, and API serialization are shared. The built-in adapters retain their mature legacy parser wrappers during migration, while exposing normalized sessions to the shared pipeline. The contracts include cache-read and cache-write counts plus reported-versus-estimated cost provenance for providers with different billing formats.

### Add a new model's pricing
New models present in LiteLLM require no manual configuration. If LiteLLM lacks a model or uses an alternate model ID, add an alias in `_ALIASES` in `src/pricing.py` or register a provider/model fallback entry in `PricingCatalog` with `uncached_input`, `cached_input`, and `output` rates ($/1M tokens). Optional `cache_write` or `cache_creation` rates are supported. `MODEL_PRICING`, `get_pricing()`, and `calculate_cost()` remain available for backward compatibility.

### Change the polling interval default
Edit `state.autoRefreshInterval` in `dashboard.js` (line ~12). Value is in milliseconds.
