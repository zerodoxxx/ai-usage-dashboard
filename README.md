# AI Tools Usage & Cost Visualizer

A local real-time dashboard that visualizes token usage and API inference costs for **OpenAI Codex**, **Claude Code**, and **Google Antigravity (AGY)** — directly from your filesystem, with no API keys or telemetry uploads.

## Features

- 🎰 **Mechanical rolling odometers** for Total Tokens, API Cost, Cached Tokens, Cache Hit Rate, Output Tokens, and Sessions
- 📊 **Interactive Chart.js visualizers** — token/model trends, tool spend, cache efficiency, hourly activity, and weekday/hour heatmap
- 🔍 **Per-model granularity table** with 12 columns: uncached input, cached input, output, reasoning tokens, cache hit %, API rates, cost with/without caching, net savings
- 🔄 **Live auto-refresh** (10s/30s/60s) with AbortController cancellation
- 🔽 **Tool filter dropdown**: All Tools, OpenAI Codex, Claude Code, AGY (Google Antigravity)
- 🗓️ **Time filter dropdown**: All time, This month, Past 30 days, Past 7 days, Past 24h, Custom range
- 📊 **Analytics snapshot**: API calls, averages, peak spend day, top-cost sessions, period comparison, and a 30-day cost projection from the selected filter's daily average
- ⚡ **Codex SQLite capture**: after a one-time history import, dashboard reads usage from the shared store and completion hooks keep it current
- 🔎 **Session search** — filter across titles, models, and session IDs
- ⚠️ **Cost provenance** — mixed, estimated, reported, and unpriced usage is surfaced explicitly

## Quickstart

```bash
conda activate ai-usage-dashboard
python run.py --open
```

To start the dashboard independently of the terminal or IDE that launched it:

```bash
bash launch_background.sh
```

The command waits until the health endpoint is ready, opens the dashboard, and
then returns so the terminal can be closed safely. The server output is written
to `~/.ai-usage-dashboard/dashboard.log` and its process is tracked in
`~/.ai-usage-dashboard/dashboard.pid`.

```bash
bash launch_background.sh --status
bash launch_background.sh --log
bash launch_background.sh --stop
```

Dashboard: http://127.0.0.1:8765

Calendar-month and custom-date filters use the machine's DST-aware local timezone. To override it explicitly, set an IANA timezone before launch:

```bash
AI_USAGE_TIMEZONE=America/New_York python run.py
```

## Data Sources

| Tool | Where Data Comes From |
|:-----|:---------------------|
| **Codex** | Shared `usage.db` (`provider = codex`), filled by Codex hooks and a one-time backfill |
| **Claude Code** | Shared `usage.db` (`provider = claude-code`), filled by a Claude Code writer and backfill |
| **AGY** | Shared `usage.db` (`provider = antigravity`), filled by the Antigravity CLI |

The dashboard reads **only** this SQLite database at request time. It never
parses `~/.claude`, `~/.codex` or Antigravity transcripts and logs; those are
parsed by the writers and backfills, which import the parsers in
`src/parsers/`. A provider with no rows shows as empty, not as an error.

The shared database has separate provider and model columns, and namespaced
session IDs, so Codex records do not mix with Antigravity records. Its default
path is `~/.local/share/ai-usage/usage.db`; set `AI_USAGE_DB_PATH` to override
it. To move an existing database from the old Antigravity location
(`~/.gemini/antigravity-cli/token_usage.db`), run `python scripts/migrate_usage_db.py`.
Antigravity's `track_usage.py` honors `AI_USAGE_DB_PATH` and defaults to the new path. Claude Code can write to the same store through the shared provider API
once its writer is configured. See [the shared usage database guide](docs/SHARED_USAGE_DB.md)
for Codex setup, backfill, and the writer contract.

## Run Tests

```bash
python test_parsers.py   # Parser + pricing verification
python test_server.py    # API endpoint tests
```

## API

| Endpoint | Description |
|:---------|:------------|
| `GET /api/usage?tool=all&time_range=all` | All tools aggregated; includes `analytics` |
| `GET /api/usage?tool=codex&time_range=30d` | Codex usage from the past 30 days |
| `GET /api/usage?tool=agy&time_range=month` | AGY usage from the current calendar month |
| `GET /api/usage?tool=claude-code&time_range=7d` | Claude Code usage from the past 7 days |
| `GET /api/pricing` | Active model rates plus `__meta__` source/freshness; pulls rates from LiteLLM's public price list |
| `GET /api/health` | Health check |

## Project Structure

```
src/
├── app.py             # FastAPI server
├── pricing.py         # Provider-aware pricing catalog
├── parsers/
│   ├── store_source.py # Dashboard read path: sessions from the shared SQLite store
│   ├── codex.py       # Codex rollout parser (used by writers/backfill)
│   ├── claude.py      # Claude Code JSONL parser (used by writers/backfill)
│   ├── agy.py         # AGY transcript parser (used by writers/backfill)
│   ├── contracts.py   # Normalized usage contracts
│   ├── source_registry.py # Provider adapter registry
│   └── aggregator.py  # Registration-driven aggregator
├── static/js/
│   ├── odometer.js    # RollingOdometer (zero-dependency)
│   ├── utils.js       # Formatting, provenance, and toast helpers
│   ├── api.js         # Fetch/cancellation and pricing metadata
│   ├── charts.js      # Chart.js visualizations and heatmap
│   ├── tables.js      # Model/session tables and search filtering
│   ├── analytics.js   # Insights renderer
│   └── dashboard.js   # UI orchestrator
└── templates/
    └── index.html     # Dashboard layout
```
