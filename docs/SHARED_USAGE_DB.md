# Shared usage database

The dashboard's only usage source is `~/.local/share/ai-usage/usage.db`. Set `AI_USAGE_DB_PATH` to override it; writer scripts also accept `--db`. Writers create directories and ensure the schema. They also refresh the summary view definitions when those have drifted, without changing `PRAGMA user_version`. Dashboard reads never create or migrate the database and never fall back to provider transcripts or the legacy location.

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

Legacy AGY rows use semantics version 1 and `cache_write_mode='embedded_in_input'`. Their cache-write estimate is already included in input and total. Readers preserve those totals but zero the cache-write fields to prevent double counting. The current AGY writer emits additive snapshots with zero cache writes: no cache-write field has been observed in agy's recorded usage, so AGY cache writes are recorded as 0.

The **cache hit rate** is `cache read / (uncached input + cache read + cache write)`, as a percentage. Providers without cache writes (Codex, AGY) reduce to cache read / (uncached input + cache read). Claude previously showed about 100% because cache writes were left out of the denominator. The SQL views `provider_daily_summary` and `provider_model_summary`, and the legacy AGY-only `daily_summary` and `model_summary`, use the same definition, counting `cache_write_tokens` only for rows whose `cache_write_mode` is `additive`.

The **estimated flag is metadata**, not a standalone table column: `metadata_json.estimated` and `token_source` distinguish token provenance. Current AGY snapshots take their counts from one of three sources per session (see [Writers and hooks](#writers-and-hooks)). Snapshots built from usage that agy recorded set `estimated: false` and `token_source: "reported"`, with `token_origin` naming the source (`agy-conversation-db` or `agy-transcript-json`). Only the transcript-text fallback sets `estimated: true` and has no `token_origin`; legacy AGY reads default to estimated. Codex and Claude default to reported token counts. Being stored in SQLite does not make estimates reported.

Cost provenance is separate. `cost_usd` retains the compatible payable/estimated amount; `cost_source`, `reported_cost_usd`, `cost_cached_estimate_usd`, `cost_uncached_usd`, `savings_usd`, and `cost_currency` retain the distinctions. A calculated cost remains estimated even when its token counts are reported. Unknown rates remain unpriced.

Events written by the AGY hook (and backfill) carry only the cost known at write time, so model ids whose rates exist only in the LiteLLM index are stored as unpriced with zero cost. The dashboard re-prices from the raw model id every time it reads, so its numbers are right. Raw SQL consumers and the SQL summary views (`provider_daily_summary`, `provider_model_summary`) see the write-time cost fields and understate AGY cost.

## Writer contract

```python
from src.usage_store import read_usage_sessions, write_usage_sessions

write_usage_sessions("claude-code", [usage_session])
sessions = read_usage_sessions("claude-code")
```

Writes replace full `UsageSession` snapshots and their event lists atomically; they are not token deltas. Replaying a snapshot cannot add the same session twice, and a shorter corrected snapshot removes old tail events. Stable event IDs support deduplication. Codex/Claude capture revisions prevent an older parse from replacing a newer snapshot; Claude additionally claims response ownership to avoid counting copied history twice.

For AGY the store ranks snapshots by `token_origin`: `agy-conversation-db` above `agy-transcript-json` above none. A lower-ranked snapshot never replaces a higher-ranked one, and a snapshot with fewer calls, or the same number of calls with fewer total tokens, never replaces a real one of the same rank, because agy may prune old conversation databases. Replays are idempotent. To correct an overcounted session, restore a backup or delete its rows and re-run the AGY backfill; see the recovery steps and SQL in [How it works](../HOW_IT_WORKS.md#antigravity-agy).

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

AGY takes token counts from the best source available for each session, in priority order:

1. **Conversation database.** Real per-call usage that agy records in `~/.gemini/antigravity-cli/conversations/<conversationId>.db` (table `gen_metadata`, protobuf). Google does not document this schema; the field layout was reverse engineered and verified locally, and it may change between agy releases. Each call gives uncached input, cache-read tokens, output tokens (with the thinking subset), and the model that ran. Snapshots carry `estimated: false`, `token_source: "reported"`, and `token_origin: "agy-conversation-db"`. If more than 5% of a conversation database's non-empty rows fail to decode (a sign the undocumented schema changed), the database is treated as unrecognised and the session falls to the next tier instead of storing partial numbers as recorded. agy's databases are opened read-only and never modified; reading through a live write-ahead log may update agy's own shared-memory index, which holds no durable data. The call count is the number of real model calls, one per `gen_metadata` row with non-zero usage. Events carry the per-call model id as recorded (for example `gemini-3.8-flash`), and a session that used several models is labelled `mixed`. Routing variants such as `gemini-3.7-flash-tiered` are priced through explicit aliases in `src/litellm_pricing.py`. The aliases assume a variant has the same per-token rates as its base model. That is an inference: Google does not publish them, and a "tiered" variant may in reality be priced differently. Unknown ids stay unpriced.
2. **Transcript JSON.** Plain JSON keys `input_tokens` (uncached input only; cached reads are the separate `cache_read_tokens`), `cache_read_tokens`, and `output_tokens` on each `PLANNER_RESPONSE` line of `brain/<id>/.system_generated/logs/transcript.jsonl`. Each line holds that call's own counts, not running totals. They exist only for sessions started on or after 2026-10-03 and are used only when every such line has them (all or nothing). Reasoning tokens are unknown and stored as 0. Snapshots carry `estimated: false`, `token_source: "reported"`, and `token_origin: "agy-transcript-json"`.
3. **Estimate.** Used only when neither real source exists. The writer estimates tokens from transcript text with `tiktoken` when available, otherwise a regex/character fallback. It treats a flat 45% of accumulated context as cached after the first call, and single-turn sessions get 0% cached. It still counts every model-source transcript line as a call, so its call counts run high. Snapshots carry `estimated: true` and no `token_origin`.

All three tiers use zero additive cache writes. The AGY hook emits `{}` and nonfatal diagnostics.

```bash
python scripts/codex_usage_writer.py --backfill
python scripts/claude_usage_writer.py --backfill
python scripts/agy_usage_writer.py --backfill
python scripts/codex_usage_writer.py --status
python scripts/claude_usage_writer.py --status
python scripts/agy_usage_writer.py --status
```

Codex backfill imports history once and becomes a no-op once capture is enabled. Claude and AGY backfills can be repeated. The AGY backfill re-imports every conversation, conversation databases first and then transcript-only sessions, and is idempotent. Take a backup with `scripts/backup_usage_db.py` before running it. Claude's `--no-enable-capture` suppresses its backfill flag. Status commands report database counts/state, not hook approval. Use `--codex-dir`, `--claude-dir`, or `--agy-dir` on the corresponding writer for alternate history roots.

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
