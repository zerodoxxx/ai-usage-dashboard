# Codex transcript retention

Codex keeps transcripts so it can reopen and resume older conversations. This
project can remove inactive transcripts after 15 days while keeping their
normalized token usage in Antigravity's shared SQLite database.

The retention command is read-only by default:

```sh
python scripts/codex_retention.py
```

It reports aggregate counts and transcript bytes that meet the policy. It does
not read transcript bodies. A conversation tree qualifies only when the
shared database has Codex usage events for the root and every spawned session,
and every rollout file still present has the exact path fingerprint and file
revision recorded during capture. A missing session, state-summary-only row,
changed or symlinked file, unknown activity time, or incomplete thread graph
protects that tree.

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
