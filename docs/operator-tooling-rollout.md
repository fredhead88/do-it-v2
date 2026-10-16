# R9: versioned operator tools and later installation

The changes here are reviewable sources. No live cron, sender, inbox script,
flow launcher or snapshot script was replaced during this burn-down. The two
Shift+Arrow bindings were applied to the existing tmux server at the operator's
request; `scripts/flow.sh` also installs them on creation or reattachment.

The future canonical entry points are:

| Runtime entry point | Versioned source |
| --- | --- |
| `~/flow.sh` | `scripts/flow.sh` |
| `~/.do-it/bin/flow-snapshot.sh` | `scripts/flow-snapshot.sh` |
| `~/.do-it/thinker-inbox.py` | `scripts/thinker-inbox.py` and `src/thinker_inbox.py` |
| `~/.do-it/telegram/tg-send.sh` | `scripts/tg-send.sh` |

After merging, replace these launchers with symlinks to the canonical repository
sources (retain backups), keeping `remember-thinker-pane.sh` beside flow's source.
The flow scripts resolve their real source directory through symlinks. The inbox
entry point also resolves its repository before importing its module. Respect
`DOIT_ROOT` and `FLOW_SESSION` when using a non-default root or session. No
credential file needs to be displayed, copied into the repository, or changed.

Both flow creation/reattachment and every snapshot refresh `state/thinker-pane`
from a proven live `flow:2.0` pane, using a private atomic replacement. Invalid
or dead observations preserve the last proven identity. Inbox fallback still
resolves `flow:2.0` and re-saves its actual pane ID. Inbox state persists a
consecutive missing-pane count; passes one and two are silent, pass three can
raise a deduplicated request with a recommendation. A live pass resets the count.
A failed delivery remains eligible for retry. Pane typing is recorded as delivered
only after tmux accepts both the text and Enter.

`owed-digest --alert` now shares the daily artifact and locking with the regular
digest. Only operator-owned rows (explicit owner, or legacy operator-action
capability with no owner) at most 48 hours old, with no later decision by subject
or reference, can be shown as requests. Older, answered and non-operator rows are
marked alerted silently. A send is permitted only in the 06:00–06:59 UTC hour,
at most once per UTC date. Reopened rows wait for the next daily summary; send
failures do not mark the day sent. Oversized summaries name the omitted count
and retain complete details in the local daily Markdown artifact. Sweep-health
notifications are merged into this summary. `--dry-run` never writes or sends.

The committed manifest has one `owed-digest` row at `0 6 * * *`, invoking
`--alert`. At rollout, remove the old `# doit-cron:owed-digest-alert` entry and its
command from the user crontab, then update the daily row. Manifest deletion alone
does not uninstall an existing cron entry. Until removed, old alert invocations
are still constrained by the daily lock/date and 06Z time window. Do not alter
unrelated cron entries. This installation was not performed here.

The sender writes one mode-0600 JSONL audit record **before each transport
attempt**, with UTC time, the first message line and sender. Failed attempts
are included. Tokens, chat IDs and later message lines are excluded. Callers set
`TG_SENDER` to `owed-digest` or `thinker-inbox`; other callers default to their
parent process name. A log failure prevents an unlogged send. Logging an attempt
does not claim Telegram acknowledged delivery. A crash after transport acceptance
and before digest dedup persistence remains an external-delivery ambiguity.

Verification uses fixture roots and fake tmux/curl/Telegram senders throughout:

```bash
python3 src/test_owed_digest.py
python3 src/test_operator_tools.py
python3 src/test_crons.py
python3 src/test_reap_tmp.py
bash -n scripts/flow.sh scripts/flow-snapshot.sh scripts/remember-thinker-pane.sh scripts/tg-send.sh
```

No test sends a real Telegram message or starts a real tmux pane/model session.
