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

Codex also maintains a SQLite database at `~/.codex/state_5.sqlite` with a `threads` table indexing all sessions and their cumulative token totals.

**Key distinction:** `cached_input_tokens` are tokens served from OpenAI's prompt cache (much cheaper). `uncached_input_tokens = input_tokens - cached_input_tokens`.

---

### 2. Google Antigravity / AGY (`~/.gemini/antigravity-cli/`)

AGY stores conversation state differently. It uses:
- **SQLite DBs** (`conversations/<uuid>.db`) — protobuf-serialized step blobs, no native token columns
- **Conversation summaries DB** (`conversation_summaries.db`) — session titles, step counts, timestamps
- **Brain transcripts** (`brain/<session-uuid>/.system_generated/logs/transcript.jsonl`) — step-by-step text records

Because AGY doesn't store raw token counts locally (quota is tracked server-side by Google), **token counts are estimated** using a standard heuristic:

```
input_tokens  ≈ total_input_chars  // 4
output_tokens ≈ (output_chars + thinking_chars) // 4
```

For multi-turn sessions (where prompt caching is very effective), a **45% cache hit rate** is assumed for input tokens. This is a conservative estimate based on typical coding session patterns.

### 3. Claude Code (`~/.claude/`)

Claude Code stores one JSONL transcript per session below `~/.claude/projects` (including delegated sessions under `subagents/`). Assistant records include the model, timestamp, and API usage fields. The adapter reads base input, cache reads, cache writes, output, and optional reasoning tokens, and deduplicates repeated records that share the same message ID. Per-message events are retained internally so rolling time ranges include only calls that occurred inside the selected window.

Active model is read from `~/.gemini/antigravity-cli/settings.json`.

---

## How the Parser Pipeline Works

```
disk files
    │
    ├── ~/.codex/state_5.sqlite         ─────┐
    ├── ~/.codex/sessions/**/rollout-*.jsonl  │──▶ src/parsers/codex.py
    │                                         │       ↓ per-model breakdown
    ├── ~/.gemini/antigravity-cli/            │       ↓ timeline (by date)
    │   ├── settings.json               ─────┤──▶ src/parsers/agy.py
    │   ├── conversation_summaries.db         │       ↓ token estimates
    │   ├── conversations/*.db                │       ↓ cost calculations
    │   └── brain/**/transcript.jsonl   ─────┘
    │
    └──────────────────────────────────────────▶ src/parsers/aggregator.py
                                                        ↓ model dedup
                                                        ↓ merged timeline
                                                        ↓ combined sessions
                                                        │
                                                 src/app.py (FastAPI)
                                                        │
                                                        GET /api/usage?tool=all&time_range=all
                                                        │
                                                 src/templates/index.html
                                                 src/static/js/dashboard.js
                                                        │
                                                 Browser Dashboard
```

---

## Performance: The Parse Cache

Codex rollout files can be large (2–4 MB each). Re-parsing 50–100 files on every 10-second poll would be slow and wasteful.

**Solution:** `src/parsers/codex.py` maintains an in-memory dictionary `_ROLLOUT_PARSE_CACHE` keyed by `(file_path, mtime_ns, size)`. If a rollout file hasn't been modified since the last parse, the cached result is returned immediately. Only new or modified files trigger actual JSONL reads.

AGY has a snapshot cache keyed by a lightweight signature of its settings, SQLite, and transcript files. A changed file invalidates the AGY snapshot; unchanged polls reuse a deep-copied result. When the dashboard requests all tools, the Codex, Claude Code, and AGY parsers run concurrently, so the request is bounded by the slowest parser instead of the sum of all three.

This means after the first load, polling responses are nearly instant.

## How Time Windows Stay Accurate

Each parsed session retains internal per-call usage events. Codex events come from incremental token usage records. AGY transcripts do not expose token counts directly, so the parser estimates the session total and allocates it across model-response events according to their character weights. The API strips these internal records from its response, but the aggregator uses them when applying `month`, `30d`, `7d`, and `24h` windows.

That means a long-running conversation is counted by the calls that actually occurred in the selected window, even when the session itself was created much earlier. Older or incomplete records fall back to the best session-level timestamp available.

---

## How the Cost Engine Works

File: `src/pricing.py`

For each model, three rates are defined ($ per 1,000,000 tokens):
- `uncached_input` — tokens not served from cache
- `cached_input` — tokens served from prompt cache (typically 90–95% cheaper)
- `output` — generated completion tokens (including reasoning/thinking)

**Cost formula (cached):**
```
cost = (uncached_input × uncached_rate
      + cached_input  × cached_rate
      + output        × output_rate) / 1,000,000
```

**Cost formula (without caching):**
```
cost = ((uncached_input + cached_input) × uncached_rate
      + output × output_rate) / 1,000,000
```

**Net savings = cost_without_caching − cost_with_caching**

This lets you see exactly how much you'd be paying if OpenAI/Google didn't have prompt caching — and how much the cache is saving you.

---

## The Frontend: How the Dashboard Updates

1. **On load:** `dashboard.js` calls `GET /api/pricing` (once) then `GET /api/usage?tool=all&time_range=all`
2. **The API response** contains: `summary` (odometer values), `models` (per-model table rows), `timeline` (chart data), `sessions` (recent activity list), and `analytics` (derived insights for the selected window)
3. **Odometers** (`odometer.js`): Each number is broken into digit characters. CSS 3D `translateY` shifts a vertical strip of 0–9 digits to land on the right number. Digits animate with staggered delays and `cubic-bezier(0.2, 0.9, 0.3, 1)` easing — right-to-left, like a real counter.
4. **Charts** (Chart.js): Token breakdown (stacked bar) and daily cost + token + API-call trend (multi-axis line + bar)
5. **Auto-refresh:** A configurable `setInterval` (10s / 30s / 60s) re-calls `GET /api/usage`. Each user-initiated action (tool switch, manual refresh) creates a new `AbortController`, cancelling any in-flight request before starting a fresh one.
6. **Session search:** Client-side filtering on `state.allSessions` — no additional server calls.
7. **Time filtering:** The header time selector requests one of `all`, `month`, `30d`, `7d`, or `24h`. The server slices per-call events where available, then rebuilds the summary, model, timeline, session, and analytics results together.

---

## Directory Reference

| Path | What It Is |
|:-----|:-----------|
| `run.py` | CLI entry point — starts uvicorn, optionally opens browser |
| `src/app.py` | FastAPI app — 4 endpoints: `/`, `/api/usage`, `/api/pricing`, `/api/health` |
| `src/pricing.py` | 14 model pricing dictionaries + cost calculator |
| `src/parsers/contracts.py` | Provider-neutral token, event, session, and cost contracts |
| `src/parsers/source_registry.py` | Provider adapter registry with canonical-key and alias lookup |
| `src/parsers/codex.py` | Reads Codex rollout JSONL + SQLite, returns structured metrics dict |
| `src/parsers/agy.py` | Reads AGY transcripts + DBs, estimates tokens, returns structured metrics dict |
| `src/parsers/claude.py` | Reads Claude Code session JSONL and normalizes API usage |
| `src/parsers/aggregator.py` | Runs registered adapters, prices normalized sessions, slices time windows, and derives analytics |
| `src/static/js/odometer.js` | `RollingOdometer` class — zero-dependency vertical digit animation |
| `src/static/js/dashboard.js` | All frontend logic: state, polling, Chart.js, table rendering, search |
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
Register a provider/model entry in `PricingCatalog` with `uncached_input`, `cached_input`, and `output` rates ($/1M tokens). Optional `cache_write` or `cache_creation` rates are supported. `MODEL_PRICING`, `get_pricing()`, and `calculate_cost()` remain available for backward compatibility.

### Change the polling interval default
Edit `state.autoRefreshInterval` in `dashboard.js` (line ~12). Value is in milliseconds.
