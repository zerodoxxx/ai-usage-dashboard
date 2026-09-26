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
- ⚡ **Fresh live polling**: source files are reparsed for each refresh, with parallel all-tool parsing
- 🔎 **Session search** — filter across titles, models, and session IDs
- ⬇️ **CSV export** — download the sessions in the selected range and search filter
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
| **Codex** | `~/.codex/state_5.sqlite` + `~/.codex/sessions/**/rollout-*.jsonl` |
| **Claude Code** | `~/.claude/projects/**/*.jsonl` assistant usage records |
| **AGY** | `~/.gemini/antigravity-cli/brain/**/transcript.jsonl` + `conversation_summaries.db` |

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
| `GET /api/pricing` | Active model rates plus `__meta__` source/freshness; OpenAI rates refresh from the official pricing table |
| `GET /api/health` | Health check |

## Project Structure

```
src/
├── app.py             # FastAPI server
├── pricing.py         # Provider-aware pricing catalog
├── parsers/
│   ├── codex.py       # Codex JSONL + SQLite parser
│   ├── claude.py      # Claude Code session JSONL parser
│   ├── agy.py         # AGY transcript + DB parser
│   ├── contracts.py   # Normalized usage contracts
│   ├── source_registry.py # Provider adapter registry
│   └── aggregator.py  # Registration-driven aggregator
├── static/js/
│   ├── odometer.js    # RollingOdometer (zero-dependency)
│   ├── utils.js       # Formatting, provenance, and toast helpers
│   ├── api.js         # Fetch/cancellation and pricing metadata
│   ├── charts.js      # Chart.js visualizations and heatmap
│   ├── tables.js      # Model/session tables and CSV export
│   ├── analytics.js   # Insights renderer
│   └── dashboard.js   # UI orchestrator
└── templates/
    └── index.html     # Dashboard layout
```
