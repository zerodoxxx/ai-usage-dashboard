# Shared usage database

The dashboard's only usage source is `~/.local/share/ai-usage/usage.db`. Set `AI_USAGE_DB_PATH` to override it; writer scripts also accept `--db`. Writers create directories and ensure the schema. Dashboard reads never create or migrate the database and never fall back to provider transcripts or the legacy location.

## Schema

`src/usage_store.py` maintains schema version 1 in `PRAGMA user_version`. This schema version is separate from the per-row token semantics version.

| Table or view | Purpose |
|:--------------|:--------|
| `sessions` | One snapshot per `session_id`: provider, model, title, timestamps, token totals, call counts, cost fields, and allowed metadata |
| `token_events` | Per-call usage with model, timestamp, event identity, cost, and metadata; unique on `(session_id, step_index)` |
| `usage_capture_state` | Provider backfill state: `enabled`, `backfill_completed_at`, and `updated_at` |
| `provider_daily_summary`, `provider_model_summary` | Summaries grouped by provider and day/model |
| `daily_summary`, `model_summary` | Legacy Antigravity-only views |

Canonical providers are `codex`, `claude-code`, and `antigravity`. Codex IDs are stored as `codex:<native-id>`; Claude IDs as `claude-code:<native-id>`. **AGY session IDs remain raw**, preserving compatibility with the external tracker. Reads return native IDs. Old rows without a provider column are treated only as Antigravity data; schema migration adds defaults without changing counters.

Both usage tables retain input, cached input, output, reasoning output, cache writes (including 5-minute and 1-hour splits), and total tokens. Session activity fields support filtering; events preserve missing timestamps through `timestamp_missing`. `metadata_json` uses an allowlist for token provenance, capture revisions, response identities, and safe TPS facts. Transcript bodies, prompts, responses, and tool-output text are not stored.

## Token and cost semantics

New snapshots written through the shared API, including the current AGY writer, use `usage_semantics_version=2` and `cache_write_mode='additive'`:

- `input_tokens` includes uncached input and cached reads.
- `cached_input_tokens` is a subset of input.
- `cache_write_tokens` is separate input, added once; lifetime fields split that count.
- `output_tokens` includes reasoning output; reasoning is a subset.
- `total_tokens` is preserved from the normalized source snapshot.

Legacy AGY rows use semantics version 1 and `cache_write_mode='embedded_in_input'`. Their cache-write estimate is already included in input and total. Readers preserve those totals but zero the cache-write fields to prevent double counting. The current AGY writer emits additive snapshots with zero cache writes.

The **estimated flag is metadata**, not a standalone table column: `metadata_json.estimated` and `token_source` distinguish token provenance. Current AGY snapshots set `estimated: true` and `token_source: "estimated"`; legacy AGY reads default to estimated. Codex and Claude default to reported token counts. Being stored in SQLite does not make estimates reported.

Cost provenance is separate. `cost_usd` retains the compatible payable/estimated amount; `cost_source`, `reported_cost_usd`, `cost_cached_estimate_usd`, `cost_uncached_usd`, `savings_usd`, and `cost_currency` retain the distinctions. A calculated cost remains estimated even when its token counts are reported. Unknown rates remain unpriced.

## Writer contract

```python
from src.usage_store import read_usage_sessions, write_usage_sessions

write_usage_sessions("claude-code", [usage_session])
sessions = read_usage_sessions("claude-code")
```

Writes replace full `UsageSession` snapshots and their event lists atomically; they are not token deltas. Replaying a snapshot cannot add the same session twice, and a shorter corrected snapshot removes old tail events. Stable event IDs support deduplication. Codex/Claude capture revisions prevent an older parse from replacing a newer snapshot; Claude additionally claims response ownership to avoid counting copied history twice.

Capture state records backfill completion, not hook configuration or approval. Dashboard reads do not require `enabled=1`.

## Writers and hooks

All three installers deploy frozen, content-addressed releases and pin the interpreter and database path. Reinstall after code or path changes; branch switches do not change a deployed release.

| Tool | Writer | Hook events | Configuration |
|:-----|:-------|:------------|:--------------|
| Codex | `scripts/codex_usage_writer.py` | `Stop`, `SubagentStop`, `Interrupt`; synchronous | `~/.codex/hooks.json` |
| Claude Code | `scripts/claude_usage_writer.py` | `Stop`, `SubagentStop`, `SessionEnd`; asynchronous | `~/.claude/settings.json` |
| AGY | `scripts/agy_usage_writer.py` | `PostInvocation`, `Stop` | `~/.gemini/config/hooks.json` |

```bash
python scripts/install_codex_usage_hooks.py
python scripts/install_claude_usage_hooks.py
python scripts/install_agy_usage_hooks.py
```

**Approve the exact Codex definitions through `/hooks` after every install.** Start a fresh Codex session or reopen Desktop before relying on capture. Installers save timestamped backups of changed configuration. AGY's installer replaces the external `agy-token-tracker` entry; other named entries are preserved.

Codex parses only the hook transcript and matching thread metadata. `SubagentStop` uses `agent_id`/`agent_transcript_path`. Hook stdout is `{}`; diagnostics go to stderr and failures are nonfatal. Claude parses the named transcript, captures subagents separately, and sweeps the subagent directory at session end; it emits no hook stdout and exits 0 on hook errors.

AGY estimates tokens with `tiktoken` when available, otherwise a regex/character fallback. It estimates cache reads at zero for the first response and 45% of accumulated context thereafter, with zero additive cache writes. Its hook emits `{}` and nonfatal diagnostics. These are estimates, not provider token reports.

```bash
python scripts/codex_usage_writer.py --backfill
python scripts/claude_usage_writer.py --backfill
python scripts/agy_usage_writer.py --backfill
python scripts/codex_usage_writer.py --status
python scripts/claude_usage_writer.py --status
python scripts/agy_usage_writer.py --status
```

Codex backfill imports history once and becomes a no-op once capture is enabled. Claude and AGY backfills can be repeated. Claude's `--no-enable-capture` suppresses its backfill flag. Status commands report database counts/state, not hook approval. Use `--codex-dir`, `--claude-dir`, or `--agy-dir` on the corresponding writer for alternate history roots.

## Concurrency

Schema setup enables SQLite WAL mode. Writes use `BEGIN IMMEDIATE` and atomic snapshot replacement; per-provider reads use a single read-only transaction. Connections use a 2,000 ms busy timeout. Locked/busy writes retry up to three attempts with short exponential delays.

Codex's `--deadline-seconds` bounds parsing and database waits/retries together. Its installed stop hooks use 25 seconds, and interrupt uses 2.5 seconds. SQLite waits shrink to the remaining deadline; expiry rolls back instead of committing a partial snapshot. A later successful hook can capture newer usage.

## Backup and restore

Create a verified backup:

```bash
python scripts/backup_usage_db.py
```

The SQLite backup API includes committed WAL data. The script checks integrity plus per-provider session/event counts and both token sums against one source snapshot, then publishes `<db dir>/backups/usage-<UTC timestamp>.db`. It retains the newest 14 backups by default. `--db`, `--dest-dir`, `--keep`, and `--json` are supported. Retention requires a new successful backup before deletion; installing hooks does not schedule regular backups.

To restore, close Codex, Claude Code, AGY, and the dashboard, and wait for hook/backfill processes to finish. Disable an installed retention job:

```bash
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.zerodoxxx.ai-usage-dashboard.codex-retention.plist"
```

With **all database users stopped**, the following restores the newest backup from the resolved database's `backups` directory. It checks the backup first and moves the replaced database and any old WAL/SHM files into a separate recovery directory so they cannot be replayed onto the restored file:

```bash
python - <<'PY'
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sqlite3
from src.usage_store import resolve_db_path

db = resolve_db_path().expanduser().resolve()
backups = sorted((db.parent / "backups").glob("usage-*.db"))
if not backups:
    raise SystemExit("No backups found")
backup = backups[-1]
with sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True) as conn:
    if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise SystemExit("Backup failed integrity check")
saved = db.parent / datetime.now(timezone.utc).strftime("pre-restore-%Y%m%dT%H%M%S.%fZ")
saved.mkdir()
for suffix in ("", "-wal", "-shm"):
    old = Path(str(db) + suffix)
    if old.exists():
        shutil.move(str(old), str(saved / old.name))
shutil.copy2(backup, db)
print(f"Restored {backup}; previous files saved in {saved}")
PY
python scripts/doctor.py
```

For an older snapshot, change the `backup` selection in the snippet to the desired file. Once doctor reports a readable store, restart capture tools and the dashboard. Restoring changes usage history to the backup's contents; it does not restore deleted Codex transcripts. Re-enable retention with the bootstrap command in [the retention guide](CODEX_RETENTION.md) if desired.

Doctor is read-only. It exits 0 for OK, 1 for failures, and 2 for warnings. Its immutable SQLite check describes the checkpointed database and warns when live WAL data is present.

## Migration

The legacy source is `~/.gemini/antigravity-cli/token_usage.db`. Stop writers and database users before migrating or replacing a target, as for restore.

```bash
python scripts/migrate_usage_db.py --dry-run
python scripts/migrate_usage_db.py
```

Migration uses SQLite backup to include WAL data, verifies per-provider session/event counts and session token sums plus integrity, then publishes the copy. It does not merge databases or run schema migration. The source is archived as `token_usage.db.migrated-YYYY-MM-DD` with sidecars unless `--keep-source` is used.

An existing target is refused, including during `--dry-run`. To preview and intentionally replace it:

```bash
python scripts/migrate_usage_db.py --force --dry-run
python scripts/migrate_usage_db.py --force
```

`--force` first backs up the target to a timestamped `.bak` beside it, then replaces it. This removes target-only rows from the active database. If you already backfilled all tools, do not treat migration as an additive import. `--source` and `--target` can instead select a separate destination for inspection.

After selecting the active database path, re-run all three hook installers, approve Codex definitions with `/hooks`, and run doctor. Backfill any remaining local history into the migrated store as needed; Codex still skips backfill if the copied store already marks capture enabled.
