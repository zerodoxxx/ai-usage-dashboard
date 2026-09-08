# AI Tools Usage & Cost Visualizer

A local real-time dashboard that visualizes token usage and API inference costs for **OpenAI Codex** and **Google Antigravity (AGY)** — directly from your filesystem, no external connections needed.

## Features

- 🎰 **Mechanical rolling odometers** for Total Tokens, API Cost, Cached Tokens, Cache Hit Rate, Output Tokens, and Sessions
- 📊 **Interactive Chart.js visualizers** — stacked token breakdown by model + dual-axis daily cost/token trend
- 🔍 **Per-model granularity table** with 12 columns: uncached input, cached input, output, reasoning tokens, cache hit %, API rates, cost with/without caching, net savings
- 🔄 **Live auto-refresh** (10s/30s/60s) with AbortController cancellation
- 🔽 **Tool filter dropdown**: All Tools, OpenAI Codex, AGY (Google Antigravity)
- 🔎 **Session search** — filter across titles, models, and session IDs

## Quickstart

```bash
conda activate ai-usage-dashboard
python run.py --open
```

Dashboard: http://127.0.0.1:8765

## Data Sources

| Tool | Where Data Comes From |
|:-----|:---------------------|
| **Codex** | `~/.codex/state_5.sqlite` + `~/.codex/sessions/**/rollout-*.jsonl` |
| **AGY** | `~/.gemini/antigravity-cli/brain/**/transcript.jsonl` + `conversation_summaries.db` |

## Run Tests

```bash
python test_parsers.py   # Parser + pricing verification
python test_server.py    # API endpoint tests
```

## API

| Endpoint | Description |
|:---------|:------------|
| `GET /api/usage?tool=all` | All tools aggregated |
| `GET /api/usage?tool=codex` | Codex only |
| `GET /api/usage?tool=agy` | AGY only |
| `GET /api/pricing` | Model pricing rates |
| `GET /api/health` | Health check |

## Project Structure

```
src/
├── app.py             # FastAPI server
├── pricing.py         # 14 model pricing rates
├── parsers/
│   ├── codex.py       # Codex JSONL + SQLite parser
│   ├── agy.py         # AGY transcript + DB parser
│   └── aggregator.py  # Multi-tool aggregator
├── static/js/
│   ├── odometer.js    # RollingOdometer (zero-dependency)
│   └── dashboard.js   # UI controller
└── templates/
    └── index.html     # Dashboard layout
```
