#!/usr/bin/env python3
"""tick — fold the ledger, compute the actionable lane, record one liveness fact.

  tick.py     one tick: fold, compute, record. Scheduled (cron) and poked (every
              dispatch ends with one). One at a time per ledger — flock — and a
              dropped duplicate loses nothing: the durable list is the queue.

A tick starts no Executor, under any backend (L-adr-0035). It is a pure
fold-and-record: it spends nothing, it can be run at any frequency by anything,
and the one event it appends is what `fold` reads for staleness. The Executor is
a supervised pane that reads the same lane for itself; nothing here launches it.
"""
import argparse, fcntl, os, pathlib, re, socket, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import carry, dispatch, fold, intake, notes, pane_resume, panes, tree_cleanup  # noqa: E402
# `relay` is imported LAZILY, inside `wait()` only — importing tick.py must not
# force `relay` (and its own `audit` import) into sys.modules for every caller,
# `pane_end.py` among them, that only wants `up.quiet_point`'s tick.py half.

# Waiting for the Executor's next durable action. `building` is in flight, not waiting.
# R7/L-spec-0192 put `shipped-owed-due` here — a due criterion was once the
# Executor's own action to take (owed_met/a re-grade). L-charter-0038/L-spec-0388
# (R1) takes it back out: a dedicated sweeper (`doit sweep-owed`) now runs every
# owed observation in batches, off the Executor's own five-minute lane pass, so
# a due-but-unswept criterion is no longer this lane's to surface.
# §10.5's RETIRE list, verbatim — every pane launcher denies it by name (D119).
# NOT dead with the spawn path: fold.caps(), think.pane_cmd() and up.pane_cmd() read these.
RETIRE = ("subagent-driven-development", "executing-plans", "writing-plans", "requesting-code-review",
          "receiving-code-review", "finishing-a-development-branch", "dispatching-parallel-agents")
MINUTES, USD = 20, 5          # the Executor's declared cap, read by fold.caps() (§4.4 correction 9)
TICK_NAME = "L-tick-local.jsonl"
POLL_STEP = 0.05              # tick.wait's poll granularity — fine enough for AC3's <1s wake
# §3.11's L2 conjunction, quantified over `mine` — a spec in one of these is done for its charter.
# L-charter-0038/L-spec-0388 (R2): `shipped-owed-expired` joins — a check the
# sweeper's own SD3 escalation already surfaced (§4.9's owed<=K, not this
# conjunct) is held like evidence, not left blocking closure forever over a
# check that already lapsed.
SPEC_DONE = ("accepted", "shipped-owed-evidence", "shipped-owed-expired", "dropped",
             "closed-unbuilt", "closed-shipped")
# L-charter-0046/L-spec-0473, R3: the five L2 conjuncts `fold.l2_complete` already
# required to reach "L2-complete", restated once at the moment of close —
# invariant across every charter, never computed per-charter (Assumptions).
PROVING_CLOSE_REASON = ("every owed check discharged; sweep at fixpoint; no open in-scope brief; "
                        "charter review complete or not owed")
LANE_LOG_BYTES = 1024 * 1024


def _log_lane_driver(lines, snapshot):
    """Keep two bounded files; record both the snapshot and observation clock."""
    from datetime import datetime, timezone
    path = fold.ROOT / "logs" / "lane-driver.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    for line in lines:
        prefix = f"{datetime.now(timezone.utc).isoformat()} snapshot={snapshot.isoformat()} "
        message = str(line).replace("\r", "\\r").replace("\n", "\\n")
        data = (prefix + message).encode('utf-8')[:min(8191, LANE_LOG_BYTES - 1)]
        data = data.decode('utf-8', errors='ignore').encode('utf-8') + b'\n'
        if path.exists() and path.stat().st_size + len(data) > LANE_LOG_BYTES:
            path.replace(path.with_suffix('.log.1'))
        with path.open('ab') as stream:
            stream.write(data)


def tick_path():
    """`L-tick-local.jsonl` under the CURRENT `fold.EVENTS` — resolved fresh on
    every call, never cached at import. `tick.wait` rebinds `fold.ROOT`/
    `fold.EVENTS` to a caller-given root (AC17), and a path computed once at
    import time would keep pointing at the importing process's own DOIT_ROOT no
    matter what root a later call named."""
    return fold.EVENTS / TICK_NAME


def _bind_root(root):
    """Rebind `fold.ROOT`/`fold.EVENTS` to `root`, exactly as `pane_end.bind_root`
    already does — a bare `root` parameter never redirects the module globals
    `dispatch.emit`/`tick_path()` actually read. `None` leaves today's binding
    (this process's own DOIT_ROOT) alone. Duplicated here rather than imported
    from `pane_end` to avoid a cycle: `pane_end` imports `up`, and `up` imports
    `tick`."""
    if root is None:
        return fold.EVENTS
    root = pathlib.Path(root)
    fold.ROOT, fold.EVENTS = root, root / "events"
    return fold.EVENTS


def _ended_since_read(sid):
    """Re-read the spawn's OWN ledger file just before calling it stale: the waiter
    writes `spawn-done` and exits, and a tick that read the stream a moment earlier
    then saw a dead waiter and emitted `spawn-stale` after a finished spawn
    (L-grader-0746/0733, L-builder-0893, 2026-10-04) — which re-dispatched work
    that had already finished."""
    if not sid:
        return False
    try:
        text = (fold.EVENTS / f"{sid}.jsonl").read_text()
    except OSError:
        return False
    return any(f'"{t}"' in text for t in ("spawn-done", "spawn-failed"))


def _spawn_busy(ev):
    """Subjects with a REAL spawn started and not ended — never escalation-busy.
    A start older than twice its role's cap with no terminal event is a dead
    wrapper: recorded once as `spawn-stale` (on the CURRENT `tick_path()`, never
    a stale import-time path), and the subject is back on the lane for the
    Executor's failed-spawn row.

    A same-host, confirmed-dead waiter goes stale immediately, independent of
    elapsed time: when the started event carries a truthy `waiter_pid` and
    `waiter_host` equal to this process's own hostname, `panes._is_live` (reused
    verbatim, built with a synthetic `{"procStart": waiter_proc_start}` meta
    dict) decides liveness — a confirmed-dead waiter never gets the benefit of
    the doubt an unreadable pid/start-time does. An event missing `waiter_pid`/
    `waiter_host`, or naming a different host, falls through to the existing
    elapsed-time check unchanged."""
    started = [e for e in ev if e["type"] in ("build-started", "spawn-started")]
    ended = {e.get("spawn") for e in ev if e["type"] in ("spawn-done", "spawn-failed", "spawn-stale")}
    busy = set()
    for e in started:
        sid = e.get("spawn")
        if sid and sid in ended:
            continue
        # A start with no spawn id is not the wrapper's — a hand-written one, or a
        # pre-wrapper event. It can never be matched to a terminal event, so it is
        # aged out on the role's own cap and there is nothing to name in a
        # `spawn-stale`: the subject simply returns to the lane, where the
        # Executor's failed-spawn row is what looks at it.
        role = "-".join(sid.split("-")[1:-1]) if sid else (e.get("role") or "")
        cap = dispatch.ROLES.get(role, (None, 60, 0))[1]
        waiter_pid = e.get("waiter_pid")
        if waiter_pid and e.get("waiter_host") == socket.gethostname() and \
                not panes._is_live(waiter_pid, {"procStart": e.get("waiter_proc_start")}):
            sid and not _ended_since_read(sid) and dispatch.emit(tick_path(), {"spawn": sid}, "spawn-stale",
                                  subject=e.get("subject"), role=role, cap_min=cap, reason="waiter-dead")
            continue
        # L-spec-0269/R1: the subject's own recorded `window_min` (its offered
        # claim window), plus the role's cap for the working wait after a claim
        # — replacing the flat `2 * cap`, which read a spec whose window was
        # raised as a dead wrapper before it had had its full chance to be
        # claimed. An event with no recorded `window_min` (every pre-existing
        # one, and every non-seat-backend one) falls back to `cap`, reproducing
        # `2 * cap` exactly.
        window = e.get("window_min") or cap
        if (fold.NOW - fold.ts(e.get("ts"))).total_seconds() / 60 > window + cap:
            sid and not _ended_since_read(sid) and dispatch.emit(tick_path(), {"spawn": sid}, "spawn-stale",
                                  subject=e.get("subject"), role=role, cap_min=cap)
        else:
            busy.add(e.get("subject"))
    return busy


def spawn_in_flight(ev):
    """`in_flight`'s busy set, MINUS the escalation-busy addition — real
    unterminated spawns only. `up.quiet_point`'s own "nothing in flight" half
    calls this, not `in_flight`, so an open `escalation-blocking` on an
    UNRELATED subject elsewhere never stalls an Executor pane's own end.
    Measured: `in_flight`'s merged `busy` set otherwise stalls every Executor
    pane behind any one open operator escalation, anywhere — contrary to R3's
    Goal (0187 Assumption 7, AC6(d))."""
    return _spawn_busy(ev)


def in_flight(ev):
    """Subjects with a spawn started and not ended, PLUS any subject whose
    newest escalation/decision/unblocked row is an open `escalation-blocking` —
    off the lane, or the next tick dispatches the same work twice, or hands the
    Executor a subject the operator is still deciding. `tick.lane`'s own use of
    this is UNCHANGED by 0187 (R7): a lane candidate legitimately wants both
    kinds excluded. `spawn_in_flight` above is the split that does not.

    A `blocked` event is deliberately NOT read here (R7). The Executor writes one
    when two units share a footprint, and a footprint collision is that unit's own
    wait — never a reason to strike a second, merely adjacent subject off the lane.
    Only an open `escalation-blocking` gates, because that one is the operator's."""
    # An open escalation is the operator's: the subject leaves the lane until a
    # decision or an unblocked event lands after it — else every cron tick pays
    # for an Executor that reads the escalation and does nothing.
    last = {}
    for e in ev:
        if e["type"] in ("escalation-blocking", "decision", "unblocked") and e.get("subject"):
            last[e["subject"]] = e["type"]
    busy = {s for s, t in last.items() if t == "escalation-blocking"}
    busy |= _spawn_busy(ev)
    return busy


def closable_fallback(events, charter):
    """`closable(events, charter)` until `the-fold-and-the-board` lands the real one
    in `fold` — (every spec accepted, sweep fixpoint derived, charter-review owed).

    It mirrors `fold.fold()`'s L2 conjunction in FULL, all five conjuncts, not the
    three the seam names: the brief conjunct and `K` ride with the fact they belong
    to (an open in-scope brief is exactly what holds the sweep short of a fixpoint;
    an owed criterion over K is exactly what holds a spec short of accepted). A
    fallback missing either would report a charter closable that `fold.fold()` holds
    at `L1-complete` — dropping it off the lane before the Executor's brief-author
    row can fire, and stranding it short of reap forever.

    L-charter-0038/L-spec-0388 (R2): the owed tally counts `fold.OWED_TALLY_STATES`
    (`shipped-owed-evidence` AND `shipped-owed-expired`), the SAME tuple `fold()`'s
    own charter `owed` field and `_closable`'s `owed_within_k` count against — never
    the bare `"shipped-owed-evidence"` literal alone. `SPEC_DONE` above admits
    `shipped-owed-expired` too; a tally that stopped at the old literal would let a
    spec `accepted`-for-closure by the state check sit outside the very `owed<=K`
    count this docstring's own "mirrors fold.fold() in FULL" claims to hold it to."""
    cid = charter["id"] if isinstance(charter, dict) else charter
    evs = charter["evs"] if isinstance(charter, dict) else [e for e in events if e.get("subject") == cid]
    specs = fold.fold(events)[0] if events else {}
    mine = [s for s in specs.values() if s["charter"] == cid and s["state"] != "void"]
    owed = sum(1 for s in mine if s["state"] in fold.OWED_TALLY_STATES)
    accepted = all(s["state"] in SPEC_DONE for s in mine) and owed <= fold.K
    fixpoint = "sweep-fixpoint" in {e["type"] for e in evs} and not fold.open_briefs(evs)
    return accepted, fixpoint, fold.charter_review(evs) != "charter-review-complete"


def _carry_packet_busy(busy):
    """L-spec-0190 R9 (fix 5) — sources whose own carry packet names a spec_id
    that is itself in-flight. A filesystem read, not a new event type (EMITS
    has no room for one; A11): glob `content/carry-*.packet.md`, parse the
    spec_id out of each filename, and read the file's trailing non-blank line
    for `Source: v4 spec <X>.`; `<X>` is busy exactly when that spec_id is
    itself in `busy`. No write to any packet file — read-only."""
    out = set()
    content = fold.ROOT / "content"
    if not content.is_dir():
        return out
    for p in sorted(content.glob("carry-*.packet.md")):
        name = p.name
        if not (name.startswith("carry-") and name.endswith(".packet.md")):
            continue
        spec_id = name[len("carry-"):-len(".packet.md")]
        if spec_id not in busy:
            continue
        try:
            lines = [l for l in p.read_text().splitlines() if l.strip()]
        except OSError:
            continue
        if not lines:
            continue
        m = re.match(r"Source:\s*v4 spec\s+(.+)\.$", lines[-1].strip())
        if m:
            out.add(m.group(1).strip())
    return out


ROUTINE_ROLES = dict(build='builder', grade='grader', review='reviewer',
                     rebuild='builder', regrade='grader')
ROUTINE_ACTIONS = set(ROUTINE_ROLES)
LAUNCH_ACTIONS = ROUTINE_ACTIONS | {'merge'}


class _PassDispatcher:
    def __init__(self, runner, events, specs, now, dry_run):
        self.runner, self.events, self.specs = runner, events, specs
        self.now, self.dry_run = now, dry_run

    def __call__(self, argv):
        return self.runner(argv)


def launch(step, *, dispatcher):
    """Launch precisely one tick-owned routine Step; failures remain standing."""
    import autodispatch
    import lane_driver
    import nextstep
    import dispatch
    if not isinstance(step, nextstep.Step):
        reason = 'undetermined: unreadable Step'
        dispatch.emit(tick_path(), {'subject': getattr(step, 'item', 'tick')},
                      'autodispatch-failed', reason=reason)
        raise ValueError(reason)
    if step.state != 'action' or step.owner != 'tick' or step.action not in LAUNCH_ACTIONS:
        return None
    if step.reason.startswith('undetermined:'):
        return None
    events = getattr(dispatcher, 'events', None)
    if events is None:
        events = fold.read_events()
    evs = [e for e in events if e.get('subject') == step.item]
    if autodispatch._standing_autodispatch_failed(evs):
        return None
    if getattr(dispatcher, 'dry_run', False):
        return None
    if step.action == 'merge':
        import merge_queue
        try:
            head = merge_queue.next_candidate(events)
        except (ValueError, OSError) as exc:
            print(f'hold undetermined: {exc}')
            return None
        if head is None or step.item_kind == 'spec' and step.item != head:
            print('hold undetermined: merge Step item differs from queue head'
                  if head is not None else 'hold undetermined: no merge candidate')
            return None
        result = dispatcher(['doit', 'window-run', '--project', 'albert-scott', '--detach'])
        if result.get('code', 1):
            raise ValueError(result.get('stderr') or result.get('stdout') or 'merge launch failed')
        return head
    role = ROUTINE_ROLES[step.action]
    try:
        project = next((e['project'] for e in reversed(evs) if e.get('project')), 'unknown')
        charter = dispatch.resolve_charter(evs, step.item, None)
        wt = fold.ROOT / 'worktrees' / project / step.item.lower()
        if step.action == 'build':
            repo = fold.ROOT / 'repos' / project
            result = dispatcher(['git', '-C', str(repo), 'symbolic-ref', '--short', 'HEAD'])
            if result.get('code', 1) or not result.get('stdout', '').strip():
                raise ValueError(result.get('stderr') or 'symbolic-ref failed')
            main = result['stdout'].strip()
            if wt.is_dir():
                result = dispatcher(['git', '-C', str(wt), 'rev-list', '--count', main + '..HEAD'])
                if result.get('code', 1) or result.get('stdout', '').strip() != '0':
                    raise ValueError(f'worktree {wt} exists with commits above {main}; not reusing')
                result = dispatcher(['git', '-C', str(wt), 'merge', '--ff-only', main])
            else:
                result = autodispatch.add_builder_worktree(repo, wt, step.item.lower(), main,
                                                          runner=dispatcher)
            if result.get('code', 1):
                raise ValueError(result.get('stderr') or result.get('stdout') or 'worktree add failed')
        options = lane_driver._review_options(evs, step.item) if step.action == 'review' else None
        if step.action == 'review' and options is None:
            raise ValueError('review round limit')
        if options and options[0] == 'needs-browser':
            raise ValueError('ui review requires review-mcp.json')
        spawn = lane_driver._dispatch(role, step.item, project, charter, dispatcher, options)
        # `doit dispatch --detach` reports the detached pid as an int; a missing one is undetermined.
        if isinstance(spawn, int) and not isinstance(spawn, bool) and spawn > 0:
            spawn = str(spawn)
        if not isinstance(spawn, str) or not spawn:
            raise ValueError('undetermined: missing spawn id')
        refusal = dispatch.emit(tick_path(), {'subject': step.item}, 'autodispatched',
                                role=role, rule=step.action, since=step.since, spawn=spawn)
        if refusal is not None:
            raise ValueError('autodispatched record refused: ' + str(refusal))
        return spawn
    except Exception as exc:
        refusal = dispatch.emit(tick_path(), {'subject': step.item}, 'autodispatch-failed',
                                role=role, rule=step.action, reason=str(exc))
        raise ValueError(str(exc) + (f'; failure record refused: {refusal}' if refusal else '')) from exc


def _steps(events, specs, now):
    """Load the caller-owned table/wave inputs and evaluate one snapshot."""
    import dispatch
    import lane_rules
    import nextstep
    waves = {}
    for sid, spec in specs.items():
        evs = spec.get('evs', [])
        written = next((e for e in reversed(evs)
                        if e.get('type') == 'spec-written' and e.get('footprint')), None)
        charter = dispatch.resolve_charter(evs, sid, None)
        if written and charter:
            waves[sid] = dispatch.spec_wave(charter, written['footprint'])
    return nextstep.next_steps(events, specs, now, lane_rules.load(), waves)


def run_steps(events, specs, now, *, dispatcher=None, dry_run=False, handled=frozenset(), steps=None):
    """Shared entry point; a tick pass supplies its single computed snapshot."""
    import autodispatch
    import lane_driver
    events = list(events)
    if steps is None:
        steps = _steps(events, specs, now)
    runner = _PassDispatcher(dispatcher or autodispatch._real_runner, events, specs, now, dry_run)
    slots = dict(builder=max(0, autodispatch.capacity() - lane_driver._in_flight_role(events, now, 'builder')),
                 reviewer=max(0, lane_driver.capacity() - lane_driver._in_flight_role(events, now, 'reviewer')))
    out = []
    for step in sorted(steps, key=lambda s: (s.since, s.item)):
        if step.item in handled:
            continue
        if step.reason.startswith('undetermined:') or step.state == 'hold':
            out.append(f"{'undetermined' if step.reason.startswith('undetermined:') else 'hold'} {step.item}: {step.reason}")
            continue
        if step.owner != 'tick' or step.action not in LAUNCH_ACTIONS:
            continue
        if step.action == 'merge':
            import merge_queue
            try:
                head = merge_queue.next_candidate(events)
            except (ValueError, OSError) as exc:
                out.append(f'hold undetermined: {exc}')
                continue
            if head is None or step.item_kind == 'spec' and step.item != head:
                out.append('hold undetermined: merge Step item differs from queue head'
                           if head is not None else 'hold undetermined: no merge candidate')
                continue
        role = ROUTINE_ROLES.get(step.action)
        if role in slots and slots[role] <= 0:
            out.append(f'seat-wait {step.item} {step.action}')
            continue
        if dry_run:
            out.append(f'would act {step.item} {step.action}')
            if role in slots:
                slots[role] -= 1
            continue
        try:
            spawn = launch(step, dispatcher=runner)
            if spawn:
                out.append(f'acted {step.item} {step.action} spawn={spawn}')
                if role in slots:
                    slots[role] -= 1
            else:
                out.append(f'hold {step.item}: standing dispatch failure')
        except ValueError as exc:
            out.append(f'failed {step.item} {step.action}: {exc}')
    return out


def _record():
    """One flock-guarded fold+lane+append — the shared core `main()` and
    `wait()` both run under the IDENTICAL non-blocking lock (this module's own
    doctrine: "one at a time per ledger — a dropped duplicate loses nothing,
    the durable list is the queue"). Returns the lane list on a real append,
    `None` when a concurrent tick/wait already holds the lock — the append is
    dropped, not queued (0187 Assumption 5).

    L-spec-0190 R9: `carry.uncarried(ev)` and `carry.sync_v4(ev)` each run once
    per tick, unconditionally (idle or not), each independently exception-guarded
    (fix 8) — one raising call never silences the other's contribution nor the
    tick's own heartbeat. A caught exception adds nothing of its own to the lane;
    the same `tick` event instead carries an additive `carry_error` field naming
    it, never a second event type."""
    fold.EVENTS.mkdir(parents=True, exist_ok=True)
    lock = open(fold.ROOT / "tick.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    ev = fold.read_events()
    # SD11 (L-spec-0242, extended by L-spec-0244 and L-spec-0243): `intake.run(ev)`
    # fires off the FIRST read, its own try/except so a raise never stops the
    # tick's own heartbeat (mirroring `carry_error` below); `notes.run(ev,
    # note_prs)` and `pr_closeout.run(ev, spec_prs)` both follow, each reading
    # its own slice of the SAME `intake.run(ev)` call's return, and both sit on
    # the SAME `intake_error` field, `; `-joined — then the ledger is RE-READ
    # before anything else, so a same-tick `inbound-registered`/
    # `inbound-awaiting`/`inbound-note-listed`/`inbound-note-gone`/
    # `inbound-closed`/`inbound-pr-commented` append is what
    # `carry.uncarried`/`fold.fold`/`lane` see.
    intake_errors = []
    r = {}
    try:
        r = intake.run(ev) or {}
    except Exception as e:
        intake_errors.append(f"run: {e}")
    try:
        notes.run(ev, r.get("note_prs") or [])
    except Exception as e:
        intake_errors.append(f"notes: {e}")
    try:
        import pr_closeout
        pr_closeout.run(ev, r.get("spec_prs", []))
    except Exception as e:
        intake_errors.append(f"pr_closeout: {e}")
    # L-spec-0431/L-charter-0042 R5d: the once-per-tick install-sync pass — agent
    # symlinks, plus any crons.toml row a merged spec's own shipped sha added or
    # changed. The MIDDLE of the three calls the Plan's Seams name (autodispatch,
    # install_sync, relay restart), on this SAME pre-re-read `ev`; its own
    # try/except never skips anything below it. relay restart's own future
    # insertion goes immediately after this, still before the re-read below.
    install_sync_error = None
    install_synced = None
    try:
        import install_sync
        install_synced = len(install_sync.run(ev))
    except Exception as e:
        install_sync_error = f"{e}"
    # L-charter-0042/L-spec-0484, R13(c): the tick's own push pass — "a merge
    # is not complete until origin carries it" — on this SAME pre-re-read
    # `ev`, right after install_sync above (Boundaries) and before the
    # re-read just below. Its own try/except, exactly like `install_sync`'s:
    # a raise never stops the tick's own heartbeat, and is named on this SAME
    # tick event, never silencing anything below it.
    push_origin_error = None
    try:
        import push_origin
        push_origin.run(ev)
    except Exception as e:
        push_origin_error = f"{e}"
    ev = fold.read_events()
    specs, charters, _, _ = fold.fold(ev)
    reaped = {e.get("subject") for e in ev if e["type"] == "tree-reaped"}
    errors = []
    try:
        inbound = carry.uncarried(ev)
    except Exception as e:
        inbound = []
        errors.append(f"uncarried: {e}")
    try:
        carry.sync_v4(ev)
    except Exception as e:
        errors.append(f"sync_v4: {e}")
    # L-spec-0274/R3: a stalled Claude pane says why, and this restarts what
    # can be restarted — its own try/except, on this SAME re-read `ev`, never
    # merged into `carry_error` (a different concern, a different field).
    pane_resume_error = None
    try:
        pane_resume.run(ev)
    except Exception as e:
        pane_resume_error = f"{e}"
    # L-spec-0322/L-charter-0036 R1/SD13/SD22: the tick's own two look-mutual-watch
    # checks — the `look` cron row missing, and `look`'s own last_pass gone stale
    # (>25 min or absent) while the row IS present. Both route through
    # `look.emit_once` (the SAME dedupe path `doit look` itself uses), never a
    # second event type. Lazily imported, guarded exactly like `carry_error`
    # above: an ImportError (module absent pre-merge) or any other exception
    # contributes nothing to the lane, and is named on this SAME `tick` event.
    look_error = None
    try:
        import crons, look
        missing = crons.check() or []
        row = next((r for r in missing if (r.get("name") if isinstance(r, dict) else r) == "look"), None)
        if row is not None:
            look.emit_once("cron-missing", "look", "thinker", row, ev)
        else:
            st = look._read_state(fold.ROOT) or {}
            last_pass = st.get("last_pass")
            stale = last_pass is None or (fold.NOW - fold.ts(last_pass)).total_seconds() > 25 * 60
            if stale:
                look.emit_once("look-stale", "look-stale", "thinker", {"last_pass": last_pass}, ev)
    except Exception as e:
        look_error = f"{e}"
    try:
        import look
        look.heartbeat_missed("look", fold.NOW, ev, root=fold.ROOT, dst=tick_path())
    except Exception as e:
        look_error = f"{look_error}; {e}" if look_error else f"{e}"
    try:
        import up
        relay_status = up.ensure_relay(ev, fold.NOW)
    except Exception:
        relay_status = "undetermined"
    # L-charter-0046/L-spec-0473, R1/R3/R4: the tick's own Proving bookkeeping —
    # automatic entry, close, reopen and the two alarms — on this SAME `specs`/
    # `charters` fold, no extra re-read (Boundaries). Lazily imported and its own
    # try/except, exactly like `look_error` above: a raise anywhere in this block
    # never stops the tick's own heartbeat, and is named on this SAME tick event.
    proving_error = None
    try:
        import proving, look  # noqa: PLC0415 — lazy, matches this file's own convention
        for cid, c in charters.items():
            if c["state"] in ("proving", "reopened"):
                mine = [s for s in specs.values() if s["charter"] == cid and s["state"] != "void"]
                title = next((e.get("title") for e in reversed(c["evs"])
                              if e.get("type") == "charter-filed" and e.get("title")), cid)
                review_owed = fold.charter_review_owed(c["evs"])
                open_briefs = fold.open_briefs(c["evs"])
                summ = proving.summary(cid, c["evs"], mine, fold.NOW, title=title,
                                        review_owed=review_owed, open_briefs=open_briefs)
                if proving.should_enter(c["evs"], mine):
                    next_text = summ["next"]["text"] if summ["next"] is not None else "nothing remaining"
                    dispatch.emit(tick_path(), {}, "charter-proving", subject=cid,
                                  reason=f"{summ['remaining']} item(s) remain; next: {next_text}",
                                  deadline=summ["deadline"])
                for item in proving.to_reopen(c["evs"], mine, fold.NOW):
                    dispatch.emit(tick_path(), {}, "charter-reopened", subject=cid,
                                  spec=item["spec"], criterion=item["criterion"],
                                  failed_src=item["failed_src"])
                    look.emit_once("proving-check-failed",
                                   f"{cid}|{item['spec']}|{item['criterion']}", "thinker",
                                   {"spec": item["spec"], "criterion": item["criterion"],
                                    "failed_src": item["failed_src"]}, ev)
                if fold.ts(summ["deadline"]) < fold.NOW:
                    key = f"{cid}|{summ['deadline']}"
                    fired = any(e.get("type") == "brief" and e.get("condition") == "proving-overdue"
                               and e.get("key") == key for e in ev)
                    if not fired:
                        look.emit_once("proving-overdue", key, "operator",
                                       {"next": (summ["next"]["text"] if summ["next"] is not None else None),
                                        "owner": summ["owner"], "deadline": summ["deadline"]}, ev)
            elif c["state"] == "L2-complete":
                proving_evs = [e for e in c["evs"] if e["type"] == "charter-proving"]
                if proving_evs:
                    newest_proving = max(fold.ts(e["ts"]) for e in proving_evs)
                    closed_evs = [e for e in c["evs"] if e["type"] == "charter-closed"]
                    newest_closed = max((fold.ts(e["ts"]) for e in closed_evs), default=None)
                    if newest_closed is None or newest_closed < newest_proving:
                        label = fold.close_label(ev, cid)
                        if label == "accepted":
                            reason = PROVING_CLOSE_REASON
                        else:
                            pending = ("non-blocking owed checks remain unproven"
                                       if label == "closed-unproven" else
                                       "blocking owed checks remain within configured allowance")
                            reason = (f"{pending}; sweep at fixpoint; no open in-scope brief; "
                                      "charter review complete or not owed")
                        dispatch.emit(tick_path(), {}, "charter-closed", subject=cid,
                                      label=label, reason=reason)
    except Exception as e:
        proving_error = f"{e}"
    # L-charter-0042/L-spec-0427, R1: the tick dispatches. LAST guarded step
    # before the Executor rows are read, on the SAME `ev`/`specs` — no re-read sits
    # between here and Steps (Boundaries), so an event `run()` appends
    # this pass never changes THIS pass's own `todo`. A raise never stops the
    # tick's own heartbeat, exactly like `carry_error` above.
    autodispatch_error = None
    lane_driver_log_error = None
    dispatch_started = time.monotonic()
    def log_outcomes(lines):
        nonlocal lane_driver_log_error
        try:
            _log_lane_driver(lines, fold.NOW)
        except OSError as exc:
            lane_driver_log_error = str(exc)
    log_outcomes(['start autodispatch'])
    try:
        import autodispatch
        import merge_queue
        steps = _steps(ev, specs, fold.NOW)
        outcomes = run_steps(ev, specs, fold.NOW, steps=steps)
        outcomes.extend(autodispatch.redispatch_dead(ev, specs, fold.NOW,
                        handled={s.item for s in steps if s.state == 'action' and s.owner == 'tick'
                                 and s.action in ROUTINE_ACTIONS}))
        outcomes.extend(merge_queue.enqueue_due(ev, fold.NOW))
        log_outcomes(outcomes or [])
        failures = [line for line in outcomes if line.startswith('failed ')]
        if failures:
            autodispatch_error = '; '.join(failures)
    except Exception as e:
        autodispatch_error = f"{e}"
        log_outcomes([f'failed autodispatch: {e}'])
    log_outcomes([f'finished autodispatch elapsed_s={time.monotonic() - dispatch_started:.3f}'])
    # L-charter-0042/L-spec-0486 (cleanup-on-finish), R15a (record): the tick's own
    # per-spec worktree reap, on this SAME `ev` — its own try/except, exactly like
    # `autodispatch_error` above: a raise never stops the tick's own heartbeat, and
    # is named on this SAME `tick` event. The `reaped` set that feeds `lane` above
    # is untouched — `worktree-reaped` never marks a charter reaped.
    worktree_reap_error = None
    worktrees_retained = None
    recorded_reap = []
    try:
        import reap_tmp
        recorded_reap = [vars(row) for row in reap_tmp._reap_recorded(ev)]
        wt_reaped, wt_retained = tree_cleanup.reap_worktrees(ev)
        for row in wt_reaped:
            dispatch.emit(tick_path(), {}, "worktree-reaped", subject=row["spec"],
                          path=row["path"], proof=row["proof"])
        if wt_retained:
            worktrees_retained = len(wt_retained)
    except Exception as e:
        worktree_reap_error = f"{e}"
    try:
        todo = [f"{step.item} · {step.action or 'hold'} · {step.owner} · {fold._step_line(step.reason)}"
                for step in _steps(ev, specs, fold.NOW) if step.owner == 'executor']
    except Exception as exc:
        todo = [f"undetermined: next_steps failed: {type(exc).__name__}"]
    busy = in_flight(ev)
    packet_busy = _carry_packet_busy(busy)
    todo.extend(f"inbound:{item['source']} · uncarried" for item in inbound
                if item.get('source') not in busy and item.get('source') not in packet_busy)
    todo.sort()
    # The one liveness fact: `fold` reads the newest of these for staleness, and
    # `lane` is the count — the whole record this process leaves behind.
    kv = {"lane": len(todo), "relay": relay_status}
    if errors:
        kv["carry_error"] = "; ".join(errors)
    if intake_errors:
        kv["intake_error"] = "; ".join(intake_errors)
    if pane_resume_error:
        kv["pane_resume_error"] = pane_resume_error
    if look_error:
        kv["look_error"] = look_error
    if install_sync_error:
        kv["install_sync_error"] = install_sync_error
    elif install_synced is not None:
        kv["install_synced"] = install_synced
    if push_origin_error:
        kv["push_origin_error"] = push_origin_error
    if autodispatch_error:
        kv["autodispatch_error"] = autodispatch_error
    if lane_driver_log_error:
        kv["lane_driver_log_error"] = lane_driver_log_error
    if recorded_reap:
        kv["recorded_reap"] = recorded_reap
    if worktree_reap_error:
        kv["worktree_reap_error"] = worktree_reap_error
    if worktrees_retained is not None:
        kv["worktrees_retained"] = worktrees_retained
    if proving_error:
        kv["proving_error"] = proving_error
    dispatch.emit(tick_path(), {}, "tick", **kv)
    # A caught sender exception can retain its traceback until cyclic GC.
    # Release the lock explicitly so that traceback cannot drop the next tick.
    lock.close()
    return todo


def main():
    """Fold, compute the lane, append exactly one `tick`. Never a spawn (L-adr-0035)."""
    todo = _record()
    if todo is None:
        print("tick: one is running; this one is dropped")
        return 0
    print("tick: idle" if not todo else f"tick: {len(todo)} on the lane\n" + "\n".join(todo))
    return 0


def _events_state(exclude_name):
    """name -> (size, mtime_ns) for every `events/*.jsonl` file EXCEPT
    `exclude_name` — the exclusion-aware snapshot `wait()` cross-checks a
    positive from `relay.ledger_changed` against, so its OWN appended `tick`
    (on return) can never register as a real change to itself or a concurrent
    waiter (AC16)."""
    d = fold.EVENTS
    out = {}
    if d.is_dir():
        for f in sorted(d.glob("*.jsonl")):
            if f.name == exclude_name:
                continue
            st = f.stat()
            out[f.name] = (st.st_size, st.st_mtime_ns)
    return out


def wait(root=None, max_s=300):
    """Block until the ledger moves or `max_s` seconds pass, then tick exactly
    once and return which happened: `"changed"` or `"interval"`.

    AC17: rebinds `fold.ROOT`/`fold.EVENTS` to `root` FIRST (as
    `pane_end.bind_root` already does), so its own tick lands under the given
    root, not the importing process's DOIT_ROOT.

    AC2: never reports "changed" on its own first look. `relay.ledger_changed`
    reads `watermark=None` as ALWAYS changed (nothing has been seen yet), so a
    baseline is established once, up front, and that first "changed" is
    discarded rather than compared against.

    AC16: `L-tick-local.jsonl` is excluded from what counts as "changed" —
    `relay.ledger_changed`'s hash is the heartbeat this polls, but a positive
    from it is trusted only once a SEPARATE, exclusion-aware snapshot
    (`_events_state`, every `events/*.jsonl` file except the tick file itself)
    confirms a REAL file moved. Otherwise every wake would append a tick that
    wakes the next waiter — a feedback loop."""
    import relay                                             # noqa: PLC0415 — lazy; see the top-of-file note
    _bind_root(root)
    _, mark = relay.ledger_changed(fold.ROOT)               # baseline; its own "changed" is discarded
    excl = _events_state(TICK_NAME)
    end = time.monotonic() + max_s
    reason = "interval"
    while True:
        changed, mark = relay.ledger_changed(fold.ROOT, mark)
        if changed:
            new_excl = _events_state(TICK_NAME)
            if new_excl != excl:
                reason = "changed"
                break
            excl = new_excl          # only the tick file moved — not a real change; keep polling
        if time.monotonic() >= end:
            break
        time.sleep(min(POLL_STEP, max(0.0, end - time.monotonic())))
    _record()                        # exactly one tick per RETURNING call (AC4); a lock-drop loses
    return reason                    # nothing per this module's own doctrine (Assumption 5)


def _wait_cli(argv=None):
    ap = argparse.ArgumentParser(prog="doit wait",
                                 description="Block until the ledger moves or SEC seconds pass, then tick once.")
    ap.add_argument("--max", type=float, default=300.0, dest="max_s",
                    help="max seconds to wait (default 300)")
    a = ap.parse_args(argv)
    print(wait(max_s=a.max_s))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "wait":
        sys.exit(_wait_cli(sys.argv[2:]))
    sys.exit(main())
