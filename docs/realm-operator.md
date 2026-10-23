# Realm operator summary

The five rows above the map answer what merges next, where your priorities
are, which decisions need you, what has actually stopped, and whether the
merge queue is keeping up. The row labels open readable lists. Long rows
scroll horizontally; short titles show their full title and spec ID on hover.
The four priority groups sit side by side on the priorities row, with full
titles and stages in the priorities panel. Shelf and Codex also have buttons
above the rows, including on phones. Counts and census live under Details.

Edit `realm_priorities.json` to change the groups or their spec IDs. It is a
JSON object mapping each group name to an array of spec IDs. All eight
initial priorities are included. Configuration changes are read on the next
request and invalidate the summary cache; no service restart is needed.

The authenticated `GET /api/operator` endpoint uses the existing live ledger
snapshot and its own fold/rule modules. The browser polls every 30 seconds;
the projection is cached for up to one minute. A failed or stale reading is
shown as unavailable. Saved and hosted snapshots carry the projection too;
replay does not present it as the current state.

- Queue positions come from explicit queue entries and the fold's clean
  review/gate readiness for older executors that merge without `queue-add`.
  Held and terminal work does not appear among the next five eligible merges.
- Rough ETA is position times the median elapsed duration of completed merge
  windows in the last 24 hours. Open/failed windows are excluded. Without
  samples, ETA is unavailable. Batching and parallel work can change the order
  and timing. Deployment takes additional time.
- Stages distinguish building, checking, in review, waiting to merge with its
  position, shipped, and live. Live requires acceptance or a deployment that
  names the spec or its exact shipped SHA; an unrelated deploy is insufficient.
- Needs you contains only open `escalation-blocking` rows explicitly owned by
  `operator`, excluding answered or superseded asks. Other open questions are
  not presented as operator escalations.
- Actually stuck uses `lane_rules` deadlines, excluding holds, waves, active
  work, open questions, queue positions and open merge windows. An overdue
  blocker does not turn its held dependants into stuck work. Rule errors are
  shown as partial evidence, with their reasons in the drill-down.
- Shelf separates held specs, ready specs without a builder, and specs already
  being handled or waiting on a ruling. Dependency and migration sequencing
  use the fold's recorded blocker, displayed as “waiting on …”.
- Today means UTC. Merged counts unique specs kept by verified windows; added
  counts unique specs enqueued or made ready by clean reviews/gates. Trend
  replays ready/enqueue arrivals, removals, rework and completed windows since
  midnight, so removals can balance arrivals without a merge.

Clicking the Codex legend, gate, ship or a construct opens the same work list.
`GET /api/codex` reuses the existing authenticated server. The list contains
Codex-backed ledger seats started today (role, spec, UTC start and outcome)
and immediate burn-down directories with a matching brief. A brief must be
`burndown/briefs/<name>-brief.md` or `<name>.md`. The title comes from its
heading. REPORT.md means done; its absence means running, independently of
process health. Only the opening prose paragraph is shown (bounded to 1,600
characters and redacted for common credential spellings and paths).

The worker scan reads only matching briefs and REPORT.md. It does not recurse
into worktrees or private directories and rejects symlinked workers, briefs
and reports. Existing Codex session metadata also excludes sessions whose
working directory is under `~/.do-it/private`. No new endpoint dispatches a
worker, appends to the ledger or writes to a repository.

Run the focused checks from the repository root:

```sh
python3 src/test_realm.py
python3 src/test_realm_ledger.py
python3 src/test_realm_operator.py
python3 src/test_stalls.py
python3 src/test_lane_rules.py
```
