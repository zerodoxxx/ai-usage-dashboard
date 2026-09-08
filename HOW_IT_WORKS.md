# How It Works — AI Tools Usage & Cost Visualizer

## Overview

This project is a local-only web dashboard that reads telemetry files already written to your disk by two AI coding tools — **OpenAI Codex** and **Google Antigravity (AGY)** — and presents them as live token metrics, API inference cost estimates, and interactive charts.

No API keys, no cloud connections, no subscriptions. Everything runs locally.

---

## The Two Tools Being Tracked

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

AGY has a snapshot cache keyed by a lightweight signature of its settings, SQLite, and transcript files. A changed file invalidates the AGY snapshot; unchanged polls reuse a deep-copied result. When the dashboard requests all tools, the Codex and AGY parsers run concurrently, so the request is bounded by the slower parser instead of the sum of both parser times.

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
| `src/parsers/codex.py` | Reads Codex rollout JSONL + SQLite, returns structured metrics dict |
| `src/parsers/agy.py` | Reads AGY transcripts + DBs, estimates tokens, returns structured metrics dict |
| `src/parsers/aggregator.py` | Runs parsers, merges by canonical model name, slices time windows, rebuilds aggregates, and derives analytics |
| `src/static/js/odometer.js` | `RollingOdometer` class — zero-dependency vertical digit animation |
| `src/static/js/dashboard.js` | All frontend logic: state, polling, Chart.js, table rendering, search |
| `src/static/css/dashboard.css` | Dark-mode styles — frosted glass cards, badge colours, table layout |
| `src/templates/index.html` | Static HTML scaffold — odometer containers, chart canvases, tables |
| `test_parsers.py` | Tests pricing engine + both parsers against live local data |
| `test_server.py` | Spins up a test server, hits all endpoints, checks response correctness |

---

## Extending It

### Add a new AI tool
1. Create `src/parsers/<toolname>.py` returning the same schema as `codex.py` and `agy.py`
2. Add a case to `src/parsers/aggregator.py → get_tool_usage()`
3. Add an `<option>` to the `<select id="tool-select">` in `index.html`

### Add a new model's pricing
Edit `MODEL_PRICING` in `src/pricing.py` — add a dict with `uncached_input`, `cached_input`, `output` keys ($/1M tokens). Optionally add an alias to `_ALIASES` for fuzzy matching.

### Change the polling interval default
Edit `state.autoRefreshInterval` in `dashboard.js` (line ~12). Value is in milliseconds.
