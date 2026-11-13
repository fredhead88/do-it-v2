#!/usr/bin/env python3
"""L-spec-0755/R2: which spawns are open, and whether a new dispatch conflicts.
`open_spawns` and `tick._spawn_busy` share the pure start-state decision.
Only tick records vanished/stale ends. `conflict` is the role table."""
import os
import re
import socket
import sys
from contextvars import ContextVar
from functools import wraps

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

START_TYPES = ("build-started", "spawn-started")
TERMINAL = ("spawn-done", "spawn-failed", "spawn-stale")
# role -> roles that may not run on the same subject beside it. Roles absent here
# (spec-writer, owed-sweeper, research, probe, reuse-scout, planner, auditors)
# run beside a build by design and are never checked.
CONFLICTS = {"builder": {"builder", "grader", "reviewer"},
             "grader": {"builder", "grader"},
             "reviewer": {"builder", "reviewer"},
             "charter-reviewer": {"charter-reviewer"}}



_active_view = ContextVar("dispatch_ledger_view", default=None)

def _read_events(**kwargs):
    """One ledger read site for invocation views and uncached advisory queries."""
    import fold
    return fold.read_events(**kwargs)


class LedgerView(list):
    """One invocation's events, with a single lazy post-backend replacement."""

    def __init__(self, events=None):
        super().__init__([] if events is None else events)
        self.loaded = events is not None
        self.after_backend = False
        self.refreshed = False

    def current(self):
        if not self.loaded or self.after_backend and not self.refreshed:
            events = _read_events()
            self[:] = events
            self.loaded = True
            if self.after_backend:
                self.refreshed = True
        return self

    def committed(self, event):
        import fold
        # Reuse the reader's normalization and same-ts merge. Its source order
        # is recovered from physical file:line identities, never write timing.
        # Supplied predicate fixtures can omit storage provenance. Retain them
        # unchanged without inventing file identities for the reader to sort.
        supplied = [e for e in self if "_src" not in e]
        stored = [e for e in self if "_src" in e]
        self[:] = supplied + fold.finish_events([*stored, event])

    def __enter__(self):
        self._token = _active_view.set(self)
        return self

    def __exit__(self, *exc):
        _active_view.reset(self._token)

def dispatch_view(events):
    return LedgerView(events)

def current_events(events=None):
    """Optional supplied input, invocation view, or standalone reader."""
    if events is not None:
        return events
    view = _active_view.get()
    if view is not None:
        return view.current()
    return _read_events()

def with_dispatch_view(fn):
    @wraps(fn)
    def invoke(*args, **kwargs):
        if _active_view.get() is not None:
            return fn(*args, **kwargs)
        with dispatch_view(None):
            return fn(*args, **kwargs)
    return invoke

def coverage_changes(checkers, *, events=None):
    """A checker whose coverage note differs from the last recorded one marks its prior passes stale."""
    import fold
    last = {e.get("checker"): e.get("coverage_note") for e in current_events(events)
            if e.get("type") == "checker-coverage-change"}
    return [("checker-coverage-change", dict(checker=c["id"], version=c["version"],
                                             coverage_note=c["coverage_note"]))
            for c in checkers if last.get(c["id"]) != c["coverage_note"]]

def resolve_cwd(value, root):
    """Planner paths prefer the ledger root; all backends receive an absolute cwd."""
    from pathlib import Path
    path = Path(value) if value else Path.cwd()
    if not path.is_absolute() and (Path(root) / path).exists():
        path = Path(root) / path
    return str(path.resolve())


def default_cwd(role, subject, project, root):
    """Resolve an implicit dispatch path using registered orchestration metadata."""
    from pathlib import Path

    def component(value, name):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value):
            raise ValueError(f'invalid {name} component {value!r}')
        return value

    root = Path(root).resolve()
    if project is None or project == '':
        raise ValueError('no-project: missing registered project')
    try:
        project = component(project, 'project')
    except ValueError as exc:
        raise ValueError(f'no-project: {exc}') from exc
    if not (root / 'repos' / project).is_dir():
        raise ValueError(f'no-project: unregistered project {project!r}')
    if role == 'builder':
        subject = component(subject, 'subject')
        return str((root / 'worktrees' / project / subject.lower()).resolve())
    return str((root / 'repos' / project).resolve())

def _role(e):
    sid = e.get("spawn")
    return "-".join(sid.split("-")[1:-1]) if sid else (e.get("role") or "")


def start_events(events):
    """One start per named attempt, retaining metadata from paired start forms."""
    named, anonymous = {}, []
    for e in events:
        if e.get('type') not in START_TYPES:
            continue
        sid = e.get('spawn')
        if not sid:
            anonymous.append(e)
        elif sid not in named:
            named[sid] = dict(e)
        else:
            original = named[sid]
            original.update({k: v for k, v in e.items()
                             if v not in (None, '') and k not in ('ts', 'type')})
            original['ts'] = min(original['ts'], e['ts'])
    return list(named.values()) + anonymous


def start_state(e, now):
    """Pure busy/stale/vanished decision shared by tick and conflict readers."""
    import dispatch, fold, panes
    cap = dispatch.ROLES.get(_role(e), (None, 60, 0))[1]
    pid = e.get('waiter_pid')
    if pid and e.get('waiter_host') == socket.gethostname() and \
            not panes._is_live(pid, {'procStart': e.get('waiter_proc_start')}):
        return 'vanished'
    window = e.get('window_min') or cap
    elapsed = (now - fold.ts(e.get('ts'))).total_seconds() / 60 > window + cap
    if elapsed:
        legacy = not any(e.get(k) for k in ('waiter_pid', 'waiter_proc_start', 'waiter_host'))
        return 'vanished' if legacy else 'stale'
    return 'busy'


def open_spawns(events, now):
    """Start events with no terminal event for their spawn id, not aged past their
    role's window + cap, and whose recorded same-host waiter pid is still live."""
    events = list(events)
    ended = {e.get("spawn") for e in events if e["type"] in TERMINAL}
    out = []
    for e in start_events(events):
        sid = e.get("spawn")
        if sid and sid in ended:
            continue
        role = _role(e)
        if start_state(e, now) != 'busy':
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


def lock_held(subject, root=None):
    """Observe the subject flock without acquiring it or creating its path.

    Hold a read-only descriptor while the shared kernel reader checks its inode.
    An unknown observation is held; only proven absence is clean. Dispatch's
    atomic acquisition remains authoritative after this advisory observation.
    """
    import health_alarms
    from pathlib import Path
    root = Path(root) if root is not None else Path(os.environ.get('DOIT_ROOT', Path.home() / '.do-it'))
    try:
        with open(root / 'state' / lock_name(subject), 'rb') as fd:
            return health_alarms.lock_held(f'/proc/self/fd/{fd.fileno()}') is not False
    except FileNotFoundError:
        return False
    except OSError:
        return True


def would_conflict(subject, role, *, events=None, now=None, root=None, worktree=None):
    """Read-only readiness snapshot, never authorization for a later dispatch.

    Explicit events/time avoid a fresh ledger read; root selects the lock and
    registered worktrees. Without events, use the normal uncached event reader
    so even its disposable cache lock cannot interfere with dispatch.
    """
    import datetime
    from pathlib import Path
    import fold
    import project_scope

    if role not in CONFLICTS:
        return None
    root = Path(root) if root is not None else fold.ROOT
    try:
        events = list(_read_events(use_cache=False) if events is None else events)
        now = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
        if worktree is None:
            worktree = default_cwd(role, subject, project_scope.subject_project(events, subject), root)
        worktree = resolve_cwd(worktree, root)
        open_ = open_spawns(events, now)
        for spawn in open_:
            if spawn.get('worktree'):
                spawn['worktree'] = resolve_cwd(spawn['worktree'], root)
        # Match acquire's precedence: a held subject lock wins over ledger state.
        if lock_held(subject, root):
            return 'dispatch-in-progress'
        if conflict(role, subject, worktree, open_):
            return 'duplicate-dispatch'
        return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return 'dispatch-in-progress'


class ClearedNotMet(Exception):
    """L-spec-0755/R3(c): a grader `cleared` entry whose verdict is not `met`."""


def norm_ac(s):
    """The leading `AC<digits>` token, case-folded (`AC5` == `AC5 [backend]`); any
    other id (`DONE-COND`) is just stripped and case-folded."""
    m = re.match(r"\s*(AC\d+)", str(s), re.I)
    return m.group(1).upper() if m else str(s).strip().casefold()


def clear_events(vs, out, subject, confirmed, spawn, *, events=None):
    """The `criterion-cleared` events a grader's Output earns (L-spec-0755/R3(c)): every
    standing rejection whose normalised id matches a `met` verdict or a `cleared` entry
    (or any, on a confirmed verdict), the standing string being the event's `criterion`.
    COMMIT-SHAPE is never cleared by a grader. A `cleared` entry whose verdict in the
    same Output is not `met` raises ClearedNotMet (the wrapper fails the spawn)."""
    import fold
    specs, *_ = fold.fold(__import__("dispatch").current_events(events))
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
