# How it works

## Read path

The dashboard reads only the shared SQLite database at `~/.local/share/ai-usage/usage.db`, overridden by `AI_USAGE_DB_PATH`. Hooks and backfills parse local telemetry and write normalized snapshots; requests never scan provider transcript directories.

```text
Codex rollout JSONL + metadata → codex_usage_writer.py ──┐
Claude session JSONL           → claude_usage_writer.py ├→ usage.db
AGY transcripts + summaries    → agy_usage_writer.py ────┘
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

`scripts/agy_usage_writer.py` parses `brain/**/transcript.jsonl` under `~/.gemini/antigravity-cli`. It reads titles and workspace metadata from `conversation_summaries.db` and assigns the model from AGY's `settings.json`, with a fallback when absent. This is the model setting at capture time, not a per-response model report.

Token counts are **estimates**, including rows stored in `usage.db`. The writer uses `tiktoken`'s `cl100k_base` encoding when available, otherwise a regex tokenizer with a character-count fallback. It accumulates context from user/system steps and creates an event for each `PLANNER_RESPONSE` or `MODEL` step. Output includes content, serialized tool calls, and thinking.

The first response estimates zero cached input; later responses estimate 45% of accumulated context as cached input. This is a writer assumption, not measured cache usage. New snapshots use zero additive cache writes and carry `estimated: true` and `token_source: "estimated"`. The old chars/4 estimator in `src/parsers/agy.py` is a legacy parser and is not used by this writer or the dashboard read path.

The installer replaces the external `agy-token-tracker` entry in `~/.gemini/config/hooks.json` with `PostInvocation` and `Stop` commands pointing to a frozen local writer.

```bash
python scripts/agy_usage_writer.py --backfill
python scripts/agy_usage_writer.py --status
```

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

Writers replace full session snapshots rather than adding cumulative totals as calls. Event timestamps let the aggregator count calls inside `month`, `30d`, `7d`, `24h`, and custom windows even when the conversation started earlier. AGY events contain the writer's per-response estimates; they are not apportioned from one session total by character weights. Records without events fall back to session timestamps.

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

Reasoning tokens are a subset of output. Claude 5-minute and 1-hour cache writes are retained separately and priced at 1.25× and 2× input rates; writes can make net savings negative. Cache-read percentage excludes additive cache writes from its denominator.

Legacy AGY `embedded_in_input` cache-write counts are diagnostic and are zeroed on read to avoid counting them twice. New AGY snapshots have zero additive writes. Token provenance (`estimated`/`token_source`) is separate from cost provenance (`cost_source`/`pricing_status`).

## Frontend

`dashboard.js` requests `/api/usage` on load, filter changes, and auto-refresh. The response contains usage, analytics, pricing with freshness metadata, and store health. Odometers, charts, tables, and capture chips use the same selected window. Session search is client-side. New requests cancel in-flight requests through an `AbortController`.

See [setup and the file tree](README.md) for entry points, and [retention](docs/CODEX_RETENTION.md) for optional transcript deletion.
