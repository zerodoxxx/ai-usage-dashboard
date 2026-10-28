# How it works

## Read path

The dashboard reads only the shared SQLite database at `~/.local/share/ai-usage/usage.db`, overridden by `AI_USAGE_DB_PATH`. Hooks and backfills parse local telemetry and write normalized snapshots; requests never scan provider transcript directories.

```text
Codex rollout JSONL + metadata → codex_usage_writer.py ──┐
Claude session JSONL           → claude_usage_writer.py ├→ usage.db
AGY conversation DBs + logs    → agy_usage_writer.py ────┘
                                                            ↓
                                                  usage_store.py
                                                            ↓
                                                  store_source.py
                                                            ↓
                                                  aggregator.py
                                                            ↓
                                                  app.py → browser
```

`StoreUsageSource` adapters in `src/parsers/store_source.py` read sessions and events through `src/usage_store.py`. Each provider read uses a read-only SQLite transaction so its session totals and events share a snapshot. The aggregator reads selected providers sequentially, merges sessions, resolves prices, filters events by time, and builds summaries, models, timelines, sessions, and analytics.

Provider keys are `codex`, `claude-code`, and `antigravity`. Codex and Claude database IDs are namespaced; AGY retains native IDs. Reads return native session IDs. The database stores counters and selected metadata, not transcript bodies. See [the shared database guide](docs/SHARED_USAGE_DB.md).

## Capture

### Codex

`scripts/codex_usage_writer.py` imports the Codex parser to read the transcript named by a hook payload and matching thread metadata. For `SubagentStop`, it uses the agent's ID and transcript rather than the parent's session ID. The installer configures synchronous `Stop`, `SubagentStop`, and `Interrupt` hooks with `--deadline-seconds`: 25 seconds for stop events and 2.5 seconds for interrupts.

Approve the exact definitions through `/hooks` in Codex after every installation. Start a fresh session or reopen Desktop to use them. A deadline expiry can leave capture incomplete; a later stop can capture the updated transcript. Hook failures are nonfatal and diagnostics go to stderr.

```bash
python scripts/codex_usage_writer.py --backfill
python scripts/codex_usage_writer.py --status
```

Backfill imports remaining local history once and records capture enabled after a successful write. Once enabled, repeating it is a no-op. Capture flags do not gate dashboard reads: existing database rows are visible even before backfill.

### Claude Code

`scripts/claude_usage_writer.py` parses assistant usage records from `~/.claude/projects/**/*.jsonl`, including subagent transcripts. Models come from the assistant records. The parser retains base input, cache reads, cache writes split by lifetime, output, and optional reasoning tokens.

The installer adds asynchronous `Stop`, `SubagentStop`, and `SessionEnd` hooks to `~/.claude/settings.json`. Subagent stops capture the child separately; session end also sweeps subagent transcripts. The writer deduplicates copied responses across sessions and rejects older capture revisions that would overwrite newer snapshots.

```bash
python scripts/claude_usage_writer.py --backfill
python scripts/claude_usage_writer.py --status
```

Backfill can be repeated. Both Codex and Claude counts come from recorded usage; calculated dollar amounts remain cost estimates unless a reported charge is available.

### Antigravity (AGY)

`scripts/agy_usage_writer.py` reads sessions under `~/.gemini/antigravity-cli`. It takes titles and workspace metadata from `conversation_summaries.db`. Token counts come from the best source available for each session, in priority order:

1. **Conversation database.** Real per-call usage that agy records in `conversations/<conversationId>.db` (table `gen_metadata`, protobuf). Google does not document this schema; the field layout was reverse engineered and verified locally, and it may change between agy releases. Each call gives uncached input, cache-read tokens, output tokens (with the thinking subset), and the model that ran. Snapshots carry `estimated: false`, `token_source: "reported"`, and `token_origin: "agy-conversation-db"`. If more than 5% of a conversation database's non-empty rows fail to decode (a sign the undocumented schema changed), the database is treated as unrecognised and the session falls to the next tier instead of storing partial numbers as recorded. agy's databases are opened read-only and never modified; reading through a live write-ahead log may update agy's own shared-memory index, which holds no durable data.
2. **Transcript JSON.** Plain JSON keys `input_tokens` (uncached input only; cached reads are the separate `cache_read_tokens`), `cache_read_tokens`, and `output_tokens` on each `PLANNER_RESPONSE` line of `brain/<id>/.system_generated/logs/transcript.jsonl`. Each line holds that call's own counts, not running totals. They exist only for sessions started on or after 2026-10-03 and are used only when every such line has them (all or nothing). Reasoning tokens are unknown and stored as 0. Snapshots carry `estimated: false`, `token_source: "reported"`, and `token_origin: "agy-transcript-json"`.
3. **Estimate.** Used only when neither real source exists. The writer uses `tiktoken`'s `cl100k_base` encoding when available, otherwise a regex tokenizer with a character-count fallback. It accumulates context from user/system steps and creates an event for each `PLANNER_RESPONSE` or `MODEL` step; output includes content, serialized tool calls, and thinking. The first response estimates zero cached input and later responses treat a flat 45% of accumulated context as cached, so single-turn sessions get 0% cached. This is a writer assumption, not measured cache usage. Snapshots carry `estimated: true` and no `token_origin`.

No cache-write field has been observed in agy's recorded usage, so AGY cache writes are recorded as 0 and new snapshots use zero additive cache writes.

**Calls and models.** With a conversation database, the call count is the number of real model calls: one per `gen_metadata` row with non-zero usage. Events carry the per-call model id as recorded (for example `gemini-3.8-flash`), and a session that used several models is labelled `mixed`. Routing variants such as `gemini-3.7-flash-tiered` are priced through explicit aliases in `src/litellm_pricing.py`. The aliases assume a variant has the same per-token rates as its base model. That is an inference: Google does not publish them, and a "tiered" variant may in reality be priced differently. Unknown ids stay unpriced. For the other two tiers the model comes from AGY's `settings.json`, with a fallback when absent. That is the model setting at capture time, not a per-response model report. The estimator still counts every model-source transcript line as a call, so its call counts run high.

**Store guard.** For AGY the store ranks snapshots by `token_origin`: `agy-conversation-db` above `agy-transcript-json` above none. A lower-ranked snapshot never replaces a higher-ranked one, and a snapshot with fewer calls, or the same number of calls with fewer total tokens, never replaces a real one of the same rank, because agy may prune old conversation databases. Replays are idempotent.

**Recovery.** Because the guard never lets a recorded snapshot be replaced by a smaller one, a decoder bug or an agy rewrite that leaves a session overcounted cannot be fixed by re-running the backfill alone. Either restore the store from a `scripts/backup_usage_db.py` backup, or delete that session's rows and re-run `--backfill`. For the second option, take a backup first and close agy, the dashboard, and any running writer. The schema has no foreign keys and nothing cascades, so delete the `token_events` rows and the `sessions` row explicitly. AGY session ids are the raw conversation id. Using the `sqlite3` shell on `~/.local/share/ai-usage/usage.db` (or `AI_USAGE_DB_PATH`):

```sql
BEGIN IMMEDIATE;
DELETE FROM token_events WHERE provider = 'antigravity' AND session_id = '<conversationId>';
DELETE FROM sessions WHERE provider = 'antigravity' AND session_id = '<conversationId>';
COMMIT;
```

Then run `python scripts/agy_usage_writer.py --backfill`, which re-imports the session from whatever sources still exist locally. If agy has already pruned its conversation database, the session is re-imported from the next available tier.

The installer replaces the external `agy-token-tracker` entry in `~/.gemini/config/hooks.json` with `PostInvocation` and `Stop` commands pointing to a frozen, content-addressed local release of the writer. After code changes, re-run `scripts/install_agy_usage_hooks.py` to deploy a new release.

```bash
python scripts/agy_usage_writer.py --backfill
python scripts/agy_usage_writer.py --status
```

`--backfill` re-imports every conversation, conversation databases first and then transcript-only sessions, and is idempotent. To upgrade:

1. Take a backup with `python scripts/backup_usage_db.py`.
2. Install the new hook with `python scripts/install_agy_usage_hooks.py`.
3. Run `python scripts/agy_usage_writer.py --backfill`.
4. Run the backfill once more after any agy session that was open during the upgrade has ended. Until agy reloads its hook configuration, an older frozen writer in that session would otherwise overwrite its real snapshot with an estimate.

## Refresh performance

Three caches reduce repeated work:

- `src/app.py` caches serialized usage responses. Keys include the database and WAL signatures (path, modification time, size), tool/range parameters, timezone, pricing catalog revision and metadata, and the current minute.
- `src/parsers/aggregator.py` caches priced sessions per database/WAL signature, selected sources, pricing revision, and timezone. A new time range can reuse these sessions while rebuilding its aggregates.
- `src/pricing.py` memoizes model/provider resolution and time-dependent rate selection. Catalog changes invalidate the derived pricing caches.

Responses are cached only for an existing, readable database, and not when its signature changes during the request. Minute-based response keys allow moving windows and analytics to advance even without database writes. Browser responses still use `Cache-Control: no-store`; these caches are in the server process.

Measured on the local history used for this branch: all-time cold response about 1.8 seconds, warm response about 1 millisecond, and 24-hour cold response about 0.32 seconds. These are observations for that dataset, not latency guarantees. Transcript parser caches in `src/parsers/file_cache.py` are separate and used by writers/backfills.

## Capture health

`/api/usage` includes a `store` block with `path`, `database_exists`, `readable`, `error`, and provider summaries containing `last_write_at` and `sessions`. The frontend shows chips for providers with stored sessions. A last write older than 24 hours is marked stale; a missing timestamp has a capture-time-unavailable tooltip. This measures writes to SQLite, not hook approval or tool activity.

A missing or unreadable database produces a banner with setup or doctor guidance. A provider with no rows otherwise shows as empty. `scripts/doctor.py` checks the database, hook configuration, deployed publishers, retention job, and backups; it cannot approve Codex hooks.

## Time windows

Writers replace full session snapshots rather than adding cumulative totals as calls. Event timestamps let the aggregator count calls inside `month`, `30d`, `7d`, `24h`, and custom windows even when the conversation started earlier. AGY events contain per-call usage (recorded by agy, or the writer's per-response estimates for sessions with no recorded counts); they are not apportioned from one session total by character weights. Records without events fall back to session timestamps.

Model totals use each event's model. Sessions using multiple models can show `mixed`. User-only AGY transcripts produce no model-response events or tokens. Preset rolling ranges use instants; calendar-month and custom boundaries use the local timezone, including daylight saving. Set `AI_USAGE_TIMEZONE` to override it.

## Costs

`src/pricing.py` expresses USD rates per million tokens for uncached input, cached input, output, and optional cache writes. It refreshes from LiteLLM's public price list with a 24-hour TTL, retaining a last-known-good snapshot and bundled fallback rates. Unknown models remain unpriced. Local records do not identify every billing tier, so estimated costs use standard short-context rates where applicable.

```text
cost with caching = (uncached input × input rate
                   + cached input × cache-read rate
                   + output × output rate
                   + cache writes × applicable write rate) / 1,000,000

cost without caching = ((uncached input + cached input + cache writes) × input rate
                      + output × output rate) / 1,000,000

net savings = cost without caching − cost with caching
```

Reasoning tokens are a subset of output. Claude 5-minute and 1-hour cache writes are retained separately and priced at 1.25× and 2× input rates; writes can make net savings negative.

The cache hit rate is `cache read / (uncached input + cache read + cache write)`, as a percentage. Providers without cache writes (Codex, AGY) reduce to cache read / (uncached input + cache read). Claude previously showed about 100% because cache writes were left out of the denominator; with them included, the overall figure for the author's data is about 96%. The SQL views `provider_daily_summary` and `provider_model_summary` (and the legacy AGY-only `daily_summary` and `model_summary`) use the same definition, counting `cache_write_tokens` only for rows whose `cache_write_mode` is `additive`. Writers refresh the view definitions when they have drifted, without a `PRAGMA user_version` change.

Legacy AGY `embedded_in_input` cache-write counts are diagnostic and are zeroed on read to avoid counting them twice. New AGY snapshots have zero additive writes because no cache-write field has been observed in agy's recorded usage. Token provenance (`estimated`/`token_source`) is separate from cost provenance (`cost_source`/`pricing_status`).

Events written by the AGY hook (and backfill) carry only the cost known at write time, so model ids whose rates exist only in the LiteLLM index are stored as unpriced with zero cost. The dashboard re-prices from the raw model id every time it reads, so its numbers are right. Raw SQL consumers and the SQL summary views (`provider_daily_summary`, `provider_model_summary`) see the write-time cost fields and understate AGY cost.

## Frontend

`dashboard.js` requests `/api/usage` on load, filter changes, and auto-refresh. The response contains usage, analytics, pricing with freshness metadata, and store health. Odometers, charts, tables, and capture chips use the same selected window. Session search is client-side. New requests cancel in-flight requests through an `AbortController`.

See [setup and the file tree](README.md) for entry points, and [retention](docs/CODEX_RETENTION.md) for optional transcript deletion.
