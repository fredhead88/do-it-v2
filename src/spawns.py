#!/usr/bin/env python3
"""L-spec-0755/R2: which spawns are open, and whether a new dispatch conflicts.
`open_spawns` mirrors `tick._spawn_busy`'s terminal tuple and aging rule but is pure:
it never emits a `spawn-stale` (the tick's job). `conflict` is the role table."""
import os
import re
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

START_TYPES = ("build-started", "spawn-started")
TERMINAL = ("spawn-done", "spawn-failed", "spawn-stale")
# role -> roles that may not run on the same subject beside it. Roles absent here
# (spec-writer, owed-sweeper, research, probe, reuse-scout, planner, auditors)
# run beside a build by design and are never checked.
CONFLICTS = {"builder": {"builder", "grader", "reviewer"},
             "grader": {"builder", "grader"},
             "reviewer": {"builder", "reviewer"}}


def _role(e):
    sid = e.get("spawn")
    return "-".join(sid.split("-")[1:-1]) if sid else (e.get("role") or "")


def open_spawns(events, now):
    """Start events with no terminal event for their spawn id, not aged past their
    role's window + cap, and whose recorded same-host waiter pid is still live."""
    import dispatch, fold, panes
    ended = {e.get("spawn") for e in events if e["type"] in TERMINAL}
    host = socket.gethostname()
    out = []
    for e in events:
        if e["type"] not in START_TYPES:
            continue
        sid = e.get("spawn")
        if sid and sid in ended:
            continue
        role = _role(e)
        cap = dispatch.ROLES.get(role, (None, 60, 0))[1]
        pid = e.get("waiter_pid")
        if pid and e.get("waiter_host") == host and \
                not panes._is_live(pid, {"procStart": e.get("waiter_proc_start")}):
            continue
        window = e.get("window_min") or cap
        if (now - fold.ts(e.get("ts"))).total_seconds() / 60 > window + cap:
            continue
        out.append({"spawn": sid, "role": role, "subject": e.get("subject"),
                    "worktree": e.get("worktree"), "ts": e.get("ts")})
    return out


def conflict(role, subject, worktree, open_):
    """A refusal string when dispatching `role` for `subject` (cwd `worktree`) collides
    with an open spawn, else None."""
    table = CONFLICTS.get(role)
    if table is None:
        return None
    for o in open_:
        if o["subject"] == subject and o["role"] in table:
            return (f"{subject} already has an open {o['role']} spawn ({o['spawn']}) — "
                    f"refusing role={role}")
    if role == "builder" and worktree:
        for o in open_:
            if o["role"] == "builder" and o.get("worktree") == worktree:
                return (f"an open builder spawn ({o['spawn']}, subject {o['subject']}) already holds "
                        f"worktree {worktree} — refusing role=builder")
    return None


def acquire(root, role, subject, worktree, events):
    """The duplicate-dispatch gate `dispatch.main` calls once for a role in CONFLICTS.
    Takes the per-subject flock (non-blocking) and checks `conflict` under it. Returns
    `(lock_fd, None)` on success — the CALLER closes the fd right after its start event
    lands — or `(None, (why, reason))` on a refusal, with the lock already released.
    An unreadable lock directory refuses too: undetermined is never clean."""
    import datetime, fcntl, pathlib
    root = pathlib.Path(root)
    try:
        (root / "state").mkdir(parents=True, exist_ok=True)
        fd = open(root / "state" / lock_name(subject), "a")
    except OSError as exc:
        return None, (f"{subject}: cannot open the dispatch lock ({exc}) — refusing role={role}", "dispatch-in-progress")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None, (f"{subject}: another dispatch holds the subject lock — refusing role={role}",
                      "dispatch-in-progress")
    dup = conflict(role, subject, worktree, open_spawns(events, datetime.datetime.now(datetime.timezone.utc)))
    if dup:
        fd.close()
        return None, (dup + " before any spend", "duplicate-dispatch")
    return fd, None


def lock_name(subject):
    """The per-subject lock file name; the subject is reduced to a safe token."""
    return "dispatch-" + re.sub(r"[^A-Za-z0-9._-]", "_", str(subject)) + ".lock"


class ClearedNotMet(Exception):
    """L-spec-0755/R3(c): a grader `cleared` entry whose verdict is not `met`."""


def norm_ac(s):
    """The leading `AC<digits>` token, case-folded (`AC5` == `AC5 [backend]`); any
    other id (`DONE-COND`) is just stripped and case-folded."""
    m = re.match(r"\s*(AC\d+)", str(s), re.I)
    return m.group(1).upper() if m else str(s).strip().casefold()


def clear_events(vs, out, subject, confirmed, spawn):
    """The `criterion-cleared` events a grader's Output earns (L-spec-0755/R3(c)): every
    standing rejection whose normalised id matches a `met` verdict or a `cleared` entry
    (or any, on a confirmed verdict), the standing string being the event's `criterion`.
    COMMIT-SHAPE is never cleared by a grader. A `cleared` entry whose verdict in the
    same Output is not `met` raises ClearedNotMet (the wrapper fails the spawn)."""
    import fold
    specs, *_ = fold.fold(fold.read_events())
    standing = fold.standing_rejects(specs.get(subject, {"evs": []})["evs"])
    met = {norm_ac(v["ac"]): v["reason"] for v in vs if v["verdict"] == "met"}
    not_met = {norm_ac(v["ac"]) for v in vs if v["verdict"] != "met"}
    cleared = {norm_ac(c["ac"]): c["evidence"] for c in out.get("cleared", [])}
    bad = sorted(k for k in cleared if k in not_met and k not in met)
    if bad:
        raise ClearedNotMet(f"cleared entr{'y' if len(bad) == 1 else 'ies'} {', '.join(bad)} "
                            "name a criterion whose verdict is not met")
    return [("criterion-cleared", dict(criterion=c, evidence=met.get(norm_ac(c)) or cleared.get(norm_ac(c))
                                       or f"confirmed verdict {spawn}"))
            for c in sorted(standing)
            if c != "COMMIT-SHAPE" and (norm_ac(c) in met or norm_ac(c) in cleared or confirmed)]


class Cancelled(Exception):
    """L-spec-0755/R1: `doit cancel` wrote `<spawn>.cancelled`. Raised only by
    `dispatch.run_seat`; `main()` exits on it quietly and emits NOTHING — the cancel
    command is the single emitter of the spawn's terminal event."""
    def __init__(self, spawn):
        self.spawn = spawn
        super().__init__(f"{spawn}: cancelled")


def on_sigterm_cancel(spawn, cancel_path):
    """Install a SIGTERM handler: `doit cancel` writes the `.cancelled` marker THEN signals,
    so a SIGTERM that finds the marker is a cancel (raise Cancelled); any other SIGTERM
    keeps its default action. Returns the previous handler, or None off the main thread
    (the marker poll alone then cancels)."""
    import signal

    def _term(signum, frame):
        if os.path.isfile(cancel_path):
            raise Cancelled(spawn)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)
    try:
        return signal.signal(signal.SIGTERM, _term)
    except ValueError:
        return None


def restore_sigterm(prev):
    import signal
    if prev is not None:
        signal.signal(signal.SIGTERM, prev)


def cancelled_exit(spawn, view=None):
    """`doit cancel` already wrote the spawn's one terminal event, so this emits NOTHING —
    it only tears down a private grading cluster and exits."""
    if view is not None:
        import grading_env
        grading_env.teardown(view)
    print(f"CANCELLED {spawn}", file=sys.stderr)
    sys.exit(1)


def arm_cancel(seat_dir, spawn):
    """`(previous SIGTERM handler, <spawn>.cancelled path)` for `dispatch.run_seat`."""
    path = seat_dir / f"{spawn}.cancelled"
    return on_sigterm_cancel(spawn, path), path


def raise_if_cancelled(spawn, cancel_path):
    if os.path.isfile(cancel_path):
        raise Cancelled(spawn)


def gate(root, role, subject, worktree, events, fail):
    """`acquire` for a role in CONFLICTS (else None); a refusal goes to `fail(why, reason=)`."""
    if role not in CONFLICTS:
        return None
    lock, refusal = acquire(root, role, subject, worktree, events)
    if refusal:
        fail(refusal[0], reason=refusal[1])
    return lock


def unserved_or_cancelled(exc, fail, spawn, view=None):
    """dispatch's handler for `except (Unserved, Cancelled)`: a cancel exits quietly."""
    if isinstance(exc, Cancelled):
        cancelled_exit(spawn, view)
    fail(f"unserved: {exc}", reason="unserved")


def or_fail(fail, fn, *args):
    """`fn(*args)`, a ClearedNotMet becoming a failed spawn (reason cleared-not-met)."""
    try:
        return fn(*args)
    except ClearedNotMet as e:
        fail(str(e), reason="cleared-not-met")
