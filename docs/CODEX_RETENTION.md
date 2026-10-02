# Codex transcript retention

Codex keeps transcripts so it can reopen and resume older conversations. This
project can remove inactive transcripts after 15 days while keeping their
normalized token usage in the shared SQLite database at
`~/.local/share/ai-usage/usage.db`. The old
`~/.gemini/antigravity-cli/token_usage.db` file is a frozen backup and is never
used to decide whether a transcript can be deleted. Set `AI_USAGE_DB_PATH` or
pass `--db` to use an alternate database.

The retention command is read-only by default:

```sh
python scripts/codex_retention.py
```

It reports aggregate counts and transcript bytes that meet the policy. Before
deletion, it reads each candidate rollout to compare the input, cached input,
uncached input, output, cache-write, reasoning-output, and total token counts
with the matching provider-tagged row in SQLite. The stored row must have
`provider='codex'` and the session ID `codex:<Codex session ID>`. Its capture
metadata must identify that rollout and match its current file revision
(resolved-path hash, modification time, change time, and size). Provider-wide
capture flags alone do not prove that a particular rollout was captured.

The database must exist, be readable, and contain Codex rows. A missing,
unreadable, legacy, or Codex-empty database stops the run. A missing session,
state-summary-only row, undercounted tokens, absent or stale capture metadata,
changed or symlinked file, unknown activity time, malformed rollout, or
incomplete thread graph protects the whole conversation tree. Skips are
reported as aggregate reasons; file paths, IDs, titles, and transcript text
are not included in the retention log.

Codex's current live Stop hook has not yet been verified to write successfully.
The available Codex rows came from the backfill completed at
`2026-10-02T12:31Z`; a newer rollout is retained until its own row and source
revision are present in SQLite.

The age check uses the newest activity time across every rollout fragment,
Codex's per-thread update and recency fields, and the stored usage activity.
Spawned sessions are treated as one tree: a recent child keeps the parent and
all siblings. The shared database's Codex rows and Antigravity rows are never
deleted or rewritten by retention.

## Automatic cleanup

The installer writes an immutable, content-addressed runtime and one
user-level macOS LaunchAgent:

```sh
python scripts/install_codex_retention.py
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.zerodoxxx.ai-usage-dashboard.codex-retention.plist"
```

The job checks every 15 minutes. If Codex Desktop, its native server, or any
Codex CLI process is running, it skips the run and tries again later. When
Codex is closed, it rechecks the full plan and each source revision before
calling Codex's own `delete` command. Codex's rollout writer lock also refuses
to delete a conversation whose transcript is actively being written. The job
does not unlink rollout files directly.

The installed job is pinned to Codex CLI `0.159.2`, the version whose delete
and spawned-thread behavior this policy was checked against. A version change
stops deletion until the compatibility pin is reviewed and the job is
reinstalled. The LaunchAgent writes only aggregate JSON status to a bounded
log at `~/.codex/usage-retention/retention.log`; Codex command output,
conversation IDs, titles, and transcript content are not logged.

Since usage totals remain in SQLite, older conversations still appear in the
dashboard after their local transcripts are removed. Codex can no longer
resume a conversation after Codex deletes it. Transcript bytes shown by the
preview are an estimate; Codex also updates its own metadata database during
deletion.
