# AI tools usage and cost dashboard

A local dashboard for OpenAI Codex, Claude Code, and Google Antigravity (AGY). It reads normalized usage from one shared SQLite database, populated by hooks and backfills. No provider API keys are needed. Costs use a local pricing catalog refreshed from LiteLLM's public price list; AGY token counts are estimates.

## Features

- Token, cost, cache, and session totals with rolling odometers
- Model and session tables, session search, charts, and activity heatmaps
- Tool filters and all-time, month, rolling, and custom date ranges
- Analytics, period comparisons, and cost projections
- Auto-refresh at 10, 30, or 60 seconds
- Token and cost provenance, capture-status chips, and database-health messages

## Setup

Run these commands from the repository root. Use Python 3.12 or newer. Hook installers pin the interpreter and database path, so keep the environment available and re-run installers after changing either path or the writer code. Set `AI_USAGE_DB_PATH` before installation to override `~/.local/share/ai-usage/usage.db`.

1. Create and activate the environment. Installing `tiktoken` enables AGY's tokenizer; without it the writer falls back to a regex estimate.

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt tiktoken
   ```

2. Install the Codex hooks:

   ```bash
   python scripts/install_codex_usage_hooks.py
   ```

   In Codex CLI, open `/hooks` and approve the exact installed definitions **after every install**. Start a fresh Codex session or reopen Codex Desktop before relying on capture. The synchronous `Stop` and `SubagentStop` writers have a 25-second deadline; `Interrupt` has 2.5 seconds.

3. Install the asynchronous Claude Code `Stop`, `SubagentStop`, and `SessionEnd` hooks:

   ```bash
   python scripts/install_claude_usage_hooks.py
   ```

4. Install AGY's `PostInvocation` and `Stop` hooks. This replaces the `agy-token-tracker` entry that invoked the external `~/.gemini/.../track_usage.py`, saving a timestamped configuration backup.

   ```bash
   python scripts/install_agy_usage_hooks.py
   ```

5. Backfill each tool with local history:

   ```bash
   python scripts/codex_usage_writer.py --backfill
   python scripts/claude_usage_writer.py --backfill
   python scripts/agy_usage_writer.py --backfill
   ```

   Codex backfill is a one-time import: it does nothing if Codex capture is already enabled in that database. Claude and AGY backfills can be repeated to refresh snapshots. These scripts import only history that is still present locally.

6. Optionally migrate `~/.gemini/antigravity-cli/token_usage.db`. Migration copies a database; it does **not merge** history. If step 5 created the target, replacement requires `--force`, which saves a backup but replaces the history just backfilled. Close capture tools and wait for writers to finish. Use this only when the legacy snapshot should become the active database:

   ```bash
   python scripts/migrate_usage_db.py --force --dry-run
   python scripts/migrate_usage_db.py --force
   ```

   For an unused target, omit `--force`. The source is archived after a verified copy unless `--keep-source` is used. Backfill remaining local history as needed; Codex still skips if the copied store marks capture enabled. See [migration options](docs/SHARED_USAGE_DB.md#migration) for a separate inspection target.

7. Check capture configuration and database health:

   ```bash
   python scripts/doctor.py
   ```

   Review failures and warnings, including stale captures or deployed writers that differ from this checkout. Doctor cannot approve Codex hooks; use `/hooks` for that.

8. Optionally enable 15-day Codex retention on macOS. **This deletes transcripts and removes the ability to resume those conversations.** Usage rows remain in SQLite. Deletion requires Codex to be closed, independently verified capture, and a fresh verified backup. Preview first:

   ```bash
   python scripts/codex_retention.py
   python scripts/install_codex_retention.py
   launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.zerodoxxx.ai-usage-dashboard.codex-retention.plist"
   ```

   The installer writes the job; `bootstrap` enables it. Disable it with:

   ```bash
   launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.zerodoxxx.ai-usage-dashboard.codex-retention.plist"
   ```

   The supported Codex version is pinned to `0.159.2`. After Codex updates, deletion stops until compatibility is reviewed and the job is reinstalled and reloaded. See [retention](docs/CODEX_RETENTION.md).

9. Start the dashboard:

   ```bash
   python run.py --open
   ```

   Open http://127.0.0.1:8765. Calendar-month and custom-date filters use the machine's local timezone. To override it:

   ```bash
   AI_USAGE_TIMEZONE=America/New_York python run.py --open
   ```

## Data sources

| Tool | Database provider | Writer inputs | Capture |
|:-----|:------------------|:--------------|:--------|
| Codex | `codex` | Codex rollout JSONL and thread metadata | Synchronous hooks and one-time backfill; reported usage |
| Claude Code | `claude-code` | `~/.claude/projects/**/*.jsonl`, including subagents | Asynchronous hooks and repeatable backfill; reported usage |
| AGY | `antigravity` | `~/.gemini/antigravity-cli/brain/**/transcript.jsonl`, summaries, and settings | AGY hooks and repeatable backfill; estimated usage |

The dashboard reads only `~/.local/share/ai-usage/usage.db` (or `AI_USAGE_DB_PATH`). It never scans provider transcripts at request time. Codex and Claude session IDs are prefixed with their provider; AGY IDs remain raw for compatibility. A provider with no rows shows as empty. A missing or unreadable database produces a health banner. See [the shared database guide](docs/SHARED_USAGE_DB.md) for schema, backup, restore, and migration.

## Run tests

```bash
python -m pytest
```

## API

| Endpoint | Description |
|:---------|:------------|
| `GET /api/usage?tool=all&time_range=all` | Aggregated usage, analytics, pricing, and a `store` health block |
| `GET /api/usage?tool=codex&time_range=30d` | Codex usage for the past 30 days |
| `GET /api/usage?tool=agy&time_range=month` | AGY usage for the current calendar month |
| `GET /api/usage?tool=claude-code&time_range=7d` | Claude Code usage for the past 7 days |
| `GET /api/pricing` | Model rates and `__meta__` source/freshness |
| `GET /api/health` | Server health check |

## Project structure

```text
run.py                         # Server entry point
scripts/
├── codex_usage_writer.py       # Codex capture and one-time backfill
├── install_codex_usage_hooks.py
├── claude_usage_writer.py      # Claude capture and backfill
├── install_claude_usage_hooks.py
├── agy_usage_writer.py         # AGY estimates and backfill
├── install_agy_usage_hooks.py
├── migrate_usage_db.py         # Legacy database migration
├── backup_usage_db.py          # Verified, rotated SQLite backups
├── doctor.py                   # Read-only health checks
├── codex_retention.py          # Retention preview and deletion
└── install_codex_retention.py  # macOS LaunchAgent installer
src/
├── app.py                     # FastAPI endpoints and response cache
├── usage_store.py             # Shared SQLite schema, reads, and writes
├── pricing.py                 # Pricing catalog and memoized resolution
├── litellm_pricing.py          # Pricing feed parser
├── timezones.py               # Local-calendar boundaries
├── parsers/
│   ├── store_source.py        # Dashboard's database-only adapters
│   ├── codex.py               # Codex writer/backfill parser
│   ├── claude.py              # Claude writer/backfill parser
│   ├── agy.py                 # Legacy AGY parser; current writer parses separately
│   ├── file_cache.py          # Transcript parser cache
│   ├── contracts.py           # Normalized usage contracts
│   ├── source_registry.py     # Provider registry
│   └── aggregator.py          # Session cache, time filtering, costs, and analytics
├── static/
│   ├── css/                   # Dashboard and chart styles
│   └── js/                    # Dashboard, charts, tables, and display helpers
└── templates/index.html       # Dashboard layout
docs/
├── SHARED_USAGE_DB.md
└── CODEX_RETENTION.md
HOW_IT_WORKS.md                 # Read path, estimates, pricing, and caches
test_*.py                      # Pytest suites
```
