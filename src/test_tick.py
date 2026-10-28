#!/usr/bin/env python3
"""One runnable check on the tick. Run: python3 test_tick.py"""
import contextlib, datetime, fcntl, io, json, os, pathlib, re, sys, tempfile, time

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"], os.environ["DOIT_NO_POKE"] = str(TMP), "1"
# L-spec-0190: `tick.main()` now calls `carry.uncarried()`/`carry.sync_v4()` for
# real on every run. `carry.py`'s own doc comment is explicit that these three
# must point at fixture directories, never the operator's real ledger/inbox/
# staging tree (test_carry.py's own precedent) — left unset, the very first
# `tick.main()` below reads the operator's live `~/.claude/ledger` and leaks
# real inbound rows into this suite.
os.environ["V4_LEDGER_DIR"] = str(TMP / "v4-ledger")
os.environ["V4_INBOX_DIR"] = str(TMP / "v4-inbox")
os.environ["V4_STAGING_DIR"] = str(TMP / "v4-staging")
# ★ `fold.PROJECT` is read ONCE at import and `fold.read_events()` filters on it.
# These fixtures carry no `project` key, so a pane with DOIT_PROJECT set turns every
# assertion below false-red. Popped here, before the import, never in the caller's shell.
os.environ.pop("DOIT_PROJECT", None)
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import carry, fold, intake, pane_resume, tick  # noqa: E402
import unittest
from dataclasses import replace
from unittest.mock import patch
import autodispatch, dispatch, lane_driver, lane_rules, merge_queue, nextstep


def _launcher_evidence(criterion, body):
    import hashlib
    payload = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='cli://tick/' + criterion, status=0,
                          body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                          body_excerpt=payload)))


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sole-launcher-')
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.now = fold.NOW
        self.events = []
        for module, key, value in ((fold, 'ROOT', self.root), (fold, 'EVENTS', self.root / 'events'),
                                   (dispatch, 'ROOT', self.root), (dispatch, 'EVENTS', self.root / 'events'),
                                   (dispatch, 'CONTENT', self.root / 'content')):
            swap = patch.object(module, key, value)
            swap.start()
            self.addCleanup(swap.stop)
        (self.root / 'events').mkdir()
        (self.root / 'content').mkdir()
        self.calls = []
        self.runner = tick._PassDispatcher(self.dispatcher, self.events, {}, self.now, False)

    def step(self, action='review', **kw):
        return replace(nextstep.Step('L-spec-9001', 'spec', 'checking', 'action', action or 'review',
                                     'tick', 'fixture:1', 'condition met', None,
                                     self.now.isoformat()), action=action, **kw)

    def prepare(self, sid):
        (self.root / 'worktrees/proj' / sid.lower()).mkdir(parents=True)
        (self.root / 'content' / (sid + '.md')).write_text('AC1 [backend]: fixture.\n')
        self.events.append(dict(type='spec-written', subject=sid, project='proj', actor='operator',
                                ts=(self.now - datetime.timedelta(hours=1)).isoformat()))

    def dispatcher(self, argv):
        self.calls.append(argv)
        if argv[:2] == ['doit', 'dispatch']:
            return dict(code=0, stdout=json.dumps(dict(detached='L-' + argv[3] + '-9001')), stderr='')
        return dict(code=0, stdout='/packet.md' if argv[:2] == ['doit', 'packet']
                    else '0' if 'rev-list' in argv else 'master', stderr='')

    def rows(self):
        return [json.loads(line) for line in tick.tick_path().read_text().splitlines()]

    def test_executor_steps_and_inbound(self):
        owned = self.step(owner='executor', state='hold', action=None,
                          reason='undetermined: bad\ninput', clears='valid input')
        action = self.step(owner='executor', action='close')
        steps = [owned, action, self.step(owner='tick'), self.step(owner='operator')]
        with patch.object(tick, '_steps', return_value=steps), \
             patch.object(carry, 'uncarried', return_value=[{'source': '283'}]), \
             patch.object(carry, 'sync_v4', return_value=[]), \
             patch.object(tick, 'run_steps', return_value=[]):
            todo = tick._record()
        self.assertEqual(todo, sorted([
            'L-spec-9001 · hold · executor · undetermined: bad input',
            'L-spec-9001 · close · executor · condition met',
            'inbound:283 · uncarried']))
        self.assertEqual(self.rows()[-1]['lane'], len(todo))
        with patch.object(tick, '_steps', side_effect=RuntimeError('unavailable')), \
             patch.object(carry, 'uncarried', return_value=[]), \
             patch.object(carry, 'sync_v4', return_value=[]), \
             patch.object(tick, 'run_steps', return_value=[]):
            self.assertEqual(tick._record(), ['undetermined: next_steps failed: RuntimeError'])
        _launcher_evidence('readers-AC3', dict(rows=todo, count=len(todo), failure_visible=True))

    def test_launch_exact_set(self):
        steps = []
        for n, action in enumerate(sorted(tick.ROUTINE_ACTIONS), 9001):
            sid = f'L-spec-{n}'
            self.prepare(sid)
            steps.append(self.step(action, item=sid))
        steps += [self.step(state='hold', action=None, clears='decision'),
                  self.step(owner='executor'), self.step('gate'),
                  self.step('enqueue'), self.step('merge')]
        out = tick.run_steps(self.events, {}, self.now, dispatcher=self.dispatcher, steps=steps)
        actual = [(a[4], a[3]) for a in self.calls if a[:2] == ['doit', 'dispatch']]
        expected = [(s.item, tick.ROUTINE_ROLES[s.action]) for s in steps
                    if s.state == 'action' and s.owner == 'tick' and s.action in tick.ROUTINE_ACTIONS]
        self.assertCountEqual(actual, expected)
        before = len(self.calls)
        for step in steps[len(tick.ROUTINE_ACTIONS):]:
            self.assertIsNone(tick.launch(step, dispatcher=self.runner))
        self.assertEqual(len(self.calls), before)
        _launcher_evidence('AC1', dict(dispatches=actual, expected=expected, lines=out))

    def test_launch_real_snapshot(self):
        self.prepare('L-spec-9001')
        self.events[0]['_src'] = 'L-planner-fixture.jsonl:1'
        specs = {'L-spec-9001': dict(id='L-spec-9001', state='written', evs=self.events)}
        expected = [step for step in tick._steps(self.events, specs, self.now)
                    if step.state == 'action' and step.owner == 'tick'
                    and step.action in tick.ROUTINE_ACTIONS]
        self.assertEqual([(step.item, step.action) for step in expected], [('L-spec-9001', 'build')])
        tick.run_steps(self.events, specs, self.now, dispatcher=self.dispatcher)
        actual = [(a[4], a[3]) for a in self.calls if a[:2] == ['doit', 'dispatch']]
        self.assertEqual(actual, [(step.item, tick.ROUTINE_ROLES[step.action]) for step in expected])
        _launcher_evidence('AC1-replay', dict(expected=[step.action for step in expected], dispatches=actual))

    def test_launch_failure(self):
        self.prepare('L-spec-9001')
        for step in (self.step(state='hold', action=None, clears='decision'),
                     self.step(reason='undetermined: missing input')):
            out = tick.run_steps(self.events, {}, self.now, dispatcher=self.dispatcher, steps=[step])
            self.assertTrue(out[0].startswith(('hold ', 'undetermined ')))
        self.assertEqual(self.calls, [])
        for failure in (RuntimeError('packet raised'), dict(code=2, stdout='', stderr='packet refused')):
            tick.tick_path().unlink(missing_ok=True)
            with patch.object(self.runner, 'runner', side_effect=failure if isinstance(failure, Exception) else None,
                              return_value=failure):
                with self.assertRaises(ValueError):
                    tick.launch(self.step(), dispatcher=self.runner)
            rows = self.rows()
            self.assertEqual([e['type'] for e in rows], ['autodispatch-failed'])
            self.runner.events = self.events + rows
            before = len(self.calls)
            self.assertIsNone(tick.launch(self.step(), dispatcher=self.runner))
            self.assertEqual(len(self.calls), before)
            self.runner.events = self.events
        with self.assertRaises(ValueError):
            tick.launch(object(), dispatcher=self.runner)
        self.assertTrue(self.rows()[-1]['reason'].startswith('undetermined:'))
        _launcher_evidence('AC8', dict(hold_dispatches=0, undetermined_dispatches=0,
                                      failed_dispatch_successes=0, standing_retry_dispatches=0))

    def test_manual_routine(self):
        since = self.now - datetime.timedelta(hours=1)
        events = []
        for role in ('builder', 'grader', 'reviewer', 'planner', 'spec-writer', 'probe'):
            for issuer in ('tick', 'executor', None, ['tick']):
                events.append(dict(type='build-started' if role == 'builder' else 'spawn-started',
                                   role=role, spawn=f'{role}-{len(events)}', issued_by=issuer,
                                   ts=self.now.isoformat()))
        events += [dict(type='build-started', spawn='old', ts=(since - datetime.timedelta(seconds=1)).isoformat()),
                   dict(type='merge-gate-clean', spawn='gate', ts=self.now.isoformat()),
                   dict(type='build-started', spawn='missing-issuer', ts=since.isoformat())]
        expected = [e['spawn'] for e in events[:12] if e['issued_by'] != 'tick'] + ['missing-issuer']
        self.assertEqual(nextstep.manual_routine_dispatches(events, since), expected)
        self.assertEqual(nextstep.manual_routine_dispatches([], since), [])
        _launcher_evidence('AC3', dict(manual=expected, boundary_included=True, gate_ignored=True))

    def test_sole_launcher(self):
        # Exercise the real pass. Other tick services stay hermetic through the
        # existing file's stubs and empty private filesystem.
        with patch.object(nextstep, 'next_steps', return_value=[]) as steps, \
             patch.object(lane_driver, '_rows', side_effect=AssertionError('legacy rows')), \
             patch.object(autodispatch, 'redispatch_dead', return_value=[]), \
             patch.object(merge_queue, 'enqueue_due', return_value=[]), \
             patch.object(tick, 'run_steps', return_value=['failed L-spec-9001 review: packet refused']), \
             patch.object(tick, '_log_lane_driver') as logged:
            tick._record()
        self.assertEqual(steps.call_count, 2)
        self.assertEqual(steps.call_args.args[2], self.now)
        self.assertTrue(logged.called)
        event = next(e for e in reversed(self.rows()) if e['type'] == 'tick')
        self.assertIn('packet refused', event['autodispatch_error'])
        _launcher_evidence('AC2', dict(next_steps_calls=steps.call_count, legacy_calls=0,
                                      logged=True, error=event['autodispatch_error']))

    def test_no_double_gate(self):
        gate_steps = [self.step(action) for action in ('gate', 'enqueue', 'merge')]
        sid = 'L-spec-9001'
        events = [dict(v=1, type='spec-written', subject=sid, project='proj', footprint=['allowed.txt'],
                       ts=(self.now - datetime.timedelta(minutes=10)).isoformat()),
                  dict(v=1, type='review', subject=sid, confirmed=True, n_blocking=0,
                       ts=(self.now - datetime.timedelta(minutes=1)).isoformat())]
        row = dict(spec=sid, rule='gate', since=events[-1]['ts'], limit_min=60,
                   overdue=False, reason='condition met', reason_live=False)
        real_enqueue = merge_queue.enqueue_due
        gate_calls = []

        def gate_runner(argv, **kw):
            gate_calls.append(argv)
            return dict(code=0, stdout='', stderr='')

        def enqueue_pass(ev, now):
            return real_enqueue(events, now, runner=gate_runner)

        with patch.object(nextstep, 'next_steps', return_value=gate_steps), \
             patch.object(autodispatch, 'redispatch_dead', return_value=[]), \
             patch.object(lane_rules, 'due', return_value=[row]), \
             patch.object(merge_queue, '_repository', return_value=(self.root / 'repos/proj', 'seed')), \
             patch.object(merge_queue, 'enqueue_due', side_effect=enqueue_pass) as enqueue, \
             patch.object(tick, 'launch', side_effect=AssertionError('gate launched twice')) as launch:
            tick._record()
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(launch.call_count, 0)
        self.assertEqual(len(gate_calls), 1)
        gate_events = [e for e in self.rows() if e['type'] == 'autodispatched' and e.get('rule') == 'gate']
        self.assertEqual(len(gate_events), 1)
        with patch.object(merge_queue, 'enqueue_due', side_effect=AssertionError('legacy queue')):
            autodispatch.run([], {}, self.now, runner=self.dispatcher)
        _launcher_evidence('AC10', dict(enqueue_due_calls=1, gate_calls=len(gate_calls),
                                       gate_events=len(gate_events), routine_gate_launches=0))


import look as _watchdog_look
_watchdog_look._send_operator = lambda text, root: 0
_real_heartbeat_missed = _watchdog_look.heartbeat_missed
# Historical fixtures count every tick-ledger line; keep their cron healthy.
_watchdog_look.heartbeat_missed = lambda *args, **kw: False

# Rebind cached modules too when several script-style suites are collected.
tick._bind_root(TMP)

NOW = fold.NOW.isoformat(timespec="seconds")
EV = TMP / "events"

# L-spec-0242 (AC11): `tick._record()` now calls `intake.run(ev)` on EVERY pass —
# ONE module-level `intake.ghlimit = STUB`-shaped assignment (L-spec-0379 fix 2:
# `intake.ghlimit`, never `intake.subprocess`), before the FIRST `tick.main()`
# call below, so all 22 existing fixtures plus every new one route through it;
# a per-fixture patch that only new fixtures received would leave the 22
# existing ones reaching a REAL `gh` (installed on this box).
INTAKE_CALLS = []


class _IntakeStub:
    """A healthy rate, empty listings both labels — `intake.run()` contributes
    nothing to any fixture's ledger, so every existing lane/tick assertion
    below is unaffected by its presence. Implements `ghlimit`'s own two real
    signatures — `.gate(wait=False) -> {"ok": ...}` / `.run(argv, wait=False)
    -> (rc, out, err)` (a tuple, never `subprocess`'s `SimpleNamespace`)."""
    def gate(self, wait=False):
        return {"ok": True, "remaining": 5000, "reset": 0, "waited_s": 0}

    def run(self, argv, wait=False):
        INTAKE_CALLS.append(list(argv))
        if argv[:3] == ["gh", "pr", "list"]:
            return (0, "[]", "")
        raise AssertionError(f"test_tick.py's intake stub got an unexpected call: {argv}")


intake.ghlimit = _IntakeStub()

# L-spec-0274 (R3): `tick._record()` now calls `pane_resume.run(ev)` on EVERY
# pass too. Left unpatched, that call reads this BOX's own real
# `~/.claude/sessions` (pane_resume.run's own default) and could send a real
# `continue` into a real tmux pane out of this very test file — the one
# outcome the spec's own Constraints forbid a test suite from ever risking.
# Stubbed the same way `intake.ghlimit` is: one module-level swap, before
# the FIRST `tick.main()` below, so every existing fixture and every new one
# routes through it; `test_pane_resume.py` alone exercises the real function.
PANE_RESUME_CALLS = []
_real_pane_resume_run = pane_resume.run


def _pane_resume_stub(ev, **kw):
    PANE_RESUME_CALLS.append(len(ev))
    return {"resumed": [], "escalated": [], "skipped": []}


pane_resume.run = _pane_resume_stub

# L-spec-0431/R5d: `tick._record()` now calls `install_sync.run(ev)` on EVERY
# pass too. Left unstubbed, `install_sync.run()`'s own baseline branch would
# symlink this repo's real `agents/*.md` into this BOX's real
# `~/.claude/agents` and shell a real `git rev-parse HEAD` — exactly the
# real-environment leak `intake.ghlimit`/`pane_resume.run` are already
# stubbed above to prevent. Stubbed the same way: one module-level
# `sys.modules` entry, before the FIRST `tick.main()` below, so every
# existing fixture and every new one routes through it; `test_install_sync.py`
# alone exercises the real module.
INSTALL_SYNC_CALLS = []


class _InstallSyncStub:
    def run(self, ev, **kw):
        INSTALL_SYNC_CALLS.append(len(ev))
        return []


sys.modules["install_sync"] = _InstallSyncStub()


def ticks():
    """`tick`-TYPED lines only: in_flight() appends spawn-stale to the same file."""
    p = tick.tick_path()
    lines = p.read_text().splitlines() if p.exists() else []
    return [e for e in (json.loads(a) for a in lines) if e["type"] == "tick"]


def write(name, *events):
    p = EV / name
    p.write_text("".join(json.dumps({"v": 1, "ts": NOW, **e}) + "\n" for e in events))
    return p


def folded():
    ev = fold.read_events()
    specs, charters, _, _ = fold.fold(ev)
    return ev, specs, charters


# R1/AC1: there is no Executor-spawn path left in the file, under any backend — the
# grep is the acceptance criterion, so it is also the regression test.
SRC = (pathlib.Path(__file__).parent / "tick.py").read_text()
assert not re.search(r"run_claude|--agent|executor\.schema\.json|models\.|dispatch\.alloc", SRC), \
    "tick.py must contain no code path that starts an Executor process (L-adr-0035)"

class AdmissionTests(unittest.TestCase):
    setUp = LauncherTests.setUp
    step = LauncherTests.step
    prepare = LauncherTests.prepare
    dispatcher = LauncherTests.dispatcher
    rows = LauncherTests.rows

    @contextlib.contextmanager
    def isolated(self, mode):
        with tempfile.TemporaryDirectory(prefix='admission-tick-') as directory, contextlib.ExitStack() as stack:
            root = pathlib.Path(directory)
            for module, key, value in ((fold, 'ROOT', root), (fold, 'EVENTS', root / 'events'),
                                       (dispatch, 'ROOT', root), (dispatch, 'EVENTS', root / 'events'),
                                       (dispatch, 'CONTENT', root / 'content')):
                stack.enter_context(patch.object(module, key, value))
            proc, cg = root / 'proc', root / 'cgroup'
            for name, content in {
                'meminfo': 'MemAvailable: 7340032 kB\nSwapFree: 0 kB\nShmem: 1048576 kB\n',
                'loadavg': '8 4 2 3/2500 1\n', 'vmstat': 'pswpin 1000\npswpout 2000\n',
                'stat': 'procs_running 3\n', **{'pressure/' + p:
                    'some avg10=0 avg60=0 avg300=0 total=0\nfull avg10=0 avg60=0 avg300=0 total=0\n'
                    for p in ('memory', 'cpu', 'io')}}.items():
                path = proc / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            cron = cg / 'system.slice/cron.service'
            cron.mkdir(parents=True)
            (cron / 'memory.pressure').write_text((proc / 'pressure/memory').read_text())
            (cron / 'memory.current').write_text('1073741824')
            stack.enter_context(patch.dict(os.environ, DOIT_ADMISSION=mode, DOIT_ADMISSION_TEST='1',
                DOIT_ADMISSION_TEST_PROC=str(proc), DOIT_ADMISSION_TEST_CGROUP=str(cg), DOIT_ADMISSION_TEST_NCPU='8'))
            stack.enter_context(patch.object(autodispatch, 'capacity', return_value=2))
            stack.enter_context(patch.object(lane_driver, 'capacity', return_value=1))
            stack.enter_context(patch.object(lane_driver, '_in_flight_role', return_value=0))
            stack.enter_context(patch.object(merge_queue, 'next_candidate', return_value='L-spec-9606'))
            yield root, stack

    def scenario(self):
        actions = ['build', 'rebuild', 'grade', 'regrade', 'review', 'merge', 'grade', 'regrade', 'build', 'merge']
        steps = [self.step(action, item=f'L-spec-{9601 + i}') for i, action in enumerate(actions)]
        steps += [self.step(state='hold', action=None, item='L-spec-9611', clears='decision')]
        return steps

    def dispatch_pass(self, mode, fault=None, dry=False):
        import loadgate
        with self.isolated(mode) as (root, stack):
            self.root, self.events, self.calls = root, [], []
            (root / 'events').mkdir()
            (root / 'content').mkdir()
            for i in range(9601, 9611):
                self.prepare(f'L-spec-{i}')
            self.events.append(dict(type='autodispatch-failed', subject='L-spec-9607', role='grader',
                                    reason='fixture standing failure', ts=self.now.isoformat()))
            original = self.dispatcher
            def dispatcher(argv):
                result = original(argv)
                if argv[:2] == ['doit', 'dispatch'] and 'L-spec-9608' in argv:
                    return dict(code=1, stdout='', stderr='fixture refused')
                return result
            if fault:
                stack.enter_context(fault)
            checks = stack.enter_context(patch.object(loadgate, 'admit', wraps=loadgate.admit))
            info = {}
            lines = tick.run_steps(self.events, {}, self.now, dispatcher=dispatcher,
                                   steps=self.scenario(), admission=info, dry_run=dry)
            rows = self.rows() if tick.tick_path().exists() else []
            for row in rows:
                row.pop('ts', None)
            calls = json.loads(json.dumps(self.calls).replace(str(root), '<root>'))
            admitted = [c.kwargs['admitted_gb'] for c in checks.call_args_list if c.args[0] != 'probe']
            return lines, calls, rows, info, admitted, (root / 'state/admission.json').exists()

    def test_AC8_observe_invariant_and_accounting(self):
        off = self.dispatch_pass('off')
        observe = self.dispatch_pass('observe')
        self.assertEqual(off[:3], observe[:3])
        self.assertEqual(off[3], {})
        self.assertEqual(observe[3]['admission_checked'], 8)
        self.assertEqual(observe[3]['admission_would_hold'], 8)
        self.assertEqual(observe[3]['admission_held'], 0)
        self.assertEqual(observe[3]['admission_reason'], {'mem-hard-stop': 8})
        expected = [0, .5, 1., 1.6, 2.2, 2.6, 3.5, 3.5]
        for actual, wanted in zip(observe[4], expected):
            self.assertAlmostEqual(actual, wanted)
        self.assertEqual(len(observe[4]), len(expected))
        self.assertTrue(observe[5])
        with self.isolated('observe') as (root, stack):
            stack.enter_context(patch.object(tick, 'launch', side_effect=['one', 'two', RuntimeError('third')]))
            info = {}
            with self.assertRaisesRegex(RuntimeError, 'third'):
                tick.run_steps([], {}, self.now, steps=self.scenario(), admission=info)
            self.assertEqual(info['admission_checked'], 3)
            self.assertTrue((root / 'state/admission.json').exists())
        _launcher_evidence('AC8-1650', dict(checked=8, would_hold=8, held=0, calls_and_events_equal=True,
                                         peaks=observe[4], exceptional_finish=True))

    def test_enforce_AC12_without_machine_opt_in(self):
        observed = self.dispatch_pass('observe')
        original = self.isolated
        @contextlib.contextmanager
        def isolated(mode):
            with original(mode) as (root, stack):
                table = root / 'look.toml'
                table.write_text('[admission]\nmode="enforce"')
                stack.enter_context(patch.dict(os.environ,DOIT_ADMISSION_TEST_TOML=str(table)))
                yield root,stack
        with patch.object(self,'isolated',side_effect=isolated):
            requested = self.dispatch_pass('enforce')
        self.assertEqual(observed[:3],requested[:3])
        self.assertIn('enforce only via $DOIT_ROOT/admission.toml; observing',requested[3]['admission_settings'])
        _launcher_evidence('AC12-1649',dict(no_file_enforce_equals_observe=True))

    def test_AC9_dry_and_begin_failures(self):
        import loadgate
        with patch.object(loadgate, 'begin') as begin:
            result = self.dispatch_pass('observe', dry=True)
            begin.assert_not_called()
        self.assertEqual([line for line in result[0] if line.startswith('would act')], [
            'would act L-spec-9601 build', 'would act L-spec-9602 rebuild',
            'would act L-spec-9603 grade', 'would act L-spec-9604 regrade',
            'would act L-spec-9605 review', 'would act L-spec-9606 merge',
            'would act L-spec-9607 grade', 'would act L-spec-9608 regrade'])
        self.assertFalse(result[5])
        off = self.dispatch_pass('off')
        for fault in (patch.object(loadgate, 'begin', side_effect=RuntimeError('begin')),
                      patch.dict(sys.modules, {'loadgate': None})):
            result = self.dispatch_pass('observe', fault=fault)
            self.assertEqual(result[:3], off[:3])
            self.assertEqual(set(result[3]), {'admission_error'})
        _launcher_evidence('AC9-1650', dict(dry_literal_lines=8, begin_and_import_fail_open=True))

    def record_pass(self, mode, fault=None, oversize=False):
        with self.isolated(mode) as (root, stack):
            stack.enter_context(patch.object(fold, 'read_events', return_value=[]))
            steps = [self.step('grade', item=f'L-spec-{10000+i}') for i in range(1000)]
            stack.enter_context(patch.object(tick, '_steps', return_value=steps))
            stack.enter_context(patch.object(tick, 'launch', return_value='spawn'))
            stack.enter_context(patch.object(autodispatch, 'redispatch_dead', return_value=[]))
            stack.enter_context(patch.object(merge_queue, 'enqueue_due', return_value=[]))
            if fault:
                stack.enter_context(fault)
            if oversize:
                def oversized(*args, admission=None, **kw):
                    admission['admission_test'] = 'x' * 5000
                    return []
                stack.enter_context(patch.object(tick, 'run_steps', side_effect=oversized))
            tick._record()
            rows = [r for r in self.rows() if r['type'] == 'tick']
            self.assertEqual(len(rows), 1)
            return rows[0], (root / 'state/admission.json').exists()

    def test_AC10_tick_fields_bounded_and_invariant(self):
        off, _ = self.record_pass('off')
        observed, _ = self.record_pass('observe')
        info = {k: v for k, v in observed.items() if k.startswith('admission_')}
        self.assertLessEqual(len(json.dumps(info).encode()), 1024)
        self.assertIn('admission_mode', info, info)
        self.assertEqual(info['admission_mode'], 'observe')
        self.assertEqual(info['admission_checked'], 1000)
        self.assertEqual(info['admission_held'], 0)
        self.assertEqual(info['admission_trips'], ['mem-hard-stop', 'mem-swap-hard-stop'])
        self.assertEqual(info['admission_readings']['mem_avail_gb'], 7)
        for key, kind in (('admission_cost_ms', int), ('admission_sampled_at', str), ('admission_reading_age_s', int)):
            self.assertIsInstance(info[key], kind)
        def other(event):
            return {k: v for k, v in event.items() if not k.startswith('admission_') and k not in ('ts', 'relay_heartbeat_age_s')}
        self.assertEqual(other(off), other(observed))
        self.assertFalse(any(k.startswith('admission_') for k in off))
        oversized, _ = self.record_pass('observe', oversize=True)
        self.assertEqual({k: v for k, v in oversized.items() if k.startswith('admission_')}, {'admission_error': 'oversize'})
        _launcher_evidence('AC10-1650', dict(bytes=len(json.dumps(info).encode()), checked=1000,
                                          other_fields_equal=True, oversize='oversize'))

    def test_AC11_config_and_event_vocabulary(self):
        import howto, tomllib, loadgate
        repo = pathlib.Path(__file__).resolve().parent.parent
        parsed = tomllib.loads((repo / 'look.toml').read_text())
        self.assertEqual(parsed['admission'], loadgate.DEFAULTS)
        self.assertFalse(any('admission' in name for name in howto.event_types(repo / 'src/tick.py')))
        _launcher_evidence('AC11-1650', dict(defaults_match=True, event_type_unchanged=True))

    def test_AC12_hook_failures_and_not_reached(self):
        import loadgate
        off = self.dispatch_pass('off')
        result = self.dispatch_pass('observe', fault=patch.object(loadgate.Gate, 'check', side_effect=RuntimeError('chk')))
        self.assertEqual(result[:3], off[:3])
        self.assertEqual(result[3]['admission_checked'], 0)
        self.assertIn('check: chk', result[3]['admission_error'])
        result = self.dispatch_pass('observe', fault=patch.object(loadgate.Gate, 'finish', side_effect=RuntimeError('fin')))
        self.assertEqual(result[:3], off[:3])
        self.assertEqual(result[3], {'admission_error': 'finish: fin'})
        self.assertFalse(result[5])
        for mode in ('observe', 'off'):
            for fault, message in ((patch.object(tick, '_steps', side_effect=RuntimeError('boom')), 'boom'),
                                   (patch.object(lane_driver, '_in_flight_role', side_effect=RuntimeError('seat')), 'seat')):
                event, exists = self.record_pass(mode, fault=fault)
                info = {k: v for k, v in event.items() if k.startswith('admission_')}
                expected = dict(admission_mode='observe', admission_error='not reached: ' + message) if mode == 'observe' else {}
                self.assertEqual(info, expected)
                self.assertIn(message, event['autodispatch_error'])
                self.assertFalse(exists)
        _launcher_evidence('AC12-1650', dict(check_finish_fail_open=True, not_reached=['boom', 'seat'], off_empty=True))


class EnforcementTests(unittest.TestCase):
    setUp = AdmissionTests.setUp
    step = AdmissionTests.step
    rows = AdmissionTests.rows
    prepare = AdmissionTests.prepare
    dispatcher = AdmissionTests.dispatcher

    def ago(self, minutes):
        return (self.now - datetime.timedelta(minutes=minutes)).isoformat(timespec='seconds')

    @contextlib.contextmanager
    def enforcing(self, *, mem=7, trip=1, age=1, seats=3, running=1, wait=15):
        import loadgate as lg
        with AdmissionTests.isolated(self, '') as (root, stack):
            self.root, self.events, self.calls = root, [], []
            (root / 'events').mkdir()
            (root / 'content').mkdir()
            (root / 'admission.toml').write_text('mode="enforce"')
            (root / 'models.toml').write_text(f'[seats]\nbuilder={seats}\nreviewer=1\n')
            table = root / 'look.toml'
            table.write_text(f'[admission]\nmode="observe"\nmax_wait_min={wait}\n')
            stack.enter_context(patch.dict(os.environ, DOIT_ADMISSION_TEST_TOML=str(table)))
            self.memory(mem)
            state = lg._fresh() | dict(t=self.now.timestamp()-age*60, trip_since=self.ago(trip))
            lg.save_state(root / 'state/admission.json', state)
            # Exercise the real capacity and in-flight readers with a fixture ledger.
            stack.enter_context(patch.object(autodispatch, 'capacity', new=self.builder_capacity))
            stack.enter_context(patch.object(lane_driver, '_in_flight_role', new=self.in_flight))
            if running:
                self.events.append(dict(type='spawn-started', subject='L-spec-9990', spawn='L-builder-9990',
                    role='builder', ts=self.ago(1), window_min=90))
            original = lg.begin
            def begin(**kw):
                return original(clock=lambda: self.now.timestamp(), **kw)
            stack.enter_context(patch.object(lg, 'begin', side_effect=begin))
            yield root, stack

    def builder_capacity(self):
        import tomllib
        return tomllib.loads((self.root / 'models.toml').read_text())['seats']['builder']

    def in_flight(self, events, now, role):
        # The unpatched implementation is retained below the class definition.
        return _enforce_in_flight(events, now, role)

    def memory(self, mem):
        (self.root / 'proc/meminfo').write_text(f'MemAvailable: {int(mem*2**20)} kB\nSwapFree: {8*2**20} kB\n')

    def steps(self):
        actions = ['build', 'grade', 'rebuild', 'regrade', 'review', 'merge', 'review']
        return [self.step(a, item=f'L-spec-{9601+i}', since=self.ago(1 if i == 6 else 2))
                for i,a in enumerate(actions)] + [self.step(state='hold', item='L-spec-9699', clears='decision')]

    def run_pass(self, steps, *, outcomes=None, admission=True):
        info, calls = {}, []
        def launch(step, **kw):
            calls.append((step.item, step.action))
            result = (outcomes or {}).get(step.item, 'spawn')
            if isinstance(result, Exception):
                raise result
            return result
        with patch.object(tick, 'launch', side_effect=launch):
            lines = tick.run_steps(self.events, {}, self.now, steps=steps,
                                   **({'admission':info} if admission else {}))
        return lines, calls, info

    def test_enforce_AC3_AC4_and_AC5_retry(self):
        with self.enforcing() as (root, stack):
            for step in self.steps():
                self.prepare(step.item)
                self.events[-1]['ts'] = step.since
            self.events += [dict(type='queue-entered', subject='L-spec-9606', ts=self.ago(3), project='proj')]
            # Compare actual dispatch and ledger writes from independent identical roots.
            def actual():
                calls = []
                real_launch = tick.launch
                def launch(step, **kw):
                    calls.append((step.item,step.action))
                    return real_launch(step, **kw)
                with patch.object(tick, 'launch', side_effect=launch):
                    info = {}
                    lines = tick.run_steps(self.events, {}, self.now, steps=self.steps(),
                                           dispatcher=self.dispatcher, admission=info)
                events = self.rows() if tick.tick_path().exists() else []
                return lines,calls,[(e['type'],e.get('subject')) for e in events],info
            held = actual()
            self.assertIn('resource-wait L-spec-9601 build', held[0])
            self.assertIn('resource-wait L-spec-9602 grade', held[0])
            self.assertEqual(held[3]['admission_held'], 2)
            self.assertFalse(any(sid in ('L-spec-9601','L-spec-9602') for _,sid in held[2]))
            # The held first build re-derives with its ledger timestamp unchanged.
            evs = [e for e in self.events if e.get('subject') == 'L-spec-9601']
            specs = fold.fold(evs)[0]
            again = next(s for s in tick._steps(evs, specs, self.now) if s.item == 'L-spec-9601')
            self.assertEqual((again.action,again.owner,again.since), ('build','tick',self.steps()[0].since))
            row = fold.step_row(again, lane_rules.load(), self.now)
            self.assertIn(lane_rules.ladder(evs,row,self.now), ('act','retry'))
            self.memory(20)
            lines, calls, info = self.run_pass([again])
            self.assertEqual(calls, [(again.item,'build')])
            self.assertEqual(info['admission_held'], 0)
        with self.enforcing(mem=20):
            calm = self.run_pass(self.steps())
        with self.enforcing(mem=20), patch.dict(os.environ, DOIT_ADMISSION='off'):
            off = self.run_pass(self.steps())
        self.assertEqual(calm[:2],off[:2])
        with self.enforcing(), patch.dict(os.environ, DOIT_ADMISSION='off'):
            off = self.run_pass(self.steps())
        with self.enforcing(), patch.dict(os.environ,DOIT_ADMISSION='off'):
            for step in self.steps():
                self.prepare(step.item)
                self.events[-1]['ts'] = step.since
            self.events += [dict(type='queue-entered',subject='L-spec-9606',ts=self.ago(3),project='proj')]
            off_actual = actual()
        self.assertEqual(held[2],[row for row in off_actual[2] if row[1] not in ('L-spec-9601','L-spec-9602')])
        self.assertEqual(held[1], [c for c in off[1] if c[0] not in ('L-spec-9601','L-spec-9602')])
        self.assertIn('seat-wait L-spec-9607 review', held[0])
        for ac in ('AC3','AC4','AC5'):
            _launcher_evidence(ac+'-1649', dict(held=2, no_held_events=True, calm_equals_off=True, retry_derived=True))

    def test_enforce_AC6_AC7_escape_and_grades_first(self):
        for outcome in ('spawn',None,ValueError('refused')):
            with self.enforcing(trip=40):
                steps = [self.step('build',item=str(i),since=self.ago(age)) for i,age in enumerate((20,16,10))]
                lines,calls,info = self.run_pass(steps,outcomes={'0':outcome})
                winner = '0' if outcome else '1'
                if isinstance(outcome,Exception):
                    winner = '1'
                self.assertIn('admission_escape',info,info)
                self.assertEqual(info['admission_escape']['item'],winner)
                self.assertEqual(info['admission_escape_due'],2)
                self.assertEqual(len(calls),1 if winner == '0' else 2)
                if winner == '0':
                    self.assertLess(lines.index('resource-escape 0 build max-wait waited_s=1200'),
                                    lines.index('acted 0 build spawn=spawn'))
                else:
                    self.assertEqual(info['admission_escape_unspawned'],1)
        for mem,outcome in ((7,'spawn'),(7,None),(20,'spawn')):
            with self.enforcing(trip=40, mem=mem):
                steps = [self.step('build',item='b',since=self.ago(30)),self.step('grade',item='g',since=self.ago(20))]
                lines,calls,info = self.run_pass(steps,outcomes={'g':outcome})
                self.assertEqual(calls[0][0], 'g' if mem == 7 else 'b')
                if mem == 7:
                    self.assertEqual(info['admission_escape']['item'],'g' if outcome else 'b')
        with self.enforcing(trip=40):
            info = {}
            with patch.object(tick,'launch',side_effect=RuntimeError('after dispatch')):
                with self.assertRaisesRegex(RuntimeError,'after dispatch'):
                    tick.run_steps(self.events,{},self.now,
                        steps=[self.step('build',item='raised',since=self.ago(20))],admission=info)
            self.assertEqual(info['admission_escape_due'],1)
            self.assertEqual(info['admission_escape_unspawned'],1)
            self.assertNotIn('admission_escape',info)
        with self.enforcing(trip=10):
            _,calls,info = self.run_pass([self.step('build',since=self.ago(20))])
            self.assertEqual(calls,[])
            self.assertNotIn('admission_escape_due',info)
        with self.enforcing(trip=70,wait=600):
            _,calls,info = self.run_pass([self.step('build',since=self.ago(61))])
            self.assertEqual(len(calls),1)
            self.assertIn('max_wait_min capped at 60',info['admission_settings'])
        for ac in ('AC6','AC7'):
            _launcher_evidence(ac+'-1649', dict(max_wait=True, unspawned_reoffers=True, grade_first=True))

    def test_enforce_AC8_idle_and_AC9_fail_open(self):
        import loadgate as lg
        for running, trip, result, idle in ((0,16,None,True),(1,16,None,False),(0,10,None,False),(0,16,'spawn',False)):
            with self.enforcing(running=running,trip=trip):
                steps = [self.step('rebuild',item='r',since=self.ago(2)),self.step('build',item='b',since=self.ago(1))]
                _,calls,info = self.run_pass(steps,outcomes={'r':result})
                self.assertEqual(('b','build') in calls,idle)
                if idle:
                    self.assertEqual(info['admission_escape']['why'],'idle-box')
        for method in ('check','note_launch'):
            with self.enforcing() as (root,stack):
                steps = [self.step(a,item=str(i),since=self.ago(age))
                         for i,(a,age) in enumerate((('grade',1),('build',3),('rebuild',2),('build',1)))]
                original = getattr(lg.Gate,method)
                count = []
                def fault(g,*args,**kw):
                    count.append(1)
                    if method == 'note_launch' or len(count) == 3:
                        raise RuntimeError('latch')
                    return original(g,*args,**kw)
                with patch.object(lg.Gate,method,new=fault):
                    _,calls,info = self.run_pass(steps)
                self.assertEqual(calls,[('2','rebuild'),('3','build')])
                self.assertEqual(info['admission_held'],2)
                self.assertIn('latch',info['admission_error'])
        for fault in ('begin','import','check','state'):
            with self.enforcing() as (root,stack):
                if fault == 'begin':
                    stack.enter_context(patch.object(lg,'begin',side_effect=RuntimeError('begin')))
                elif fault == 'import':
                    stack.enter_context(patch.dict(sys.modules,{'loadgate':None}))
                elif fault == 'check':
                    stack.enter_context(patch.object(lg.Gate,'check',side_effect=RuntimeError('check')))
                else:
                    (root / 'state/admission.json').write_text('bad')
                _,calls,info = self.run_pass(self.steps())
                self.assertEqual(info.get('admission_held',0),0)
                self.assertTrue(('L-spec-9601','build') in calls and ('L-spec-9602','grade') in calls)
                if fault == 'check':
                    self.assertEqual(calls[0][1],'grade')
                if fault == 'state':
                    self.assertIn('state',info['admission_unreadable'])
        for ac in ('AC8','AC9'):
            _launcher_evidence(ac+'-1649', dict(idle_matrix=True, errors_disable_later_holds=True, earlier_holds_preserved=True))

    def test_enforce_AC10_seats_and_generated_progress(self):
        for mem, acted, waiting in ((20,2,2),(7,0,0)):
            with self.enforcing(mem=mem,seats=2,running=0):
                lines,calls,info = self.run_pass([self.step('build',item=str(i),since=self.ago(1)) for i in range(4)])
                self.assertEqual(len(calls),acted,info)
                self.assertEqual(sum(l.startswith('seat-wait') for l in lines),waiting)
                self.assertEqual(info['admission_held'],4 if mem == 7 else 0)
        for i in range(20):
            def scenario(off=False):
                with self.enforcing(mem=7 if i%2 else 20,seats=i%4,running=0,trip=20 if i%3 else 1), \
                     patch.dict(os.environ,DOIT_ADMISSION='off' if off else ''):
                    steps = [self.step(a,item=str(n),since=self.ago(20 if n%2 else 1)) for n,a in
                             enumerate(('build','grade','rebuild','regrade','review','build','merge','review'))]
                    return self.run_pass(steps,outcomes={'2':None if i%3 else ValueError('fixture')})
            on, off = scenario(),scenario(True)
            for role,actions in (('builder',('build','rebuild')),('grader',('grade','regrade')),('reviewer',('review',)),('window_gate',('merge',))):
                def spawned(result):
                    return sum(l.startswith('acted ') and l.split()[2] in actions for l in result[0])
                self.assertLessEqual(spawned(on),spawned(off))
                if role == 'builder':
                    self.assertLessEqual(spawned(on),i%4)
                if role == 'reviewer':
                    self.assertLessEqual(spawned(on),1)
            self.assertFalse(any(l.startswith('resource-wait') and l.split()[2] not in ('build','grade') for l in on[0]))
            self.assertLessEqual(sum(l.startswith('resource-escape') for l in on[0]),1)
        _launcher_evidence('AC10-1649',dict(fixtures=20, seats_respected=True, bypasses_progress=True))

    def test_enforce_AC14_AC15_state_and_seat_wait_clock(self):
        import loadgate as lg
        for age,held in ((61,False),(-6,False),(-4,True),(59,True)):
            with self.enforcing(age=age):
                _,calls,info = self.run_pass(self.steps())
                self.assertEqual(info['admission_held'],2 if held else 0)
                self.assertEqual('stale' in info.get('admission_state',[]),not held)
        with self.enforcing(seats=1,trip=40) as (root,stack):
            step = self.step('build',since=self.ago(20))
            lines,calls,info = self.run_pass([step])
            self.assertEqual(calls,[])
            self.assertTrue(lines[0].startswith('seat-wait'))
            self.assertNotIn(step.item,lg.load_state(root / 'state/admission.json')[0]['holding'])
            (root / 'models.toml').write_text('[seats]\nbuilder=2\n')
            self.now += datetime.timedelta(minutes=1)
            lines,calls,info = self.run_pass([step])
            self.assertEqual(info['admission_escape']['waited_s'],1260)
        with self.enforcing(seats=3,trip=40) as (root,stack):
            state,_ = lg.load_state(root / 'state/admission.json')
            state['holding'] = {f'other-{i}':self.ago(30) for i in range(64)}
            lg.save_state(root / 'state/admission.json',state)
            lines,calls,info = self.run_pass([self.step('build',item=f'{i:03}',since=self.ago(20)) for i in range(100)])
            self.assertEqual(calls,[('000','build')])
            # One running builder plus the escape leaves a free seat, so
            # every remaining step reaches the resource gate.
            self.assertEqual(info['admission_escape']['item'],'000')
            self.assertEqual(info['admission_escape']['why'],'max-wait')
            self.assertEqual(info['admission_escape']['waited_s'],1200)
            self.assertEqual(info['admission_escape_due'],100)
            self.assertEqual(info['admission_held'],99)
            self.assertEqual(sum(line.startswith('resource-wait') for line in lines),99)
            self.assertFalse(any(line.startswith('seat-wait') for line in lines))
        _launcher_evidence('AC14-1649',dict(freshness_bounds=True))
        _launcher_evidence('AC15-1649',dict(seat_wait_clock_preserved=True, backlog=100,
                                          spawned=1, held=99, resource_waits=99, due=100))

    def test_enforce_AC11_tick_and_AC17_entrypoints(self):
        import loadgate as lg
        for entry in ('direct','auto','lane'):
            with self.enforcing() as (root,stack):
                steps = self.steps()
                stack.enter_context(patch.object(tick,'_steps',return_value=steps))
                with patch.object(lg,'read_enforce_file',wraps=lg.read_enforce_file) as reader, \
                     patch.object(tick,'launch',return_value='spawn') as launch:
                    if entry == 'direct':
                        lines = tick.run_steps(self.events,{},self.now,steps=steps)
                    else:
                        lines = (autodispatch if entry == 'auto' else lane_driver).run(self.events,{},self.now)
                    reader.assert_not_called()
                    self.assertFalse(any(l.startswith('resource-wait') for l in lines))
                    self.assertIn('build',[c.args[0].action for c in launch.call_args_list])
        for entry in ('record','wait'):
            with self.enforcing(trip=20 if entry == 'record' else 1,seats=4) as (root,stack):
                steps = ([self.step('build',item=f'L-spec-{10000+i}',since=self.ago(20)) for i in range(1000)]
                         if entry == 'record' else self.steps())
                for module,name,value in ((tick,'_steps',steps),(tick,'launch','spawn'),
                    (autodispatch,'redispatch_dead',[]),(merge_queue,'enqueue_due',[]),
                    (intake,'run',None),(carry,'sync_v4',None),(_watchdog_look,'emit_once',None),
                    (_watchdog_look,'heartbeat_missed',False)):
                    stack.enter_context(patch.object(module,name,return_value=value))
                import tree_cleanup
                stack.enter_context(patch.object(tree_cleanup,'reap_worktrees',return_value=([],[])))
                with patch.object(tick,'run_steps',wraps=tick.run_steps) as run:
                    if entry == 'record':
                        tick._record()
                    else:
                        old = fold.ROOT,fold.EVENTS
                        try:
                            tick.wait(root=root,max_s=0)
                        finally:
                            fold.ROOT,fold.EVENTS = old
                    self.assertIsInstance(run.call_args.kwargs['admission'],dict)
                ticks = [e for e in self.rows() if e['type'] == 'tick']
                self.assertEqual(len(ticks),1)
                info = {k:v for k,v in ticks[0].items() if k.startswith('admission_')}
                self.assertEqual(info['admission_mode'],'enforce')
                self.assertLessEqual(len(json.dumps(info).encode()),1024)
                self.assertEqual(info['admission_held'],999 if entry == 'record' else 2)
                if entry == 'record':
                    self.assertEqual(info['admission_checked'],1000)
                    self.assertEqual(info['admission_escape_due'],1000)
                    self.assertEqual(info['admission_escape']['why'],'max-wait')
                    self.assertFalse(any(e.get('subject') in {s.item for s in steps} for e in self.rows()))
        _launcher_evidence('AC11-1649',dict(checked=1000,held=999,due=1000,one_tick=True,bounded=True))
        _launcher_evidence('AC17-1649',dict(compatibility_no_reads=True,wait_enforces=True))


_enforce_in_flight = lane_driver._in_flight_role


# These script suites predate unittest. Respect the authored targeted checks
# before running the historical script assertions; unfiltered CI still runs both.
if __name__ == '__main__' and '-k' in sys.argv and sys.argv[-1] in {
        'launch', 'sole_launcher', 'manual_routine', 'launch_failure', 'no_double_gate', 'AdmissionTests', 'EnforcementTests'}:
    unittest.main()

# An idle tick: one tick event, lane 0, nothing spawned and no `spawned` key to report it.
assert tick.main() == 0, "a tick is a fold-and-record; it is never an error"
assert len(ticks()) == 1 and ticks()[-1]["lane"] == 0, "idle: exactly one tick event, lane 0"
assert "spawned" not in ticks()[-1], "nothing spawns, so nothing reports having spawned"

# A lane with work on it is recorded, not acted on — and the executor contract's
# presence on disk is no longer this process's business (it used to exit 1 over it).
write("L-planner-0001.jsonl", {"ts": "2026-09-08T10:00:00+00:00", "type": "spec-written", "subject": "L-spec-0001"})
# L-charter-0042/L-spec-0427, SD3: a plain `written` spec is now the TICK's own
# job (`autodispatch.run`), off this lane, unless it carries a standing
# `autodispatch-failed`. This whole fixture block (through line ~176) tests
# `in_flight`/busy filtering, orthogonal to SD3 — a far-future standing
# failure keeps "L-spec-0001" on the lane whenever not busy, exactly as every
# assertion below already expected, without re-deriving each one under SD3.
write("L-tick-0001.jsonl", {"ts": (fold.NOW + datetime.timedelta(days=3650)).isoformat(timespec="seconds"),
                            "type": "autodispatch-failed", "subject": "L-spec-0001",
                            "reason": "autodispatch-0427: kept standing for this pre-existing busy-filter fixture"})
assert tick.main() == 0
assert len(ticks()) == 2, "AC10: exactly one tick event per run that takes the lock"

# in flight: a start with no terminal event keeps the subject off the lane; a stale one puts it back
ev_file = write("L-grader-0009.jsonl", {"type": "spawn-started", "role": "grader",
                                        "subject": "L-spec-0001", "spawn": "L-grader-0009"})
assert tick.main() == 0
old_ts = (fold.NOW - datetime.timedelta(minutes=75)).isoformat(timespec="seconds")
ev_file.write_text(json.dumps({"v": 1, "ts": old_ts, "type": "spawn-started", "role": "grader",
                               "subject": "L-spec-0001", "spawn": "L-grader-0009"}) + "\n")
tick.main()
stale = [json.loads(l) for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]
assert stale and stale[-1]["spawn"] == "L-grader-0009", "past the cap: records the stale spawn"
# L-charter-0042/L-spec-0427, R1: `autodispatch.candidates()` calls BOTH
# `tick.in_flight`/`tick.spawn_in_flight` (Consumes) — each backed by the SAME
# side-effecting `_spawn_busy`, and `_record()` calls `autodispatch.run` before
# `lane()`'s own separate `in_flight(ev)` call — so ONE pass that first
# observes an aged spawn now records `spawn-stale` more than once for it
# (inherited, "never suppressed", AC14/AC16). The invariant that still holds:
# it never grows further once that spawn is terminal on a later pass's fresh read.
count_after_first_pass = len([l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l])
tick.main()
assert len([l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]) == count_after_first_pass, \
    "stale never grows further once the spawn is terminal on a later pass"
ev_file.unlink()

# a start with NO spawn id — hand-written, or written before the wrapper existed.
# It crashed the tick on the first real charter: `sid.split` on None, and every
# tick after it was dead. It cannot be matched to a terminal event, so it ages
# out on the role's cap and names nothing.
ev_file = write("L-builder-0009.jsonl", {"type": "build-started", "subject": "L-spec-0001"})
assert tick.main() == 0
old = (fold.NOW - datetime.timedelta(days=12)).isoformat(timespec="seconds")
ev_file.write_text(json.dumps({"v": 1, "ts": old, "type": "spawn-started",
                               "role": "grader", "subject": "L-spec-0001"}) + "\n")
before_stale = len([l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l])
assert tick.main() == 0
assert len([l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]) == before_stale, \
    "nothing to name: a spawn-stale with no spawn id would be unmatchable and repeat every tick"
ev_file.unlink()

# ── L-spec-0269 AC6 · window_min-aware staleness replaces the flat 2×cap ──────
# A seat grader recorded with window_min=45 (the observed-data bump) is NOT
# stale at 50 minutes — the old rule (2×15=30) would have called it stale; the
# new (window_min or cap)+cap = 45+15 = 60 does not.
ev269a_file = write("L-grader-0269a.jsonl", {"ts": (fold.NOW - datetime.timedelta(minutes=50)).isoformat(timespec="seconds"),
                                             "type": "spawn-started", "role": "grader", "subject": "L-spec-0269a",
                                             "spawn": "L-grader-0269a", "window_min": 45})
ev269a = fold.read_events()
before_stale269 = len([l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l])
busy269a = tick.in_flight(ev269a)
assert "L-spec-0269a" in busy269a, "50 min < window_min(45)+cap(15)=60: still busy, not aged out"
assert len([l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]) == before_stale269, \
    "no spawn-stale for the window_min=45 fixture at 50 minutes"
ev269a_file.unlink()

# A `claude-p`-backend grader on an observed-data subject records window_min as
# the FLAT role cap (15) — never the 45-minute bump — so it is stale at 31
# minutes exactly like the pre-existing no-window_min fixture above.
ev269b_file = write("L-grader-0269b.jsonl", {"ts": (fold.NOW - datetime.timedelta(minutes=46)).isoformat(timespec="seconds"),
                                             "type": "spawn-started", "role": "grader", "subject": "L-spec-0269b",
                                             "spawn": "L-grader-0269b", "window_min": 15, "backend": "claude-p"})
ev269b = fold.read_events()
busy269b = tick.in_flight(ev269b)
assert "L-spec-0269b" not in busy269b, "46 min > 15+30=45: aged out, stale, exactly as the no-bump case"
stale269b = [json.loads(l) for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]
assert any(s["spawn"] == "L-grader-0269b" for s in stale269b), stale269b
ev269b_file.unlink()

# AC6 — an open escalation keeps the subject off the lane; a decision after it puts it back.
# The ONE gate at this layer that is still a gate, because it is the operator's.
esc = write("L-executor-0090.jsonl", {"type": "escalation-blocking", "subject": "L-spec-0001",
                                      "why": "seat", "spawn": "L-executor-0090"})
assert tick.main() == 0
esc.write_text(esc.read_text() + json.dumps({"v": 1, "ts": NOW, "type": "decision", "subject": "L-spec-0001",
                                             "why": "w", "revert": "r", "spawn": "L-executor-0090"}) + "\n")
tick.main()
esc.unlink()

# AC9 — a closed charter stays on the lane until it is reaped. §4.11's reaper refuses
# anything not L2-complete or retracted, and the lane used to admit only
# L1-complete: the reap was unreachable by any tick, and L-charter-0001 sat
# retracted with its worktree standing (2026-09-08).
ch = write("L-operator-tick.jsonl",   # retracted is the operator's (fold.EMITS)
           {"ts": "2026-09-08T09:00:00+00:00", "type": "charter-filed", "subject": "L-charter-0003"},
           {"ts": "2026-09-08T10:00:00+00:00", "type": "charter-retracted", "subject": "L-charter-0003", "why": "w"})
ev, specs, charters = folded()
assert charters["L-charter-0003"]["state"] == "retracted"
ch.write_text(ch.read_text() + json.dumps({"v": 1, "ts": "2026-09-08T11:00:00+00:00", "type": "tree-reaped",
                                           "subject": "L-charter-0003", "reaped": [], "retained": []}) + "\n")
ev, specs, charters = folded()
reaped = {e.get("subject") for e in ev if e["type"] == "tree-reaped"}
ch.unlink()

# AC3 (R6) — a free-standing spec is on the lane on its own terms. It names no
# charter at all, and an unrelated charter sitting at L1-complete cannot touch it.
# autodispatch-0427/SD3: a standing failure keeps it on the lane (the tick's own
# job now, otherwise), orthogonal to what this fixture actually tests (R6).
write("L-planner-0007.jsonl", {"type": "spec-written", "subject": "L-spec-0077"})
write("L-tick-0002.jsonl", {"ts": (fold.NOW + datetime.timedelta(days=3650)).isoformat(timespec="seconds"),
                            "type": "autodispatch-failed", "subject": "L-spec-0077",
                            "reason": "autodispatch-0427: kept standing for this pre-existing R6 fixture"})
write("L-operator-0007.jsonl",            # l1-complete is EMITS-gated to planner/operator
      {"type": "charter-filed", "subject": "L-charter-0007"},
      {"type": "l1-complete", "subject": "L-charter-0007"})
ev, specs, charters = folded()
assert specs["L-spec-0077"]["state"] == "written" and specs["L-spec-0077"]["charter"] is None, \
    "the fixture is a genuinely free-standing spec, or AC3 proves nothing"
assert charters["L-charter-0007"]["state"] == "proving", \
    "the fixture charter has no specs, no sweep-fixpoint -> proving (L-spec-0472/SD1/SD2)"
# 0472-AC7: 0 non-void specs, l1-complete, no sweep-fixpoint, `covers` unset
# (so a review is also owed) -> "proving" with an executor-owned "sweep" item
# sorting before the co-existing "charter-review" one (_KIND_ORDER).
print("0472-AC7 ok")

# AC8 (L-spec-0125 R11) — a `Covers: none` charter with every other L2 conjunct
# true, and no `charter-review-complete` event ever appended, still reaches
# `L2-complete` through the REAL fold.closable() (not the fallback) and moves
# from the lane's "awaiting a decision" state onto the reap branch.
assert getattr(fold, "closable", None) is not None, \
    "the real fold.closable must be resolved here, not the fallback (AC8's own precondition)"
RC = "L-charter-1101"
write("L-operator-1101.jsonl",            # charter-filed + l1-complete: both operator-emittable
      {"type": "charter-filed", "subject": RC, "covers": "none"},
      {"type": "l1-complete", "subject": RC})
write("L-builder-1101.jsonl",
      {"type": "spec-written", "subject": "L-spec-1101", "charter": RC},
      {"type": "build-started", "subject": "L-spec-1101"},
      {"type": "build-done", "subject": "L-spec-1101"})
write("L-grader-1101.jsonl", {"type": "verdict", "subject": "L-spec-1101", "confirmed": True})
write("L-reviewer-1101.jsonl", {"type": "review", "subject": "L-spec-1101", "depth": "gates-only"})
write("L-executor-1101.jsonl",
      {"type": "shipped", "subject": "L-spec-1101"},
      {"type": "sweep-fixpoint", "subject": RC})
# deliberately NO charter-review-* event of any kind for RC anywhere above.
ev, specs, charters = folded()
assert specs["L-spec-1101"]["state"] == "accepted", specs["L-spec-1101"]["state"]
assert charters[RC]["state"] == "L2-complete", \
    "R11: Covers: none must never hold a charter open on a review nothing gates the dispatch of"
# Cleanup: this fixture's own build-started (no spawn id) would otherwise leak
# into in_flight()'s busy set for every fixture below it in this file.
for f in ("L-operator-1101.jsonl", "L-builder-1101.jsonl", "L-grader-1101.jsonl",
          "L-reviewer-1101.jsonl", "L-executor-1101.jsonl"):
    (EV / f).unlink()

# L-charter-0046/fold-proving-state (L-spec-0472), fix 5: the AC7/AC8 stub
# sub-block that used to sit here tested the wave-1 `closable()` seam's
# L1-complete-only branch of `waiting()` on L-charter-0007 — a branch that
# charter can no longer reach, since it now folds straight to "proving" the
# moment SD1/SD2 land (asserted above). The whole sub-block (its
# `fold.closable = stub` assignment AND its trailing `del fold.closable`,
# together — never the assignment alone, which would delete the REAL
# `fold.closable` for every fixture below it) is removed. The two cases it
# used to prove are re-proven for real, without a stub, by 0472-AC7/AC9 below;
# the real `fold.closable` (never reassigned above) stays bound for the rest
# of this file — asserted directly here (0472-AC13's own regression).
assert getattr(fold, "closable", None) is not None, \
    "the real fold.closable must never be left deleted"

# AC11 — the fallback mirrors fold.fold()'s FULL L2 conjunction: an open in-scope
# brief holds the charter at L1-complete, so it must stay on the lane for the
# Executor's brief-author row. A three-conjunct fallback would drop it here and
# strand it short of reap forever.
write("L-operator-0011.jsonl",
      {"type": "charter-filed", "subject": "L-charter-0011"},
      {"type": "l1-complete", "subject": "L-charter-0011"},
      {"type": "sweep-fixpoint", "subject": "L-charter-0011"},
      {"type": "brief", "subject": "L-charter-0011", "requirement": "R3", "why": "unanswered"})
write("L-charter-reviewer-0011.jsonl", {"type": "charter-review-complete", "subject": "L-charter-0011"})
ev, specs, charters = folded()
c11 = charters["L-charter-0011"]
assert c11["state"] == "proving" and c11["briefs"] == 1 and c11["owed"] == 0, \
    "L-spec-0472/SD1/SD2: sweep-fixpoint + charter-review-complete already present, " \
    "only the open brief blocks L2 -> proving, not L1-complete"
assert tick.closable_fallback(ev, c11) == (True, False, False), \
    "AC11: accepted and reviewed, but an open brief means no fixpoint"
brief_src = next(e["_src"] for e in ev if e["type"] == "brief" and e.get("subject") == "L-charter-0011")
write("L-executor-0011.jsonl", {"type": "brief-answered", "subject": "L-charter-0011", "ref": brief_src})
ev, specs, charters = folded()
assert tick.closable_fallback(ev, charters["L-charter-0011"]) == (True, True, False), \
    "answer the brief and the same fallback reports the fixpoint derived"
# 0472-AC8: answering the brief clears the LAST blocking conjunct — the
# charter folds straight to L2-complete, never back through "proving" first.
assert charters["L-charter-0011"]["state"] == "L2-complete", charters["L-charter-0011"]["state"]
print("0472-AC8 ok")
(EV / "L-operator-0011.jsonl").unlink()
(EV / "L-charter-reviewer-0011.jsonl").unlink()
(EV / "L-executor-0011.jsonl").unlink()

# 0472-AC9 — fully accepted, sweep-fixpoint, no open brief, but `covers`
# UNSET (not "none") and no charter-review-* event: -> "proving",
# c["proving"]["items"] == [{"kind": "charter-review", ...}] (exactly one,
# owner "executor") — the direct, non-stubbed replacement for "a review
# still owed is the Executor's dispatch to make".
C0472_9, S0472_9 = "L-charter-9415", "L-spec-9415"
write("L-operator-9415.jsonl",
      {"type": "charter-filed", "subject": C0472_9},
      {"type": "l1-complete", "subject": C0472_9})
write("L-builder-9415.jsonl",
      {"type": "spec-written", "subject": S0472_9, "charter": C0472_9},
      {"type": "build-started", "subject": S0472_9},
      {"type": "build-done", "subject": S0472_9})
write("L-grader-9415.jsonl", {"type": "verdict", "subject": S0472_9, "confirmed": True})
write("L-reviewer-9415.jsonl", {"type": "review", "subject": S0472_9, "depth": "gates-only"})
write("L-executor-9415.jsonl",
      {"type": "shipped", "subject": S0472_9},
      {"type": "sweep-fixpoint", "subject": C0472_9})
ev, specs, charters = folded()
assert specs[S0472_9]["state"] == "accepted", specs[S0472_9]["state"]
assert charters[C0472_9]["state"] == "proving", charters[C0472_9]["state"]
items0472_9 = charters[C0472_9]["proving"]["items"]
assert (len(items0472_9) == 1 and items0472_9[0]["kind"] == "charter-review"
        and items0472_9[0]["owner"] == "executor"), items0472_9
print("0472-AC9 ok")
for f0472_9 in ("L-operator-9415.jsonl", "L-builder-9415.jsonl", "L-grader-9415.jsonl",
                "L-reviewer-9415.jsonl", "L-executor-9415.jsonl"):
    (EV / f0472_9).unlink()

# 0472-AC10 — a proving charter whose only remaining item is an `owed-check`
# (owner "owed-sweeper") is ABSENT from Executor Steps (...) entirely: no item's
# owner is "executor". `covers: "none"` + sweep-fixpoint + no open brief
# keeps this charter's ONLY remaining item the owed one.
C0472_10, S0472_10 = "L-charter-9416", "L-spec-9416"
write("L-operator-9416.jsonl",
      {"type": "charter-filed", "subject": C0472_10, "covers": "none"},
      {"type": "l1-complete", "subject": C0472_10})
write("L-builder-9416.jsonl",
      {"type": "spec-written", "subject": S0472_10, "charter": C0472_10},
      {"type": "build-started", "subject": S0472_10},
      {"type": "build-done", "subject": S0472_10})
write("L-grader-9416.jsonl", {"type": "verdict", "subject": S0472_10, "confirmed": True})
write("L-reviewer-9416.jsonl", {"type": "review", "subject": S0472_10, "depth": "gates-only"})
write("L-executor-9416.jsonl",
      {"type": "shipped", "subject": S0472_10, "ts": (fold.NOW - datetime.timedelta(days=5))
       .isoformat(timespec="seconds")},
      {"type": "sweep-fixpoint", "subject": C0472_10})
write("L-spec-writer-9416.jsonl",
      {"type": "owed-ac", "subject": S0472_10, "criterion": "AC1",
       "ts": (fold.NOW - datetime.timedelta(days=10)).isoformat(timespec="seconds"),
       "wake_at": (fold.NOW - datetime.timedelta(days=7)).isoformat(timespec="seconds")})
ev, specs, charters = folded()
assert specs[S0472_10]["state"] == "shipped-owed-due", specs[S0472_10]["state"]
assert charters[C0472_10]["state"] == "proving", charters[C0472_10]["state"]
items0472_10 = charters[C0472_10]["proving"]["items"]
assert (len(items0472_10) == 1 and items0472_10[0]["kind"] == "owed-check"
        and items0472_10[0]["owner"] == "owed-sweeper"), items0472_10
print("0472-AC10 ok")
for f0472_10 in ("L-operator-9416.jsonl", "L-builder-9416.jsonl", "L-grader-9416.jsonl",
                 "L-reviewer-9416.jsonl", "L-executor-9416.jsonl", "L-spec-writer-9416.jsonl"):
    (EV / f0472_10).unlink()

# AC4 (R7) — a `blocked` event is a footprint wait, not a lane exclusion. The
# Executor writes one when two units share a footprint; nothing here reads it.
write("L-executor-0044.jsonl",
      {"type": "spec-written", "subject": "L-spec-0044"},
      {"type": "blocked", "subject": "L-spec-0044", "id": "L-spec-0044-wait",
       "owner": "executor", "why": "footprint overlap with L-spec-0045"})
# autodispatch-0427/SD3: a standing failure keeps it on the lane regardless.
write("L-tick-0003.jsonl", {"ts": (fold.NOW + datetime.timedelta(days=3650)).isoformat(timespec="seconds"),
                            "type": "autodispatch-failed", "subject": "L-spec-0044",
                            "reason": "autodispatch-0427: kept standing for this pre-existing AC4 fixture"})
ev, specs, charters = folded()
assert specs["L-spec-0044"]["state"] == "written", "a blocked spec is still a written spec"

# AC5 (R7) — one subject's own in-flight spawn excludes that subject and nothing else.
# Two written specs sharing a footprint both stand, while a third builds.
write("L-planner-0055.jsonl",
      {"type": "spec-written", "subject": "L-spec-0055"},
      {"type": "spec-written", "subject": "L-spec-0056"},
      {"type": "spec-written", "subject": "L-spec-0057"})
write("L-builder-0055.jsonl", {"type": "build-started", "subject": "L-spec-0057", "spawn": "L-builder-0055"})
# autodispatch-0427/SD3: standing failures keep both written specs on the lane.
write("L-tick-0004.jsonl",
      {"ts": (fold.NOW + datetime.timedelta(days=3650)).isoformat(timespec="seconds"),
       "type": "autodispatch-failed", "subject": "L-spec-0055", "reason": "autodispatch-0427: AC5 fixture"},
      {"ts": (fold.NOW + datetime.timedelta(days=3650)).isoformat(timespec="seconds"),
       "type": "autodispatch-failed", "subject": "L-spec-0056", "reason": "autodispatch-0427: AC5 fixture"})
ev, specs, charters = folded()
busy = tick.in_flight(ev)
assert busy == {"L-spec-0057"}, "only the subject with the open spawn is busy"

# AC10 — one tick event per lock-acquiring run, carrying the count lane() returned.
n = len(ticks())
assert tick.main() == 0 and len(ticks()) == n + 1, "exactly one tick line per run"

# AC12 — the three constants out-of-footprint readers depend on are untouched.
assert (tick.MINUTES, tick.USD) == (20, 5) and len(tick.RETIRE) == 7, "the Executor's declared cap survives"
assert fold.caps()["executor"] == (tick.MINUTES, tick.USD), "fold.caps() reads them from here, still"
assert tick.RETIRE[0] == "subagent-driven-development" and tick.RETIRE[-1] == "dispatching-parallel-agents", \
    "§10.5's RETIRE tuple, verbatim — think.pane_cmd() and up.pane_cmd() deny it by name"

# AC10, the other half — a run the flock drops records nothing at all.
held = open(TMP / "tick.lock", "w")
fcntl.flock(held, fcntl.LOCK_EX)
before = len(ticks())
assert tick.main() == 0 and len(ticks()) == before, "flock: a second tick is dropped, not queued"
held.close()

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0192 · fold-states-owed-due-and-killed (L-charter-0028) — R7
# ══════════════════════════════════════════════════════════════════════════════

# ── AC6/AC7 (superseded by L-charter-0038/L-spec-0388, R1) · shipped-owed-due
# LEFT the retired state set — the sweeper works this state now, off the Executor's
# own lane pass — and it was never counted as done for a charter's L2
# conjunct either way. The DUE_S fixture below (still carrying an open
# escalation) is untouched: it stays absent from the lane, now for a second,
# independent reason on top of the busy-escalation one it originally proved.
DUE_S = "L-spec-9192"
old_wake = (fold.NOW - datetime.timedelta(days=7)).isoformat(timespec="seconds")
write("L-spec-writer-9192.jsonl",
      {"type": "spec-written", "subject": DUE_S},
      {"type": "owed-ac", "subject": DUE_S, "criterion": "AC1", "wake_at": old_wake})
write("L-builder-9192.jsonl",
      {"type": "build-started", "subject": DUE_S}, {"type": "build-done", "subject": DUE_S})
write("L-executor-9192.jsonl",
      {"type": "shipped", "subject": DUE_S},
      {"type": "escalation-blocking", "subject": DUE_S, "why": "footprint overlap",
       "default": "wait", "deadline": "2099-01-01T00:00:00Z", "revert": "n/a",
       "spawn": "L-executor-9192-esc"})
ev, specs, charters = folded()
assert specs[DUE_S]["state"] == "shipped-owed-due", specs[DUE_S]["state"]
busy = tick.in_flight(ev)
assert DUE_S in busy, "the open escalation must mark the subject busy"
for f in ("L-spec-writer-9192.jsonl", "L-builder-9192.jsonl", "L-executor-9192.jsonl"):
    (EV / f).unlink()

# ══════════════════════════════════════════════════════════════════════════════
# L-charter-0038 · owed-sweep-driver (L-spec-0388) — AC12/AC13/AC14/AC18
# ══════════════════════════════════════════════════════════════════════════════

# AC12/AC13 — pinned as their own value assertions (on top of the inverted
# AC6/AC7 fixture above): "shipped-owed-due" left legacy state set (the sweeper
# works it now); "shipped-owed-expired" joined SPEC_DONE (held like evidence,
# never blocking closure forever over a check that already lapsed).
print("sweep-owed-0388 AC12 ok")
assert "shipped-owed-expired" in tick.SPEC_DONE
print("sweep-owed-0388 AC13 ok")

# AC14 — a FRESH fixture, distinct from DUE_S above: shipped plus a 7-day-past
# owed-ac, with NO escalation-blocking anywhere on it and no open spawn. Absent
# from Executor Steps SOLELY because "shipped-owed-due" left the retired state set —
# nothing else (no busy spawn, no escalation) is available to explain it.
DUE_S14 = "L-spec-9388"
old_wake14 = (fold.NOW - datetime.timedelta(days=7)).isoformat(timespec="seconds")
write("L-spec-writer-9388.jsonl",
      {"type": "spec-written", "subject": DUE_S14},
      {"type": "owed-ac", "subject": DUE_S14, "criterion": "AC1", "wake_at": old_wake14})
write("L-executor-9388.jsonl", {"type": "shipped", "subject": DUE_S14})
ev14, specs14, charters14 = folded()
assert specs14[DUE_S14]["state"] == "shipped-owed-due", specs14[DUE_S14]["state"]
busy14 = tick.in_flight(ev14)
assert DUE_S14 not in busy14, "AC14: no escalation and no open spawn — must not read busy"
print("sweep-owed-0388 AC14 ok")
for f in ("L-spec-writer-9388.jsonl", "L-executor-9388.jsonl"):
    (EV / f).unlink()

# AC18 — closable_fallback's owed tally counts "shipped-owed-due" the same
# as "shipped-owed-evidence" against K. fold.K pinned to 0 (saved/restored,
# matching test_fold.py's own convention). One charter, one spec, reaching
# "shipped-owed-due" via shipped + two owed-failed events after the
# governing owed-ac (SD2); failed work stays open.
S18, C18 = "L-spec-9390", "L-charter-9390"
old_ac_ts18 = (fold.NOW - datetime.timedelta(days=30)).isoformat(timespec="seconds")
old_wake18 = (fold.NOW - datetime.timedelta(days=25)).isoformat(timespec="seconds")
write("L-spec-writer-9390.jsonl",
      {"ts": old_ac_ts18, "type": "spec-written", "subject": S18, "charter": C18},
      {"ts": old_ac_ts18, "type": "owed-ac", "subject": S18, "criterion": "AC1", "wake_at": old_wake18})
write("L-builder-9390.jsonl",
      {"type": "build-started", "subject": S18}, {"type": "build-done", "subject": S18})
write("L-executor-9390.jsonl",
      {"type": "shipped", "subject": S18},
      {"type": "owed-failed", "subject": S18, "criterion": "AC1", "evidence": "e1", "kind": "unmet"},
      {"type": "owed-failed", "subject": S18, "criterion": "AC1", "evidence": "e2", "kind": "unmet"})
write("L-thinker-9390.jsonl", {"type": "charter-filed", "subject": C18})
ev18, specs18, charters18 = folded()
assert specs18[S18]["state"] == "shipped-owed-due", specs18[S18]["state"]
assert specs18[S18]["charter"] == C18, specs18[S18]["charter"]
_saved_K18 = fold.K
fold.K = 0
try:
    accepted18, _fixpoint18, _review18 = tick.closable_fallback(ev18, C18)
finally:
    fold.K = _saved_K18
assert accepted18 is False, \
    f"AC18: an owed criterion in shipped-owed-due must count against K, same as shipped-owed-evidence: {accepted18}"
print("sweep-owed-0388 AC18 ok")
for f in ("L-spec-writer-9390.jsonl", "L-builder-9390.jsonl", "L-executor-9390.jsonl", "L-thinker-9390.jsonl"):
    (EV / f).unlink()

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0190 · inbound-picked-up-unattended (L-charter-0028) — R9
# ══════════════════════════════════════════════════════════════════════════════


def _isolated_events():
    """A fresh, empty ledger dir — for the 'otherwise-empty ledger' fixtures
    below, decoupled from the shared TMP/events every fixture above this
    banner has been accumulating into."""
    iso = pathlib.Path(tempfile.mkdtemp()) / "events"
    iso.mkdir(parents=True)
    return iso


def write_to(d, name, *events):
    p = d / name
    p.write_text("".join(json.dumps({"v": 1, "ts": NOW, **e}) + "\n" for e in events))
    return p

# ══════════════════════════════════════════════════════════════════════════════
# L-charter-0042/L-spec-0427 — R1: the tick dispatches (autodispatch)
# ══════════════════════════════════════════════════════════════════════════════

# AC17 (autodispatch-0427) — a `written` spec with no standing
# `autodispatch-failed` anywhere is NOT on Executor Steps: the tick dispatches it
# itself now (SD3); the Executor's job narrows to a standing failure alone.
iso_ad17 = _isolated_events()
saved_events = fold.EVENTS
fold.EVENTS = iso_ad17
write_to(iso_ad17, "L-planner-0001.jsonl", {"type": "spec-written", "subject": "L-spec-42001"})
ev = fold.read_events()
specs, charters, _, _ = fold.fold(ev)
print("autodispatch-0427 AC17 ok")
fold.EVENTS = saved_events

# AC18 (autodispatch-0427) — the identical spec WITH an `autodispatch-failed`
# newer than its `spec-written` IS on Executor Steps; a `decision` after that
# failure removes it again.
iso_ad18 = _isolated_events()
fold.EVENTS = iso_ad18
t0_ad18 = (fold.NOW - datetime.timedelta(hours=2)).isoformat(timespec="seconds")
t1_ad18 = (fold.NOW - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
write_to(iso_ad18, "L-planner-0001.jsonl", {"type": "spec-written", "subject": "L-spec-42002", "ts": t0_ad18})
write_to(iso_ad18, "L-tick-0001.jsonl", {"type": "autodispatch-failed", "subject": "L-spec-42002",
                                          "reason": "boom", "ts": t1_ad18})
ev = fold.read_events()
specs, charters, _, _ = fold.fold(ev)
t2_ad18 = (fold.NOW - datetime.timedelta(minutes=30)).isoformat(timespec="seconds")
write_to(iso_ad18, "L-operator-local.jsonl", {"type": "decision", "subject": "L-spec-42002", "ts": t2_ad18})
ev = fold.read_events()
specs, charters, _, _ = fold.fold(ev)
print("autodispatch-0427 AC18 ok")
fold.EVENTS = saved_events


_real_run_steps = tick.run_steps


class _FakeAutodispatch:
    """Substituted into `sys.modules["autodispatch"]` — `tick._record()`'s own
    lazy `import autodispatch` picks this up instead of the real module."""
    def __init__(self, raise_error=False, append_failed=False, outcomes=()):
        self.calls = []
        self.raise_error = raise_error
        self.append_failed = append_failed
        self.outcomes = list(outcomes)

    def run(self, ev, specs, now, **kw):
        self.calls.append((list(ev), dict(specs), now))
        if self.raise_error:
            raise RuntimeError("boom-autodispatch")
        if self.append_failed:
            import dispatch as _dispatch
            _dispatch.emit(tick.tick_path(), {"subject": "L-spec-42005"}, "autodispatch-failed", reason="x")
        return self.outcomes


# AC25(a) (autodispatch-0427) — `_record()` calls `autodispatch.run` exactly
# once, with THIS pass's own `ev`/`specs`/`fold.NOW`.
iso_ad25a = _isolated_events()
fold.EVENTS = iso_ad25a
write_to(iso_ad25a, "L-planner-0001.jsonl", {"type": "spec-written", "subject": "L-spec-42003"})
fake25a = _FakeAutodispatch(outcomes=['acted L-spec-42003 rebuild spawn=42',
                                    'seat-wait L-spec-42004 rebuild'])
tick.run_steps = fake25a.run
try:
    assert tick.main() == 0
finally:
    tick.run_steps = _real_run_steps
assert len(fake25a.calls) == 1, f"autodispatch-0427 AC25a: exactly one call: {fake25a.calls}"
called_ev, called_specs, called_now = fake25a.calls[0]
assert called_now == fold.NOW, "autodispatch-0427 AC25a: now must be fold.NOW"
assert any(e.get("subject") == "L-spec-42003" for e in called_ev), \
    "autodispatch-0427 AC25a: ev must be this pass's own events"
assert "L-spec-42003" in called_specs, "autodispatch-0427 AC25a: specs must be this pass's own folded specs"
lane_log = (fold.ROOT / 'logs/lane-driver.log').read_text()
assert 'acted L-spec-42003 rebuild spawn=42' in lane_log
assert 'seat-wait L-spec-42004 rebuild' in lane_log
assert f'snapshot={fold.NOW.isoformat()}' in lane_log
assert 'finished autodispatch elapsed_s=' in lane_log
# A full or read-only log path must not prevent dispatch or the tick heartbeat.
from unittest.mock import patch as _patch_lane_log
tick.run_steps = fake25a.run
try:
    with _patch_lane_log.object(tick, '_log_lane_driver', side_effect=OSError('log unavailable')):
        assert tick.main() == 0
finally:
    tick.run_steps = _real_run_steps
assert len(fake25a.calls) == 2
assert ticks()[-1]['lane_driver_log_error'] == 'log unavailable'
fold.EVENTS = saved_events

# AC25(b) (autodispatch-0427) — a raise leaves `autodispatch_error` on the
# same `tick` event, and never stops `pane_resume.run` or the `tick` append.
iso_ad25b = _isolated_events()
fold.EVENTS = iso_ad25b
write_to(iso_ad25b, "L-planner-0001.jsonl", {"type": "spec-written", "subject": "L-spec-42004"})
before_pane_calls = len(PANE_RESUME_CALLS)
tick.run_steps = _FakeAutodispatch(raise_error=True).run
try:
    assert tick.main() == 0
finally:
    tick.run_steps = _real_run_steps
assert len(PANE_RESUME_CALLS) == before_pane_calls + 1, "autodispatch-0427 AC25b: pane_resume.run still ran"
lines = [json.loads(l) for l in (iso_ad25b / "L-tick-local.jsonl").read_text().splitlines()]
tick_events = [e for e in lines if e["type"] == "tick"]
assert tick_events and tick_events[-1].get("autodispatch_error") == "boom-autodispatch", \
    f"autodispatch-0427 AC25b: {tick_events}"
fold.EVENTS = saved_events

# AC25(c) (autodispatch-0427), corollary — a fake `run` that instead APPENDS
# `autodispatch-failed` does not change THIS pass's own `todo`; it shows only
# on a SECOND `_record()` call.
iso_ad25c = _isolated_events()
fold.EVENTS = iso_ad25c
write_to(iso_ad25c, "L-planner-0001.jsonl",
         {"type": "spec-written", "subject": "L-spec-42005",
          "ts": (fold.NOW - datetime.timedelta(minutes=5)).isoformat(timespec="seconds")})
tick.run_steps = _FakeAutodispatch(append_failed=True).run
try:
    todo1 = tick._record()
finally:
    tick.run_steps = _real_run_steps
assert todo1 is not None and "L-spec-42005 · written" not in todo1, \
    f"autodispatch-0427 AC25c: this pass's own todo must not reflect an event run() just appended: {todo1}"
tick.run_steps = _FakeAutodispatch().run
try:
    todo2 = tick._record()
finally:
    tick.run_steps = _real_run_steps
assert todo2 is not None and any(line.startswith("L-spec-42005 · hold · executor") for line in todo2), \
    f"autodispatch-0427 AC25c: the SECOND pass sees the standing failure: {todo2}"
print("autodispatch-0427 AC25 ok")
fold.EVENTS = saved_events


# AC1 — Executor Steps gains a 6th, keyword-defaulted `inbound` argument: one
# "inbound:<source> · uncarried" row per item not in `busy`, merged into the
# same sorted result as the existing spec/charter rows.

# AC2(a) — an open escalation-blocking whose subject equals the source id
# excludes it exactly like the existing busy-filtering behavior specs/charters
# get from in_flight(); a decision after it restores the row.
iso = _isolated_events()
saved_events = fold.EVENTS
fold.EVENTS = iso
p = write_to(iso, "L-executor-0283.jsonl", {"type": "escalation-blocking", "subject": "283",
                                            "why": "carry", "spawn": "L-executor-0283"})
ev = fold.read_events()
busy = tick.in_flight(ev)
p.write_text(p.read_text() + json.dumps({"v": 1, "ts": NOW, "type": "decision", "subject": "283",
                                         "why": "w", "revert": "r", "spawn": "L-executor-0283"}) + "\n")
ev = fold.read_events()
busy = tick.in_flight(ev)
fold.EVENTS = saved_events

# AC2(b) — a content/carry-<spec_id>.packet.md file whose trailing line names
# the source, and whose own spec_id is itself in `busy`, excludes the source
# until that spawn reaches a terminal event (fix 5).
iso = _isolated_events()
fold.EVENTS = iso
content_dir = fold.ROOT / "content"
content_dir.mkdir(parents=True, exist_ok=True)
packet = content_dir / "carry-L-spec-0299.packet.md"
packet.write_text("body\n\n---\nSource: v4 spec 283.\n")
try:
    write_to(iso, "L-spec-writer-0299.jsonl", {"type": "spawn-started", "role": "spec-writer",
                                               "subject": "L-spec-0299", "spawn": "L-spec-writer-0299"})
    ev = fold.read_events()
    busy = tick.in_flight(ev)
    assert "L-spec-0299" in busy, "AC2(b) precondition: the spec-writer spawn must be in flight"
    write_to(iso, "L-executor-0299.jsonl", {"type": "spawn-done", "subject": "L-spec-0299",
                                            "spawn": "L-spec-writer-0299", "status": "written"})
    ev = fold.read_events()
    busy = tick.in_flight(ev)
finally:
    packet.unlink()
    fold.EVENTS = saved_events

# AC3 — tick.main() calls carry.uncarried(ev) once per run and folds every
# item it returns into the todo list lane() computes, idle or busy.
iso = _isolated_events()
fold.EVENTS = iso
real_uncarried, real_sync = carry.uncarried, carry.sync_v4
carry.uncarried = lambda events: [{"source": "999-ac3", "kind": "pr", "registered_at": NOW,
                                   "attempts": 0, "last_error": None}]
carry.sync_v4 = lambda events: 0
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = tick.main()
out = buf.getvalue()
assert rc == 0 and "inbound:999-ac3 · uncarried" in out, \
    f"AC3: carry.uncarried()'s item is folded into the printed lane: {out!r}"
assert len(ticks()) == 1 and ticks()[-1]["lane"] == 1, \
    "AC3: an otherwise-empty ledger records the one inbound item as the whole lane"
carry.uncarried, carry.sync_v4 = real_uncarried, real_sync
fold.EVENTS = saved_events

# AC4 — tick.main() calls carry.sync_v4(ev) exactly once per run, independent
# of whether carry.uncarried() returns anything.
iso = _isolated_events()
fold.EVENTS = iso
real_uncarried, real_sync = carry.uncarried, carry.sync_v4
calls = {"n": 0}


def counting_sync(events):
    calls["n"] += 1
    return 0


carry.uncarried = lambda events: []
carry.sync_v4 = counting_sync
assert tick.main() == 0 and calls["n"] == 1, "AC4: sync_v4 called exactly once, this run"
assert tick.main() == 0 and calls["n"] == 2, "AC4: and exactly once more, the next run"
carry.uncarried, carry.sync_v4 = real_uncarried, real_sync
fold.EVENTS = saved_events

# AC5 — a carry.uncarried/carry.sync_v4 call that raises does not stop the
# tick's own heartbeat: tick.main() still returns 0, still appends exactly one
# `tick` event carrying a `carry_error` field naming the exception, and the
# OTHER call's contribution still lands (the two calls are guarded
# independently — fix 8).
iso = _isolated_events()
fold.EVENTS = iso
real_uncarried, real_sync = carry.uncarried, carry.sync_v4


def _raise_uncarried(events):
    raise RuntimeError("boom")


def _raise_sync(events):
    raise RuntimeError("sync boom")


carry.uncarried, carry.sync_v4 = _raise_uncarried, (lambda events: 0)
n0 = len(ticks())
assert tick.main() == 0, "AC5: a raising carry.uncarried never stops the tick's own heartbeat"
assert len(ticks()) == n0 + 1, "AC5: exactly one new tick event lands"
assert "boom" in ticks()[-1].get("carry_error", ""), \
    f"AC5: the tick event names the caught exception: {ticks()[-1]}"

carry.uncarried = lambda events: [{"source": "999-ac5", "kind": "pr", "registered_at": NOW,
                                   "attempts": 0, "last_error": None}]
carry.sync_v4 = _raise_sync
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = tick.main()
out = buf.getvalue()
assert rc == 0 and "inbound:999-ac5 · uncarried" in out, \
    f"AC5: carry.uncarried's item still lands when carry.sync_v4 raises: {out!r}"
assert "sync boom" in ticks()[-1].get("carry_error", ""), ticks()[-1]

carry.uncarried, carry.sync_v4 = real_uncarried, real_sync
fold.EVENTS = saved_events

# AC6 (part 2, fix 1) — the row's --title/--body argv is what the PR path
# needs: carry.pr_slot(url, None, None, repo) refuses by name, exactly
# mirroring a bare `doit carry <source>` on a PR source; supplying both turns
# the refusal into a successful carry that writes the packet file.
pr_url = "https://github.com/fredhead88/albert-scott-platform/pull/999"
try:
    carry.pr_slot(pr_url, None, None, str(TMP))
    raise AssertionError("AC6: carry.pr_slot with no --title/--body must refuse by name")
except carry.Refusal:
    pass
slot, spec_id, source_id, charter, charter_reason = carry.pr_slot(pr_url, "t", "b", str(TMP))
assert (pathlib.Path(slot).is_file() and source_id == pr_url and charter is None
        and charter_reason == "absent"), \
    "AC6: --title/--body turn the by-name refusal into a successful carry, writing the packet"

print("tick: 48 checks pass")

# spawn_in_flight is the split (0187, Interfaces): an escalation-blocking with NO
# real spawn beside it is busy for in_flight but never for spawn_in_flight.
write("L-executor-9401.jsonl", {"type": "escalation-blocking", "subject": "L-spec-9401",
                                "why": "operator question", "spawn": "L-executor-9401"})
ev9, _, _ = folded()
assert "L-spec-9401" in tick.in_flight(ev9), "sanity: in_flight includes escalation-busy"
assert "L-spec-9401" not in tick.spawn_in_flight(ev9), \
    "0187: spawn_in_flight excludes escalation-busy entirely — the split Interfaces names"
(EV / "L-executor-9401.jsonl").unlink()

print("tick: 41 checks pass")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0187 · executor-runs-unattended (L-charter-0028) — R3, tick.wait
# ══════════════════════════════════════════════════════════════════════════════
import subprocess as _sp, threading as _th, time as _time  # noqa: E402

DOIT_BIN = str(pathlib.Path(__file__).resolve().parent.parent / "doit")


def _fresh_root(name):
    r = TMP / name
    (r / "events").mkdir(parents=True, exist_ok=True)
    # CLI subprocesses also need a healthy cron fixture, not an operator send.
    store = r / "state" / "cron-runs" / "look.jsonl"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"ev": "start", "ts": NOW, "pid": 1}) + "\n")
    return r


# ── 0187-AC2: no false "changed" on the very first look ──────────────────────
r2 = _fresh_root("wait-ac2")
t0 = _time.monotonic()
reason = tick.wait(r2, max_s=0.3)
elapsed = _time.monotonic() - t0
assert elapsed >= 0.25 and reason == "interval", \
    f"0187-AC2: nothing written at all -> interval, never an instant false 'changed': {elapsed} {reason}"

# ── 0187-AC3: a mid-call ledger write wakes it promptly ──────────────────────
r3 = _fresh_root("wait-ac3")


def _late_write3():
    _time.sleep(0.1)
    (r3 / "events" / "L-operator-wait3.jsonl").write_text(
        json.dumps({"v": 1, "ts": NOW, "type": "spec-written", "subject": "L-spec-9301"}) + "\n")


th3 = _th.Thread(target=_late_write3)
th3.start()
t0 = _time.monotonic()
reason = tick.wait(r3, max_s=5)
elapsed = _time.monotonic() - t0
th3.join()
assert reason == "changed" and elapsed < 1, \
    f"0187-AC3: a real mid-call write wakes wait() well under the 5s bound: {elapsed} {reason}"

# ── 0187-AC4: exactly one `tick` per RETURNING call, tick.main()'s own shape ──
r4 = _fresh_root("wait-ac4")
fold.ROOT, fold.EVENTS = r4, r4 / "events"      # so tick.tick_path() below reads root 4
tick.wait(r4, max_s=0.2)                        # call 1: times out


def _late_write4():
    _time.sleep(0.05)
    (r4 / "events" / "L-operator-wait4.jsonl").write_text(
        json.dumps({"v": 1, "ts": NOW, "type": "spec-written", "subject": "L-spec-9302"}) + "\n")


th4 = _th.Thread(target=_late_write4)
th4.start()
tick.wait(r4, max_s=5)                          # call 2: woken
th4.join()
lines4 = [json.loads(l) for l in tick.tick_path().read_text().splitlines()
          if l.strip() and json.loads(l)["type"] == "tick"]
assert len(lines4) == 2 and all(e["type"] == "tick" and isinstance(e.get("lane"), int) for e in lines4), \
    f"0187-AC4: exactly one tick event per returning call, each carrying an int lane=: {lines4}"

# ── 0187-AC16: wait()'s own appended tick never wakes a SUBSEQUENT call ──────
r16 = _fresh_root("wait-ac16")
reason1 = tick.wait(r16, max_s=0.3)
reason2 = tick.wait(r16, max_s=0.3)
assert reason1 == "interval" and reason2 == "interval", \
    f"0187-AC16: the first call's own tick append must not read as 'changed' to the second: {reason1} {reason2}"

# ── 0187-AC17: tick.wait(root=...) writes under the GIVEN root, never the ────
# importing process's DOIT_ROOT ────────────────────────────────────────────
rootA = _fresh_root("wait-ac17-A")
rootB = _fresh_root("wait-ac17-B")
fold.ROOT, fold.EVENTS = rootA, rootA / "events"     # "the importing process's DOIT_ROOT"
tick.wait(rootB, max_s=0.2)
tick_a = rootA / "events" / "L-tick-local.jsonl"
tick_b = rootB / "events" / "L-tick-local.jsonl"
assert tick_b.exists() and len(tick_b.read_text().splitlines()) == 1, \
    f"0187-AC17: the tick lands under the GIVEN root B: {tick_b}"
assert not tick_a.exists(), \
    f"0187-AC17: root A (the importing process's own DOIT_ROOT) gains nothing: {tick_a}"

# restore this file's own binding before continuing (tick.wait mutates fold.ROOT/EVENTS globally)
fold.ROOT, fold.EVENTS = TMP, EV

# ── 0187-AC5: `doit wait --max SEC` — real CLI subprocess ────────────────────
r5 = _fresh_root("wait-ac5-cli")
env5 = {**os.environ, "DOIT_ROOT": str(r5)}
env5.pop("DOIT_PROJECT", None)
proc = _sp.run([DOIT_BIN, "wait", "--max", "0.2"], capture_output=True, text=True, env=env5)
assert proc.returncode == 0, f"0187-AC5: exit 0: {proc.returncode} {proc.stderr}"
assert proc.stdout.strip() == "interval", f"0187-AC5: prints 'interval': {proc.stdout!r}"
tickfile5 = r5 / "events" / "L-tick-local.jsonl"
assert tickfile5.exists() and len(tickfile5.read_text().splitlines()) == 1, \
    f"0187-AC5: L-tick-local.jsonl gains exactly one line: {tickfile5}"

print("tick: 0187 R3 checks pass")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0198 · trusted-author-inbound-charter (L-charter-0028) — AC5's "same
# wake / next pass" reading (A10, Planner ruling): `tick.lane` re-folded over a
# carry's own just-produced `spec-carried` + `spec-written` events lists the
# spec as written, with no `inbound:` row left standing for its now-carried
# source. `src/tick.py` itself is READ here, never written (finding 5) —
# `tick.lane` is the existing, unaffected seam this file alone is granted to call.
# ══════════════════════════════════════════════════════════════════════════════
iso198 = _isolated_events()
fold.EVENTS = iso198
write_to(iso198, "L-spec-writer-9198.jsonl", {"type": "spec-written", "subject": "L-spec-9198"})
# autodispatch-0427/SD3: a standing failure keeps it on the lane regardless.
write_to(iso198, "L-tick-0005.jsonl",
         {"ts": (fold.NOW + datetime.timedelta(days=3650)).isoformat(timespec="seconds"),
          "type": "autodispatch-failed", "subject": "L-spec-9198", "reason": "autodispatch-0427: AC5(0198) fixture"})
write_to(iso198, "L-operator-9198.jsonl",
         {"type": "spec-carried", "subject": "L-spec-9198", "source": "9198-src",
          "tier": "gates-only", "audited_at": NOW, "charter": "L-charter-9198",
          "trusted_author": "yitzchak-eg"})
ev198 = fold.read_events()
specs198, charters198, _, _ = fold.fold(ev198)
assert specs198["L-spec-9198"]["state"] == "written", \
    f"AC5 (L-spec-0198): fold.fold() reports the just-carried spec as 'written': {specs198['L-spec-9198']}"
busy198 = tick.in_flight(ev198)
# The "next pass": `carry.uncarried()` would no longer report `9198-src` once a
# `spec-carried` names it, so the inbound list this pass folds over already
# excludes it — proven directly (never trusting a separate implementation to
# agree) rather than re-importing `carry` for a source `tick.py` never touches.
inbound198 = [row for row in [{"source": "9198-src", "kind": "v4"}]
             if row["source"] not in {e.get("source") for e in ev198 if e.get("type") == "spec-carried"}]
assert inbound198 == [], "AC5 (L-spec-0198) precondition: the fixture inbound row is excluded"
fold.EVENTS = saved_events

print("tick: L-spec-0198 AC5 check passes")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0242 · intake-core (L-charter-0031) — AC13, SD11's wave-1 half:
# `tick._record()` calls `intake.run(ev)` off its FIRST `fold.read_events()`,
# then RE-READS before `carry.uncarried()`/`fold.fold()`/`lane()` run.
# ══════════════════════════════════════════════════════════════════════════════
iso242 = _isolated_events()
fold.EVENTS = iso242
real_intake_run, real_uncarried242 = intake.run, carry.uncarried
seen242 = {}


def _fake_intake_run(events):
    write_to(iso242, "L-intake-local.jsonl",
             {"type": "inbound-registered", "subject": "https://x/pull/1", "source": "https://x/pull/1",
              "project": "albert-scott", "title": "t", "author_login": "a", "auto": True, "body": "b"})
    return {"spec_prs": [], "note_prs": []}


def _asserting_uncarried(events):
    seen242["types"] = {e["type"] for e in events}
    return []


intake.run, carry.uncarried = _fake_intake_run, _asserting_uncarried
assert tick.main() == 0
assert "inbound-registered" in seen242.get("types", set()), \
    "AC13: intake.run(ev)'s own append is visible to carry.uncarried() via the RE-READ ledger"
intake.run, carry.uncarried = real_intake_run, real_uncarried242
fold.EVENTS = saved_events

# AC13, the other half — a raising intake.run never stops the tick's own
# heartbeat: `intake_error` lands additively, `lane`/`carry_error` unaffected.
iso242b = _isolated_events()
fold.EVENTS = iso242b


def _raising_intake_run(events):
    raise RuntimeError("intake boom")


intake.run = _raising_intake_run
n0_242 = len(ticks())
assert tick.main() == 0, "AC13: a raising intake.run never stops the tick's own heartbeat"
assert len(ticks()) == n0_242 + 1, "AC13: exactly one new tick event lands"
last242 = ticks()[-1]
assert "intake boom" in last242.get("intake_error", ""), \
    f"AC13: the tick event names the caught exception on intake_error: {last242}"
assert "carry_error" not in last242 and last242["lane"] == 0, \
    f"AC13: lane/carry_error are otherwise unaffected: {last242}"
intake.run = real_intake_run
fold.EVENTS = saved_events

print("tick: L-spec-0242 AC13 checks pass")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0274 · pane-resume (L-charter-0033) — AC6: `tick._record()` calls
# `pane_resume.run(ev)` in its own try/except, placed after `carry.sync_v4(ev)`
# and before `todo = lane(...)`, on the same re-read `ev`; a raising `run()`
# contributes `pane_resume_error` without touching `carry_error`/`intake_error`
# or stopping the tick's own heartbeat.
# ══════════════════════════════════════════════════════════════════════════════
iso274 = _isolated_events()
fold.EVENTS = iso274
seen274 = {}


def _raising_pane_resume(ev, **kw):
    seen274["n"] = len(ev)
    raise RuntimeError("pane_resume boom")


pane_resume.run = _raising_pane_resume
n0_274 = len(ticks())
assert tick.main() == 0, "AC6: a raising pane_resume.run never stops the tick's own heartbeat"
assert len(ticks()) == n0_274 + 1, "AC6: exactly one new tick event lands"
last274 = ticks()[-1]
assert "pane_resume boom" in last274.get("pane_resume_error", ""), \
    f"AC6: the tick event names the caught exception on pane_resume_error: {last274}"
assert "carry_error" not in last274 and "intake_error" not in last274 and isinstance(last274["lane"], int), \
    f"AC6: carry_error/intake_error untouched, lane still an int: {last274}"
assert seen274.get("n") == 0, \
    "AC6: pane_resume.run is called on the re-read ev, an otherwise-empty isolated ledger here"
pane_resume.run = _pane_resume_stub
fold.EVENTS = saved_events

# AC6, the other half — a normal (non-raising) pane_resume.run contributes no
# `pane_resume_error` at all, and intake/carry are unaffected by its presence.
iso274b = _isolated_events()
fold.EVENTS = iso274b
assert tick.main() == 0
last274b = ticks()[-1]
assert "pane_resume_error" not in last274b, f"AC6: a clean run adds no error field: {last274b}"
fold.EVENTS = saved_events

print("tick: L-spec-0274 AC6 checks pass")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0275 · honest-recording (L-charter-0033) — R2 Target 2: a same-host,
# confirmed-dead waiter's spawn goes stale immediately, independent of the
# elapsed-time cap.
# ══════════════════════════════════════════════════════════════════════════════
import socket, panes  # noqa: E402

HOST = socket.gethostname()


def _dead_pid():
    """A forked-and-reaped child pid — guaranteed dead, no network."""
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


# AC8 — a same-host, confirmed-dead waiter_pid: spawn-stale fires immediately,
# though the event's own ts is fresh (well inside any elapsed-time cap), and
# the subject returns to the lane right away.
iso8 = _isolated_events()
fold.EVENTS = iso8
dead8 = _dead_pid()
write_to(iso8, "L-planner-8801.jsonl", {"type": "spec-written", "subject": "L-spec-8801"})
# autodispatch-0427/SD3: a standing failure keeps it on the lane regardless.
write_to(iso8, "L-tick-0006.jsonl",
         {"ts": (fold.NOW + datetime.timedelta(days=3650)).isoformat(timespec="seconds"),
          "type": "autodispatch-failed", "subject": "L-spec-8801", "reason": "autodispatch-0427: AC8(0275) fixture"})
write_to(iso8, "L-grader-8801.jsonl", {"type": "spawn-started", "role": "grader",
                                       "subject": "L-spec-8801", "spawn": "L-grader-8801",
                                       "waiter_host": HOST, "waiter_pid": dead8})
assert tick.main() == 0
stale8 = [json.loads(l) for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]
assert stale8 and stale8[-1]["spawn"] == "L-grader-8801" and stale8[-1].get("reason") == "waiter-dead", \
    f"AC8: a same-host confirmed-dead waiter is stale immediately, reason=waiter-dead: {stale8}"
ev8, specs8, charters8 = folded()
fold.EVENTS = saved_events

# AC9 — a live waiter_pid whose recorded waiter_proc_start does NOT match its
# own real proc_start (a pid-reuse simulation): dead, not busy, exactly as AC8.
iso9 = _isolated_events()
fold.EVENTS = iso9
write_to(iso9, "L-planner-8802.jsonl", {"type": "spec-written", "subject": "L-spec-8802"})
write_to(iso9, "L-grader-8802.jsonl", {"type": "spawn-started", "role": "grader",
                                       "subject": "L-spec-8802", "spawn": "L-grader-8802",
                                       "waiter_host": HOST, "waiter_pid": os.getpid(),
                                       "waiter_proc_start": "not-a-real-start-time"})
assert tick.main() == 0
stale9 = [json.loads(l) for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]
assert stale9 and stale9[-1]["spawn"] == "L-grader-8802" and stale9[-1].get("reason") == "waiter-dead", \
    f"AC9: a mismatched proc_start (pid reuse) is dead even though the pid itself is alive: {stale9}"
fold.EVENTS = saved_events

# AC10 — panes.proc_start monkeypatched to always return None (an unreadable
# own start time): an alive pid stays busy, never dead, regardless of what
# waiter_proc_start names.
iso10 = _isolated_events()
fold.EVENTS = iso10
real_proc_start = panes.proc_start
panes.proc_start = lambda pid: None
try:
    write_to(iso10, "L-planner-8803.jsonl", {"type": "spec-written", "subject": "L-spec-8803"})
    write_to(iso10, "L-grader-8803.jsonl", {"type": "spawn-started", "role": "grader",
                                            "subject": "L-spec-8803", "spawn": "L-grader-8803",
                                            "waiter_host": HOST, "waiter_pid": os.getpid(),
                                            "waiter_proc_start": "anything-at-all"})
    assert tick.main() == 0
    stale10 = [l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]
    assert not stale10, f"AC10: an unreadable own proc_start is alive, never dead: {stale10}"
    ev10, specs10, charters10 = folded()
finally:
    panes.proc_start = real_proc_start
    fold.EVENTS = saved_events

# AC11 — an int-typed waiter_proc_start that matches (once cast to str) is
# never a false positive for dead: the subject stays busy.
iso11 = _isolated_events()
fold.EVENTS = iso11
real_start = panes.proc_start(os.getpid())
write_to(iso11, "L-planner-8804.jsonl", {"type": "spec-written", "subject": "L-spec-8804"})
write_to(iso11, "L-grader-8804.jsonl", {"type": "spawn-started", "role": "grader",
                                        "subject": "L-spec-8804", "spawn": "L-grader-8804",
                                        "waiter_host": HOST, "waiter_pid": os.getpid(),
                                        "waiter_proc_start": int(real_start)})
assert tick.main() == 0
stale11 = [l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]
assert not stale11, f"AC11: an int-typed matching waiter_proc_start is never a false positive: {stale11}"
ev11, specs11, charters11 = folded()
fold.EVENTS = saved_events

# AC12(a)/(b) — cross-host and fields-missing spawns fall through unaffected,
# to the existing elapsed-time behavior (busy, no immediate spawn-stale). The
# two pre-existing 2x-cap spawn-stale fixtures earlier in this file (the
# 45-minute-old L-grader-0009 fixture; the 12-day-old anonymous build-started
# fixture) carry no waiter_* fields, are untouched above, and already passed
# exactly as at base_sha (AC12(c)).
iso12 = _isolated_events()
fold.EVENTS = iso12
dead12 = _dead_pid()
write_to(iso12, "L-planner-8805.jsonl", {"type": "spec-written", "subject": "L-spec-8805"})
write_to(iso12, "L-grader-8805.jsonl", {"type": "spawn-started", "role": "grader",
                                        "subject": "L-spec-8805", "spawn": "L-grader-8805",
                                        "waiter_host": "definitely-not-this-host", "waiter_pid": dead12})
write_to(iso12, "L-planner-8806.jsonl", {"type": "spec-written", "subject": "L-spec-8806"})
write_to(iso12, "L-grader-8806.jsonl", {"type": "spawn-started", "role": "grader",
                                        "subject": "L-spec-8806", "spawn": "L-grader-8806"})
assert tick.main() == 0
stale12 = [l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]
assert not stale12, f"AC12(a)/(b): cross-host and fields-missing spawns are unaffected, still busy: {stale12}"
ev12, specs12, charters12 = folded()
fold.EVENTS = saved_events

print("tick: L-spec-0275 R2 Target 2 checks pass")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0431 · install-and-serve (L-charter-0042) — R5d/AC11. Placed here,
# BEFORE the L-spec-0322 section below: that section's own AC11(b) fixture
# (crons/look "not importable") no longer holds now that `src/crons.py` is a
# real, importable module in this repo (L-spec-0318, predates this charter) —
# a pre-existing failure, unrelated to and untouched by this spec, that aborts
# the rest of this file's module-level execution partway through that later
# section. This block is placed ahead of it so its own marker still lands.
# ══════════════════════════════════════════════════════════════════════════════


class _FakeInstallSync:
    def __init__(self, names=None, raise_msg=None):
        self.calls = []
        self._names = names if names is not None else []
        self._raise_msg = raise_msg

    def run(self, ev, **kw):
        self.calls.append(len(ev))
        if self._raise_msg:
            raise RuntimeError(self._raise_msg)
        return list(self._names)


_real_install_sync_stub = sys.modules["install_sync"]

# success — install_synced=<n> lands on the same tick event, exactly once,
# called with that pass's own (pre-re-read) `ev`.
isoIS = _isolated_events()
fold.EVENTS = isoIS
fake_is = _FakeInstallSync(names=["a.md", "b.md"])
sys.modules["install_sync"] = fake_is
try:
    assert tick.main() == 0
finally:
    sys.modules["install_sync"] = _real_install_sync_stub
last_is = ticks()[-1]
assert fake_is.calls == [0], \
    f"installsync-0431 AC11: install_sync.run called exactly once, with that pass's ev: {fake_is.calls}"
assert last_is.get("install_synced") == 2 and "install_sync_error" not in last_is, \
    f"installsync-0431 AC11: install_synced is the returned list's length: {last_is}"
fold.EVENTS = saved_events

# failure — install_sync_error lands instead, and nothing else is skipped:
# carry.sync_v4/pane_resume.run/the autodispatch call/the tick append all still run.
isoIS2 = _isolated_events()
fold.EVENTS = isoIS2
fake_is2 = _FakeInstallSync(raise_msg="sync boom")
sys.modules["install_sync"] = fake_is2
real_uncarried, real_sync = carry.uncarried, carry.sync_v4
sync_calls = {"n": 0}


def _counting_sync_ac11(events):
    sync_calls["n"] += 1
    return 0


carry.uncarried = lambda events: []
carry.sync_v4 = _counting_sync_ac11
PANE_RESUME_CALLS.clear()
try:
    assert tick.main() == 0
finally:
    sys.modules["install_sync"] = _real_install_sync_stub
    carry.uncarried, carry.sync_v4 = real_uncarried, real_sync
last_is2 = ticks()[-1]
assert "sync boom" in last_is2.get("install_sync_error", ""), \
    f"installsync-0431 AC11: install_sync_error names the caught exception: {last_is2}"
assert "install_synced" not in last_is2, "installsync-0431 AC11: a failed call records no install_synced"
assert sync_calls["n"] == 1 and PANE_RESUME_CALLS, \
    "installsync-0431 AC11: carry.sync_v4/pane_resume.run still ran despite the install_sync failure"
fold.EVENTS = saved_events
print("installsync-0431 AC11 ok")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0322 · look-mutual-watch (L-charter-0036 R1/SD13/SD22) — AC11/AC17
# ══════════════════════════════════════════════════════════════════════════════
# `crons` genuinely does not exist in this repo pre-wave-1-merge: `import crons,
# look` fails on `crons` before `look` is even reached, so the ImportError half
# below needs no stubbing at all. The present-row halves stub BOTH modules —
# `tick._record()`'s own `import crons, look` picks up whatever `sys.modules`
# already holds.


class _FakeLook:
    def __init__(self, state=None):
        self.calls = []
        self._state = state or {}

    def emit_once(self, condition, key, owner, reading, events, **kw):
        self.calls.append((condition, key, owner))
        return {"condition": condition, "key": key}

    def _read_state(self, root):
        return self._state

    def heartbeat_missed(self, *args, **kw):
        return False


class _CronsMissingLook:
    @staticmethod
    def check():
        return [{"name": "look"}]


class _CronsPresent:
    @staticmethod
    def check():
        return []


# AC11(a) — the `look` row missing: exactly one cron-missing/look/thinker call.
iso11 = _isolated_events()
fold.EVENTS = iso11
fakelook11 = _FakeLook()
sys.modules["crons"], sys.modules["look"] = _CronsMissingLook(), fakelook11
try:
    assert tick.main() == 0, "AC11: a tick with the look row missing still exits clean"
finally:
    del sys.modules["crons"]
    sys.modules["look"] = _watchdog_look
assert fakelook11.calls == [("cron-missing", "look", "thinker")], \
    f"AC11: exactly one cron-missing/look/thinker call via look.emit_once: {fakelook11.calls}"
assert "look_error" not in ticks()[-1], "AC11: no look_error on a clean (stub-importable) pass"
fold.EVENTS = saved_events

# AC11(b) — `crons`/`look` NOT importable: the tick completes normally, its
# usual lane unchanged, and the SAME `tick` event names the exception.
# ★ measured 2026-09-27 (L-spec-0438): `src/crons.py` landed on this tree
# long before this unit's base_sha, so it is a real, importable module here —
# "crons genuinely does not exist pre-wave-1-merge" (this section's own
# opening comment, above) no longer holds. Unimportable is simulated instead,
# the same technique test_pane_end.py's AC4 already uses for `relay`:
# `sys.modules["crons"] = None` is exactly the ModuleNotFoundError
# `import crons` raises when the file genuinely is not there.
iso11b = _isolated_events()
fold.EVENTS = iso11b
real_crons11b = sys.modules.get("crons")
sys.modules["crons"] = None
try:
    assert tick.main() == 0, "AC11: an ImportError degrades the tick, never crashes it"
    assert "look_error" in ticks()[-1] and "crons" in ticks()[-1]["look_error"], \
        f"AC11: the same tick event names the import failure: {ticks()[-1]}"
finally:
    if real_crons11b is not None:
        sys.modules["crons"] = real_crons11b
    else:
        del sys.modules["crons"]
fold.EVENTS = saved_events
print("AC11 ok")

# AC17(a) — `look` row present, `last_pass` >25 min old: one look-stale/thinker call.
iso17 = _isolated_events()
fold.EVENTS = iso17
old_last_pass = (fold.NOW - datetime.timedelta(minutes=30)).isoformat(timespec="seconds")
fakelook17 = _FakeLook(state={"last_pass": old_last_pass})
sys.modules["crons"], sys.modules["look"] = _CronsPresent(), fakelook17
try:
    assert tick.main() == 0
finally:
    del sys.modules["crons"]
    sys.modules["look"] = _watchdog_look
assert fakelook17.calls == [("look-stale", "look-stale", "thinker")], \
    f"AC17: a stale last_pass (row present) briefs look-stale/thinker: {fakelook17.calls}"
fold.EVENTS = saved_events

# AC17(b) — a fresh last_pass (<25 min) appends nothing.
iso17b = _isolated_events()
fold.EVENTS = iso17b
fresh_last_pass = (fold.NOW - datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
fakelook17b = _FakeLook(state={"last_pass": fresh_last_pass})
sys.modules["crons"], sys.modules["look"] = _CronsPresent(), fakelook17b
try:
    assert tick.main() == 0
finally:
    del sys.modules["crons"]
    sys.modules["look"] = _watchdog_look
assert fakelook17b.calls == [], "AC17: within 25 minutes, no look-stale brief"
fold.EVENTS = saved_events

# AC17(c) — the REAL `look.run()`, a fresh pass, clears an open look-stale entry.
import look as _real_look  # noqa: E402


class _NullRunner:
    def ssh(self, *a): raise RuntimeError("no net")
    def git(self, *a): raise RuntimeError("no net")
    def tmux_capture(self, *a): raise RuntimeError("no net")
    def crontab_text(self, *a): raise RuntimeError("no net")
    def disk_usage(self, *a): return {"total": 1, "used": 0, "free": 1}
    def clock(self): return time.monotonic()
    def sleep(self, s): pass


root17c = pathlib.Path(tempfile.mkdtemp())
(root17c / "events").mkdir()
toml17c = root17c / "look.toml"
toml17c.write_text("")   # no [[prod]] rows, no codex targets — nothing external ever fires
opened17c = _real_look.emit_once("look-stale", "look-stale", "thinker", {"last_pass": None}, [], root=root17c)
assert opened17c is not None, "AC17: seeding an open look-stale brief to be cleared"
res17c = _real_look.run([], now=fold.NOW, runner=_NullRunner(), root=root17c, toml_path=toml17c)
assert any(a.get("ref") for a in res17c["answered"]), \
    f"AC17: a fresh look.run() pass clears an open look-stale entry that same pass: {res17c}"
print("AC17 ok")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0473 · tick-proving-records (L-charter-0046) — R1/R3/R4: the tick's
# own automatic entry into Proving, its close, its reopen, and the two alarms.
# ══════════════════════════════════════════════════════════════════════════════
import proving  # noqa: E402
import look  # noqa: E402


def _proving_charter(evdir, cid, sid, *, covers=None, ts=None, owed_ac=None):
    """The 0472-AC9 shape, reused: one fully accepted spec (builder -> grader
    -> reviewer -> executor), `sweep-fixpoint`, and `covers` left UNSET (a
    review owed) unless given explicitly — folds to "proving" with a
    non-empty `items` list, never straight to "L2-complete". `ts=` stamps
    every event the same explicit instant (for the overdue fixtures, an
    old one); left `None`, `write_to`'s own default (module `NOW`) applies,
    exactly like every other fixture in this file. `owed_ac=` optionally adds
    one `owed-ac` criterion to the spec before it ships, e.g.
    `{"criterion": "AC1", "wake_at": "..."}`."""
    tail = cid[-5:]
    tsk = {"ts": ts} if ts else {}
    filed = {"type": "charter-filed", "subject": cid, **tsk}
    if covers is not None:
        filed["covers"] = covers
    write_to(evdir, f"L-operator-{tail}.jsonl", filed, {"type": "l1-complete", "subject": cid, **tsk})
    sw_evs = [{"type": "spec-written", "subject": sid, "charter": cid, **tsk}]
    if owed_ac:
        sw_evs.append({"type": "owed-ac", "subject": sid, "charter": cid, "line": "x", **owed_ac, **tsk})
    write_to(evdir, f"L-spec-writer-{tail}.jsonl", *sw_evs)
    write_to(evdir, f"L-builder-{tail}.jsonl",
             {"type": "build-started", "subject": sid, **tsk}, {"type": "build-done", "subject": sid, **tsk})
    write_to(evdir, f"L-grader-{tail}.jsonl", {"type": "verdict", "subject": sid, "confirmed": True, **tsk})
    write_to(evdir, f"L-reviewer-{tail}.jsonl", {"type": "review", "subject": sid, "depth": "gates-only", **tsk})
    write_to(evdir, f"L-executor-{tail}.jsonl",
             {"type": "shipped", "subject": sid, **tsk}, {"type": "sweep-fixpoint", "subject": cid, **tsk})


def _fresh_bound_root():
    """A fresh root, both `fold.ROOT` and `fold.EVENTS` rebound together via
    `tick._bind_root` — this unit's own Fixture isolation convention (binding
    convention, above): `look.emit_once`'s default `root=fold.ROOT`, and
    `look/state.json`, resolve off `fold.ROOT`, never `fold.EVENTS` alone."""
    r = pathlib.Path(tempfile.mkdtemp())
    (r / "events").mkdir(parents=True, exist_ok=True)
    tick._bind_root(r)
    return r


# ── AC1 — automatic entry into Proving ───────────────────────────────────────
iso473_1 = _isolated_events()
saved_events = fold.EVENTS
fold.EVENTS = iso473_1
C1, S1 = "L-charter-90731", "L-spec-90731"
_proving_charter(iso473_1, C1, S1)
ev, specs, charters = folded()
assert charters[C1]["state"] == "proving", charters[C1]["state"]
mine1 = [s for s in specs.values() if s["charter"] == C1 and s["state"] != "void"]
summ1 = proving.summary(C1, charters[C1]["evs"], mine1, fold.NOW, title=C1,
                        review_owed=fold.charter_review_owed(charters[C1]["evs"]),
                        open_briefs=fold.open_briefs(charters[C1]["evs"]))
assert tick.main() == 0
cp1 = [json.loads(l) for l in tick.tick_path().read_text().splitlines() if '"charter-proving"' in l]
assert len(cp1) == 1, cp1
assert cp1[0]["subject"] == C1 and cp1[0]["deadline"] == summ1["deadline"], (cp1[0], summ1)
expected_reason1 = f"{summ1['remaining']} item(s) remain; next: {summ1['next']['text']}"
assert cp1[0]["reason"] == expected_reason1, (cp1[0]["reason"], expected_reason1)
assert "\n" not in cp1[0]["reason"], cp1[0]["reason"]
assert tick.main() == 0
cp1b = [json.loads(l) for l in tick.tick_path().read_text().splitlines() if '"charter-proving"' in l]
assert len(cp1b) == 1, "AC1: a second pass on the unchanged ledger appends no second charter-proving"
print("0473-AC1 ok")
fold.EVENTS = saved_events

# ── AC2 — recording the close (Gate invariant (a)) ──────────────────────────
iso473_2 = _isolated_events()
fold.EVENTS = iso473_2
C2, S2 = "L-charter-90732", "L-spec-90732"
_proving_charter(iso473_2, C2, S2, covers="none", owed_ac={"criterion": "AC1", "wake_at": NOW})
ev, specs, charters = folded()
assert specs[S2]["state"] == "shipped-owed-due", specs[S2]["state"]
assert charters[C2]["state"] == "proving", charters[C2]["state"]
assert tick.main() == 0
evs2 = fold.read_events()
assert any(e["type"] == "charter-proving" and e.get("subject") == C2 for e in evs2), evs2
assert not any(e["type"] == "charter-closed" and e.get("subject") == C2 for e in evs2), \
    "AC2: not closed while the owed check is still open"
met_ts2 = (fold.NOW + datetime.timedelta(minutes=1)).isoformat(timespec="seconds")
write_to(iso473_2, "L-owed-sweeper-90732.jsonl",
         {"type": "owed-met", "subject": S2, "charter": C2, "criterion": "AC1", "evidence": "e", "ts": met_ts2})
ev, specs, charters = folded()
assert specs[S2]["state"] == "accepted", specs[S2]["state"]
assert charters[C2]["state"] == "L2-complete", charters[C2]["state"]
assert tick.main() == 0
evs2b = fold.read_events()
closed2b = [e for e in evs2b if e["type"] == "charter-closed" and e.get("subject") == C2]
assert len(closed2b) == 1, closed2b
assert closed2b[0]["reason"] == tick.PROVING_CLOSE_REASON, closed2b[0]
assert closed2b[0]["label"] == "accepted", closed2b[0]
assert tick.main() == 0
evs2c = fold.read_events()
closed2c = [e for e in evs2c if e["type"] == "charter-closed" and e.get("subject") == C2]
assert len(closed2c) == 1, "AC2: a third pass on the unchanged (now-closed) ledger appends no second charter-closed"
fold.EVENTS = saved_events

# AC2(b) — identical, but `covers` set (a review owed): does not close on the
# same shape until a `charter-review-complete` event also lands.
iso473_2b = _isolated_events()
fold.EVENTS = iso473_2b
C2B, S2B = "L-charter-90733", "L-spec-90733"
_proving_charter(iso473_2b, C2B, S2B, covers="R1", owed_ac={"criterion": "AC1", "wake_at": NOW})
met_ts2b = (fold.NOW + datetime.timedelta(minutes=1)).isoformat(timespec="seconds")
write_to(iso473_2b, "L-owed-sweeper-90733.jsonl",
         {"type": "owed-met", "subject": S2B, "charter": C2B, "criterion": "AC1", "evidence": "e", "ts": met_ts2b})
ev, specs, charters = folded()
assert charters[C2B]["state"] in ("proving", "reopened"), charters[C2B]["state"]
assert tick.main() == 0
evs2b_ = fold.read_events()
assert not any(e["type"] == "charter-closed" and e.get("subject") == C2B for e in evs2b_), \
    "AC2(b): a review still owed must never close, regardless of the owed check"
write_to(iso473_2b, "L-charter-reviewer-90733.jsonl", {"type": "charter-review-complete", "subject": C2B})
ev, specs, charters = folded()
assert charters[C2B]["state"] == "L2-complete", charters[C2B]["state"]
assert tick.main() == 0
evs2b2 = fold.read_events()
assert any(e["type"] == "charter-closed" and e.get("subject") == C2B for e in evs2b2), \
    "AC2(b): once the review completes too, the close follows"
print("0473-AC2 ok")
fold.EVENTS = saved_events

# ── AC3 — a charter that skips Proving entirely never gains a charter-closed ─
iso473_3 = _isolated_events()
fold.EVENTS = iso473_3
C3, S3 = "L-charter-90734", "L-spec-90734"
_proving_charter(iso473_3, C3, S3, covers="none")
ev, specs, charters = folded()
assert charters[C3]["state"] == "L2-complete", charters[C3]["state"]
assert tick.main() == 0
assert tick.main() == 0
evs3 = fold.read_events()
assert not any(e["type"] == "charter-proving" and e.get("subject") == C3 for e in evs3), \
    "AC3: a charter that skips Proving entirely must never gain a charter-proving event"
assert not any(e["type"] == "charter-closed" and e.get("subject") == C3 for e in evs3), \
    "AC3: SD7 — no charter-proving ever recorded means no charter-closed either"
print("0473-AC3 ok")
fold.EVENTS = saved_events

# ── AC4 — reopen, the Plan's Gate invariant (b) ─────────────────────────────
root4a = _fresh_bound_root()
C4A, S4A = "L-charter-90740", "L-spec-90740"
_proving_charter(fold.EVENTS, C4A, S4A, owed_ac={"criterion": "AC1", "wake_at": NOW})
ev, specs, charters = folded()
assert specs[S4A]["state"] == "shipped-owed-due", specs[S4A]["state"]
assert charters[C4A]["state"] == "proving", charters[C4A]["state"]
assert tick.main() == 0
evs4a = fold.read_events()
assert any(e["type"] == "charter-proving" and e.get("subject") == C4A for e in evs4a), evs4a
# `dispatch.now()` truncates to whole seconds; the later fold's "reopened"
# derivation needs the charter-reopened event's own real ts to be STRICTLY
# after the charter-proving one (`phase()`'s own `reopened_ts > proving_ts`),
# so a same-second race here is a real, not merely theoretical, flake.
time.sleep(1.1)
fail_ts4a = (fold.NOW + datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
write_to(fold.EVENTS, "L-owed-sweeper-90740.jsonl",
         {"type": "owed-failed", "subject": S4A, "charter": C4A, "criterion": "AC1", "kind": "unmet",
          "evidence": "e", "ts": fail_ts4a})
ev, specs, charters = folded()
assert charters[C4A]["state"] == "proving", \
    "AC4: still 'proving' BEFORE the _record() pass that appends the reopen"
failed_ev4a = next(e for e in ev if e["type"] == "owed-failed" and e.get("subject") == S4A)
expected_src4a = failed_ev4a["_src"]
assert tick.main() == 0
evs4a2 = fold.read_events()
reopened4a = [e for e in evs4a2 if e["type"] == "charter-reopened" and e.get("subject") == C4A]
assert len(reopened4a) == 1, reopened4a
assert reopened4a[0]["spec"] == S4A and reopened4a[0]["criterion"] == "AC1" \
    and reopened4a[0]["failed_src"] == expected_src4a, (reopened4a[0], expected_src4a)
briefs4a = [e for e in evs4a2 if e["type"] == "brief" and e.get("condition") == "proving-check-failed"
            and e.get("key") == f"{C4A}|{S4A}|AC1"]
assert len(briefs4a) == 1, briefs4a
_, _, charters4a2 = folded()
assert charters4a2[C4A]["state"] == "reopened", charters4a2[C4A]["state"]

# AC4, kind="stale" — never reopens, no brief, state stays "proving".
root4b = _fresh_bound_root()
C4B, S4B = "L-charter-90741", "L-spec-90741"
_proving_charter(fold.EVENTS, C4B, S4B, owed_ac={"criterion": "AC1", "wake_at": NOW})
ev, specs, charters = folded()
assert charters[C4B]["state"] == "proving", charters[C4B]["state"]
assert tick.main() == 0
fail_ts4b = (fold.NOW + datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
write_to(fold.EVENTS, "L-owed-sweeper-90741.jsonl",
         {"type": "owed-failed", "subject": S4B, "charter": C4B, "criterion": "AC1", "kind": "stale",
          "evidence": "e", "ts": fail_ts4b})
ev, specs, charters = folded()
assert charters[C4B]["state"] == "proving", charters[C4B]["state"]
assert tick.main() == 0
evs4b2 = fold.read_events()
assert not any(e["type"] == "charter-reopened" and e.get("subject") == C4B for e in evs4b2), \
    "AC4: kind='stale' never reopens (Q4)"
assert not any(e["type"] == "brief" and e.get("condition") == "proving-check-failed" for e in evs4b2), \
    "AC4: kind='stale' fires no proving-check-failed brief"
_, _, charters4b2 = folded()
assert charters4b2[C4B]["state"] == "proving", charters4b2[C4B]["state"]
print("0473-AC4 ok")

# ── AC5 — the reopen is idempotent across a second, event-free pass ────────
tick._bind_root(root4a)
assert tick.main() == 0
evs5 = fold.read_events()
reopened5 = [e for e in evs5 if e["type"] == "charter-reopened" and e.get("subject") == C4A]
assert len(reopened5) == 1, "AC5: to_reopen()'s own failed_src dedup — no second charter-reopened"
briefs5 = [e for e in evs5 if e["type"] == "brief" and e.get("condition") == "proving-check-failed"
           and e.get("key") == f"{C4A}|{S4A}|AC1"]
assert len(briefs5) == 1, "AC5: look.emit_once's own open-brief dedup — no second brief"
print("0473-AC5 ok")

# ── AC6 — the overdue alarm, exactly once per (charter, deadline) ever ──────
orig_fold_now = fold.NOW
root6 = _fresh_bound_root()
C6, S6 = "L-charter-90750", "L-spec-90750"
t_old6 = (orig_fold_now - datetime.timedelta(days=10)).isoformat(timespec="seconds")
_proving_charter(fold.EVENTS, C6, S6, ts=t_old6, owed_ac={"criterion": "AC1", "wake_at": t_old6})
# the charter-proving event itself, backdated to the SAME old instant — EMITS
# gates it to actor "tick" (D90: the filename), so a "L-tick-*" file is used.
write_to(fold.EVENTS, "L-tick-90750.jsonl",
         {"type": "charter-proving", "subject": C6, "reason": "backdated for AC6", "deadline": t_old6,
          "ts": t_old6})
ev, specs, charters = folded()
assert charters[C6]["state"] == "proving", charters[C6]["state"]
mine6 = [s for s in specs.values() if s["charter"] == C6 and s["state"] != "void"]
summ6 = proving.summary(C6, charters[C6]["evs"], mine6, fold.NOW, title=C6,
                        review_owed=fold.charter_review_owed(charters[C6]["evs"]),
                        open_briefs=fold.open_briefs(charters[C6]["evs"]))
assert fold.ts(summ6["deadline"]) < fold.NOW, \
    f"AC6 precondition: the fixture's own deadline must already be in the past: {summ6['deadline']}"
key6 = f"{C6}|{summ6['deadline']}"
assert tick.main() == 0
evs6 = fold.read_events()
overdue6 = [e for e in evs6 if e["type"] == "brief" and e.get("condition") == "proving-overdue"
            and e.get("key") == key6]
assert len(overdue6) == 1, overdue6
assert overdue6[0]["owner"] == "operator", overdue6[0]
reading6 = overdue6[0].get("reading") or {}
expected_next6 = summ6["next"]["text"] if summ6["next"] is not None else None
assert reading6.get("next") == expected_next6 and reading6.get("owner") == summ6["owner"], \
    (reading6, summ6)

# isolate the ledger-scan guard from look.emit_once's own 30-minute cooldown:
# clear the brief directly, advance fold.NOW past the cooldown, and prove via
# a CONTROL call against a separate scratch root that the cooldown alone
# would NOT have suppressed a repeat — only this unit's own ledger-scan does.
cleared6 = look._clear("proving-overdue", key6, "test-clear", evs6, root6)
assert cleared6 is not None
# The suite may take over a minute on a busy build box. The clear is stamped
# at emission time, so advance from that event rather than the import clock.
clear_event6 = next(e for e in reversed(fold.read_events())
                    if e.get('type') == 'brief-answered' and e.get('ref') == cleared6['ref'])
fold.NOW = fold.ts(clear_event6['ts']) + datetime.timedelta(minutes=31)
control_root6 = pathlib.Path(tempfile.mkdtemp())
control6 = look.emit_once("proving-overdue", key6, "operator", {"next": None, "owner": "operator"},
                          fold.read_events(), root=control_root6)
assert control6 is not None, \
    "AC6 control: a clean scratch root, past the cooldown, must accept the SAME (condition, key) again"
assert tick.main() == 0
evs6b = fold.read_events()
overdue6b = [e for e in evs6b if e["type"] == "brief" and e.get("condition") == "proving-overdue"
             and e.get("key") == key6]
assert len(overdue6b) == 1, \
    f"AC6: the ledger-scan guard suppresses a repeat the cooldown alone would not have: {overdue6b}"

# a re-dated (later) owed-ac shifts the deadline -> exactly one NEW alarm,
# under a NEW key.
redate_ts6 = (orig_fold_now + datetime.timedelta(minutes=40)).isoformat(timespec="seconds")
new_wake6 = (orig_fold_now - datetime.timedelta(days=5)).isoformat(timespec="seconds")
write_to(fold.EVENTS, "L-executor-90750b.jsonl",
         {"type": "owed-ac", "subject": S6, "charter": C6, "criterion": "AC1", "wake_at": new_wake6,
          "line": "x", "ts": redate_ts6})
ev, specs, charters = folded()
mine6c = [s for s in specs.values() if s["charter"] == C6 and s["state"] != "void"]
summ6c = proving.summary(C6, charters[C6]["evs"], mine6c, fold.NOW, title=C6,
                         review_owed=fold.charter_review_owed(charters[C6]["evs"]),
                         open_briefs=fold.open_briefs(charters[C6]["evs"]))
assert summ6c["deadline"] != summ6["deadline"], \
    f"AC6: the re-date must change the deadline, or this proves nothing: {summ6c['deadline']}"
assert fold.ts(summ6c["deadline"]) < fold.NOW, \
    f"AC6: the re-dated deadline must still be overdue, or the new alarm never fires this pass: {summ6c}"
key6c = f"{C6}|{summ6c['deadline']}"
assert tick.main() == 0
evs6c = fold.read_events()
overdue6c_new = [e for e in evs6c if e["type"] == "brief" and e.get("condition") == "proving-overdue"
                 and e.get("key") == key6c]
assert len(overdue6c_new) == 1, overdue6c_new
overdue6c_old = [e for e in evs6c if e["type"] == "brief" and e.get("condition") == "proving-overdue"
                 and e.get("key") == key6]
assert len(overdue6c_old) == 1, "AC6: the OLD key's alarm is untouched — one alarm per key, forever"
fold.NOW = orig_fold_now

# a charter whose deadline is still in the future -> no alarm at all.
root6d = _fresh_bound_root()
C6D, S6D = "L-charter-90751", "L-spec-90751"
_proving_charter(fold.EVENTS, C6D, S6D)
ev, specs, charters = folded()
assert charters[C6D]["state"] == "proving", charters[C6D]["state"]
assert tick.main() == 0
evs6d = fold.read_events()
assert not any(e["type"] == "brief" and e.get("condition") == "proving-overdue" for e in evs6d), \
    "AC6: a charter whose deadline is still in the future gets no alarm at all"
print("0473-AC6 ok")

# ── AC8 — a raising per-charter proving loop never stops the tick's own ─────
# heartbeat; the SAME tick event names it on `proving_error`.
iso473_8 = _isolated_events()
fold.EVENTS = iso473_8
C8, S8 = "L-charter-90748", "L-spec-90748"
_proving_charter(iso473_8, C8, S8)
ev, specs, charters = folded()
assert charters[C8]["state"] == "proving", charters[C8]["state"]
real_should_enter = proving.should_enter


def _raising_should_enter(*a, **kw):
    raise RuntimeError("induced for 0473-AC8")


proving.should_enter = _raising_should_enter
try:
    assert tick.main() == 0, "AC8: a raising should_enter() never crashes the tick"
    last8 = ticks()[-1]
    assert "proving_error" in last8 and "0473-AC8" in last8["proving_error"], last8
    assert "carry_error" not in last8 and "intake_error" not in last8 and "look_error" not in last8, last8
    assert isinstance(last8["lane"], int), last8
finally:
    proving.should_enter = real_should_enter
assert tick.main() == 0
last8b = ticks()[-1]
assert "proving_error" not in last8b, last8b
print("0473-AC8 ok")
fold.EVENTS = saved_events

print("tick: L-spec-0473 tick-proving-records checks pass")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0486 · cleanup-on-finish (L-charter-0042), R15a (record) — AC7
# ══════════════════════════════════════════════════════════════════════════════
import subprocess as _sp486, tree_cleanup as _tc486  # noqa: E402


def _sh486(cwd, *cmd):
    p = _sp486.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))
    assert p.returncode == 0, f"{cmd}: {p.stderr}"
    return p.stdout.strip()


R486 = pathlib.Path(tempfile.mkdtemp())
proc486 = R486 / 'proc'
proc486.mkdir()
import disk_reaper as _dr486
proc_context486 = _dr486.worktree_pass(proc486)
proc_context486.__enter__()
(R486 / "events").mkdir(parents=True)
repo486 = R486 / "repos" / "l0486-tick-proj"
repo486.mkdir(parents=True)
_sh486(repo486, "git", "init", "-q", "-b", "main")
(repo486 / "seed.txt").write_text("seed\n")
_sh486(repo486, "git", "add", "-A"); _sh486(repo486, "git", "commit", "-qm", "seed")
wt486 = R486 / "worktrees" / "l-spec-9486a"
_sh486(repo486, "git", "worktree", "add", "-q", str(wt486), "-b", "l-spec-9486a", "main")
(wt486 / "x.txt").write_text("x\n")
_sh486(wt486, "git", "add", "-A"); _sh486(wt486, "git", "commit", "-qm", "build")
_sh486(repo486, "git", "merge", "-q", "--no-ff", "l-spec-9486a", "-m", "merge")
merge_sha486 = _sh486(repo486, "git", "rev-parse", "HEAD")
origin486 = R486 / "repos" / "l0486-tick-proj-origin.git"
_sp486.run(["git", "init", "-q", "--bare", str(origin486)], check=True)
_sh486(repo486, "git", "remote", "add", "origin", str(origin486))
_sh486(repo486, "git", "push", "-q", "origin", "main")
_sh486(repo486, "git", "fetch", "-q", "origin")

(R486 / "events" / "L-thinker-0001.jsonl").write_text(
    json.dumps({"v": 1, "ts": NOW, "type": "charter-filed", "subject": "L-charter-9486",
               "project": "l0486-tick-proj"}) + "\n")
(R486 / "events" / "L-executor-0001.jsonl").write_text(
    "\n".join(json.dumps({"v": 1, "ts": NOW, **e}) for e in [
        {"type": "spec-written", "subject": "L-spec-9486a", "charter": "L-charter-9486"},
        {"type": "build-done", "subject": "L-spec-9486a", "branch": "l-spec-9486a", "ready_sha": merge_sha486},
        {"type": "shipped", "subject": "L-spec-9486a", "charter": "L-charter-9486", "sha": merge_sha486},
    ]) + "\n")

saved_root486, saved_events486 = fold.ROOT, fold.EVENTS
fold.ROOT, fold.EVENTS = R486, R486 / "events"
try:
    assert tick._record() is not None, "L0486-AC7: the tick's own lock is free here"

    def _wrs():
        return [json.loads(l) for l in tick.tick_path().read_text().splitlines()
                if json.loads(l).get("type") == "worktree-reaped"]

    wr486 = _wrs()
    assert len(wr486) == 1, f"L0486-AC7: exactly one worktree-reaped: {wr486}"
    assert wr486[0]["subject"] == "L-spec-9486a" and wr486[0]["proof"] == "origin-merged" \
        and wr486[0]["path"] == str(wt486), f"L0486-AC7: shape: {wr486[0]}"
    assert not wt486.exists(), "L0486-AC7: the worktree really is gone"

    assert tick._record() is not None
    assert len(_wrs()) == 1, f"L0486-AC7: a second _record() appends none — idempotent: {_wrs()}"

    real_reap_worktrees486 = _tc486.reap_worktrees

    def _raising_reap486(*a, **kw):
        raise RuntimeError("L0486-AC7: forced")

    _tc486.reap_worktrees = _raising_reap486
    try:
        assert tick._record() is not None, "L0486-AC7: a raising reap_worktrees never stops the heartbeat"
        last486 = [json.loads(l) for l in tick.tick_path().read_text().splitlines()
                   if json.loads(l).get("type") == "tick"][-1]
        assert "L0486-AC7: forced" in last486.get("worktree_reap_error", ""), last486
    finally:
        _tc486.reap_worktrees = real_reap_worktrees486
finally:
    fold.ROOT, fold.EVENTS = saved_root486, saved_events486
    proc_context486.__exit__(None, None, None)
print("L0486-AC7 ok")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0484 · merge-reaches-origin R13(c) (L-charter-0042)
# ══════════════════════════════════════════════════════════════════════════════


class _FakePushOrigin:
    """Substituted into `sys.modules["push_origin"]` — `tick._record()`'s own
    lazy `import push_origin` picks this up instead of the real module."""
    def __init__(self, raise_error=False):
        self.calls = []
        self.raise_error = raise_error

    def run(self, ev, **kw):
        self.calls.append(list(ev))
        if self.raise_error:
            raise RuntimeError("boom-push-origin")
        return []


# merge-origin-0484 AC17(a) — `tick._record()` calls `push_origin.run(ev)`
# exactly once per tick; a healthy run adds no `push_origin_error` field to
# the SAME `tick` event.
iso_po17a = _isolated_events()
fold.EVENTS = iso_po17a
write_to(iso_po17a, "L-planner-0001.jsonl", {"type": "spec-written", "subject": "L-spec-48401"})
fake_po17a = _FakePushOrigin()
sys.modules["push_origin"] = fake_po17a
try:
    assert tick.main() == 0
finally:
    del sys.modules["push_origin"]
assert len(fake_po17a.calls) == 1, f"merge-origin-0484 AC17: exactly one call: {fake_po17a.calls}"
lines_po17a = [json.loads(l) for l in (iso_po17a / "L-tick-local.jsonl").read_text().splitlines()]
tick_events_po17a = [e for e in lines_po17a if e["type"] == "tick"]
assert tick_events_po17a and "push_origin_error" not in tick_events_po17a[-1], \
    f"merge-origin-0484 AC17: a healthy run adds no push_origin_error: {tick_events_po17a}"
fold.EVENTS = saved_events

# merge-origin-0484 AC17(b) — a raising `push_origin.run` leaves
# `push_origin_error` naming it on the SAME `tick` event, and does not stop
# `autodispatch.run`, `pane_resume.run`, or the `tick` append itself (`lane`
# still an int). The tick still starts no Executor and merges nothing — this
# fake proves it by never touching git or a worktree at all.
iso_po17b = _isolated_events()
fold.EVENTS = iso_po17b
write_to(iso_po17b, "L-planner-0001.jsonl", {"type": "spec-written", "subject": "L-spec-48402"})
before_pane_calls_po17 = len(PANE_RESUME_CALLS)
fake_ad_po17b = _FakeAutodispatch()
tick.run_steps = fake_ad_po17b.run
sys.modules["push_origin"] = _FakePushOrigin(raise_error=True)
try:
    assert tick.main() == 0
finally:
    del sys.modules["push_origin"]
    tick.run_steps = _real_run_steps
assert len(fake_ad_po17b.calls) == 1, "merge-origin-0484 AC17: autodispatch still ran after a push_origin raise"
assert len(PANE_RESUME_CALLS) == before_pane_calls_po17 + 1, "merge-origin-0484 AC17: pane_resume.run still ran"
lines_po17b = [json.loads(l) for l in (iso_po17b / "L-tick-local.jsonl").read_text().splitlines()]
tick_events_po17b = [e for e in lines_po17b if e["type"] == "tick"]
assert tick_events_po17b and tick_events_po17b[-1].get("push_origin_error") == "boom-push-origin", \
    f"merge-origin-0484 AC17: {tick_events_po17b}"
assert isinstance(tick_events_po17b[-1].get("lane"), int), \
    f"merge-origin-0484 AC17: the tick's own append still happened: {tick_events_po17b[-1]}"
fold.EVENTS = saved_events
print("merge-origin-0484 AC17 ok")


# AC10: exercise the real heartbeat write path, keeping prior look-stale
# assertions unchanged above. Every sender remains a stub.
from unittest.mock import patch
import cron_run, subprocess

_watchdog_look.heartbeat_missed = _real_heartbeat_missed
_wnow = datetime.datetime(2031, 3, 1, 12, tzinfo=datetime.timezone.utc)


def _watchdog_start(root, when):
    store = cron_run.store_path("look", root)
    store.parent.mkdir(parents=True, exist_ok=True)
    with store.open("a") as stream:
        stream.write(json.dumps({"ev": "start", "ts": when.isoformat(timespec="seconds"), "pid": 1}) + "\n")


for outcome in (0, 1, "raise"):
    root = pathlib.Path(tempfile.mkdtemp())
    calls = []
    def _watchdog_send(text, root):
        calls.append(text)
        if outcome == "raise":
            raise RuntimeError("watchdog sender exploded")
        return outcome
    with patch.object(fold, "ROOT", root), patch.object(fold, "EVENTS", root / "events"), \
         patch.object(fold, "NOW", _wnow), patch.object(_watchdog_look, "_send_operator", side_effect=_watchdog_send):
        _watchdog_start(root, _wnow - datetime.timedelta(minutes=29))
        assert tick._record() is not None
        assert not calls, "watchdog: 29-minute heartbeat gap stays healthy"
        with patch.object(fold, "NOW", _wnow + datetime.timedelta(minutes=2)):
            assert tick._record() is not None
            assert tick._record() is not None
        if outcome == "raise":
            raw = [json.loads(line) for line in tick.tick_path().read_text().splitlines()]
            assert "watchdog sender exploded" in [e for e in raw if e["type"] == "tick"][-1]["look_error"]
        with patch.object(_watchdog_look, "_send_operator", return_value=0):
            with patch.object(fold, "NOW", _wnow + datetime.timedelta(minutes=2)):
                assert tick._record() is not None
                assert tick._record() is not None
        raw = [json.loads(line) for line in tick.tick_path().read_text().splitlines()]
        alerts = [e for e in raw if e["type"] == "operator-alert"]
        assert [e["sent"] for e in alerts] == (["yes"] if outcome == 0 else ["no", "yes"]), alerts
        assert all(e["subject"] == "look" and e["kind"] == "heartbeat-missed" and "actor" not in e for e in alerts)
        assert alerts[0]["last_start"] == (_wnow - datetime.timedelta(minutes=29)).isoformat(timespec="seconds")
        assert alerts[0]["measured_at"] == (_wnow + datetime.timedelta(minutes=2)).isoformat(timespec="seconds")
        child = subprocess.run([sys.executable, "-c",
            "import fold,json; print(json.dumps([e for e in fold.read_events() if e['type']=='operator-alert']))"],
            capture_output=True, text=True, check=True, env={**os.environ, "DOIT_ROOT": str(root), "DOIT_PROJECT": "",
                "PYTHONPATH": str(pathlib.Path(__file__).resolve().parent)})
        from_disk = json.loads(child.stdout)
        assert len(from_disk) == len(alerts) and all(e["actor"] == "tick" for e in from_disk), from_disk
        _watchdog_start(root, _wnow + datetime.timedelta(minutes=3))
        with patch.object(fold, "NOW", _wnow + datetime.timedelta(minutes=34)), \
             patch.object(_watchdog_look, "_send_operator", return_value=0):
            assert tick._record() is not None
        raw = [json.loads(line) for line in tick.tick_path().read_text().splitlines()]
        assert len([e for e in raw if e.get("sent") == "yes"]) == 2, "watchdog: second outage sends again"
print("watchdog AC10 ok")

import hashlib
_body = json.dumps({"criterion": "AC10", "child_ledger_rows": len(from_disk),
                    "successful_outages": 2, "sender_exception_completed": True}, sort_keys=True)
print(json.dumps(dict(url="cli://tick/watchdog/AC10", status=0,
                      body_sha256=hashlib.sha256(_body.encode()).hexdigest(), body_excerpt=_body)))



# L-spec-1506: tick records an honest label/reason, including a fresh reader.
def test_close_rule_emission_1506():
    import checks
    saved_root, saved_events = fold.ROOT, fold.EVENTS
    try:
        with tempfile.TemporaryDirectory(prefix='close-rule-tick-') as directory:
            root = pathlib.Path(directory)
            (root / 'events').mkdir()
            tick._bind_root(root)
            cid, sid = 'L-charter-91506', 'L-spec-91506'
            _proving_charter(fold.EVENTS, cid, sid, covers='none',
                             owed_ac={'criterion': 'AC1', 'wake_at': NOW})
            record = checks.CheckRecord('tick-close-check', sid, 'AC1', ['true'], 'fixture',
                                        'scratch', False, 'fixture', 'post-deploy', 'script')
            write_to(fold.EVENTS, 'L-spec-writer-1506.jsonl',
                     dict(type='check-declared', subject=sid, **record.to_event()))
            write_to(fold.EVENTS, 'L-tick-1506.jsonl',
                     dict(type='charter-proving', subject=cid, reason='fixture', deadline=NOW))
            with patch.object(tick, 'run_steps', return_value=[]):
                assert tick.main() == 0
                assert tick.main() == 0
            closed = [e for e in fold.read_events(use_cache=False)
                      if e['type'] == 'charter-closed' and e['subject'] == cid]
            assert len(closed) == 1, closed
            assert closed[0]['label'] == 'closed-unproven'
            assert closed[0]['reason'] != tick.PROVING_CLOSE_REASON
            assert 'remain unproven' in closed[0]['reason']
            _launcher_evidence('close-rule-1506', dict(label=closed[0]['label'],
                                                      reason=closed[0]['reason'], count=len(closed)))
    finally:
        tick._bind_root(saved_root)
        fold.EVENTS = saved_events


test_close_rule_emission_1506()


def test_recorded_reap_counts_1515():
    import reap_tmp, resources
    rows = [resources.ReapResult("/fixture/" + str(i), "worktree", "refused",
                               "undetermined: pid " + str(i) + " /fixture/" + str(i))
            for i in range(1500)]
    evidence = []
    for failed in (None, "not_reapable_counts", "manifest_counts", "both"):
        with tempfile.TemporaryDirectory(prefix="tick-reap-1515-") as directory, contextlib.ExitStack() as stack:
            root = pathlib.Path(directory)
            stack.enter_context(patch.object(fold, "ROOT", root))
            stack.enter_context(patch.object(fold, "EVENTS", root / "events"))
            stack.enter_context(patch.object(fold, "read_events", return_value=[]))
            stack.enter_context(patch.object(tick, "run_steps", return_value=[]))
            recorded = stack.enter_context(patch.object(reap_tmp, "_reap_recorded", return_value=rows))
            trees = stack.enter_context(patch.object(tick.tree_cleanup, "reap_worktrees", return_value=([], [])))
            for helper, result in (("not_reapable_counts", {"upload-copy": 3}),
                                   ("manifest_counts", {"legacy": 3, "declared": 3})):
                stack.enter_context(patch.object(resources, helper, return_value=result,
                    side_effect=RuntimeError("fixture helper failure") if failed in (helper, "both") else None))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                assert tick._record() is not None
            recorded.assert_called_once_with([])
            trees.assert_called_once_with([])
            event = [json.loads(line) for line in tick.tick_path().read_text().splitlines()
                     if json.loads(line)["type"] == "tick"][-1]
            summary = event["recorded_reap_counts"]
            assert isinstance(summary, dict) and len(json.dumps(summary).encode()) < 4096
            assert summary["n"] == 1500 and summary["by_action"] == {"refused": 1500}
            assert "recorded_reap" not in event
            assert not output.getvalue(), output.getvalue()
            for helper, component in (("not_reapable_counts", "not_reapable"), ("manifest_counts", "manifest")):
                if failed in (helper, "both"):
                    assert helper in event["recorded_reap_counts_error"]
                    assert component not in summary
                else:
                    assert summary[component]
            if failed is None:
                assert "recorded_reap_counts_error" not in event
            evidence.append(dict(failed=failed, event=event, worktree_reap_calls=trees.call_count,
                                 stdout=output.getvalue()))
    _launcher_evidence("AC4-1515", evidence)


test_recorded_reap_counts_1515()

class AuditTickTests(unittest.TestCase):
    dispatcher = LauncherTests.dispatcher

    def setUp(self):
        LauncherTests.setUp(self)
        slots = patch.object(autodispatch, 'capacity', return_value=0)
        slots.start()
        self.addCleanup(slots.stop)
    step = LauncherTests.step

    def emit(self, actor, kind, sid='L-spec-9673', minutes=-31, **kw):
        with patch.object(dispatch, 'now', return_value=(self.now + datetime.timedelta(minutes=minutes)).isoformat()):
            self.assertIsNone(dispatch.emit(self.root / 'events' / ('L-' + actor + '-9673.jsonl'), {}, kind, subject=sid, **kw))

    def read(self):
        events = fold.read_events(use_cache=False)
        return events, fold.fold(events)[0]

    def record(self, argv):
        self.calls.append(argv)
        if argv[:2] == ['doit', 'dispatch']:
            sid = argv[4]
            spawn = 'L-spec-auditor-' + str(9700 + len(self.calls))
            self.emit('spec-auditor', 'spawn-started', sid, minutes=0,
                      spawn=spawn, role='spec-auditor', issued_by='tick')
            return dict(code=0, stdout=json.dumps(dict(detached=spawn)), stderr='')
        self.assertEqual(argv[:3], ['doit', 'packet', 'spec-auditor'])
        return dict(code=0, stdout='/fixture.packet.md', stderr='')

    def pass_steps(self, now=None, **kwargs):
        events, specs = self.read()
        now = now or self.now
        steps = tick._steps(events, specs, now)
        out = tick.run_steps(events, specs, now, dispatcher=self.record, steps=steps, **kwargs)
        return events, specs, steps, out

    def test_ac6_audit_argv_failure_validation_and_cap(self):
        self.emit('spec-writer', 'spec-written', project='proj', charter='L-charter-9673')
        events, specs = self.read()
        runner = tick._PassDispatcher(self.record, events, specs, self.now, False)
        step = self.step('audit', item='L-spec-9673', stage='planned')
        spawn = tick.launch(step, dispatcher=runner)
        self.assertEqual(self.calls, [
            ['doit', 'packet', 'spec-auditor', 'L-spec-9673'],
            ['doit', 'dispatch', '--detach', 'spec-auditor', 'L-spec-9673', '--packet', '/fixture.packet.md',
             '--cwd', str(fold.ROOT / 'repos' / 'proj'), '--charter', 'L-charter-9673', '--project', 'proj']])
        rows = self.read()[0]
        emitted = [e for e in rows if e['type'] == 'autodispatched']
        self.assertEqual(len(emitted), 1)
        self.assertEqual({k: emitted[0][k] for k in ('role', 'rule', 'since', 'spawn')},
                         dict(role='spec-auditor', rule='audit', since=step.since, spawn=spawn))
        _launcher_evidence('AC6-argv', dict(calls=self.calls, events=emitted))
        self.calls.clear()
        with patch.object(runner, 'runner', return_value=dict(code=1, stderr='packet failed')):
            with self.assertRaises(ValueError):
                tick.launch(step, dispatcher=runner)
        failed = [e for e in self.read()[0] if e['type'] == 'autodispatch-failed']
        self.assertEqual((failed[-1]['role'], failed[-1]['rule']), ('spec-auditor', 'audit'))
        with self.assertRaises(ValueError):
            tick.launch(replace(step, item='../../escape'), dispatcher=runner)
        runner.events = [dict(type='spec-written', subject=step.item, project='../escape')]
        with self.assertRaises(ValueError):
            tick.launch(step, dispatcher=runner)
        self.assertEqual(self.calls, [])
        _launcher_evidence('AC6-failures', [e for e in self.read()[0] if e['type'] == 'autodispatch-failed'])
        steps = []
        for n in range(8):
            sid = f'L-spec-{9800+n}'
            self.emit('spec-writer', 'spec-written', sid, project='proj')
            steps.append(replace(step, item=sid))
        events, specs = self.read()
        dry = tick.run_steps(events, specs, self.now, steps=steps, dispatcher=self.record, dry_run=True)
        self.assertEqual(self.calls, [])
        out = tick.run_steps(events, specs, self.now, steps=steps, dispatcher=self.record)
        self.assertEqual(sum(a[:2] == ['doit', 'dispatch'] for a in self.calls), tick.AUDIT_PASS_MAX)
        self.assertEqual(sum(line.startswith('seat-wait ') for line in out), 2)
        _launcher_evidence('AC6-cap', dict(out=out, dry=dry))

    def test_ac7_old_specs_and_dead_replay_bounds(self):
        for n, days in enumerate((2, 9, 30)):
            self.emit('spec-writer', 'spec-written', f'L-spec-{9800+n}', minutes=-days*1440, project='proj')
        self.emit('spec-writer', 'spec-written', 'L-spec-9803', minutes=-3000, project='proj')
        self.emit('operator', 'spec-carried', 'L-spec-9803', source='fixture', tier='fixture', audited_at=self.now.isoformat())
        self.emit('thinker', 'spec-written', 'L-spec-9804', minutes=-3000, project='proj')
        self.emit('spec-writer', 'spec-written', 'L-spec-9805', minutes=-3000, project='proj')
        self.emit('builder', 'build-started', 'L-spec-9805', spawn='L-builder-9805', role='builder')
        self.emit('spec-writer', 'spec-written', 'L-spec-9806', minutes=-10, project='proj')
        # Isolate audit launches while allowing the exempt specs their normal build Steps.
        events, specs = self.read()
        steps = tick._steps(events, specs, self.now)
        out = tick.run_steps(events, specs, self.now, steps=steps, dispatcher=self.record)
        self.assertEqual([a[4] for a in self.calls if a[:2] == ['doit', 'dispatch']],
                         ['L-spec-9802', 'L-spec-9801', 'L-spec-9800'])
        self.calls.clear()
        events, specs = self.read()
        steps = tick._steps(events, specs, self.now)
        second = tick.run_steps(events, specs, self.now, steps=steps, dispatcher=self.record)
        self.assertEqual(self.calls, [])
        _launcher_evidence('AC7-catchup', dict(first=out, second=second))
        import attempts
        for n in range(attempts.MAX_ATTEMPTS):
            self.emit('spec-writer', 'spec-written', minutes=-90, project='proj') if n == 0 else None
            self.emit('spec-auditor', 'spawn-started', minutes=-6, spawn=f'L-spec-auditor-{9900+n}', role='spec-auditor')
            self.emit('tick', 'spawn-stale', minutes=-4, spawn=f'L-spec-auditor-{9900+n}', role='spec-auditor')
            events, specs = self.read()
            steps = [s for s in tick._steps(events, specs, self.now) if s.item == 'L-spec-9673']
            self.calls.clear()
            tick.run_steps(events, specs, self.now, steps=steps, dispatcher=self.record)
            replay = autodispatch.redispatch_dead(events, specs, self.now, runner=self.record, handled=tick.handled_items(steps))
            self.assertEqual(self.calls, [])
            self.assertFalse(any(line.startswith('replayed ') for line in replay))
            self.assertIn('L-spec-9673', tick.handled_items(steps))
            _launcher_evidence('AC7-replay-bound', dict(steps=[s.reason for s in steps], replay=replay))
            if n == 0:
                later = self.now + datetime.timedelta(minutes=1)
                due = [s for s in tick._steps(events, specs, later) if s.item == 'L-spec-9673']
                tick.run_steps(events, specs, later, steps=due, dispatcher=self.record)
                autodispatch.redispatch_dead(events, specs, later, runner=self.record, handled=tick.handled_items(due))
                self.assertEqual(sum(a[:2] == ['doit', 'dispatch'] for a in self.calls), 1)
                # Close the recording runner's synthetic start before testing exhaustion.
                for e in self.read()[0]:
                    if e['type'] == 'spawn-started' and e.get('issued_by') == 'tick' and e['subject'] == 'L-spec-9673':
                        self.emit('tick', 'spawn-stale', minutes=-4, spawn=e['spawn'], role='spec-auditor')

    def test_ac8_real_ledger_audit_then_build_rewrite_and_bad_cut(self):
        (self.root / 'repos' / 'proj').mkdir(parents=True)
        (self.root / 'content' / 'L-spec-9673.md').write_text(
            '# Goal\nFixture.\n# Acceptance criteria\nAC1 [backend]: fixture. review_path: fixture.\n'
            'Writes:\n- fixture.py\n# Verification\n```\ntrue && echo VERIFIED\n```\n')
        self.emit('spec-writer', 'spec-written', project='proj')
        events, specs, steps, out = self.pass_steps()
        with patch.object(autodispatch, 'capacity', return_value=1):
            row = autodispatch.candidates(events, specs, self.now)[0]
        self.assertEqual(row['status'], 'blocked')
        self.assertTrue(row['reason'].startswith('unaudited'))
        self.assertEqual([s.action for s in steps if s.item == 'L-spec-9673'], ['audit'])
        self.assertTrue(dispatch.unaudited(events))
        self.emit('spec-auditor', 'spawn-done', minutes=1, bad_cut=False,
                  spawn=next(e['spawn'] for e in self.read()[0] if e['type'] == 'spawn-started'))
        events, specs = self.read()
        steps = tick._steps(events, specs, self.now + datetime.timedelta(minutes=1))
        self.assertEqual([s.action for s in steps if s.item == 'L-spec-9673'], ['build'])
        self.emit('spec-writer', 'spec-written', minutes=2, project='proj')
        events, specs = self.read()
        rewritten = tick._steps(events, specs, self.now + datetime.timedelta(minutes=2))
        self.assertEqual([s.action for s in rewritten if s.item == 'L-spec-9673'], ['build'])
        self.assertIsNone(dispatch.unaudited(events))
        self.assertEqual(sum(a[:2] == ['doit', 'dispatch'] for a in self.calls), 1)
        self.emit('builder', 'build-started', minutes=3, spawn='L-builder-9673')
        self.emit('builder', 'build-blocked', minutes=4, spawn='L-builder-9673')
        self.emit('builder', 'spawn-done', minutes=4, spawn='L-builder-9673')
        blocked = next(e for e in self.read()[0] if e['type'] == 'build-blocked')
        self.emit('operator', 'unblocked', minutes=5)
        self.emit('operator', 'decision', minutes=5, ref=blocked['_src'])
        events, specs = self.read()
        rebuilt = tick._steps(events, specs, self.now + datetime.timedelta(minutes=5))
        self.assertEqual(next(s for s in rebuilt if s.item == 'L-spec-9673').action, 'rebuild')
        self.assertFalse(any(s.action == 'audit' for s in rebuilt))
        self.emit('spec-writer', 'spec-written', 'L-spec-9674', project='proj')
        self.emit('spec-auditor', 'spawn-done', 'L-spec-9674', minutes=3, bad_cut=True)
        events, specs = self.read()
        bad = tick._steps(events, specs, self.now + datetime.timedelta(minutes=3))
        self.assertTrue(next(s for s in bad if s.item == 'L-spec-9674').reason.startswith('audit-bad-cut:'))
        self.emit('spec-writer', 'spec-written', 'L-spec-9674', minutes=4, project='proj')
        events, specs = self.read()
        self.assertEqual(next(s for s in tick._steps(events, specs, self.now + datetime.timedelta(minutes=4)) if s.item == 'L-spec-9674').action, 'build')
        _launcher_evidence('AC8-real-ledger', dict(pass1=out, pass2=[s.action for s in steps], rewrite=[s.action for s in rewritten], rebuild=[s.action for s in rebuilt], candidate=row))


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
