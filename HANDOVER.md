# AI Tools Usage & Cost Visualizer — Handover Document

**Date:** September 8, 2026
**Status:** ✅ Complete, Verified, 100% Operational on Live Telemetry
**Branch:** `feat/ai-usage-webview`
**Location:** `/Users/zerodoxxx/Desktop/Self Projects/ai-usage-dashboard`
**Conda Env:** `ai-usage-dashboard` (Python 3.12.14)

---

## 1. How to Run Immediately

```bash
cd "/Users/zerodoxxx/Desktop/Self Projects/ai-usage-dashboard"
conda activate ai-usage-dashboard
python run.py --open
```

Dashboard: **http://127.0.0.1:8765**

Run tests:
```bash
python test_parsers.py  # All parser + pricing tests
python test_server.py   # All API endpoint tests
```

---

## 2. Verified Live Metrics (2026-09-08)

| Metric | All Tools | OpenAI Codex | Google AGY |
|:-------|:---------:|:------------:|:----------:|
| Sessions | **177** | 51 | 126 |
| Total Tokens | **155,786,115** | 151,722,460 | 4,063,655 |
| Cache Hit Rate | **94.13%** | 95.04% | 44.96% |
| Est. API Cost | **$31.07** | $30.38 | $0.69 |
| Uncached Cost | **$209.25** | $208.55 | $0.78 |
| Savings (caching) | **$178.17** | $178.08 | $0.09 |

---

## 3. File Structure

```
ai-usage-dashboard/
├── run.py                   # Entry: python run.py [--open] [--port 8765]
├── pyproject.toml           # fastapi, uvicorn[standard], pydantic
├── requirements.txt
├── test_parsers.py          # Parser + pricing tests (all pass)
├── test_server.py           # API endpoint tests (all pass)
├── HANDOVER.md              # This file
├── README.md                # Quick-start guide
└── src/
    ├── app.py               # FastAPI: GET /, /api/usage, /api/pricing, /api/health
    ├── pricing.py           # 14 model rates, get_pricing(), calculate_cost()
    ├── parsers/
    │   ├── codex.py         # Reads ~/.codex/sessions/**/rollout-*.jsonl + state_5.sqlite
    │   ├── agy.py           # Reads ~/.gemini/antigravity-cli/brain/**/transcript.jsonl + DBs
    │   └── aggregator.py   # get_tool_usage(tool="all"|"codex"|"agy")
    ├── static/
    │   ├── css/dashboard.css
    │   └── js/
    │       ├── odometer.js  # RollingOdometer — CSS 3D vertical reel digits
    │       └── dashboard.js # Charts, state, polling, AbortController
    └── templates/
        └── index.html       # Single-page dark-mode UI
```

---

## 4. Data Sources

**Codex:** `~/.codex/state_5.sqlite` (threads table) + `~/.codex/sessions/**/rollout-*.jsonl`
- Exact token fields: `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`
- File mtime+size cache prevents re-reads on polling

**AGY:** `~/.gemini/antigravity-cli/`
- `settings.json` → active model
- `conversation_summaries.db` + `conversations/*.db` → session metadata
- `brain/**/transcript.jsonl` → character-length token estimation (chars // 4)
- 45% cache hit rate applied for multi-turn sessions

---

## 5. API Endpoints

| Endpoint | Description |
|:---------|:------------|
| `GET /` | Dashboard HTML |
| `GET /api/usage?tool=all\|codex\|agy` | Live parsed metrics JSON |
| `GET /api/pricing` | All 14 model pricing rates |
| `GET /api/health` | Health check |

---

## 6. Pricing ($/1M tokens)

| Model | Uncached In | Cached In | Output |
|:------|:-----------:|:---------:|:------:|
| gpt-6-astra | $10.00 | $1.00 | $50.00 |
| gpt-5.6-luna | $0.20 | $0.02 | $1.20 |
| gpt-5.6-sol | $0.50 | $0.05 | $2.50 |
| gpt-5.6-terra | $0.80 | $0.08 | $4.00 |
| gpt-4o | $2.50 | $1.25 | $10.00 |
| gpt-4o-mini | $0.15 | $0.075 | $0.60 |
| o1 | $15.00 | $7.50 | $60.00 |
| o1-mini | $1.10 | $0.55 | $4.40 |
| o3-mini | $1.10 | $0.55 | $4.40 |
| Gemini 3.8 Flash (High) | $0.10 | $0.025 | $0.40 |
| Gemini 2.5 Flash | $0.30 | $0.075 | $2.50 |
| Gemini 2.5 Pro | $1.25 | $0.3125 | $10.00 |
| Gemini 1.5 Flash | $0.075 | $0.01875 | $0.30 |
| Gemini 1.5 Pro | $1.25 | $0.3125 | $5.00 |

---

## 7. What's Completed ✅

- [x] Conda env `ai-usage-dashboard` (Python 3.12.14), fastapi 0.141.1, uvicorn 0.52.4, pydantic 2.13.5
- [x] Git repo on `feat/ai-usage-webview`, 6 commits
- [x] Full data engine: pricing, codex parser (mtime cache), agy parser, aggregator (model dedup)
- [x] FastAPI server + static file serving
- [x] Zero-dep `RollingOdometer` (CSS 3D, staggered cubic-bezier animation)
- [x] Dark-mode SPA: 6 odometer cards, dual Chart.js charts, 12-col model table, session search
- [x] CLI runner with --open, --port, --host flags
- [x] o1-mini correct pricing (not aliased to o1)
- [x] XSS-safe toasts, AbortController, NoneType guards, query_only PRAGMA
- [x] All 11 tests pass (parsers + server)

---

## 8. What's Left 📋

### P1 — Push to GitHub
```bash
cd "/Users/zerodoxxx/Desktop/Self Projects/ai-usage-dashboard"
gh repo create ai-usage-dashboard --private --source=. --remote=origin --push
gh pr create --title "feat: AI Usage & Cost Visualizer" --body "Initial implementation" --base main
```

### P2 — Optional Enhancements
1. **Date range filter** — pill selector (Today / 7D / 30D / All) filtering `timeline` + `sessions` in `dashboard.js`
2. **Claude Cost Tracker integration** — add `src/parsers/claude.py` reading `~/.claude-cost-tracker/usage.db` (schema: session_id, model, input/output/cache tokens, estimated_cost_usd) + 3rd dropdown option
3. **Export button** — convert `state.allSessions` to CSV blob in `dashboard.js`

### P3 — Code Quality
- `dashboard.js` is 834 lines — extract `chart-helpers.js` and `table-renderer.js`
- Add support for multiple AGY model labels when user switches models

---

## 9. Git Log

```
64d04b6 fix(hardening): guard timeline, models, and cache key on error
3a45f04 feat: complete AI usage dashboard with rolling odometers, live multi-tool metrics, and hardened parsers
2c98089 fix(dashboard): harden edge cases, chart reuse, abort controller, and CLI validation
6de9c2d feat(dashboard): implement Phase 3 FastAPI server and Phase 4 complete dashboard UI
6ec5847 feat(parsers): implement Phase 2 core data engine and model pricing
02bc121 chore: initial repository and environment setup
```
