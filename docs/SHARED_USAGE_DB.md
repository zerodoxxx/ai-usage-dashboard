# Shared usage database

Codex, Claude Code, and Antigravity share Antigravity's existing SQLite
database:

```text
~/.gemini/antigravity-cli/token_usage.db
```

Set `AI_USAGE_DB_PATH` to override that path. Tests and custom Codex data roots
should pass `db_path=` explicitly so they do not touch a user's live database.
The database stores normalized token counts, timestamps, model IDs, cost
provenance, dashboard timing metadata, and the session title/workspace fields
used by the dashboard. It does not store transcript bodies.

## Python contract

Use the existing provider-neutral contracts from
`src.parsers.contracts` and the shared store APIs from `src.usage_store`:

```python
from src.usage_store import read_usage_sessions, write_usage_sessions

write_usage_sessions("claude-code", [usage_session])
sessions = read_usage_sessions("claude-code")
```

`write_usage_sessions` accepts complete `UsageSession` snapshots, not token
deltas. Replaying the same session replaces its event rows atomically, so
retries cannot double count tokens and a corrected, shorter transcript removes
stale events. Give each `UsageEvent` a stable `event_id` when the provider
exposes one. Store IDs are namespaced (`codex:<native-id>` and
`claude-code:<native-id>`); reads return the original ID. The canonical
provider values are `antigravity`, `codex`, and `claude-code`.

Reads use one SQLite read transaction so concurrent hooks cannot make session
totals and event rows come from different snapshots. If a provider has no
event timestamp, the schema preserves that absence even though Antigravity's
legacy event table requires a non-null timestamp column.

Call `ensure_schema()` explicitly when setting up the database. Writes also
ensure the schema before opening their data transaction. Reads are read-only:
an absent database returns an empty list, and an old provider-less database is
treated as Antigravity data only. Schema migration adds defaults to existing
Antigravity rows without changing their counters. The legacy `daily_summary`
and `model_summary` views stay Antigravity-only; shared reports are available
through `provider_daily_summary` and `provider_model_summary`.

## Token and cost semantics

New Codex and Claude Code sessions use semantics version 2 and
`cache_write_mode='additive'`:

- `input_tokens` includes uncached and cached-read input.
- `cached_input_tokens` is the cached-read subset of input.
- `cache_write_tokens` is separate and additive to input; the optional 5-minute
  and 1-hour fields break that write count down by cache lifetime.
- `output_tokens` includes reasoning output; reasoning is a subset, not an
  extra category to add again.
- `total_tokens` is the provider's authoritative total and is preserved.

Existing Antigravity rows use semantics version 1 and
`cache_write_mode='embedded_in_input'`. Antigravity's cache-write count is
diagnostic and already included in its input and total, so readers must not add
it again.

`cost_usd` retains the session's payable or estimated amount for compatibility.
The additive cost columns preserve `cost_source`, `reported_cost_usd`,
`cost_cached_estimate_usd`, `cost_uncached_usd`, `savings_usd`, and currency.
A calculated estimate must
remain marked `estimated`; it must not be represented as a provider-reported
charge. When no cost is available, use an unpriced cost or let the dashboard
reprice from the exact token events.

The store also preserves normalized event timestamps, model names, stable event
IDs, reasoning effort, and safe TPS metadata (`tps_duration_seconds`,
`tps_output_tokens`, and `tps_trustworthy`). Metadata with prompt, response,
message, or tool-output content is discarded.

## Codex capture

Codex's completion-hook payload does not include token counts. The publisher
`scripts/codex_usage_writer.py` reads only the transcript path supplied by the
installed `Stop`, `SubagentStop`, or `Interrupt` hook and passes it through the
existing Codex parser. It writes a normalized snapshot to the shared DB;
it does not run a dashboard-time history importer. `SubagentStop` must use
`agent_id` and `agent_transcript_path`, since its `session_id` identifies the
parent thread. Hook output is always `{}` on stdout, with safe diagnostics on
stderr, and usage-write failures do not block a Codex turn.

After installing and trusting the hook, capture existing sessions once before
enabling database-only Codex reads:

```sh
python scripts/codex_usage_writer.py --backfill
python scripts/codex_usage_writer.py --status
```

Backfill parses existing local history and writes one full snapshot per
session. Only after that write succeeds does it mark Codex capture enabled.
Repeating backfill after activation is a no-op.
While capture is enabled, the dashboard reads Codex usage from SQLite and does
not fall back to raw rollout files. This preserves imported dashboard history
if an original transcript is later removed. It does not preserve the ability
to resume that deleted Codex conversation.

`--status` reports only per-provider session/event/token counts and database
capture state, including whether backfill enabled database-backed reads. It
omits native session IDs and conversation content. It does not report whether
hooks are configured or trusted; verify those separately with `/hooks` in the
Codex CLI. A hook run that persists usage reports its token total on stderr; a
run that finds no stable transcript usage reports a successful skip.

## Claude Code capture

Claude Code writes provider `claude-code` (alias `claude`). The publisher
`scripts/claude_usage_writer.py` runs as a `Stop`, `SubagentStop`, and
`SessionEnd` hook. The hook payload only names a transcript, so the script
parses that transcript with the existing `src/parsers/claude.py` logic
(response-ID de-duplication, cache-write splits, estimated cost) and writes one
complete session snapshot. Re-running on the same transcript replaces the
session's events and never double counts. `SubagentStop` captures the agent's
own transcript as a separate session; `SessionEnd` also sweeps the session's
`subagents/` directory. A response ID that the DB already attributes to another
session (a resumed transcript copying earlier history) is not counted twice.
Each snapshot records the transcript's stat revision, so an overlapping hook
that parsed an older file cannot overwrite a newer snapshot. The hook is
asynchronous, always exits 0, writes nothing to stdout, and reports skips and
errors on stderr.

Install, inspect, or remove the hooks (this edits `~/.claude/settings.json`,
writes a timestamped `settings.json.<time>.bak` first, and preserves every
other setting and hook):

```sh
python scripts/install_claude_usage_hooks.py --dry-run
python scripts/install_claude_usage_hooks.py
python scripts/install_claude_usage_hooks.py --uninstall
```

The installer copies the publisher and `src/` into a content-addressed folder
under `~/.claude/usage-publisher/releases/`, so switching git branches cannot
break a running hook. Re-run it after changing the publisher or parser.

Import existing history (every `~/.claude/projects/**/*.jsonl`, subagent
transcripts included, de-duplicated across transcripts). It is safe to repeat;
rerunning refreshes snapshots without changing counts for unchanged
transcripts:

```sh
python scripts/claude_usage_writer.py --backfill
python scripts/claude_usage_writer.py --status
```

Backfill marks `claude-code` capture enabled in `usage_capture_state` (use
`--no-enable-capture` to skip). Pass `--claude-dir` or `--db` to target other
locations; tests always do.
