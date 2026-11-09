#!/usr/bin/env python3
"""tick — fold the ledger, compute the actionable lane, record one liveness fact.

  tick.py     one tick: fold, compute, record. Scheduled (cron) and poked (every
              dispatch ends with one). One at a time per ledger — flock — and a
              dropped duplicate loses nothing: the durable list is the queue.

A tick starts no Executor, under any backend (L-adr-0035). Its guarded dispatch
passes launch routine roles, including due charter close reviews. The ordinary
tick event is the durable heartbeat and close-review fork receipt. The Executor
is a supervised pane that reads the same lane for itself.
"""
from contextvars import ContextVar
from itertools import chain

import ledger_files
import ledger_cache
import argparse, fcntl, hashlib, json, os, pathlib, re, socket, sys, time
from datetime import datetime, timedelta, timezone
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import carry, dispatch, fold, intake, notes, pane_resume, panes, tree_cleanup  # noqa: E402
# `realm_ledger` is imported LAZILY, inside `wait()` only — importing tick.py must not
# force the directory watcher into sys.modules for every caller,
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
POLL_STEP = 0.25              # tick.wait's snapshot fallback step when notifications fail
NOTIFY_MIN_GAP = 0.02         # bound readable-but-unusable notification wakes
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
# Only the current guarded pass owns these observations and accepted launches.
# Context-local state also covers recovery without changing its public contract.
_PASS = ContextVar('tick_pass', default=None)


def _committed(refused):
    state = _PASS.get()
    if refused is None and state is not None:
        state['wrote'] = True
    return refused


def _committed_brief(brief):
    if brief is not None and _PASS.get() is not None:
        _PASS.get()['wrote'] = True
    return brief


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


def _tick_spawn_ends():
    """Read tick-owned ends through the same ledger route as waiter-owned ends."""
    try:
        text = ledger_files.read_text(fold.EVENTS, tick_path().name)
    except OSError:
        return {}
    ends = {}
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except (ValueError, TypeError):
            continue
        if e.get('type') in ('spawn-done', 'spawn-failed', 'spawn-stale'):
            ends.setdefault(e.get('spawn'), set()).add(e['type'])
    return ends


def _ended_since_read(sid):
    """Honor waiter or tick completions landing after the caller's snapshot."""
    if not sid:
        return False
    if _tick_spawn_ends().get(sid, set()) & {'spawn-done', 'spawn-failed'}:
        return True
    try:
        text = ledger_files.read_text(fold.EVENTS, f"{sid}.jsonl")
    except OSError:
        return False
    return any(f'"{t}"' in text for t in ("spawn-done", "spawn-failed"))


def _spawn_busy(ev):
    """Reconcile vanished attempts once, preserving elapsed-stale live work."""
    import spawns
    ends = _tick_spawn_ends()
    for e in ev:
        if e.get('type') in spawns.TERMINAL:
            ends.setdefault(e.get('spawn'), set()).add(e['type'])
    busy = set()
    for e in spawns.start_events(ev):
        sid = e.get('spawn')
        terminal = ends.get(sid, set()) if sid else set()
        if terminal & {'spawn-done', 'spawn-failed'}:
            continue
        state = spawns.start_state(e, fold.NOW)
        if state == 'vanished':
            if sid and not _ended_since_read(sid):
                fields = dict(subject=e.get('subject'), role=spawns._role(e),
                              waiter_pid=e.get('waiter_pid') or None, since=e.get('ts'),
                              reason='vanished')
                if 'project' in e:
                    fields['project'] = e['project']
                _committed(dispatch.emit(tick_path(), {'spawn': sid}, 'spawn-failed', **fields))
                ends.setdefault(sid, set()).add('spawn-failed')
        elif terminal:
            # Stale remains outside busy, but is not a real terminal for reconciliation.
            continue
        elif state == 'stale':
            if sid and not _ended_since_read(sid):
                cap = dispatch.ROLES.get(spawns._role(e), (None, 60, 0))[1]
                _committed(dispatch.emit(tick_path(), {'spawn': sid}, 'spawn-stale',
                              subject=e.get('subject'), role=spawns._role(e), cap_min=cap))
                ends.setdefault(sid, set()).add('spawn-stale')
        else:
            busy.add(e.get('subject'))
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
    import nextstep
    busy.update(h['subject'] for h,r in nextstep.retry_hold_pairs(ev) if r is None)
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
                     rebuild='builder', regrade='grader', audit='spec-auditor')
ROUTINE_ACTIONS = set(ROUTINE_ROLES)
AUDIT_PASS_MAX = 6
LAUNCH_ACTIONS = ROUTINE_ACTIONS | {'merge', 'merge-direct'}
MERGE_DIRECT_PROBLEM = 'merge-direct-pass-failed'
_PASS_MERGED = []  # (spec, sha) merged by the pass in flight; read by the failure reporter


def _merge_direct(step, events):
    """Run direct_merge.merge_one for one tick-owned merge-direct Step."""
    import direct_merge
    import dispatch
    import merge_queue
    import subprocess
    repo = fold.ROOT / 'repos' / 'do-it-v2'
    row = next((r for r in merge_queue.queue(events) if r.get('spec') == step.item), None)
    if row is None:
        return dict(status='regate', sha=None, detail='queue row no longer live')

    def emit(type_, spec, **kv):
        refused = dispatch.emit(tick_path(), {'subject': spec}, type_, **kv)
        if refused:
            raise ValueError(f'{type_} refused: {refused}')

    return direct_merge.merge_one(step.item, row, repo=repo, gate=direct_merge.cli_gate(repo),
                                  run=subprocess.run, emit=emit, now=fold.NOW)


def _report_merge_pass_failure(merged, exc):
    """One problem-occurred per merged spec when the pass fails after a merge."""
    import dispatch
    for spec, sha in merged:
        try:
            if any(e.get('type') == 'problem-occurred' and e.get('problem') == MERGE_DIRECT_PROBLEM
                   and e.get('subject') == spec and e.get('sha') == sha for e in fold.read_events()):
                continue
            dispatch.emit(tick_path(), {'subject': spec}, 'problem-occurred',
                          problem=MERGE_DIRECT_PROBLEM, sha=sha,
                          detail=f'{type(exc).__name__} after merge of {spec} at {sha}')
        except Exception as report_exc:
            print(f'merge-direct failure report not recorded: {report_exc}', file=sys.stderr)


class _PassDispatcher:
    def __init__(self, runner, events, specs, now, dry_run):
        self.runner, self.events, self.specs = runner, events, specs
        self.now, self.dry_run = now, dry_run

    def __call__(self, argv):
        result = self.runner(argv)
        state = _PASS.get()
        if (state is not None and argv[:2] == ['doit', 'dispatch']
                and result.get('code', 1) == 0):
            # Record acceptance before the synchronous append: a later failure
            # cannot erase a launch, including on the recovery path.
            try:
                accepted = json.loads(result.get('stdout') or '').get('detached')
            except (ValueError, AttributeError):
                accepted = None
            if accepted and '--detach' in argv:
                state['launched'].add(argv[argv.index('--detach') + 2])
        return result


def _launch_events():
    """The launcher and its skip observer share the guarded ledger read."""
    try:
        return fold.read_events()
    except ledger_cache.LockTimeout as exc:
        print(str(exc), file=sys.stderr)
        return None


def _record_held_skips(events, specs, *, reporter=None):
    """Persist only changes from the latest observed skip for each live spec."""
    import autodispatch
    by_subject = {}
    shipped = set()
    for e in events:
        by_subject.setdefault(e.get('subject'), []).append(e)
        if e.get('type') == 'shipped':
            shipped.add(e.get('subject'))
    reasons = {}
    for sid, spec in sorted(specs.items()):
        try:
            if not str(sid).startswith('L-spec-') or spec.get('state') in dispatch.SETTLED_STATES:
                continue
            evs = by_subject.get(sid, [])
            reason = autodispatch._automatic_hold(evs, events, shipped=shipped)
            if reason:
                reasons[sid] = reason
        except Exception as exc:
            if reporter is None:
                raise
            reporter.fail(sid, exc, phase='hold-observer')
    if not reasons:
        return
    latest = {}
    # Recovery reuses the pass snapshot plus only successful observations.
    # Independent direct calls still verify their persisted observations.
    state = _PASS.get()
    persisted = state['skips'] if state is not None else _launch_events()
    if persisted is None:
        return
    observations = [e for e in chain(events, persisted if state is None else ())
                    if e.get('type') == 'autodispatch-skipped' and e.get('actor') == 'tick']
    for e in sorted(observations, key=autodispatch._hold_order):
        latest[e.get('subject')] = e.get('reason')
    if state is not None:
        for event in state['skips']:
            latest[event['subject']] = event['reason']
    for sid, reason in reasons.items():
        if reason and latest.get(sid) != reason:
            refused = _committed(dispatch.emit(tick_path(), {'subject': sid}, 'autodispatch-skipped',
                                    strict=True, reason=reason))
            if refused:
                raise ValueError('autodispatch-skipped record refused: ' + str(refused))
            if state is not None:
                state['skips'].append(dict(type='autodispatch-skipped', actor='tick',
                                           subject=sid, reason=reason, ts=dispatch.now()))


HOLD_REASONS = {}  # item -> reason of the latest fresh-check hold, read by run_steps


def _fresh_hold(step, dispatcher):
    """Re-read the ledger right before preparation; True means do not start."""
    import attempts
    import bundle_wait
    import nextstep
    import project_scope
    import start_check
    HOLD_REASONS.pop(step.item, None)
    fresh = _launch_events()
    if fresh is None:
        print(f'hold {step.item}: fresh ledger read failed; not preparing', file=sys.stderr)
        HOLD_REASONS[step.item] = 'undetermined: fresh ledger read failed'
        return True
    role, now = ROUTINE_ROLES[step.action], getattr(dispatcher, 'now', fold.NOW)
    main = None
    if role == 'grader':
        project = project_scope.subject_project(fresh, step.item)
        mains = getattr(dispatcher, 'mains', None) or {}
        main = mains[project] if project in mains else (
            bundle_wait.project_main(project, root=fold.ROOT) if project else None)
    refused = start_check.refusal(role, step.item, fresh, now=now, root=fold.ROOT, main_sha=main)
    if refused is not None:
        HOLD_REASONS[step.item] = 'not-ready:' + refused.reason
        return True
    own = [e for e in fresh if e.get('subject') == step.item]
    if attempts.history(nextstep._grader_history(own)[0] if role == 'grader' else own,
                        dict(subject=step.item, ts=step.since), role, now=now).live:
        HOLD_REASONS[step.item] = 'not-ready:duplicate-dispatch'
        return True
    return False


def launch(step, *, dispatcher):
    """Launch precisely one tick-owned routine Step; failures remain standing."""
    import autodispatch
    import lane_driver
    import nextstep
    import dispatch
    if not isinstance(step, nextstep.Step):
        reason = 'undetermined: unreadable Step'
        _committed(dispatch.emit(tick_path(), {'subject': getattr(step, 'item', 'tick')},
                      'autodispatch-failed', reason=reason))
        raise ValueError(reason)
    if step.state != 'action' or step.owner != 'tick' or step.action not in LAUNCH_ACTIONS:
        return None
    if step.reason.startswith('undetermined:'):
        return None
    events = getattr(dispatcher, 'events', None)
    if events is None:
        events = _launch_events()
        if events is None:
            return None
    if step.action == 'merge-direct':
        return _merge_direct(step, events)
    evs = [e for e in events if e.get('subject') == step.item]
    base_refresh = False
    if step.action == 'build' and step.reason.startswith('base-refresh:'):
        # Prose on a fabricated Step cannot grant the existing-worktree exception.
        import lane_rules
        current = nextstep.step_for(events, getattr(dispatcher, 'specs', None)
                                    or {step.item: {'state': 'written'}}, step.item,
                                    getattr(dispatcher, 'now', fold.NOW), lane_rules.load())
        base_refresh = (current.state == 'action' and current.owner == 'tick'
                        and current.action == 'build' and current.reason.startswith('base-refresh:')
                        and (current.trigger, current.since) == (step.trigger, step.since))
        if not base_refresh:
            return None
    if any(release is None for hold,release in nextstep.retry_hold_pairs(evs)):
        return None
    if step.action in ('build', 'rebuild') and autodispatch._automatic_hold(evs, events):
        if not getattr(dispatcher, 'dry_run', False):
            _record_held_skips(events, {step.item: {'state': 'written'}})
        return None
    if step.action != 'audit':
        failure = autodispatch._standing_autodispatch_failed(evs)
        # Only a due retry or a newer trigger can release a standing failure.
        if failure and not (step.action in ('grade', 'regrade')
                            and nextstep._attempt_role(failure) == 'grader'
                            and (step.reason.startswith('retry due:')
                                 or nextstep._time(step.since) > nextstep._time(failure['ts']))):
            return None
    if step.action in ROUTINE_ROLES and step.action != 'audit':
        import attempts
        role = ROUTINE_ROLES[step.action]
        if not base_refresh and any(e.get('type') in {'spawn-failed','autodispatch-failed'} and nextstep._attempt_role(e) == role
               and nextstep._time(e['ts']) >= nextstep._time(step.since) for e in evs):
            history = attempts.history(nextstep._grader_history(evs)[0] if role == 'grader' else evs,
                dict(subject=step.item,ts=step.since),role,now=getattr(dispatcher,'now',fold.NOW))
            if (history.consumed or history.live or history.exhausted or history.not_ready
                    or history.next_retry_at and history.next_retry_at > getattr(dispatcher,'now',fold.NOW)):
                return None
    options = lane_driver._review_options(evs, step.item) if step.action == 'review' else None
    if step.action == 'review':
        import review_browser
        if review_browser.hold(step, options, dry_run=getattr(dispatcher, 'dry_run', False)):
            return None
    if getattr(dispatcher, 'dry_run', False):
        return None
    if step.action in ROUTINE_ROLES and step.action != 'audit' and _fresh_hold(step, dispatcher):
        return None
    if step.action == 'merge':
        import merge_queue
        try:
            head = merge_queue.next_candidate(events)
            refill = merge_queue.refill_candidate(events)
        except (ValueError, OSError) as exc:
            print(f'hold undetermined: {exc}')
            return None
        # A rehearsal refill may start while a preceding batch is still in flight.
        head = head if head is not None else refill
        if head is None or step.item_kind == 'spec' and step.item not in (head, refill):
            print('hold undetermined: merge Step item differs from queue head'
                  if head is not None else 'hold undetermined: no merge candidate')
            return None
        result = dispatcher(['doit', 'window-run', '--project', 'albert-scott', '--detach'])
        if result.get('code', 1):
            raise ValueError(result.get('stderr') or result.get('stdout') or 'merge launch failed')
        return head
    role = ROUTINE_ROLES[step.action]
    heal = None
    try:
        if step.action == 'audit':
            if not re.fullmatch(r'L-spec-\d+', step.item):
                raise ValueError('invalid audit spec id')
            project = next((e['project'] for e in reversed(evs) if e.get('project')), 'unknown')
            if not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', project):
                raise ValueError('invalid audit project')
            charter = dispatch.resolve_charter(evs, step.item, None)
            packet = dispatcher(['doit', 'packet', role, step.item])
            if packet.get('code', 1):
                raise ValueError(packet.get('stderr') or packet.get('stdout') or 'packet failed')
            result = dispatcher(['doit', 'dispatch', '--detach', role, step.item,
                '--packet', packet.get('stdout', '').strip(),
                '--cwd', str(fold.ROOT / 'repos' / project), '--charter', charter or '', '--project', project])
            if result.get('code', 1):
                raise ValueError(result.get('stderr') or result.get('stdout') or 'dispatch failed')
            spawn = json.loads(result.get('stdout') or '')['detached']
            if isinstance(spawn, int) and not isinstance(spawn, bool) and spawn > 0:
                spawn = str(spawn)
            if not isinstance(spawn, str) or not spawn:
                raise ValueError('undetermined: missing spawn id')
            refusal = _committed(dispatch.emit(tick_path(), {'subject': step.item}, 'autodispatched',
                                    role=role, rule='audit', since=step.since, spawn=spawn))
            if refusal is not None:
                raise ValueError('autodispatched record refused: ' + str(refusal))
            return spawn
        try:
            import worktree_heal
        except Exception:
            pass  # A broken optional repair module must preserve legacy dispatch.
        else:
            if worktree_heal.enabled(fold.ROOT):
                heal = worktree_heal
        project = (heal.project_of(evs, fold.ROOT) if heal else
                   next((e['project'] for e in reversed(evs) if e.get('project')), 'unknown'))
        charter = dispatch.resolve_charter(evs, step.item, None)
        wt = fold.ROOT / 'worktrees' / project / step.item.lower()
        if step.action == 'build':
            repo = fold.ROOT / 'repos' / project
            import packet
            base = packet._effective_base(repo, project, dispatch.spec_path(step.item), step.item)
            if wt.is_dir() and base_refresh:
                # Detached preflight owns all Git safety checks and recutting.
                result = {'code': 0}
            elif wt.is_dir():
                result = dispatcher(['git', '-C', str(wt), 'rev-list', '--count', base + '..HEAD'])
                if result.get('code', 1) or result.get('stdout', '').strip() != '0':
                    raise ValueError(f'worktree {wt} exists with commits above {base}; not reusing')
                result = dispatcher(['git', '-C', str(wt), 'merge', '--ff-only', base])
            else:
                result = autodispatch.add_builder_worktree(repo, wt, step.item.lower(), base,
                                                          runner=dispatcher)
            if result.get('code', 1):
                raise ValueError(result.get('stderr') or result.get('stdout') or 'worktree add failed')
        if step.action == 'review' and options is None:
            raise ValueError('review round limit')
        fresh = heal.prepare_fresh(step, evs, fold.ROOT, project, wt, dispatcher) if heal else None
        recreated = fresh is not None
        if fresh is None and heal and step.action in ('rebuild', 'grade', 'regrade', 'review'):
            recreated = heal.repair(step.item, step.action, evs, fold.ROOT, project, wt, dispatcher)
        spawn = lane_driver._dispatch(role, step.item, project, charter, dispatcher, options,
                                      **({'base_sha': fresh} if fresh is not None else {}))
        # `doit dispatch --detach` reports the detached pid as an int; a missing one is undetermined.
        if isinstance(spawn, int) and not isinstance(spawn, bool) and spawn > 0:
            spawn = str(spawn)
        if not isinstance(spawn, str) or not spawn:
            raise ValueError('undetermined: missing spawn id')
        refusal = _committed(dispatch.emit(tick_path(), {'subject': step.item}, 'autodispatched',
                                role=role, rule=step.action, since=step.since, spawn=spawn,
                                **({'worktree_recreated': True} if recreated else {})))
        if refusal is not None:
            raise ValueError('autodispatched record refused: ' + str(refusal))
        return spawn
    except Exception as exc:
        refusal = _committed(dispatch.emit(tick_path(), {'subject': step.item}, 'autodispatch-failed',
                                role=role, rule=step.action, reason=str(exc)))
        raise ValueError(str(exc) + (f'; failure record refused: {refusal}' if refusal else '')) from exc


def _steps(events, specs, now):
    """Load the caller-owned table/wave inputs and evaluate one snapshot."""
    import dispatch
    import lane_rules
    import nextstep
    waves, failed = {}, []
    usable = dict(specs)
    for sid, spec in specs.items():
        try:
            evs = spec.get('evs', [])
            written = next((e for e in reversed(evs)
                            if e.get('type') == 'spec-written' and e.get('footprint')), None)
            charter = dispatch.resolve_charter(evs, sid, None)
            if written and charter:
                waves[sid] = dispatch.spec_wave(charter, written['footprint'])
        except Exception as exc:
            usable.pop(sid, None)
            own = [e for e in events if e.get('subject') == sid]
            failed.append(nextstep._spec_exception(sid, own, exc))
    import bundle_wait
    mains = bundle_wait.project_mains(events, usable, root=fold.ROOT)
    planned = nextstep.next_steps(events, usable, now, lane_rules.load(), waves, mains)
    return sorted(planned + failed, key=lambda step: (step.item_kind, step.item))


def _record_retry_exhaustion(step, events, now):
    import nextstep
    import attempts
    match = re.match(r'retries-exhausted: (builder|grader|reviewer);', step.reason)
    if not match:
        return None
    role = match.group(1)
    own = [e for e in events if e.get('subject') == step.item]
    trigger = next((e for e in own if nextstep._source(e) == step.trigger), None)
    if trigger is None or not step.trigger:
        return 'failed exhaustion: governing trigger unavailable'
    history = attempts.history(nextstep._grader_history(own)[0] if role == 'grader' else own,
                               dict(trigger, subject=step.item), role, now=now)
    if not history.exhausted:
        return None
    # The caller already supplied the raw historical snapshot. Only this
    # writer's newly appended rows need a fresh read for same-pass dedup.
    persisted = []
    path = tick_path()
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    for number,line in enumerate(lines,1):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event,dict):
            persisted.append(dict(event,actor=fold.actor_of(path.name),_src=f'{path.name}:{number}'))
    if any(e.get('type') == 'escalation-blocking' and e.get('kind') == 'retries-exhausted'
           and (e.get('subject'),e.get('role'),e.get('ref')) == (step.item,role,step.trigger)
           and nextstep._admitted_actor(e) in {'tick','executor','thinker','operator'}
           for e in list(events)+persisted):
        return None
    reason = _committed(dispatch.emit(tick_path(), {'subject':step.item}, 'escalation-blocking', strict=True,
        kind='retries-exhausted', owner='thinker', role=role, reasons=list(history.reasons), ref=step.trigger,
        default='leave the action stopped pending a human decision',
        deadline=(now+timedelta(hours=24)).isoformat(),
        revert='no automatic action was taken; nothing to revert'))
    return ('failed durable exhaustion: '+str(reason) if reason else 'escalated '+step.item+' retries-exhausted')


def priority_items(root=None):
    """Operator-ordered items that take seats before everything else waiting:
    one item id per line in state/build-priority, `#` starts a comment."""
    try:
        text = ((root or fold.ROOT) / 'state' / 'build-priority').read_text()
    except OSError:
        return {}
    items = [line.split('#')[0].strip() for line in text.splitlines()]
    return {item: rank for rank, item in enumerate(i for i in items if i)}


def visit_key(step, grades_first, first):
    grade = (step.action == 'grade' and step.owner == 'tick' and step.state != 'hold'
             and not step.reason.startswith('undetermined:'))
    return (step.action != 'window-default', step.item not in first, first.get(step.item, 0),
            not grade if grades_first else False, step.since, step.item)


def run_steps(events, specs, now, *, dispatcher=None, dry_run=False, handled=frozenset(), steps=None, admission: dict | None = None, seat_state: dict | None = None):
    """Shared entry point; a tick pass supplies its single computed snapshot."""
    import autodispatch
    import lane_driver
    events = events if isinstance(events, list) else list(events)
    by_subject = {}
    for e in events:
        by_subject.setdefault(e.get('subject'), []).append(e)
    import dispatch_isolation
    reporter = None if dry_run else dispatch_isolation.Pass(events, now)
    if not dry_run:
        _record_held_skips(events, specs, reporter=reporter)
    if steps is None:
        steps = _steps(events, specs, now)
    runner = _PassDispatcher(dispatcher or autodispatch._real_runner, events, specs, now, dry_run)
    in_flight = {role: lane_driver._in_flight_role(events, now, role)
                 for role in ('builder', 'reviewer', 'grader')}
    slots = dict(builder=max(0, autodispatch.capacity() - in_flight['builder']),
                 reviewer=max(0, lane_driver.capacity() - in_flight['reviewer']))
    gate, gate_errors = None, []
    if not dry_run:
        try:
            import loadgate
            gate = loadgate.begin(running=in_flight, allow_enforce=admission is not None)
        except Exception as exc:
            if admission is not None:
                admission['admission_error'] = str(exc)[:160]
    out = []
    audit_launches = 0
    gate_disabled = False
    merge_direct_done = False
    merged = _PASS_MERGED
    merged.clear()
    try:
        try:
            grades_first = bool(gate is not None and gate.grades_first)
        except Exception:
            grades_first = False
        first = priority_items()
        def visit_order(step):
            return visit_key(step, grades_first, first)
        for step in sorted(steps, key=visit_order):
            try:
                if reporter and step.item in reporter.failed:
                    out.append(f'failed {step.item}: hold evaluation failed')
                    continue
                if reporter and 'evaluation failed:' in step.reason:
                    reporter.fail(step.item, step.reason, phase='planning')
                if step.item in handled:
                    continue
                if step.action == 'window-default' and step.owner == 'tick':
                    applied = __import__('window_policy').apply_default(
                        step.item, step.trigger, now, dry_run=dry_run)
                    if applied:
                        out.append(f"{'would act' if dry_run else 'acted'} {step.item} window-default")
                    continue
                if __import__('empty_grade').complete(step, events, dry_run=dry_run):
                    out.append(f"{'would act' if dry_run else 'acted'} {step.item}: zero grader criteria; automatic pass")
                    continue
                if not dry_run:
                    try:
                        exhaustion = _record_retry_exhaustion(step, events, now)
                    except Exception as exc:
                        exhaustion = 'failed durable exhaustion: ' + str(exc)
                    if exhaustion:
                        out.append(exhaustion)
                evs = by_subject.get(step.item, [])
                hold = autodispatch._automatic_hold(evs, events)
                if hold and step.action in ('build', 'rebuild'):
                    out.append(f'hold {step.item}: {hold}')
                    continue
                if not dry_run:
                    __import__('bundle_wait').run(step, events, now, root=fold.ROOT, dst=tick_path())
                if step.reason.startswith('undetermined:') or step.state == 'hold':
                    out.append(f"{'undetermined' if step.reason.startswith('undetermined:') else 'hold'} {step.item}: {step.reason}")
                    continue
                if step.owner != 'tick' or step.action not in LAUNCH_ACTIONS:
                    continue
                if step.action == 'review':
                    import review_browser
                    reason = review_browser.hold(step, lane_driver._review_options(evs, step.item),
                                                 dry_run=dry_run)
                    if reason:
                        out.append(f'hold {step.item}: {reason}')
                        continue
                if step.action == 'merge-direct':
                    # A merge takes no builder seat; one merge-direct merge per pass.
                    if merge_direct_done:
                        out.append(f'merge-wait {step.item}')
                    elif dry_run:
                        out.append(f'would act {step.item} merge-direct')
                    else:
                        result = launch(step, dispatcher=runner)
                        status = result.get('status') if isinstance(result, dict) else None
                        if status == 'shipped':
                            merge_direct_done = True
                            merged.append((step.item, result.get('sha')))
                            out.append(f"acted {step.item} merge-direct shipped sha={result.get('sha')}")
                        else:
                            if status == 'merge-failed':
                                merge_direct_done = True
                            out.append(f"hold {step.item}: merge-direct {status or 'standing dispatch failure'}")
                    continue
                if step.action == 'merge':
                    import merge_queue
                    try:
                        head = merge_queue.next_candidate(events)
                        refill = merge_queue.refill_candidate(events)
                    except (ValueError, OSError) as exc:
                        out.append(f'hold undetermined: {exc}')
                        continue
                    head = head if head is not None else refill
                    if head is None or step.item_kind == 'spec' and step.item not in (head, refill):
                        out.append('hold undetermined: merge Step item differs from queue head'
                                   if head is not None else 'hold undetermined: no merge candidate')
                        continue
                role = ROUTINE_ROLES.get(step.action)
                if step.action == 'audit' and audit_launches >= AUDIT_PASS_MAX:
                    out.append(f'seat-wait {step.item} audit')
                    continue
                if role in slots and slots[role] <= 0:
                    out.append(f'seat-wait {step.item} {step.action}')
                    continue
                if gate is not None and not gate_disabled:
                    try:
                        decision = gate.check(step.item, role or 'window_gate', action=step.action, since=step.since)
                        if decision.hold:
                            out.append(f'resource-wait {step.item} {step.action}')
                            continue
                        if decision.override:
                            detail = decision.override
                            if detail == 'max-wait':
                                detail += f" waited_s={gate.pending_override['waited_s']}"
                            out.append(f'resource-escape {step.item} {step.action} {detail}')
                    except Exception as exc:
                        gate_errors.append('check: ' + str(exc))
                        gate_disabled = getattr(gate, 'mode', 'observe') == 'enforce'
                if dry_run:
                    out.append(f'would act {step.item} {step.action}')
                    if role in slots:
                        slots[role] -= 1
                    continue
                try:
                    spawned, raised = True, True  # Any exception may follow a started process.
                    try:
                        if step.action == 'audit':
                            audit_launches += 1
                        spawn = launch(step, dispatcher=runner)
                        spawned, raised = bool(spawn), False
                    finally:
                        if gate is not None and not gate_disabled:
                            try:
                                gate.note_launch(step.item, spawned, raised=raised)
                            except Exception as exc:
                                gate_errors.append('note_launch: ' + str(exc))
                                gate_disabled = getattr(gate, 'mode', 'observe') == 'enforce'
                    if spawn:
                        if _PASS.get() is not None:
                            _PASS.get()['launched'].add(step.item)
                        out.append(f'acted {step.item} {step.action} spawn={spawn}')
                        if role in slots:
                            slots[role] -= 1
                    else:
                        out.append(f'hold {step.item}: ' + (HOLD_REASONS.pop(step.item, None)
                                                       or 'standing dispatch failure'))
                except ValueError as exc:
                    if reporter:
                        reporter.fail(step.item, exc, phase='launch')
                    out.append(f'failed {step.item} {step.action}: {exc}')
            except Exception as exc:
                if merged:
                    _report_merge_pass_failure(merged, exc)
                if reporter:
                    reporter.fail(step.item, exc, phase='dispatch')
                detail = ': ' + dispatch._failure_detail(exc) if isinstance(exc, ValueError) else ''
                out.append(f'failed {step.item} {step.action}: {type(exc).__name__}{detail}')
    except Exception as exc:
        if merged:
            _report_merge_pass_failure(merged, exc)
        raise
    finally:
        if gate is not None:
            try:
                fields = gate.finish()
            except Exception as exc:
                fields = {'admission_error': 'finish: ' + str(exc)}
            if gate_errors:
                fields['admission_error'] = '; '.join(
                    ([fields['admission_error']] if fields.get('admission_error') else [])
                    + gate_errors)[:160]
            if admission is not None:
                admission.update(fields)
        if reporter:
            try:
                reporter.finish()
            except OSError as exc:
                # Bookkeeping failure must not turn completed launches into a
                # failed whole pass; the heartbeat retains this diagnostic.
                out.append(f'failed autodispatch item accounting: {type(exc).__name__}')
    # Publish only a completed pass's existing accounting. A caller must not
    # reconstruct seats from starts: accepted launches may not have started yet.
    if seat_state is not None:
        seat_state['builder'] = slots['builder']
    return out


def handled_items(steps):
    """Keep audit retries exclusively under the bounded next-step decision."""
    return {s.item for s in steps
            if s.item_kind == 'spec' or (s.state == 'action' and s.owner == 'tick' and s.action in ROUTINE_ACTIONS)
            or (s.state == 'hold' and s.reason.startswith(
                ('audit-wait:', 'audit-exhausted:', 'audit-bad-cut:')))}


def _reap_summary(rows, *, not_reapable, manifest) -> dict | None:
    """Bound the tick's reaper report while retaining kinds and reason classes."""
    from collections import Counter, defaultdict

    def kind_key(kind):
        kind = str(kind)[:40]
        return "other(kind)" if kind == "other" else kind

    def capped(counts, *, actions=False):
        if len(counts) <= 16:
            return dict(counts)
        total = (lambda v: sum(v.values())) if actions else (lambda v: v)
        ranked = sorted(counts, key=lambda k: (-total(counts[k]), k))
        kept = {k: counts[k] for k in ranked[:15]}
        if actions:
            rest = Counter()
            for k in ranked[15:]:
                rest.update(counts[k])
            kept["other"] = dict(rest)
        else:
            kept["other"] = sum(counts[k] for k in ranked[15:])
        return kept

    by_action, by_reason = Counter(), Counter()
    by_kind = defaultdict(Counter)
    n = 0
    for row in rows:
        n += 1
        kind = kind_key(row.kind)
        by_action[row.action] += 1
        by_kind[kind][row.action] += 1
        tokens = [token.strip("'\"`,:;()") for token in row.reason.split()]
        reason = " ".join("<path>" if token.startswith("/") else token for token in tokens)
        reason = re.sub(r"\d+", "#", reason)[:60]
        by_reason[kind + ":" + reason] += 1
    rootless = Counter()
    for kind, count in (not_reapable or {}).items():
        if count:
            rootless[kind_key(kind)] += count
    if not n and not rootless and not any((manifest or {}).values()):
        return None
    result = dict(n=n, by_action=dict(by_action),
                  by_kind=capped({k: dict(v) for k, v in by_kind.items()}, actions=True),
                  by_reason=capped(by_reason))
    if not_reapable is not None:
        result["not_reapable"] = capped(rootless)
    if manifest is not None:
        result["manifest"] = dict(manifest)
    return result


def rework_waits(events, now, free_slots):
    """Observe unresolved builder waits in reader order, without side effects."""
    import lane_driver
    import lane_rules
    import nextstep
    if free_slots <= 0:
        return []
    valid = []
    for event in events:
        try:
            stamp = datetime.fromisoformat(event['ts'])
            if stamp.tzinfo is None:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        valid.append(event)
    rules = lane_rules.load()
    assignments = {e.get('spawn'): e.get('subject') for e in valid
                   if e.get('type') == 'autodispatched' and e.get('spawn')}
    pending, settled, escalated = {}, set(), set()
    resets = {'build-started', 'branch-recut', 'shipped', 'spec-killed', 'spec-closed',
              'escalation-blocking'}
    failures = {'spawn-failed', 'autodispatch-failed', 'merge-gate-undetermined',
                'merge-gate-rework', 'build-blocked'}
    for event in valid:
        subject, kind = event.get('subject'), event.get('type')
        if kind == 'build-started' and not subject:
            subject = assignments.get(event.get('spawn'))
        if not isinstance(subject, str) or not re.fullmatch(r'L-spec-\d+', subject):
            continue
        if kind == 'spec-written':
            settled.discard(subject)
        if kind in {'decision', 'unblocked'}:
            escalated.discard(subject)
        if kind in {'shipped', 'spec-killed', 'spec-closed'}:
            settled.add(subject)
        escalate = kind == 'conflict-rework' and event.get('verdict') == 'escalate'
        if kind == 'escalation-blocking' or escalate:
            escalated.add(subject)
        if kind in resets or escalate or kind == 'verdict' and event.get('confirmed') is True:
            pending.pop(subject, None)
            continue
        if subject in settled or subject in escalated or subject in pending:
            continue
        trigger = (kind == 'conflict-rework' and not event.get('reason')
                   or kind == 'verdict' and event.get('confirmed') is False)
        if kind in failures:
            trigger = nextstep._failure([event], rules)[1] == 'base-refresh'
        # Fresh same-pass starts/resolutions may follow the pass's clock. They
        # still discharge waits; only future triggers must not start one.
        if trigger and datetime.fromisoformat(event['ts']) <= now:
            pending[subject] = event

    # _in_flight_role deliberately has no subject argument. Retain starts for
    # this spec and *all* their terminal receipts, even spawn-subject terminals.
    starts = {}
    ends = {}
    for event in valid:
        if event.get('type') in {'spawn-started', 'build-started'}:
            subject = event.get('subject') or assignments.get(event.get('spawn'))
            if subject:
                starts.setdefault(subject, []).append(dict(event, subject=subject))
        if event.get('type') in {'spawn-done', 'spawn-failed', 'spawn-stale'}:
            ends.setdefault(event.get('spawn'), []).append(event)
    candidates = []
    for subject, event in sorted(pending.items()):
        wait = (now - datetime.fromisoformat(event['ts'])).total_seconds()
        if wait <= 1800:
            continue
        projection = starts.get(subject, [])
        spawn_ids = {e.get('spawn') for e in projection if e.get('spawn')}
        projection = projection + [e for spawn in spawn_ids for e in ends.get(spawn, [])]
        if lane_driver._in_flight_role(projection, now, 'builder'):
            continue
        identity = ['rework-wait-v1', subject, event['type'], event['ts'],
                    event.get('spawn') or '', event.get('reason') or '',
                    event.get('verdict') or '', event.get('confirmed'), event.get('attempt')]
        trigger = hashlib.sha256(json.dumps(identity, separators=(',', ':'),
                                           ensure_ascii=True).encode('utf-8')).hexdigest()
        candidates.append(dict(subject=subject, trigger=trigger,
                               trigger_source=event.get('_src') or event.get('src'),
                               trigger_type=event['type'], since=event['ts'],
                               wait_seconds=wait, free_slots=free_slots))
    return candidates


def _record_rework_waits(events, steps, now, free_slots, outcomes=()):
    """Use the post-dispatch snapshot, existing holds and the durable look door."""
    import autodispatch
    import look
    candidates = rework_waits(events, now, free_slots)
    state = _PASS.get()
    if state is not None and state.get('observer_excluded'):
        candidates = [row for row in candidates if row['subject'] not in state['observer_excluded']]
    by_subject = {}
    for event in events:
        by_subject.setdefault(event.get('subject'), []).append(event)
    by_step = {step.item: step for step in steps}
    summary = dict(eligible=len(candidates), held=0, held_reasons={}, observed=0)

    def emitted(candidate):
        condition = 'rework-wait:' + candidate['trigger']
        # The shared snapshot supplies archived ledger evidence. Read the current
        # look source afresh as well, so retries with a stale snapshot see appends.
        persisted = list(events)
        try:
            text = ledger_files.read_text(fold.EVENTS, look.LEDGER_NAME)
        except FileNotFoundError:
            text = ''
        for line in text.splitlines():
            try:
                persisted.append(json.loads(line))
            except ValueError:
                continue
        for event in persisted:
            if event.get('type') != 'brief' or event.get('condition') != condition:
                continue
            if event.get('key') == candidate['subject']:
                return True
            if any(item.get('key') == candidate['subject']
                   and item.get('reading', {}).get('trigger') == candidate['trigger']
                   for item in event.get('items', [])):
                return True
        return False

    for candidate in candidates:
        subject = candidate['subject']
        step = by_step.get(subject)
        base_hold = step is not None and step.reason.startswith('base-refresh:')
        reason = None
        if autodispatch._automatic_hold(by_subject.get(subject, []), events):
            reason = 'automatic-hold'
        elif step is not None and (step.state == 'hold' or step.reason.startswith('undetermined:')) and not base_hold:
            reason = 'step-hold'
        elif any(line.startswith((f'acted {subject} ', f'would act {subject} ')) for line in outcomes):
            reason = 'dispatched'
        elif any(line.startswith((f'resource-wait {subject} ', f'hold {subject}:',
                                  f'undetermined {subject}:')) for line in outcomes) and not base_hold:
            reason = 'pass-hold'
        if reason:
            summary['held'] += 1
            summary['held_reasons'][reason] = summary['held_reasons'].get(reason, 0) + 1
            continue
        if not emitted(candidate):
            _committed_brief(look.emit_once('rework-wait:' + candidate['trigger'], subject, 'thinker',
                           candidate, events, root=fold.ROOT))
            summary['observed'] += 1
    return summary


def _record_next_step_held(events, specs, steps, now):
    """Observe the computed lane using fresh assignment evidence, never launch.

    The subject namespace keeps these facts out of lifecycle clocks. Only real
    starts (including a replay joined by spawn id) can end a no-run interval.
    """
    import spawns
    by_subject, ends, assignments = {}, {}, {}
    for event in events:
        by_subject.setdefault(event.get('subject'), []).append(event)
        if event.get('type') in spawns.TERMINAL:
            ends.setdefault(event.get('spawn'), []).append(event)
        if event.get('type') == 'autodispatched' and event.get('spawn'):
            assignments[event['spawn']] = event.get('subject')
    starts = spawns.start_events(events)
    live = spawns.open_spawns(events, now)
    for step in steps:
        if (step.item_kind != 'spec' or step.item not in specs
                or specs[step.item]['state'] in dispatch.SETTLED_STATES):
            continue
        evs = by_subject.get(step.item, [])
        builds = [e for e in evs if e.get('type') == 'build-done']
        if not builds:
            continue
        build = max(builds, key=lambda e: fold.ts(e['ts']))
        since = fold.ts(build['ts'])
        def follows(run):
            subject = run.get('subject') or assignments.get(run.get('spawn'))
            return (subject == step.item and fold.ts(run['ts']) >= fold.ts(build['ts'])
                    and (not build.get('spawn') or run.get('spawn') != build['spawn']))
        if any(follows(run) for run in live):
            continue
        for run in starts:
            if not follows(run):
                continue
            terminal = ends.get(run.get('spawn'), []) if run.get('spawn') else []
            if terminal:
                ended = min(fold.ts(e['ts']) for e in terminal)
            else:
                # Same expiry as open_spawns. Reconciliation normally provides
                # a terminal row, but elapsed legacy starts need a stable clock.
                cap = dispatch.ROLES.get(spawns._role(run), (None, 60, 0))[1]
                ended = fold.ts(run['ts']) + timedelta(minutes=(run.get('window_min') or cap) + cap)
            since = max(since, ended)
        if (now - since).total_seconds() < 900:
            continue
        held = [e for e in by_subject.get('held:' + step.item, [])
                if e.get('type') == 'next-step-held' and e.get('spec') == step.item
                and fold.admits(e, e.get('actor', '')) is None
                and fold.ts(e['since']) == since]
        # read_events preserves append order for same-file timestamp ties.
        if held and held[-1]['reason'] == step.reason:
            continue
        refusal = _committed(dispatch.emit(tick_path(), {}, 'next-step-held',
                                subject='held:' + step.item, spec=step.item,
                                reason=step.reason, owner=step.owner, since=since.isoformat()))
        if refusal is not None:
            raise RuntimeError(refusal)


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
    token = _PASS.set(dict(skips=[], launched=set(), wrote=False))
    try:
        return _record_pass()
    finally:
        _PASS.reset(token)
        lock.close()


def _record_pass():
    """The locked pass: at most two whole-ledger loads, one planning attempt."""
    snapshot = fold.ReaderSnapshot()
    try:
        # Take the marker BEFORE the body load: a racing append can only make
        # the pre-dispatch check conservative, never certify an older view.
        generation = ledger_files.stat_keys(fold.EVENTS)
        ev = fold.read_events(snapshot=snapshot)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return None
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
    # A2: reconcile the held events first; intake and vanished-failure rows
    # become visible together in the only possible pre-dispatch refresh.
    _spawn_busy(ev)
    loads = 1
    try:
        if ledger_files.stat_keys(fold.EVENTS) != generation:
            ev = fold.read_events(snapshot=snapshot)
            loads += 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return None
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
    # (>40 min or absent) while the row IS present. Both route through
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
            _committed_brief(look.emit_once("cron-missing", "look", "thinker", row, ev))
        else:
            st = look._read_state(fold.ROOT) or {}
            last_pass = st.get("last_pass")
            stale = last_pass is None or (fold.NOW - fold.ts(last_pass)).total_seconds() > 40 * 60
            if stale:
                _committed_brief(look.emit_once("look-stale", "look-stale", "thinker", {"last_pass": last_pass}, ev))
    except Exception as e:
        look_error = f"{e}"
    try:
        import look
        look.heartbeat_missed("look", fold.NOW, ev, root=fold.ROOT, dst=tick_path())
    except Exception as e:
        look_error = f"{look_error}; {e}" if look_error else f"{e}"
    try:
        import up
        relay_status = up.ensure_relay(ev, datetime.now(timezone.utc))
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
                    _committed(dispatch.emit(tick_path(), {}, "charter-proving", subject=cid,
                                  reason=f"{summ['remaining']} item(s) remain; next: {next_text}",
                                  deadline=summ["deadline"]))
                for item in proving.to_reopen(c["evs"], mine, fold.NOW):
                    _committed(dispatch.emit(tick_path(), {}, "charter-reopened", subject=cid,
                                  spec=item["spec"], criterion=item["criterion"],
                                  failed_src=item["failed_src"]))
                    _committed_brief(look.emit_once("proving-check-failed",
                                   f"{cid}|{item['spec']}|{item['criterion']}", "thinker",
                                   {"spec": item["spec"], "criterion": item["criterion"],
                                    "failed_src": item["failed_src"]}, ev))
                if fold.ts(summ["deadline"]) < fold.NOW:
                    key = f"{cid}|{summ['deadline']}"
                    fired = any(e.get("type") == "brief" and e.get("condition") == "proving-overdue"
                               and e.get("key") == key for e in ev)
                    if not fired:
                        _committed_brief(look.emit_once("proving-overdue", key, "operator",
                                       {"next": (summ["next"]["text"] if summ["next"] is not None else None),
                                        "owner": summ["owner"], "deadline": summ["deadline"]}, ev))
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
                        _committed(dispatch.emit(tick_path(), {}, "charter-closed", subject=cid,
                                      label=label, reason=reason))
    except Exception as e:
        proving_error = f"{e}"
    # L-charter-0042/L-spec-0427, R1: the tick dispatches. LAST guarded step
    # before the Executor rows are read, on the SAME `ev`/`specs` — no re-read sits
    # between here and Steps (Boundaries), so an event `run()` appends
    # this pass never changes THIS pass's own `todo`. A raise never stops the
    # tick's own heartbeat, exactly like `carry_error` above.
    admission = {}
    autodispatch_error = None
    autodispatch_pass_failed = False
    lane_driver_log_error = None
    dispatch_started = time.monotonic()
    steps = []
    steps_attempted = False
    steps_failure = None
    steps_now = fold.NOW

    def pass_steps():
        """Dispatch and todo share one attempt on the pre-dispatch inputs.

        An import can fail before planning. In that case todo still makes the
        first attempt; a failed attempt and a completed empty lane are distinct.
        """
        nonlocal steps, steps_attempted, steps_failure
        if not steps_attempted:
            steps_attempted = True
            try:
                steps = _steps(ev, specs, steps_now)
            except Exception as exc:
                steps_failure = exc
        if steps_failure is not None:
            raise steps_failure
        return steps

    outcomes = []
    seat_state = {}
    def log_outcomes(lines):
        nonlocal lane_driver_log_error
        try:
            _log_lane_driver(lines, fold.NOW)
        except OSError as exc:
            lane_driver_log_error = str(exc)
    close_reviews_error = None
    close_reviews_outcomes = []
    try:
        import close_reviews
        import autodispatch
        close_reviews_outcomes = close_reviews.run(ev, specs, fold.NOW,
                                                   runner=_PassDispatcher(autodispatch._real_runner, ev, specs, fold.NOW, False))
    except Exception as exc:
        close_reviews_error = str(exc)
        close_reviews_outcomes = getattr(exc, 'outcomes', close_reviews_outcomes)
    log_outcomes(close_reviews_outcomes)
    log_outcomes(['start autodispatch'])
    try:
        import autodispatch
        import merge_queue
        steps = pass_steps()
        outcomes = run_steps(ev, specs, fold.NOW, steps=steps, admission=admission, seat_state=seat_state)
        outcomes.extend(autodispatch.redispatch_dead(ev, specs, fold.NOW,
                        handled=handled_items(steps),
                        runner=_PassDispatcher(autodispatch._real_runner, ev, specs, fold.NOW, False)))
        launch_precheck(ev, fold.NOW)
        outcomes.extend(merge_queue.enqueue_due(ev, fold.NOW))
        log_outcomes(outcomes or [])
        failures = [line for line in outcomes if line.startswith('failed ')]
        if failures:
            autodispatch_error = '; '.join(failures)
    except Exception as e:
        if _PASS_MERGED:
            _report_merge_pass_failure(list(_PASS_MERGED), e)
        autodispatch_pass_failed = True
        autodispatch_error = f"{e}"
        if not admission:
            try:
                import loadgate
                config, _ = loadgate.settings()
                if config['mode'] != 'off':
                    admission.update(admission_mode=config['mode'],
                                     admission_error=('not reached: ' + str(e))[:160])
            except Exception as exc:
                admission['admission_error'] = str(exc)[:160]
        log_outcomes([f'failed autodispatch: {e}'])
    log_outcomes([f'finished autodispatch elapsed_s={time.monotonic() - dispatch_started:.3f}'])
    held_error = None
    held_events = ev
    observer_view = 'initial'
    observer_excluded = set()
    try:
        state = _PASS.get()
        if loads == 2:
            observer_view = 'pre-dispatch'
            observer_excluded = state['launched']
        elif state['wrote'] or state['launched']:
            # Retain the existing fresh cached reader when the budget permits.
            held_events = ledger_cache.tail_refresh()
            observer_view = 'post-dispatch'
    except Exception as exc:
        held_error = str(exc)
        held_events = None
        observer_view = 'unavailable'
    state['observer_excluded'] = observer_excluded
    if held_events is not None:
        try:
            observer_steps = [step for step in steps if step.item not in observer_excluded]
            _record_next_step_held(held_events, specs, observer_steps, fold.NOW)
        except Exception as exc:
            held_error = str(exc)
    rework_wait_error = None
    rework_wait = None
    try:
        if 'builder' not in seat_state:
            rework_wait_error = 'remaining builder seats unavailable; retry next tick'
        elif held_events is None:
            rework_wait_error = 'post-dispatch ledger unavailable; retry next tick'
        else:
            rework_wait = _record_rework_waits(held_events, steps, fold.NOW,
                                              seat_state['builder'], outcomes)
    except Exception as exc:
        rework_wait_error = str(exc)
    # L-charter-0042/L-spec-0486 (cleanup-on-finish), R15a (record): the tick's own
    # per-spec worktree reap, on this SAME `ev` — its own try/except, exactly like
    # `autodispatch_error` above: a raise never stops the tick's own heartbeat, and
    # is named on this SAME `tick` event. The `reaped` set that feeds `lane` above
    # is untouched — `worktree-reaped` never marks a charter reaped.
    worktree_reap_error = None
    worktrees_retained = None
    recorded_reap = []
    reap_counts_errors = []
    manifest = not_reapable = None
    try:
        import resources
        manifest = resources.manifest_counts()
    except Exception:
        reap_counts_errors.append("manifest_counts")
    try:
        import resources
        not_reapable = resources.not_reapable_counts(ev)
    except Exception:
        reap_counts_errors.append("not_reapable_counts")
    try:
        import reap_tmp
        recorded_reap = reap_tmp._reap_recorded(ev)
        wt_reaped, wt_retained = tree_cleanup.reap_worktrees(ev)
        for row in wt_reaped:
            _committed(dispatch.emit(tick_path(), {}, "worktree-reaped", subject=row["spec"],
                          path=row["path"], proof=row["proof"]))
        if wt_retained:
            worktrees_retained = len(wt_retained)
    except Exception as e:
        worktree_reap_error = f"{e}"
    try:
        todo = [f"{step.item} · {step.action or 'hold'} · {step.owner} · {fold._step_line(step.reason)}"
                for step in pass_steps() if step.owner == 'executor']
    except Exception as exc:
        todo = [f"undetermined: next_steps failed: {type(exc).__name__}"]
    busy = in_flight(ev)
    packet_busy = _carry_packet_busy(busy)
    todo.extend(f"inbound:{item['source']} · uncarried" for item in inbound
                if item.get('source') not in busy and item.get('source') not in packet_busy)
    todo.sort()
    # The one liveness fact: `fold` reads the newest of these for staleness, and
    # `lane` is the count — the whole record this process leaves behind.
    kv = {"lane": len(todo), "relay": relay_status,
          "autodispatch_pass_failed": autodispatch_pass_failed,
          "observer_view": observer_view,
          "observer_excluded": sorted(observer_excluded),
          "observer_excluded_count": len(observer_excluded),
          "observer_excluded_reason": "accepted launch in this pass" if observer_excluded else None}
    if relay_status == "wedged":
        try:
            kv["relay_heartbeat_age_s"] = (datetime.now(timezone.utc).timestamp()
                                            - (fold.ROOT / "relay.heartbeat").stat().st_mtime)
        except OSError:
            kv["relay_heartbeat_age_s"] = None
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
    if held_error is not None:
        kv["held_error"] = held_error
    if rework_wait_error is not None:
        kv['rework_wait_error'] = rework_wait_error
    if rework_wait is not None and rework_wait['eligible']:
        kv['rework_wait'] = rework_wait
    if lane_driver_log_error:
        kv["lane_driver_log_error"] = lane_driver_log_error
    reap_counts = _reap_summary(recorded_reap, not_reapable=not_reapable, manifest=manifest)
    if reap_counts is not None:
        kv["recorded_reap_counts"] = reap_counts
    if reap_counts_errors:
        kv["recorded_reap_counts_error"] = "; ".join(reap_counts_errors)
    if worktree_reap_error:
        kv["worktree_reap_error"] = worktree_reap_error
    if worktrees_retained is not None:
        kv["worktrees_retained"] = worktrees_retained
    if proving_error:
        kv["proving_error"] = proving_error
    kv['close_reviews_outcomes'] = close_reviews_outcomes
    if close_reviews_error:
        kv['close_reviews_error'] = close_reviews_error
    try:
        if any(not key.startswith('admission_') for key in admission) or len(json.dumps(admission).encode()) > 1024:
            admission = {'admission_error': 'oversize'}
    except Exception:
        admission = {'admission_error': 'oversize'}
    kv.update(admission)
    _committed(dispatch.emit(tick_path(), {}, "tick", **kv))
    return todo


def launch_precheck(events, now, *, project='albert-scott'):
    import precheck
    return precheck.launch_due(events, now, project=project)


def main():
    """Run the guarded dispatch passes and append one ordinary tick heartbeat."""
    todo = _record()
    if todo is None:
        print("tick: one is running; this one is dropped")
        return 0
    print("tick: idle" if not todo else f"tick: {len(todo)} on the lane\n" + "\n".join(todo))
    return 0


def _events_state(exclude_name):
    """name -> (size, mtime_ns) for every `events/*.jsonl` file EXCEPT
    `exclude_name` — the exclusion-aware snapshot `wait()` uses to confirm a
    directory notification (or polls when the watcher fails), so its OWN appended `tick`
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

    AC2: never reports "changed" on its own first look. Open the directory
    watcher before taking the baseline, so writes already in that snapshot
    are ignored and later writes cannot fall between snapshot and watch.

    AC16: `L-tick-local.jsonl` is excluded from what counts as "changed" —
    directory notifications block without scans; a non-tick notification is
    trusted only when `_events_state` confirms a real file moved. Otherwise
    every wake would append a tick that wakes the next waiter. An unavailable
    or lost watcher falls back to exclusion-aware snapshots every POLL_STEP.
    The monotonic deadline bounds both paths, and the watcher always closes."""
    _bind_root(root)
    import realm_ledger                                      # noqa: PLC0415 — lazy; see module note
    import math
    import select

    watcher = realm_ledger.Changes(fold.EVENTS)
    try:
        excl = _events_state(TICK_NAME)
        end = time.monotonic() + max_s
        reason = "interval"
        if watcher.fd is None:
            watcher.close()
            watcher = None
        else:
            ready = select.poll()
            ready.register(watcher.fd, select.POLLIN)
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            if watcher is None:
                time.sleep(min(POLL_STEP, left))
                new_excl = _events_state(TICK_NAME)
                if new_excl != excl:
                    reason = "changed"
                    break
                excl = new_excl
                continue
            try:
                readable = ready.poll(math.ceil(left * 1000))
            except InterruptedError:
                continue
            if not readable:
                continue
            names = watcher.poll()
            if names is None:
                # Changes already closed itself. Drop it before any fallback scan.
                watcher.close()
                watcher = None
            if names is None or names - {TICK_NAME}:
                new_excl = _events_state(TICK_NAME)
                if new_excl != excl:
                    reason = "changed"
                    break
                excl = new_excl
            if watcher is not None:
                time.sleep(min(NOTIFY_MIN_GAP, max(0.0, end - time.monotonic())))
        _record()                    # exactly one tick per RETURNING call (AC4); a lock-drop loses
        return reason                # nothing per this module's own doctrine (Assumption 5)
    finally:
        if watcher is not None:
            watcher.close()


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
