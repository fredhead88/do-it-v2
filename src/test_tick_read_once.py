"""Scratch evidence for tick's raw own-file reader seam."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix='tick-read-once-import-')
os.environ.update(DOIT_ROOT=_IMPORT_ROOT.name, DOIT_NO_POKE='1',
                  V4_LEDGER_DIR=_IMPORT_ROOT.name + '/v4-ledger',
                  V4_INBOX_DIR=_IMPORT_ROOT.name + '/v4-inbox',
                  V4_STAGING_DIR=_IMPORT_ROOT.name + '/v4-staging')
os.environ.pop('DOIT_PROJECT', None)

import dispatch
import fold
import ledger_cache
import ledger_files
import look
import push_origin


def report(criterion, body):
    text = json.dumps(body, sort_keys=True, default=str)
    print(json.dumps(dict(url='cli://tick-read-once/' + criterion, status=200,
                         body_sha256=hashlib.sha256(text.encode()).hexdigest(),
                         body_excerpt=text)))


class Scratch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='tick-read-once-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.events = self.root / 'events'
        self.events.mkdir()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for module, name, value in ((fold, 'ROOT', self.root), (fold, 'EVENTS', self.events),
                                    (fold, 'PROJECT', ''), (dispatch, 'ROOT', self.root),
                                    (dispatch, 'EVENTS', self.events),
                                    (dispatch, 'CONTENT', self.root / 'content')):
            self.stack.enter_context(patch.object(module, name, value))
        self.stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(self.root)))

    def segment(self, name, text):
        directory = self.events / ledger_files.SEGMENT_DIR
        directory.mkdir(exist_ok=True)
        data = text.encode()
        digest = hashlib.sha256(data).hexdigest()
        (directory / 'fixture.jsonl').write_bytes(data)
        member = dict(name=name, offset=0, length=len(data), lines=len(data.splitlines()),
                      sha256=digest, ends_nl=data.endswith(b'\n'))
        (directory / 'fixture.idx.json').write_text(json.dumps(dict(
            version=1, segment='fixture.jsonl', bytes=len(data), sha256=digest, members=[member])))

    def fresh(self, code, *args):
        env = dict(os.environ, DOIT_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE='1')
        env.pop('DOIT_PROJECT', None)
        result = subprocess.run([sys.executable, '-B', '-c',
                                 'import sys; sys.path.insert(0,sys.argv.pop(1)); ' + code,
                                 str(Path(__file__).resolve().parent), *args],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def baseline(self):
        repo = Path(__file__).resolve().parent.parent
        # Pin git merge-base HEAD main as resolved at dispatch. Moving main after
        # merge must not turn this comparison into candidate versus itself.
        before = subprocess.check_output(['git', '-C', str(repo), 'rev-parse',
                                          '64ad99a8bfca3ecb5d41b1228d1cbb1ad1960345'],
                                         text=True).strip()
        merged = subprocess.check_output(['git', '-C', str(repo), 'merge-base',
                                          'HEAD', before], text=True).strip()
        self.assertEqual(merged, before, 'pinned dispatch base must remain an ancestor')
        print('BEFORE_SHA:', before, '(pinned dispatch baseline)', flush=True)
        source = self.root / 'baseline'
        source.mkdir()
        archive = subprocess.check_output(['git', '-C', str(repo), 'archive', before,
                                           'src', 'lane_rules.toml'])
        subprocess.run(['tar', '-xf', '-', '-C', str(source)], input=archive, check=True)
        return before, source / 'src'

    def execute(self, source, scenario, mode='steps'):
        code = ('import json,runpy,sys; sys.path.insert(0,sys.argv[1]); '
                'scope=runpy.run_path(sys.argv[2],run_name="fixture"); '
                'print(json.dumps(scope["_steps_fixture"](sys.argv[3],sys.argv[4]),sort_keys=True))')
        result = subprocess.run([sys.executable, '-B', '-c', code, str(source),
                                 str(Path(__file__).resolve()), scenario, mode],
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        body = json.loads(result.stdout.splitlines()[-1])
        for name, digest in body['source_hashes'].items():
            self.assertEqual(digest, hashlib.sha256((source / name).read_bytes()).hexdigest())
        return body


class TestOriginDedupe(Scratch):
    def test_supplied_events_and_committed_observations(self):
        """A pass snapshot replaces the hidden read; only real writes dedupe."""
        repo = self.root / 'repos' / 'scratch-project'
        repo.mkdir(parents=True)
        cfg = dict(push=dict(projects=['scratch-project', 'scratch-project'],
                             main={'scratch-project': 'main'}, path={}))
        sha, origin_sha = 'a' * 40, 'b' * 40
        calls = []

        class DivergenceRunner:
            def git(self, args, cwd, timeout):
                if Path(cwd) != repo:
                    raise AssertionError('non-scratch Git target')
                calls.append(args)
                replies = {'symbolic-ref': (0, 'main\n', ''),
                           'fetch': (0, '', ''),
                           'rev-parse': (0, sha + '\n' + origin_sha + '\n', ''),
                           'rev-list': (0, '1 1\n', ''),
                           'merge-base': (1, '', '')}
                return replies[args[0]]  # No real fetch/push is possible.

        supplied = []
        with patch.object(look, 'load_toml', return_value=cfg), \
             patch.object(fold, 'read_events', side_effect=AssertionError('hidden full read')):
            self.assertEqual(push_origin.run(supplied, runner=DivergenceRunner()), [])
        self.assertEqual(supplied, [], 'caller snapshot must remain private')
        rows = self.fresh('import json,fold,ledger_files,push_origin; '
                          'print(json.dumps([json.loads(s) for s in '
                          'ledger_files.read_text(fold.EVENTS,push_origin.EVENT_NAME).splitlines()]))')
        expected = dict(type='origin-diverged', subject='scratch-project', sha=sha,
                        origin_sha=origin_sha, branch='main')
        self.assertEqual(len(rows), 1, 'repeated configured targets dedupe successful writes')
        self.assertEqual({key: rows[0][key] for key in expected}, expected)
        admitted = self.fresh('import json,fold; print(json.dumps(fold.read_events()))')
        self.assertEqual(len(admitted), 1)
        self.assertEqual(admitted[0]['actor'], 'tick')
        self.assertTrue(admitted[0]['_src'].startswith(push_origin.EVENT_NAME + ':'))
        before = (self.events / push_origin.EVENT_NAME).read_bytes()
        with patch.object(look, 'load_toml', return_value=cfg), \
             patch.object(fold, 'read_events', side_effect=AssertionError('hidden full read')):
            push_origin.run(admitted, runner=DivergenceRunner())
        self.assertEqual((self.events / push_origin.EVENT_NAME).read_bytes(), before)
        with patch.object(look, 'load_toml', return_value=cfg), \
             patch.object(dispatch, 'emit', return_value='refused') as emitter:
            push_origin.run([], runner=DivergenceRunner())
            self.assertEqual(emitter.call_count, 2, 'refused writes must remain retryable')
        with patch.object(fold, 'read_events', side_effect=OSError('unavailable')), \
             patch.object(dispatch, 'emit', side_effect=AssertionError('unavailable append')):
            push_origin._record_diverged('scratch-project', 'main', sha, origin_sha)
        with patch.object(fold, 'read_events', return_value=admitted) as reader, \
             patch.object(dispatch, 'emit', side_effect=AssertionError('duplicate append')):
            push_origin._record_diverged('scratch-project', 'main', sha, origin_sha)
            self.assertEqual(reader.call_count, 1)
        report('AC5-origin', dict(committed=expected, fresh_actor=admitted[0]['actor'],
                                 repeated_targets=2, persisted_rows=len(rows),
                                 refused_attempts=2, git_commands=[args[0] for args in calls]))


class TestOwnFile(Scratch):
    def test_virtual_live_raw_and_committed_append(self):
        inventory = ['plain live', 'archived virtual member', 'member plus suffix',
                     'same-pass append', 'missing directory', 'missing logical file',
                     'blank malformed nondict', 'transcript', 'cron store', 'fresh committed brief']
        report('AC4-inventory', inventory)
        name = 'L-look-raw.jsonl'
        path = self.events / name
        history = dict(type='tree-reaped', subject='L-charter-9999', actor='raw-actor',
                       _src='raw-source', project='other')
        suffix = dict(type='brief', condition='raw', key='suffix')
        raw = json.dumps(history) + '\n\ninvalid\n[]\n3\n'
        with patch.object(fold, 'read_events', side_effect=AssertionError('full fold read')), \
             patch.object(ledger_cache, 'read', side_effect=AssertionError('full cache read')), \
             patch.object(ledger_cache, 'read_conditional', side_effect=AssertionError('full conditional read')), \
             patch.object(ledger_cache, 'tail_refresh', side_effect=AssertionError('full tail refresh')), \
             patch.object(fold.ReaderSnapshot, '_copy', side_effect=AssertionError('full snapshot copy')):
            path.write_text(raw)
            self.assertEqual(look._read_jsonl(path), [history])
            path.unlink()
            self.segment(name, raw)
            self.assertEqual(look._read_jsonl(path), [history])
            path.write_text(json.dumps(suffix) + '\n')
            self.assertEqual(look._read_jsonl(path), [history, suffix])
            with path.open('a') as stream:
                stream.write('{"committed":"same-pass"}\n')
            self.assertEqual(look._read_jsonl(path), [history, suffix, {'committed': 'same-pass'}])
            self.assertEqual(look._read_jsonl(self.events / 'absent.jsonl'), [])
            self.assertEqual(look._read_jsonl(self.root / 'missing/events/absent.jsonl'), [])
            for local in ('transcript.jsonl', 'cron.store'):
                target = self.root / local
                target.write_text(raw)
                self.assertEqual(look._read_jsonl(target), [history])
            look.emit_once('tick-read-once-roundtrip', 'unique-scratch-key', 'thinker',
                           {'proof': 'real-emit'}, [], root=self.root)
        rows = self.fresh('import look,fold; import json; '
                          'print(json.dumps(look._read_jsonl(fold.EVENTS / look.LEDGER_NAME)))')
        self.assertEqual(len(rows), 1)
        item = rows[0]['items'][0]
        self.assertEqual((rows[0]['condition'], item['key'], item['reading']),
                         ('tick-read-once-roundtrip', 'unique-scratch-key', {'proof': 'real-emit'}))
        report('AC4', dict(layouts=inventory, committed=rows))


class ReadCounter:
    """Count owning loads; retain nested calls and reject unowned sources."""
    def __init__(self, stack, allowlist, timeline):
        import carry
        import tick
        self.loads, self.calls, self.ancestry, self.violations = 0, [], [], []
        self.allowlist, self.timeline = allowlist, timeline
        whole = [(fold, 'read_events'), (ledger_cache, 'read'),
                 (ledger_cache, 'read_conditional'), (ledger_cache, 'tail_refresh'),
                 (fold.ReaderSnapshot, '_copy')]
        for module, name in whole:
            self.wrap(stack, module, name, 'whole')
        self.wrap(stack, ledger_files, 'read_text', 'own')
        self.wrap(stack, ledger_files, 'iter_sources', 'sources')
        self.wrap(stack, carry, 'scan_events', 'scan')
        self.wrap(stack, ledger_files, 'stat_keys', 'metadata')
        self.wrap(stack, tick, '_spawn_busy', 'reconcile')

    def wrap(self, stack, module, name, kind):
        original = getattr(module, name)
        label = getattr(module, '__name__', 'ReaderSnapshot') + '.' + name

        def wrapped(*args, **kwargs):
            frame = sys._getframe(1)
            while frame and (frame.f_code.co_filename == __file__ or
                             'unittest/mock.py' in frame.f_code.co_filename):
                frame = frame.f_back
            consumer = (Path(frame.f_code.co_filename).name + ':' + frame.f_code.co_name
                        if frame else 'unknown')
            owning = any(entry[1] == 'whole' for entry in self.ancestry)
            logical = args[1] if kind == 'own' else None
            row = dict(call=label, consumer=consumer, logical=logical,
                       ancestry=[entry[0] for entry in self.ancestry], owning_load=False)
            if kind == 'whole' and not owning:
                self.loads += 1
                row['owning_load'] = True
                self.timeline.append('load')
            if kind == 'own' and not owning and logical not in self.allowlist:
                self.violations.append(row)
            if kind in ('sources', 'scan') and not owning:
                self.violations.append(row)
            if kind == 'metadata' and consumer == 'tick.py:_record_pass':
                self.timeline.append('generation')
            if kind == 'reconcile':
                self.timeline.append('reconcile')
            self.calls.append(row)
            self.ancestry.append((label, kind))
            try:
                result = original(*args, **kwargs)
            finally:
                self.ancestry.pop()
            if kind == 'sources':
                ancestry = list(self.ancestry)
                def sources():
                    # iter_sources yields lazy bodies; ancestry remains owned
                    # while the caller consumes them, including an empty scan.
                    for source in result:
                        self.calls.append(dict(call=label + ':source', consumer=consumer,
                                               logical=source[0],
                                               ancestry=[entry[0] for entry in ancestry]))
                        yield source
                return sources()
            return result
        stack.enter_context(patch.object(module, name, side_effect=wrapped))


def _steps_fixture(scenario, mode='steps', temperature='cold'):
    """Execute the selected source tree in a fresh process and private root."""
    import builtins
    import importlib
    import re
    from dataclasses import replace
    from datetime import datetime, timezone
    import nextstep
    import lane_rules
    import tick

    case = Scratch()
    case.setUp()
    try:
        root, stack = case.root, case.stack
        now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        stack.enter_context(patch.object(fold, 'NOW', now))
        stack.enter_context(patch.object(dispatch, 'now', return_value=now.isoformat()))
        (root / 'content').mkdir()
        rule_path = root / 'lane_rules.toml'
        rule_path.write_bytes(Path(lane_rules.DEFAULT_TABLE).read_bytes())
        stack.enter_context(patch.dict(os.environ, DOIT_LANE_RULES=str(rule_path)))
        original_limit = lane_rules.load()['close']['limit_min']
        seed = [dict(v=1, type='spec-written', subject=sid, project='scratch',
                     ts='2026-11-09T11:00:00+00:00')
                for sid in ('L-spec-9901', 'L-spec-9902')]
        seed.append(dict(v=1, type='escalation-blocking', subject='busy-source',
                         ts='2026-11-09T11:00:00+00:00'))
        consecutive = scenario == 'consecutive busy passes'
        held = scenario.startswith('held skips')
        raised = scenario == 'held skips raised then next pass'
        if held:
            seed.extend(dict(v=1, type='dependency-hold', subject=sid, waits_for=dep,
                             why='fixture ' + dep, ts='2026-11-09T11:00:00+00:00')
                        for sid, dep in (('L-spec-9901', 'L-spec-9000'),
                                         ('L-spec-9902', 'L-spec-9001')))
        if consecutive:
            seed.append(dict(v=1, type='spec-written', subject='L-spec-9904', project='scratch',
                             ts='2026-11-09T11:00:00+00:00'))
        (case.events / 'L-operator-local.jsonl').write_text(
            ''.join(json.dumps(row) + '\n' for row in seed))
        first = nextstep.Step('L-spec-9901', 'spec', 'planned', 'action', 'close',
                              'executor', 'fixture:1', 'line one\nline two', None,
                              '2026-11-09T11:00:00+00:00')
        lane = [first, first, replace(first, owner='operator'),
                replace(first, action='audit', owner='tick'),
                replace(first, item='L-spec-9902', state='hold', action=None,
                        owner='tick', reason='fixture hold', clears='fixture decision')]
        if consecutive:
            seed_done = [dict(v=1, type='build-done', subject=sid, status='DONE',
                              ts='2026-11-09T11:00:00+00:00')
                         for sid in ('L-spec-9901', 'L-spec-9902', 'L-spec-9904')]
            (case.events / 'L-builder-fixture.jsonl').write_text(
                ''.join(json.dumps(row) + '\n' for row in seed_done))
        dispatch_cases = {'post-dispatch read failure', 'contained errors',
                          'dispatch-only append', 'accepted launch without synchronous append',
                          'own intake plus dispatch writes', 'external intake plus dispatch writes',
                          'partial dispatch append then exception',
                          'intake plus reconciliation plus dispatch append'}
        if scenario == 'unchanged pass' or scenario.startswith('entrypoint') or \
                mode != 'steps' and scenario not in dispatch_cases:
            lane = [step for step in lane if step.action != 'audit']
        if scenario == 'computed empty':
            lane = []
        pass_lanes = None
        if held:
            builds = [replace(first, item=sid, action='build', owner='tick')
                      for sid in ('L-spec-9901', 'L-spec-9902', 'L-spec-9903')]
            pass_lanes = [builds[:2], builds if raised else builds[:2]]
        if consecutive:
            launch_step = replace(first, action='audit', owner='tick')
            hold_one = replace(first, item='L-spec-9902', state='hold', action=None,
                               owner='tick', reason='fixture hold', clears='fixture decision')
            hold_two = replace(hold_one, item='L-spec-9904', reason='second fixture hold')
            # Pass 1 launches 9901; pass 2 sees it again only as an unlaunched hold.
            pass_lanes = [[launch_step, hold_one],
                          [replace(first, state='hold', action=None, owner='tick',
                                   reason='rework wait', clears='fixture decision'),
                           hold_one, hold_two]]
        calls, requests, views, intakes = [], [], [], []
        timeline = []
        intake_cases = {'pre-dispatch lock timeout', 'generation check failure', 'own intake write', 'external intake write',
                        'own intake plus dispatch writes', 'external intake plus dispatch writes',
                        'own intake plus _spawn_busy spawn-failed/vanished append',
                        'external intake plus _spawn_busy spawn-failed/vanished append',
                        'intake plus reconciliation plus dispatch append',
                        'consecutive busy passes'}
        reconcile = 'reconciliation' in scenario or '_spawn_busy' in scenario
        launch_events = None
        real_next_steps = nextstep.next_steps
        if mode != 'steps':
            # Unrelated sources, live and archived, prevent an accidental scan
            # from looking like an allowed read of the tick/look/spawn files.
            (case.events / 'L-operator-unrelated.jsonl').write_text(
                json.dumps(dict(v=1, type='observed', ts=now.isoformat(), subject='unrelated')) + '\n')
            case.segment('L-operator-archived.jsonl', json.dumps(dict(
                v=1, type='observed', ts=now.isoformat(), subject='archived-unrelated')) + '\n')
            if reconcile:
                from datetime import timedelta
                import socket
                seed = [dict(v=1, ts=(now-timedelta(hours=1)).isoformat(),
                             type='spec-written', subject='L-spec-9903', project='scratch')]
                with (case.events / 'L-operator-local.jsonl').open('a') as stream:
                    stream.write(json.dumps(seed[0]) + '\n')
                (case.events / 'L-builder-fixture.jsonl').write_text(json.dumps(dict(
                    v=1, type='build-done', subject='L-spec-9903', status='DONE',
                    ts=(now-timedelta(minutes=30)).isoformat())) + '\n')
                (case.events / 'L-grader-9903.jsonl').write_text(json.dumps(dict(
                    v=1, type='spawn-started', subject='L-spec-9903', spawn='L-grader-9903',
                    role='grader', waiter_host=socket.gethostname(), waiter_pid=99999999,
                    ts=(now-timedelta(minutes=20)).isoformat())) + '\n')

        selected = []

        def evaluate(events, specs, clock, *args):
            calls.append(clock.isoformat())
            views.append([e.get('type') for e in events])
            if scenario in ('steps RuntimeError', 'import autodispatch then steps failure',
                            'import merge_queue then steps failure'):
                raise RuntimeError('fixture steps unavailable')
            changed = args[0]['close']['limit_min'] != original_limit
            result = [replace(s, reason='edited lane rule') if changed and s.owner == 'executor'
                      else s for s in (pass_lanes[len(intakes) - 1] if pass_lanes else lane)]
            if reconcile:
                result.extend(step for step in real_next_steps(events, specs, clock, *args)
                              if step.item == 'L-spec-9903')
            selected.append([(s.item, s.state, s.action, s.owner, s.reason) for s in result])
            return result

        def runner(argv):
            requests.append([arg.replace(str(root), '<scratch>') for arg in argv])
            if scenario == 'lane edit between dispatch and render':
                text = rule_path.read_text()
                rule_path.write_text(re.sub(r'(name = "close"[^[]*limit_min = )\d+',
                                            lambda match: match[1] + str(original_limit + 1), text))
            if argv[:2] == ['doit', 'dispatch']:
                return dict(code=0, stdout=json.dumps(dict(detached='fixture-accepted')), stderr='')
            return dict(code=0, stdout='fixture-packet.md', stderr='')

        initial_reconcile_step = []

        def intake_write(events):
            intakes.append(1)
            if reconcile:
                initial_reconcile_step.extend((s.item, s.state, s.action, s.owner, s.reason)
                    for s in real_next_steps(events, fold.fold(events)[0], now, lane_rules.load())
                    if s.item == 'L-spec-9903')
            if scenario == 'writing pass' or scenario in intake_cases:
                dst = (case.events / 'L-operator-external.jsonl'
                       if scenario.startswith('external') else tick.tick_path())
                refusal = dispatch.emit(dst, {'subject': 'L-spec-9902'},
                                        'observed' if scenario.startswith('external') else 'autodispatch-skipped',
                                        strict=True, reason='intake fixture')
                if refusal is not None:
                    raise AssertionError(refusal)
            return {}

        origin_run = push_origin.run
        no_effects = {
            'notes': {'run': []}, 'pr_closeout': {'run': []},
            'install_sync': {'run': []}, 'push_origin': {'run': []},
            'carry': {'uncarried': [{'source': 'new-source'}, {'source': 'busy-source'}],
                      'sync_v4': []}, 'pane_resume': {'run': []},
            'crons': {'check': []}, 'look': {'_read_state': {'last_pass': now.isoformat()},
                                           'heartbeat_missed': None},
            'up': {'ensure_relay': 'quiet'}, 'close_reviews': {'run': []},
            'autodispatch': {'redispatch_dead': []}, 'merge_queue': {'enqueue_due': []},
            'resources': {'manifest_counts': {}, 'not_reapable_counts': {}},
            'reap_tmp': {'_reap_recorded': []}, 'tree_cleanup': {'reap_worktrees': ([], [])},
            'loadgate': {'begin': None}}
        if held:
            no_effects['autodispatch'] = {}  # Recovery runs for real.
        for name, replacements in no_effects.items():
            module = importlib.import_module(name)
            for attr, result in replacements.items():
                stack.enter_context(patch.object(module, attr, return_value=result))
        stack.enter_context(patch.object(importlib.import_module('intake'), 'run',
                                         side_effect=intake_write))
        stack.enter_context(patch.object(importlib.import_module('autodispatch'), '_real_runner',
                                         side_effect=runner))
        stack.enter_context(patch.object(tick, 'launch_precheck', return_value=[]))
        launch_events = stack.enter_context(patch.object(tick, '_launch_events',
                                                         wraps=tick._launch_events))
        stack.enter_context(patch.object(nextstep, 'next_steps', side_effect=evaluate))
        planned = stack.enter_context(patch.object(tick, '_steps', wraps=tick._steps))
        routine = tick.run_steps
        dispatch_results = []

        def run_steps(*args, **kwargs):
            result = routine(*args, **kwargs)
            dispatch_results.append(dict(outcomes=list(result),
                                         admission=dict(kwargs.get('admission') or {}),
                                         seats=dict(kwargs.get('seat_state') or {})))
            return result

        stack.enter_context(patch.object(tick, 'run_steps', side_effect=run_steps))
        if scenario in ('external append only after dispatch', 'consecutive busy passes'):
            def external_append(*args, **kwargs):
                refusal = dispatch.emit(case.events / 'L-operator-external.jsonl',
                                        {'subject': 'external-after-%d' % len(intakes)}, 'observed')
                assert refusal is None, refusal
                return []
            stack.enter_context(patch.object(importlib.import_module('merge_queue'), 'enqueue_due',
                                             side_effect=external_append))
        if scenario == 'partial dispatch append then exception':
            def partial(*args, **kwargs):
                run_steps(*args, **kwargs)
                raise RuntimeError('partial dispatch fixture')
            stack.enter_context(patch.object(tick, 'run_steps', side_effect=partial))
        if scenario == 'accepted launch without synchronous append':
            emitter = dispatch.emit
            def delayed(dst, base, kind, **kwargs):
                if kind == 'autodispatched':
                    return None
                return emitter(dst, base, kind, **kwargs)
            stack.enter_context(patch.object(dispatch, 'emit', side_effect=delayed))
        if scenario == 'origin divergence append':
            # Same real run/_push_one/_record_diverged path as the round-trip
            # fixture; only Git transport and configuration are substituted.
            repo = root / 'repos/scratch'
            repo.mkdir(parents=True)
            config = dict(push=dict(projects=['scratch', 'scratch'], main={'scratch': 'main'}, path={}))
            class Git:
                def git(self, args, cwd, timeout):
                    assert Path(cwd) == repo
                    return {'symbolic-ref': (0, 'main\n', ''), 'fetch': (0, '', ''),
                            'rev-parse': (0, 'a'*40 + '\n' + 'b'*40 + '\n', ''),
                            'rev-list': (0, '1 1\n', ''), 'merge-base': (1, '', '')}[args[0]]
            stack.enter_context(patch.object(look, 'load_toml', return_value=config))
            stack.enter_context(patch.object(push_origin, 'run',
                                             side_effect=lambda events: origin_run(events, runner=Git())))
        if raised:
            def flaky(*args, **kwargs):
                result = run_steps(*args, **kwargs)
                if len(intakes) == 1:
                    raise RuntimeError('held dispatch fixture')
                return result
            stack.enter_context(patch.object(tick, 'run_steps', side_effect=flaky))
        if scenario == 'dispatch exception after steps':
            stack.enter_context(patch.object(tick, 'run_steps', side_effect=RuntimeError('dispatch fixture')))
        original_import = builtins.__import__

        def importing(name, *args, **kwargs):
            if scenario.startswith('import ') and name == scenario.split()[1]:
                # Only fail tick's guarded imports, after fixture setup finished.
                if sys._getframe(1).f_globals.get('__name__') == 'tick':
                    raise ImportError('fixture missing ' + name)
            return original_import(name, *args, **kwargs)

        snapshots = []

        class Tracked(fold.ReaderSnapshot):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                snapshots.append(self)

        stack.enter_context(patch.object(fold, 'ReaderSnapshot', Tracked))
        read_calls, stat_calls = [], []
        real_stat = ledger_files.stat_keys

        def stat_keys(*args, **kwargs):
            if sys._getframe(1).f_code.co_name == '_record_pass':
                stat_calls.append(1)
                if scenario == 'generation check failure' and len(stat_calls) == 2:
                    raise OSError('fixture generation check unavailable')
            return real_stat(*args, **kwargs)

        if mode == 'steps':
            stack.enter_context(patch.object(ledger_files, 'stat_keys', new=stat_keys))
        if scenario == 'post-dispatch read failure':
            stack.enter_context(patch.object(ledger_cache, 'tail_refresh',
                                             side_effect=OSError('fixture post-dispatch unavailable')))
        if scenario == 'contained errors':
            for name, attr, message in (('intake', 'run', 'intake boom'), ('carry', 'uncarried', 'carry boom'),
                                        ('crons', 'check', 'look boom')):
                stack.enter_context(patch.object(importlib.import_module(name), attr,
                                                 side_effect=RuntimeError(message)))
        allowlist = sorted({tick.TICK_NAME, look.LEDGER_NAME} |
                           ({'L-grader-9903.jsonl'} if reconcile else set()))
        counters = None
        if mode != 'steps':
            if temperature == 'disabled':
                stack.enter_context(patch.dict(os.environ, DOIT_READ_CACHE='0'))
            else:
                stack.enter_context(patch.dict(os.environ, DOIT_READ_CACHE='1'))
            if temperature == 'warm':
                fold.read_events()
            counters = ReadCounter(stack, allowlist, timeline)
        real_read = fold.read_events

        def reader_fn(*args, **kwargs):
            read_calls.append(1)
            if scenario == 'initial lock timeout' and len(read_calls) == 1:
                raise ledger_cache.LockTimeout('fixture initial lock timeout')
            if scenario == 'pre-dispatch lock timeout' and len(read_calls) == 2:
                raise ledger_cache.LockTimeout('fixture required refresh lock timeout')
            return real_read(*args, **kwargs)

        import io
        import fcntl
        out, err = io.StringIO(), io.StringIO()
        contention = None
        with patch.object(builtins, '__import__', new=importing), \
             patch.object(fold, 'read_events', side_effect=reader_fn) as reader, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if scenario == 'lock contention':
                with open(root / 'tick.lock', 'w') as held_lock:
                    fcntl.flock(held_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    todo = tick._record()
                    contention = tick.main()
            elif scenario == 'entrypoint main':
                tick.main()
                todo = None
            elif scenario == 'entrypoint wait':
                todo = tick.wait(root, max_s=0)
            else:
                todo = tick._record()
        full_reads = reader.call_count
        first_calls = planned.call_count
        next_pass_todo = None
        pass_clear = lambda: getattr(tick, '_PASS', None) is None or tick._PASS.get() is None
        mid_state = pass_clear()
        if raised:
            with (case.events / 'L-operator-local.jsonl').open('a') as stream:
                for row in (dict(v=1, type='spec-written', subject='L-spec-9903', project='scratch'),
                            dict(v=1, type='dependency-hold', subject='L-spec-9903',
                                 waits_for='L-spec-9002', why='fixture L-spec-9002')):
                    stream.write(json.dumps(dict(row, ts='2026-11-09T11:00:00+00:00')) + '\n')
        if scenario in ('lane edit between dispatch and render', 'consecutive busy passes') or held:
            next_pass_todo = tick._record()
        rows = ([json.loads(line) for line in tick.tick_path().read_text().splitlines()]
                if tick.tick_path().exists() else [])
        heartbeats = [row for row in rows if row['type'] == 'tick']
        dropped = scenario in ('initial lock timeout', 'pre-dispatch lock timeout', 'lock contention')
        # The baseline has no generation check, so its count is not asserted here.
        assert scenario == 'generation check failure' or \
            len(heartbeats) == (0 if dropped else 2 if next_pass_todo is not None else 1), rows
        source = Path(tick.__file__).resolve().parent
        fresh_code = ('import json,sys,fold,ledger_files; '
                      'rows=[json.loads(l) for l in ledger_files.read_text(fold.EVENTS,sys.argv[2]).splitlines() if l.strip()]; '
                      'adm=[{k:e.get(k) for k in ("type","subject","reason","actor","_src")} '
                      'for e in fold.read_events() if e["type"]=="autodispatch-skipped"]; '
                      'print(json.dumps(dict(rows=rows,admitted=adm)))')
        env = dict(os.environ, DOIT_ROOT=str(root), PYTHONDONTWRITEBYTECODE='1')
        env.pop('DOIT_PROJECT', None)
        fresh = dict(rows=[], admitted=[]) if not tick.tick_path().exists() else json.loads(subprocess.run(
            [sys.executable, '-B', '-c', 'import sys; sys.path.insert(0,sys.argv[1]); ' + fresh_code,
             str(source), tick.TICK_NAME], env=env, capture_output=True, text=True,
            timeout=30, check=True).stdout)
        return dict(fresh=fresh, launch_events_calls=launch_events.call_count,
                    pass_state_clear=pass_clear() and mid_state,
                    scenario=scenario, todo=todo, lane=heartbeats[0]['lane'] if heartbeats else None,
                    steps_calls=first_calls, evaluation_calls=len(calls), full_reads=full_reads,
                    load_count=counters.loads if counters else None,
                    read_calls=counters.calls if counters else [], allowlist=allowlist,
                    violations=counters.violations if counters else [],
                    timeline=timeline, selected=selected, initial_reconcile_step=initial_reconcile_step,
                    next_pass_todo=next_pass_todo,
                    clocks=calls, views=views, requests=requests,
                    dispatch_results=dispatch_results,
                    rows=rows, heartbeat=heartbeats[0] if heartbeats else None, heartbeats=heartbeats,
                    stdout=out.getvalue(), stderr=err.getvalue(), pass_calls=len(snapshots),  # a guarded pass starts by taking its snapshot
                    
                    contention=contention, snapshots=len(snapshots), snapshots_distinct=len({id(x) for x in snapshots}),
                    reads=len(read_calls),
                    source_hashes={name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                                   for name in ('tick.py', 'look.py', 'push_origin.py',
                                                'close_reviews.py', 'relay.py', 'fold.py', 'fold_emits.py')})
    finally:
        case.doCleanups()


class TestReadBudget(Scratch):
    def test_pass_matrix(self):
        inventory = ['unchanged no-write', 'heartbeat-only', 'own intake write',
                     'external intake write', 'reconciliation-only append',
                     'dispatch-only append', 'accepted launch without synchronous append',
                     'own intake plus dispatch writes', 'external intake plus dispatch writes',
                     'partial dispatch append then exception', 'external append only after dispatch',
                     'own intake plus _spawn_busy spawn-failed/vanished append',
                     'external intake plus _spawn_busy spawn-failed/vanished append',
                     'intake plus reconciliation plus dispatch append', 'origin divergence append']
        print('AC1 inventory:', json.dumps(inventory), flush=True)
        print('AC1 finite allowlist:', json.dumps(['L-tick-local.jsonl', look.LEDGER_NAME,
                                                  'L-grader-9903.jsonl']), flush=True)
        before, baseline = self.baseline()
        expected_dispatch = {scenario: self.execute(baseline, scenario, 'budget')
                             for scenario in inventory}
        evidence = []
        for temperature in ('cold', 'warm', 'disabled'):
            for scenario in inventory:
                with self.subTest(temperature=temperature, scenario=scenario):
                    result = _steps_fixture(scenario, 'budget', temperature)
                    expected = 1 if scenario in ('unchanged no-write', 'heartbeat-only',
                                                'external append only after dispatch') else 2
                    self.assertEqual(result['violations'], [], result['violations'])
                    self.assertEqual(result['load_count'], expected, result['read_calls'])
                    self.assertEqual(result['steps_calls'], 1)
                    base = expected_dispatch[scenario]
                    self.assertEqual(result['requests'], base['requests'])
                    self.assertEqual(result['dispatch_results'], base['dispatch_results'])
                    self.assertEqual([list(step) for step in result['selected'][0]],
                                     base['selected'][0])
                    self.assertEqual(result['timeline'][:4],
                                     ['generation', 'load', 'reconcile', 'generation'])
                    if 'reconciliation' in scenario or '_spawn_busy' in scenario:
                        self.assertIn('spawn-failed', result['views'][0])
                        failed = [row for row in result['rows'] if row.get('reason') == 'vanished']
                        self.assertEqual(len(failed), 1)
                        current = [step for step in result['selected'][0] if step[0] == 'L-spec-9903']
                        self.assertNotEqual(current, result['initial_reconcile_step'])
                    evidence.append(dict(temperature=temperature,
                                         baseline_source_hashes=base['source_hashes'],
                                         baseline_requests=base['requests'],
                                         baseline_dispatch_results=base['dispatch_results'], **result))
        report('AC1', dict(before_sha=before, cases=evidence))


def _held_rows(rows):
    """next-step-held content, split into one list per tick pass."""
    passes, current = [], []
    for row in rows:
        if row['type'] == 'next-step-held':
            current.append((row['spec'], row['reason'], row['owner'], row['since']))
        elif row['type'] == 'tick':
            passes.append(current)
            current = []
    return passes


class TestDispatchEquality(Scratch):
    def test_seeded_decisions_and_observers(self):
        scenarios = {'no write': 'unchanged no-write', 'intake write': 'own intake write',
                     'dispatch write': 'dispatch-only append',
                     'both writes': 'own intake plus dispatch writes',
                     'external intake append': 'external intake write',
                     'reconciliation append': 'reconciliation-only append',
                     'own intake plus reconciliation append':
                         'own intake plus _spawn_busy spawn-failed/vanished append',
                     'external intake plus reconciliation append':
                         'external intake plus _spawn_busy spawn-failed/vanished append',
                     'intake plus reconciliation plus dispatch append':
                         'intake plus reconciliation plus dispatch append',
                     'real launch and held spec': 'accepted launch without synchronous append',
                     'partial dispatch failure': 'partial dispatch append then exception',
                     'origin divergence': 'origin divergence append',
                     'consecutive busy passes': 'consecutive busy passes'}
        print('AC2 inventory:', json.dumps(sorted(scenarios)), flush=True)
        self.assertEqual(len(scenarios), 13)
        repo = Path(__file__).resolve().parent.parent
        before, baseline = self.baseline()
        self.assertTrue((baseline / 'tick.py').is_file(), 'baseline source missing')
        evidence = []
        for name, fixture in scenarios.items():
            with self.subTest(scenario=name):
                base = self.execute(baseline, fixture, 'budget')
                cand = self.execute(repo / 'src', fixture, 'budget')
                self.assertNotEqual(base['source_hashes'], cand['source_hashes'],
                                    'baseline and candidate must be different code')
                self.assertEqual(cand['requests'], base['requests'])
                self.assertEqual(cand['dispatch_results'], base['dispatch_results'])
                # The pinned dispatch base already has one planning pass.
                # Compare every selection and clock, including recovery passes.
                passes = len(cand['selected'])
                self.assertEqual(len(base['selected']), passes)
                self.assertEqual(cand['selected'], base['selected'])
                self.assertEqual(cand['clocks'], base['clocks'])
                self.assertEqual(cand['todo'], base['todo'])
                self.assertEqual(cand['lane'], base['lane'])
                launched = sorted({argv[argv.index('--detach') + 2] for argv in cand['requests']
                                   if argv[:2] == ['doit', 'dispatch'] and '--detach' in argv})
                beat = cand['heartbeat']
                # A pre-dispatch refresh exists exactly when intake or reconciliation
                # changed the ledger before dispatch (independent of the later fresh read).
                twice = any(word in fixture for word in ('intake', 'reconciliation', 'spawn-failed', 'consecutive', 'origin'))
                if twice:
                    self.assertEqual(beat['observer_view'], 'pre-dispatch')
                    self.assertEqual(beat['observer_excluded'], launched)
                    self.assertEqual(beat['observer_excluded_count'], len(launched))
                else:
                    self.assertNotIn(beat['observer_view'], ('pre-dispatch', 'unavailable'))
                    self.assertEqual(beat['observer_excluded'], [])
                self.assertNotIn('held_error', beat)
                if name == 'partial dispatch failure':
                    self.assertEqual(beat['autodispatch_error'], 'partial dispatch fixture')
                    self.assertEqual(beat['autodispatch_error'], base['heartbeat']['autodispatch_error'])
                else:
                    self.assertNotIn('autodispatch_error', beat)
                if 'reconciliation' in name:
                    self.assertEqual(cand['timeline'][:4],
                                     ['generation', 'load', 'reconcile', 'generation'])
                    self.assertIn('spawn-failed', cand['views'][0])
                    self.assertEqual(len([r for r in cand['rows'] if r.get('reason') == 'vanished']), 1)
                # Observations: the candidate may only omit subjects it launched this pass.
                base_held = _held_rows(base['rows'])
                base_held[0] = [row for row in base_held[0] if row[0] not in launched]
                cand_held = _held_rows(cand['rows'])
                self.assertEqual(cand_held, base_held)
                for held in cand_held:
                    self.assertEqual(len(held), len(set(held)), 'duplicate observation')
                if name == 'consecutive busy passes':
                    self.assertEqual(len(cand['heartbeats']), 2)
                    self.assertEqual(cand['heartbeats'][0]['observer_excluded'], ['L-spec-9901'])
                    self.assertEqual(cand['heartbeats'][0]['observer_excluded_reason'],
                                     'accepted launch in this pass')
                    first, second = cand_held
                    since = '2026-11-09T11:00:00+00:00'
                    # Zero-pass delay: each unlaunched hold lands in its first eligible pass.
                    self.assertEqual(first, [('L-spec-9902', 'fixture hold', 'tick', since)])
                    # The pass-1 launch is evaluated by pass 2 on that pass's own view.
                    self.assertEqual(second, [('L-spec-9901', 'rework wait', 'tick', since),
                                              ('L-spec-9904', 'second fixture hold', 'tick', since)])
                    self.assertEqual(len([r for r in cand['requests'] if r[:2] == ['doit', 'dispatch']]), 1)
                    self.assertEqual(cand['heartbeats'][1]['observer_excluded'], [])
                evidence.append(dict(scenario=name, fixture=fixture, requests=cand['requests'],
                                     dispatch_results=cand['dispatch_results'],
                                     base_requests=base['requests'], held=cand_held,
                                     base_held=base_held, loads=cand['load_count'],
                                     observer=dict(view=beat['observer_view'],
                                                   excluded=beat['observer_excluded'],
                                                   count=beat['observer_excluded_count'],
                                                   reason=beat['observer_excluded_reason']),
                                     baseline_source_hashes=base['source_hashes'],
                                     candidate_source_hashes=cand['source_hashes']))
        self.assertGreaterEqual(sum(bool(row['requests']) for row in evidence), 5, 'empty action inventory')
        report('AC2', dict(before_sha=before, cases=evidence))


class TestPassDedupe(Scratch):
    def test_routine_recovery_and_direct_launch(self):
        inventory = ['held skips routine', 'held skips raised then next pass',
                     'direct launch', 'direct run_steps', 'unreadable direct launch',
                     'origin divergence']
        print('AC5 inventory:', json.dumps(inventory), flush=True)
        repo = Path(__file__).resolve().parent.parent
        before, baseline = self.baseline()
        evidence = []
        for scenario in inventory[:2]:
            with self.subTest(scenario=scenario):
                base = self.execute(baseline, scenario, 'budget')
                cand = self.execute(repo / 'src', scenario, 'budget')
                skipped = lambda body: [(row['subject'], row['reason']) for row in body['fresh']['admitted']]
                self.assertEqual(skipped(cand), skipped(base))
                self.assertEqual(len(skipped(cand)), len(set(skipped(cand))), 'repeated observation')
                expected = ['L-spec-9901', 'L-spec-9902'] + (['L-spec-9903'] if 'raised' in scenario else [])
                self.assertEqual([subject for subject, _ in skipped(cand)], expected)
                self.assertEqual(cand['violations'], [])
                self.assertEqual(cand['launch_events_calls'], 0, 'hidden _launch_events body read')
                self.assertEqual(base['launch_events_calls'], 0, 'pinned base already deduplicates held observations')
                self.assertTrue(cand['pass_state_clear'], 'pass state leaked past _record')
                self.assertLessEqual(cand['load_count'], 2 * len(cand['heartbeats']) )
                for row in cand['fresh']['admitted']:
                    self.assertEqual(row['actor'], 'tick')
                    self.assertTrue(row['_src'].startswith('L-tick-local.jsonl:'))
                beats = [row for row in cand['fresh']['rows'] if row['type'] == 'tick']
                self.assertEqual(beats, cand['heartbeats'])
                for beat in beats:
                    self.assertIsInstance(beat['lane'], int)
                    self.assertIn(beat['observer_view'], ('initial', 'pre-dispatch', 'post-dispatch'))
                if 'raised' in scenario:
                    self.assertEqual(cand['heartbeats'][0]['autodispatch_error'], 'held dispatch fixture')
                    self.assertNotIn('autodispatch_error', cand['heartbeats'][1])
                evidence.append(dict(scenario=scenario, persisted=skipped(cand),
                                     launch_events_calls=cand['launch_events_calls'],
                                     base_launch_events_calls=base['launch_events_calls'],
                                     heartbeats=[dict(lane=b['lane'], view=b['observer_view'])
                                                 for b in beats]))
        self.origin_roundtrip()
        evidence.append(self.direct_calls())
        report('AC5', dict(before_sha=before, cases=evidence))

    def origin_roundtrip(self):
        TestOriginDedupe.test_supplied_events_and_committed_observations(self)

    def direct_calls(self):
        from datetime import datetime, timezone
        import nextstep
        import tick
        now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        stamp = '2026-11-09T11:00:00+00:00'
        (self.events / 'L-operator-local.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in (
            dict(v=1, type='spec-written', subject='L-spec-9901', project='scratch', ts=stamp),
            dict(v=1, type='dependency-hold', subject='L-spec-9901', waits_for='L-spec-9000',
                 why='fixture', ts=stamp))))
        step = nextstep.Step('L-spec-9901', 'spec', 'planned', 'action', 'build', 'tick',
                             'fixture:1', 'direct', None, stamp)
        sent = []
        def runner(argv):
            sent.append(argv)
            return dict(code=0, stdout='', stderr='')
        persisted = lambda: self.fresh(
            'import json,fold; print(json.dumps([[e["subject"],e["reason"]] for e in '
            'fold.read_events() if e["type"]=="autodispatch-skipped"]))')
        with patch.object(fold, 'NOW', now), patch.object(dispatch, 'now', return_value=now.isoformat()), \
             patch.object(tick, '_launch_events', wraps=tick._launch_events) as guarded:
            self.assertIsNone(tick.launch(step, dispatcher=runner))
            first_reads, first_rows = guarded.call_count, persisted()
            self.assertGreaterEqual(first_reads, 1, 'direct launch keeps its guarded own read')
            self.assertEqual(len(first_rows), 1)
            self.assertIsNone(tick.launch(step, dispatcher=runner))
            self.assertEqual(persisted(), first_rows, 'persisted dedupe: no repeat')
            self.assertGreater(guarded.call_count, first_reads)
            events = fold.read_events()
            specs = fold.fold(events)[0]
            tick.run_steps(events, specs, now, dispatcher=runner, steps=[step])
            self.assertEqual(persisted(), first_rows, 'direct run_steps keeps public dedupe')
            with patch.object(fold, 'read_events', side_effect=ledger_cache.LockTimeout('fixture')):
                self.assertIsNone(tick.launch(step, dispatcher=runner))
            self.assertEqual(persisted(), first_rows, 'unreadable ledger emits nothing')
        self.assertEqual(sent, [])
        self.assertIsNone(tick._PASS.get())
        return dict(scenario='direct', guarded_reads=first_reads, persisted=first_rows)


class TestCompatibility(Scratch):
    def test_entrypoints_and_failures(self):
        inventory = ['direct record', 'entrypoint main', 'entrypoint wait', 'lock contention',
                     'initial lock timeout', 'pre-dispatch lock timeout', 'generation check failure',
                     'post-dispatch read failure', 'contained errors', 'steps RuntimeError',
                     'partial dispatch append then exception', 'held skips raised then next pass']
        print('AC6 inventory:', json.dumps(inventory), flush=True)
        repo = Path(__file__).resolve().parent.parent
        before, baseline = self.baseline()
        fixture = lambda name: 'unchanged pass' if name == 'direct record' else name
        got = {}
        for name in inventory:
            got[name] = (self.execute(baseline, fixture(name)), self.execute(repo / 'src', fixture(name)))
        for name in ('direct record', 'entrypoint main', 'entrypoint wait'):
            base, cand = got[name]
            self.assertEqual(cand['pass_calls'], 1, name)
            self.assertEqual(len(cand['heartbeats']), 1, name)
            self.assertEqual((cand['stdout'], cand['todo']), (base['stdout'], base['todo']), name)
            self.assertEqual(cand['steps_calls'], 1, name)
        self.assertTrue(got['entrypoint main'][1]['stdout'].startswith('tick: '))
        self.assertEqual(got['entrypoint wait'][1]['todo'], 'interval')
        for name in ('lock contention', 'initial lock timeout', 'pre-dispatch lock timeout',
                     'generation check failure'):
            base, cand = got[name]
            self.assertIsNone(cand['todo'], name)
            self.assertEqual((cand['heartbeats'], cand['requests'], cand['steps_calls']), ([], [], 0), name)
            if name != 'generation check failure':
                self.assertEqual((cand['todo'], cand['requests'], cand['heartbeats'], cand['stdout']),
                                 (base['todo'], base['requests'], base['heartbeats'], base['stdout']), name)
        contended = got['lock contention'][1]
        self.assertEqual((contended['pass_calls'], contended['reads'], contended['contention']), (0, 0, 0))
        self.assertIn('dropped', contended['stdout'])
        self.assertEqual(contended['rows'], [])
        for name, message in (('initial lock timeout', 'fixture initial lock timeout'),
                              ('pre-dispatch lock timeout', 'fixture required refresh lock timeout'),
                              ('generation check failure', 'fixture generation check unavailable')):
            self.assertIn(message, got[name][1]['stderr'], name)
        self.assertEqual(got['initial lock timeout'][1]['rows'], [], 'no invented empty ledger')
        self.assertFalse([row for row in got['generation check failure'][1]['rows']
                          if row['type'] in ('tick', 'autodispatched')])
        base, cand = got['post-dispatch read failure']
        self.assertEqual((cand['todo'], cand['requests']), (base['todo'], base['requests']))
        self.assertTrue(any(argv[:2] == ['doit', 'dispatch'] for argv in cand['requests']))
        self.assertTrue(any(row['type'] == 'autodispatched' for row in cand['rows']), 'launch cancelled')
        self.assertEqual(cand['heartbeat']['held_error'], 'fixture post-dispatch unavailable')
        self.assertEqual(cand['heartbeat']['observer_view'], 'unavailable')
        self.assertEqual(cand['heartbeat']['lane'], len(cand['todo']))
        base, cand = got['contained errors']
        for field in ('intake_error', 'carry_error', 'look_error'):
            self.assertEqual(cand['heartbeat'][field], base['heartbeat'][field], field)
        self.assertEqual(cand['requests'], base['requests'])
        self.assertTrue(any(row['type'] == 'autodispatched' for row in cand['rows']))
        for name in ('steps RuntimeError', 'partial dispatch append then exception'):
            base, cand = got[name]
            self.assertEqual(cand['todo'], base['todo'], name)
            self.assertEqual(cand['requests'], base['requests'], name)
            self.assertEqual(cand['heartbeat'].get('autodispatch_error'),
                             base['heartbeat'].get('autodispatch_error'), name)
            self.assertIn('autodispatch_error', cand['heartbeat'])
            self.assertEqual(cand['pass_calls'], 1)
            self.assertTrue(cand['pass_state_clear'], name)
        partial = got['partial dispatch append then exception'][1]
        self.assertTrue(any(row['type'] == 'autodispatched' for row in partial['rows']),
                        'accepted launch survives a later failure')
        base, cand = got['held skips raised then next pass']
        self.assertTrue(cand['pass_state_clear'])
        self.assertEqual((cand['snapshots'], cand['snapshots_distinct']), (2, 2),
                         'each pass owns a fresh snapshot, including after an exception')
        self.assertIn('autodispatch_error', cand['heartbeats'][0])
        self.assertNotIn('autodispatch_error', cand['heartbeats'][1])
        report('AC6', dict(before_sha=before, cases=[
            dict(scenario=name, todo=cand['todo'], requests=len(cand['requests']),
                 heartbeats=len(cand['heartbeats']), stderr=cand['stderr'],
                 base_todo=base['todo'], pass_calls=cand['pass_calls'])
            for name, (base, cand) in got.items()]))


class TestStepsOnce(Scratch):
    def test_todo_real_launch_and_failure(self):
        inventory = ['unchanged pass', 'writing pass', 'steps RuntimeError',
                     'dispatch exception after steps', 'import autodispatch',
                     'import merge_queue', 'import autodispatch then steps failure',
                     'import merge_queue then steps failure', 'computed empty',
                     'lane edit between dispatch and render']
        print('AC3 inventory:', json.dumps(inventory), flush=True)
        repo = Path(__file__).resolve().parent.parent
        before, baseline = self.baseline()

        evidence = []
        for scenario in inventory:
            base = self.execute(baseline, scenario)
            candidate = self.execute(repo / 'src', scenario)
            self.assertEqual(candidate['steps_calls'], 1, scenario)
            self.assertLessEqual(candidate['full_reads'], 2, scenario)
            if scenario == 'unchanged pass':
                self.assertEqual(candidate['full_reads'], 1)
            self.assertEqual(candidate['lane'], len(candidate['todo']), scenario)
            if scenario == 'lane edit between dispatch and render':
                self.assertTrue(any('line one line two' in row for row in base['todo']))
                self.assertTrue(any('line one line two' in row for row in candidate['todo']))
                self.assertEqual(candidate['next_pass_todo'], base['next_pass_todo'])
                self.assertTrue(any('edited lane rule' in row for row in candidate['next_pass_todo']))
            else:
                self.assertEqual(candidate['todo'], base['todo'], scenario)
            self.assertEqual(candidate['requests'], base['requests'], scenario)
            if 'steps failure' in scenario or scenario == 'steps RuntimeError':
                self.assertEqual(candidate['todo'], ['inbound:new-source · uncarried',
                                 'undetermined: next_steps failed: RuntimeError'])
                self.assertIn('autodispatch_error', candidate['heartbeat'])
            if scenario in ('unchanged pass', 'writing pass'):
                self.assertEqual(sum('line one line two' in row for row in candidate['todo']), 2)
                self.assertNotIn('inbound:busy-source · uncarried', candidate['todo'])
            if scenario == 'writing pass':
                self.assertTrue(any(row['type'] == 'autodispatched' for row in candidate['rows']))
                self.assertTrue(any(argv[:2] == ['doit', 'dispatch'] for argv in candidate['requests']))
            if scenario == 'unchanged pass':
                self.assertEqual([row['type'] for row in candidate['rows']], ['tick'])
                self.assertEqual(candidate['requests'], [])
            if scenario == 'computed empty':
                self.assertEqual(candidate['todo'], ['inbound:new-source · uncarried'])
                self.assertNotIn('autodispatch_error', candidate['heartbeat'])
            if scenario.startswith('import '):
                self.assertIn('fixture missing ' + scenario.split()[1],
                              candidate['heartbeat']['autodispatch_error'])
                self.assertEqual(base['steps_calls'], 1, 'base renders after pre-planning import failure')
                self.assertEqual(candidate['requests'], [])
            evidence.append(dict(scenario=scenario, base_calls=base['steps_calls'],
                                 candidate_calls=candidate['steps_calls'],
                                 expected=base['todo'], actual=candidate['todo'],
                                 requests=candidate['requests'],
                                 baseline_source_hashes=base['source_hashes'],
                                 candidate_source_hashes=candidate['source_hashes']))
        report('AC3', dict(before_sha=before, cases=evidence))


if __name__ == '__main__':
    unittest.main()
