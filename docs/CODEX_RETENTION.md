# Codex transcript retention

Optional macOS retention removes inactive Codex conversation trees after 15 days while preserving normalized usage in the shared SQLite database. **It deletes transcripts: Codex can no longer reopen or resume those conversations.** A database backup preserves usage, not conversation text.

The active database defaults to `~/.local/share/ai-usage/usage.db`; `AI_USAGE_DB_PATH` or `--db` overrides it. The legacy `~/.gemini/antigravity-cli/token_usage.db` path is rejected for deletion decisions.

## Preview and capture checks

The default command is a read-only preview:

```bash
python scripts/codex_retention.py
```

The preview reports aggregate eligible trees, threads, transcript count, bytes, and skip reasons. The database must exist, be readable, and contain provider-tagged Codex rows. Missing, unreadable, provider-less, Codex-empty, or legacy-path databases stop the run.

For every rollout fragment, retention independently reads raw token records and checks that the matching `provider='codex'`, `session_id='codex:<native-id>'` snapshot covers input, cached and uncached input, output, cache writes (including lifetime splits), reasoning output, and total tokens. It also requires capture metadata matching the resolved-path hash, modification time, change time, and size. Provider-wide backfill flags alone do not prove capture.

A session needs positive tokens and verified events; a state-summary-only snapshot is insufficient. Sources must remain inside the verified Codex home and must not be symlinked or change during verification. Unsafe directories or scan failures stop the plan.

Age uses the newest activity across rollout modification times, Codex thread update/recency fields, and stored usage activity. Parent and spawned sessions form one tree: a recent child keeps its parent and siblings.

| Skip reason | Why the tree is kept |
|:------------|:---------------------|
| `ambiguous_thread_tree` | Multiple roots/parents, cycles, or an inconsistent graph |
| `missing_thread_metadata` | A related thread is absent from Codex metadata |
| `thread_activity_unknown` | Thread activity is absent or cannot be parsed |
| `transcript_not_exactly_captured` | Missing/stale source revision, unsafe file, or a change during verification |
| `usage_ambiguous` | Raw counters cannot be independently reconciled |
| `sqlite_usage_below_rollout_totals` | Stored counters do not cover independently verified rollout usage |
| `sqlite_usage_missing` | Missing provider-tagged capture or rollout source |
| `no_verified_token_events` | No positive usage/events, or only a state summary |
| `no_transcripts` | No verified transcript sources |
| `within_retention_window` | Activity is newer than the cutoff |

Raw verification accepts records carrying both thread and turn totals, repeated/null-info legacy `token_count` events, and response records exceeding legacy counts. Contradictory or decreasing counters, cumulative totals above summed usage, unscoped turn counts, conflicting response records, and legacy counts above responses remain ambiguous.

## Deletion and backup

Apply requires Codex Desktop, its server, and all Codex CLI processes to be closed. Unknown process state fails the safety check; a running Codex defers scheduled work. Before each deletion, retention rebuilds eligibility and rechecks source revisions and closed state.

Before the first deletion in a run, it makes a **fresh verified SQLite backup** through `scripts/backup_usage_db.py`. Creation, integrity, per-provider session/event counts and token sums, publication, and rotation must succeed. A failure produces `backup_failed` and deletes no trees in that run. Eligibility is checked again after backup. Backups default to `<db dir>/backups`, retaining 14; see [backup and restore](SHARED_USAGE_DB.md#backup-and-restore).

Deletion calls Codex's own `--no-daemon delete <root-id> --force` command instead of unlinking files. Child processes pin **both `CODEX_HOME` and `CODEX_SQLITE_HOME`** to the resolved, verified Codex directory. Ambient home overrides cannot redirect deletion to another Codex store. Codex's writer lock can refuse deletion if a thread becomes active; failures stop remaining work.

Retention never deletes or rewrites usage rows for any provider. Transcript byte counts are estimates; Codex deletion also updates its metadata.

## Automatic cleanup

The installer writes an immutable, content-addressed runtime and one user LaunchAgent. It does not load or unload the job:

```bash
python scripts/install_codex_retention.py
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.zerodoxxx.ai-usage-dashboard.codex-retention.plist"
```

The job runs on load and every 15 minutes, applying the 15-day policy only when safety checks pass. Disable it with:

```bash
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.zerodoxxx.ai-usage-dashboard.codex-retention.plist"
```

The installer pins the interpreter, database, Codex home, CLI executable, and supported version **`0.159.2`**. Apply checks that exact version; the installer rejects unsupported versions too. **After a Codex update, deletion stops until the compatibility pin is reviewed and the job is reinstalled.** Reinstallation alone cannot approve an unsupported version.

After a compatible runtime or path change, unload the old job, reinstall, and reload it:

```bash
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.zerodoxxx.ai-usage-dashboard.codex-retention.plist"
python scripts/install_codex_retention.py
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.zerodoxxx.ai-usage-dashboard.codex-retention.plist"
```

`--codex-home`, `--db`, `--python`, and `--codex` select alternate install paths. Use matching `--codex-dir` and `--db` for a preview of that installation.

The job writes bounded aggregate JSON status to `~/.codex/usage-retention/retention.log` (under the selected Codex home for custom installs). It omits Codex command output, conversation IDs, titles, and transcript text.
