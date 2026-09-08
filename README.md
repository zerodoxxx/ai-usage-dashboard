# AI Tools Usage & Cost Visualizer

A local real-time dashboard that visualizes token usage and API inference costs for **OpenAI Codex**, **Claude Code**, and **Google Antigravity (AGY)** — directly from your filesystem, no external connections needed.

## Features

- 🎰 **Mechanical rolling odometers** for Total Tokens, API Cost, Cached Tokens, Cache Hit Rate, Output Tokens, and Sessions
- 📊 **Interactive Chart.js visualizers** — stacked token breakdown by model + dual-axis daily cost/token trend
- 🔍 **Per-model granularity table** with 12 columns: uncached input, cached input, output, reasoning tokens, cache hit %, API rates, cost with/without caching, net savings
- 🔄 **Live auto-refresh** (10s/30s/60s) with AbortController cancellation
- 🔽 **Tool filter dropdown**: All Tools, OpenAI Codex, Claude Code, AGY (Google Antigravity)
- 🗓️ **Time filter dropdown**: All time, This month, Past 30 days, Past 7 days, Past 24h
- 📊 **Analytics snapshot**: API calls, averages, peak spend day, top-cost sessions, period comparison, and monthly run-rate projection
- ⚡ **Efficient live polling**: cached parser work for unchanged files plus parallel all-tool parsing
- 🔎 **Session search** — filter across titles, models, and session IDs

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
| `GET /api/pricing` | Model pricing rates |
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
│   └── dashboard.js   # UI controller
└── templates/
    └── index.html     # Dashboard layout
```
