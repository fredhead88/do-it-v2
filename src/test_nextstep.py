"""Regression tests for the pure next-step snapshot and read-only CLI."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import nextstep as ns
import dispatch
import fold
import lane_rules as lr


NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
SID = 'L-spec-9001'
KEYS = {'item', 'item_kind', 'stage', 'state', 'action', 'owner',
        'trigger', 'reason', 'clears', 'since', 'attempt', 'retry_at'}
ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / 'src/fixtures/nextstep'


def event(kind, minutes=-30, subject=SID, **extra):
    return dict(type=kind, ts=(NOW + timedelta(minutes=minutes)).isoformat(),
                subject=subject, _src=f'L-operator-fixture.jsonl:{minutes + 20000}', **extra)


def specs_for(events):
    return {sid: dict(state='written') for sid in {e['subject'] for e in events}
            if sid.startswith('L-spec-')}


def replay(name):
    directory = FIXTURES / name
    events = [json.loads(l) for l in (directory / 'events.jsonl').read_text().splitlines()]
    lines = (directory / 'source.txt').read_text().splitlines()
    now = datetime.fromisoformat(lines[0].split('cut: ')[1])
    # The caller supplies fold state; replay time never comes from fold.NOW.
    normalized = ns._snapshot(events, {})[0]
    with patch.object(fold, 'NOW', now):
        specs = fold.fold(normalized)[0]
    waves = json.loads((directory / 'waves.json').read_text())
    return events, specs, now, waves


class NextStepTests(unittest.TestCase):
    def setUp(self):
        self.rules = lr.load()

    def steps(self, events, specs=None, now=NOW, rules=None, waves=None):
        return ns.next_steps(events, specs if specs is not None else specs_for(events),
                             now, self.rules if rules is None else rules, waves)

    def one(self, events, specs=None, **kwargs):
        return next(s for s in self.steps(events, specs, **kwargs) if s.item == SID)

    def test_ac1_frozen_validated_shape(self):
        step = ns.Step(SID, 'spec', 'planned', 'hold', None, 'executor',
                       None, 'waiting', 'decision', NOW.isoformat())
        self.assertEqual({f.name for f in fields(step)}, KEYS)
        self.assertIsNot(replace(step), step)
        with self.assertRaises(FrozenInstanceError):
            step.owner = 'tick'
        for change in ({'state': 'action'}, {'reason': ''}, {'clears': None},
                       {'owner': 'nobody'}, {'item_kind': 'row'},
                       {'stage': 'done'}, {'state': 'unknown'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(step, **change)

    def test_ac2_live_item_identity_and_order(self):
        events = [event('charter-filed', subject='L-charter-9001')]
        states = ['written', 'building', 'graded', 'reviewing', 'unknown', *dispatch.SETTLED_STATES]
        specs = {}
        expected = {('charter', 'L-charter-9001'), ('attempt', 'L-builder-9001'),
                    ('window', 'win-fixture')}
        for i, state in enumerate(states):
            sid = f'L-spec-{9100 + i}'
            specs[sid] = dict(state=state)
            events.append(event('spec-written', subject=sid))
            if state not in dispatch.SETTLED_STATES:
                expected.add(('spec', sid))
            if state in ns.OWED_STATES:
                events += [event('owed-ac', -25, subject=sid, criterion='AC1', wake_at=event('x', -24)['ts']),
                           event('shipped', -20, subject=sid)]
                expected.add(('check', sid + ':AC1'))
        events += [event('build-started', -2, subject='L-spec-9101', spawn='L-builder-9001'),
                   event('window-opened', -3, subject='win-fixture')]
        baseline = self.steps(events, specs)
        identities = [(s.item_kind, s.item) for s in baseline]
        self.assertEqual(set(identities), expected)
        self.assertEqual(len(identities), len(expected))
        self.assertEqual(identities, sorted(identities))
        self.assertEqual(baseline, self.steps(events, specs))
        random.Random(41).shuffle(events)
        self.assertEqual(baseline, self.steps(events, specs))

    def selected(self, results, rules=None):
        predicates = {n: (lambda c, n=n: results.get(n)) for n in ns.PREDICATES}
        with patch.dict(ns.PREDICATES, predicates, clear=True):
            return self.one([event('spec-written')], rules=rules)

    def test_ac3_predicate_precedence_and_conflicts(self):
        row = dict(since=event('x')['ts'], reason='ready')
        for actions, expected in [(['build'], 'build'), (['build', 'rebuild'], 'build'),
                                  (['grade', 'regrade'], 'grade')]:
            results = {n: dict(row) for n in actions}
            results.update({'wave-held': dict(row, blocker='L-spec-9002'), 'idle': row})
            step = self.selected(results)
            self.assertEqual((step.state, step.action), ('action', expected))
        conflict = self.selected({'build': row, 'grade': row})
        self.assertTrue(conflict.reason.startswith('undetermined: conflicting actions'))
        self.assertEqual((conflict.state, conflict.owner), ('hold', 'executor'))
        for name in ('wave-held', 'dependency-held'):
            held = self.selected({name: dict(row, blocker='L-spec-9002'), 'idle': row})
            self.assertEqual((held.state, held.owner), ('hold', 'tick'))
            self.assertIn('L-spec-9002', held.clears)
        self.assertEqual(self.selected({'idle': row}).owner, 'executor')
        held = self.selected({'build': dict(row, reason='undetermined: missing timestamp')})
        self.assertEqual((held.state, held.owner), ('hold', 'executor'))
        # Table order, rather than predicate registry order, wins ties.
        reversed_rules = dict(reversed(list(self.rules.items())))
        self.assertEqual(self.selected({'build': row, 'rebuild': row}, reversed_rules).action, 'rebuild')

    def test_ac4_table_owners_and_judgment(self):
        row = dict(since=event('x')['ts'], reason='ready')
        for action in ns.ACTION_STAGES:
            step = self.selected({action: row})
            self.assertEqual(step.owner, self.rules[action]['actor'])
            self.assertEqual(step.owner, ns.ACTIONS[step.action])
        self.assertEqual(ns.ACTIONS, {**{n: self.rules[n]['actor'] for n in ns.ACTION_STAGES},
                                     'sweep-check': 'sweeper'})
        changed = deepcopy(self.rules)
        changed['build']['actor'] = 'executor'
        self.assertEqual(self.selected({'build': row}, changed).owner, 'executor')
        events = [event('spec-written'), event('question', -20)]
        self.assertEqual(self.one(events).owner, 'executor')
        events[-1] = event('escalation-blocking', -20)
        self.assertEqual(self.one(events).owner, 'executor')
        events = [event('spec-written'), event('owed-ac', -25, criterion='AC1', wake_at=event('x', -24)['ts']),
                  event('shipped', -20), event('owed-unobservable', -10, criterion='AC1', capability='zzz')]
        check = self.steps(events, {SID: dict(state='shipped-owed-expired')})[0]
        self.assertEqual((check.state, check.owner, check.reason), ('hold', 'executor', 'owed-expired'))

    def test_ac5_pure_inherited_predicates_and_clock(self):
        events, specs, now, waves = replay('w42')
        events += [event('window-opened', subject='win-fixture')]
        # Carry a footprint to catch accidental reuse of the file-reading wave helper.
        events[0]['footprint'] = ['fixture.py']
        originals = deepcopy((events, specs, self.rules, waves))
        def calls():
            return (ns.next_steps(events, specs, now, self.rules, waves),
                    ns.step_for(events, specs, 'L-spec-1459', now, self.rules, waves),
                    ns.classify_failure('ENOSPC', 1, self.rules),
                    ns.flow_counts(events, specs, now, self.rules, waves))
        expected = calls()
        class NoEnvironment:
            def fail(self, *args, **kwargs):
                raise AssertionError('environment read')
            get = __getitem__ = __iter__ = __contains__ = fail
        with ExitStack() as stack:
            for name in ('io.open', 'builtins.open', 'pathlib.Path.open', 'subprocess.run',
                         'time.time', 'lane_rules.load', 'lane_rules.due',
                         'dispatch.spec_wave', 'fold.spec_state', 'fold.owed_due'):
                stack.enter_context(patch(name, side_effect=AssertionError(name)))
            stack.enter_context(patch.object(os, 'environ', NoEnvironment()))
            stack.enter_context(patch.object(fold, 'NOW', NOW.replace(year=2040)))
            self.assertEqual(calls(), expected)
        self.assertEqual((events, specs, self.rules, waves), originals)
        later = ns.next_steps(events, specs, now + timedelta(days=1), self.rules, waves)
        self.assertNotEqual(later, expected[0])
        a, b = 'L-spec-9002', SID
        wave_events = [event('spec-written', subject=s, charter='L-charter-9001', footprint=['file.py'])
                       for s in (a, b)]
        step = self.one(wave_events, waves={a: 1, b: 2})
        self.assertEqual((step.state, step.owner), ('hold', 'tick'))
        self.assertIn(a, step.clears)

    def test_ac6_undetermined_is_scoped_and_never_action(self):
        events = [event('spec-written'), event('spec-written', subject='L-spec-9002')]
        baseline = self.steps(events)
        for bad in ({'type': 'decision', 'subject': SID},
                    event('decision', ts_override=True)):
            if 'ts_override' in bad:
                bad.pop('ts_override')
                bad['ts'] = '2026-10-23T11:00:00'
            result = self.steps(events + [bad])
            self.assertEqual([s for s in result if s.item != SID], [s for s in baseline if s.item != SID])
            step = next(s for s in result if s.item == SID)
            self.assertEqual((step.state, step.owner), ('hold', 'executor'))
            self.assertTrue(step.reason.startswith('undetermined:'))
        for state in ('unknown', 'invented'):
            self.assertTrue(self.one(events[:1], {SID: dict(state=state)}).reason.startswith('undetermined:'))
        terminals = events[:1] + [event('shipped', -10), event('spec-killed', -5)]
        self.assertTrue(self.one(terminals, {SID: dict(state='killed')}).reason.startswith('undetermined:'))
        for rules in (None, {}, [], 1, {'build': {}}, dict(self.rules, build={'name': 'build'})):
            steps = ns.next_steps(events, specs_for(events), NOW, rules)
            self.assertEqual(len(steps), 2)
            self.assertTrue(all(s.state == 'hold' and s.owner == 'executor'
                                and s.reason.startswith('undetermined:') for s in steps))
        missing = ns.next_steps([], {SID: dict(state='written')}, NOW, self.rules)[0]
        self.assertTrue(missing.reason.startswith('undetermined:'))
        missing = self.selected({'build': dict(since='not-a-time', reason='ready')})
        self.assertTrue(missing.reason.startswith('undetermined:'))
        malformed = [{'type': 'build-started', 'subject': SID, 'spawn': 'L-builder-9001'},
                     {'type': 'charter-filed', 'subject': 'L-charter-9001'},
                     {'type': 'window-opened', 'subject': 'win-bad'}]
        steps = self.steps(malformed)
        self.assertEqual(len(steps), 4)
        self.assertTrue(all(s.state == 'hold' and s.owner == 'executor'
                            and s.reason.startswith('undetermined:') for s in steps))

    def test_ac7_failure_classifier(self):
        infra = ['No space left on device', '[Errno 28]', 'Out of diskspace', 'ENOSPC',
                 'fatal: could not create work tree dir', 'git worktree add failed',
                 'failed git worktree add', 'Connection reset by peer', 'Connection refused',
                 'at capacity', 'overloaded', 'HTTP 429',
                 'ledger cache: lock not acquired within 120s (/home/x/.do-it/cache/ledger-abc.lock)']
        for text in infra:
            with self.subTest(text=text):
                self.assertEqual(ns.classify_failure(text, 1), 'infra')
                self.assertEqual(ns.classify_failure(text + '\nFAILED', 1), 'unknown')
                self.assertEqual(ns.classify_failure('AssertionError\n' + text, 1), 'unknown')
        for text in ['FAILED', 'AssertionError', 'Ran 12 tests\nFAILED']:
            self.assertEqual(ns.classify_failure(text, 1), 'work')
        for text in ['', None]:
            self.assertEqual(ns.classify_failure(text, 124), 'infra')
            for code in (None, 0, 1, 127):
                self.assertEqual(ns.classify_failure(text, code), 'unknown')
        for text in ['14290', 'all green', 'HTTP 14290', 'ledger cache: lock acquired']:
            self.assertEqual(ns.classify_failure(text, 1), 'unknown')
        self.assertEqual(ns.classify_failure('ENOSPC', None), 'unknown')

    def test_ac8_stage_counts_and_seven_day_boundary(self):
        events = [event('spec-written'), event('build-started', -20, spawn='L-builder-9001'),
                  event('window-opened', -10, subject='win-fixture'),
                  event('charter-filed', subject='L-charter-9001')]
        specs = {SID: dict(state='building')}
        for i, age in enumerate((timedelta(days=6, hours=23), timedelta(days=7, hours=1))):
            sid = f'L-spec-{9100+i}'
            events.append(dict(event('shipped', subject=sid), ts=(NOW-age).isoformat()))
            specs[sid] = dict(state='shipped')
        sid = 'L-spec-9100'
        events.append(event('owed-ac', -10, subject=sid, criterion='AC1', wake_at=event('x', -9)['ts']))
        specs[sid]['state'] = 'shipped-owed-due'
        steps = self.steps(events, specs)
        counts = ns.flow_counts(events, specs, NOW, self.rules)
        self.assertEqual(set(counts), set(ns.STAGES))
        self.assertTrue(all(type(v) is int for v in counts.values()))
        self.assertEqual(counts['live'], 1)
        for stage in ns.STAGES[:-1]:
            self.assertEqual(counts[stage], sum(s.stage == stage for s in steps))
        self.assertTrue(all(s.stage == 'live' for s in steps if s.item_kind == 'check'))
        row = dict(since=event('x')['ts'], reason='ready')
        for action, stage in ns.ACTION_STAGES.items():
            self.assertEqual(self.selected({action: row}).stage, stage)
        for kind, stage in [('spec-written', 'planned'), ('build-started', 'building'),
                            ('build-done', 'checking'), ('verdict', 'checking'), ('queue-add', 'merging')]:
            with patch.dict(ns.PREDICATES, {n: lambda c: None for n in ns.PREDICATES}):
                self.assertEqual(self.one([event('spec-written', -60), event(kind)]).stage, stage)

    def test_ac9_real_replay_shapes(self):
        actions = {'w41': ('L-spec-1451', 'L-thinker-0004.jsonl:6272', '2026-10-22T13:52:25+00:00'),
                   'w50': ('L-spec-1418', 'L-thinker-0004.jsonl:6139', '2026-10-22T07:55:19+00:00'),
                   'w51': ('L-spec-1417', 'L-thinker-0004.jsonl:6210', '2026-10-22T11:21:20+00:00')}
        for name, (sid, trigger, since) in actions.items():
            events, specs, now, waves = replay(name)
            steps = ns.next_steps(events, specs, now, self.rules, waves)
            matching = [s for s in steps if s.item == sid]
            self.assertEqual(len(matching), 1)
            expected = ns.Step(sid, 'spec', 'building', 'action', 'rebuild', 'tick', trigger,
                               'condition met', None, since, 1)
            for key in KEYS:
                self.assertEqual(asdict(matching[0])[key], asdict(expected)[key], (name, key))
            if name == 'w41':
                blocked = next(e for e in events if e['type'] == 'build-blocked')
                decision = next(e for e in events if e['_src'] == trigger)
                self.assertEqual(decision['ref'], blocked['_src'])
                ship = json.loads((FIXTURES/name/'shipped.json').read_text())
                specs[sid]['state'] = 'shipped'
                self.assertFalse(any(s.item == sid for s in ns.next_steps(events+[ship], specs, now, self.rules, waves)))
            print(name, json.dumps(asdict(expected), sort_keys=True))
        events, specs, now, waves = replay('w42')
        step = ns.step_for(events, specs, 'L-spec-1459', now, self.rules, waves)
        expected = ns.Step('L-spec-1459', 'spec', 'checking', 'hold', None, 'tick',
                           'L-thinker-0004.jsonl:6353', 'dependency: blocker-open: build',
                           'L-spec-1457 shipped', '2026-10-22T16:42:29+00:00')
        self.assertEqual(asdict(step), asdict(expected))
        self.assertFalse(any(e['type'] == 'shipped' and e['subject'] == 'L-spec-1457' for e in events))
        print('w42', json.dumps(asdict(step), sort_keys=True))
        events, specs, now, waves = replay('w56')
        for item, kind, reason in [('L-builder-3018', 'attempt', 'in flight'),
                                   ('L-spec-1417', 'spec', 'in flight: L-builder-3018')]:
            expected = ns.Step(item, kind, 'building', 'hold', None, 'tick', 'L-builder-3018.jsonl:1',
                               reason, 'L-builder-3018 spawn-done/spawn-failed', '2026-10-22T10:14:59+00:00')
            if kind == 'spec':
                expected = replace(expected, action='rebuild', trigger='L-thinker-0004.jsonl:6180',
                                   reason='in flight', clears='retry time, evidence or decision',
                                   since='2026-10-22T10:05:20+00:00', attempt=1)
            self.assertEqual(ns.step_for(events, specs, item, now, self.rules, waves), expected)
        self.assertEqual(next(e for e in events if e['_src'] == 'L-builder-3018.jsonl:1')['issued_by'], 'executor')
        print('w56', json.dumps(asdict(expected), sort_keys=True))
        events, specs, now, waves = replay('disk')
        expected = ns.Step('L-spec-1186', 'spec', 'checking', 'hold', None, 'executor',
                           'L-executor-0001.jsonl:21570', 'infra: retry required',
                           'infra-retry backoff or freed disk', '2026-10-23T08:11:56+00:00')
        self.assertEqual(ns.step_for(events, specs, expected.item, now, self.rules, waves), expected)
        window = ns.step_for(events, specs, 'win-proj-a', now, self.rules, waves)
        self.assertEqual(window, ns.Step('win-proj-a', 'window', 'merging', 'hold', None, 'executor',
                                        'L-executor-0001.jsonl:21568', 'window waiting; elapsed_min=0',
                                        'window resolution', '2026-10-23T08:11:22+00:00'))
        print('disk', json.dumps(asdict(expected), sort_keys=True))

    def test_ac10_fixture_redaction_and_field_minimum(self):
        forbidden = ['albert-scott', 'fredhead88', 'qurlife', 'amazon', '/opt/', '/home/',
                     'docs/sessions', 'tests/pipelines', 'dashboard/', '.env']
        allowed = set('v ts type subject spawn role issued_by backend branch reason findings why error ref '
                      'rework regrade close status blocker charter src _src waits_for'.split())
        for path in FIXTURES.rglob('*'):
            if not path.is_file():
                continue
            text = path.read_text()
            for fragment in forbidden:
                self.assertNotIn(fragment, text, str(path))
            if path.name in {'events.jsonl', 'shipped.json'}:
                for line in text.splitlines():
                    self.assertLessEqual(set(json.loads(line)), allowed)

    def test_ac9_failure_first_and_newer_retry(self):
        events = [event('spec-written', -60), event('build-blocked', -50),
                  event('decision', -40, actor='thinker', rework='yes')]
        for kind in ('merge-gate-rework', 'spawn-failed', 'autodispatch-failed'):
            for text, prefix in [('ENOSPC', 'infra:'), ('unmatched', 'undetermined:')]:
                failure = event(kind, -30, findings=text, role='builder',
                                spawn='L-builder-9001', reason=text)
                step = self.one(events + [failure])
                if kind == 'merge-gate-rework':
                    self.assertEqual((step.state, step.owner), ('hold', 'executor'))
                    self.assertTrue(step.reason.startswith(prefix))
                else:
                    self.assertEqual((step.state, step.owner, step.action, step.attempt),
                                     ('action', 'tick', 'rebuild', 2))
            failure = event(kind, -30, findings='FAILED')
            with patch.dict(ns.PREDICATES, {'rebuild': lambda c: dict(
                    since=events[-1]['ts'], reason='table evaluated')}):
                self.assertEqual(self.one(events + [failure]).action, 'rebuild')
            retry = event('decision', -20, actor='thinker', rework='yes')
            self.assertEqual(self.one(events + [event(kind, -30, findings='ENOSPC'), retry]).action, 'rebuild')

    def test_ac11_cli_read_only_json_text_and_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'events').mkdir()
            events = [event('spec-written'), event('question', -5, reason='line\nbreak\x1b')]
            (root/'events/L-operator-fixture.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
            env = dict(os.environ, DOIT_ROOT=str(root), PYTHONDONTWRITEBYTECODE='1')
            for key in ('DOIT_LEDGER_FILE', 'DOIT_LANE_RULES', 'DOIT_PROJECT'):
                env.pop(key, None)
            def tree():
                return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in root.rglob('*') if p.is_file()}
            def run(*args, environment=None):
                return subprocess.run([str(ROOT/'doit'), 'next', *args], env=environment or env,
                                      text=True, capture_output=True)
            before = tree()
            result = run('--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            obj = json.loads(result.stdout)
            self.assertEqual(set(obj), {'counts', 'steps'})
            self.assertTrue(obj['steps'])
            self.assertTrue(all(set(s) == KEYS for s in obj['steps']))
            for args in [(), (SID,)]:
                result = run(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(result.stdout.splitlines()), 1)
                self.assertIn(SID+' | hold | executor | ', result.stdout)
            self.assertEqual(run('absent').returncode, 1)
            for environment in [dict(env, DOIT_LANE_RULES=str(root/'absent.toml')),
                                dict(env, DOIT_ROOT=str(root/'absent'))]:
                result = run(environment=environment)
                self.assertEqual((result.returncode, result.stdout), (2, ''))
                self.assertTrue(result.stderr.strip())
            (root/'bad.toml').write_text('invalid = [')
            result = run(environment=dict(env, DOIT_LANE_RULES=str(root/'bad.toml')))
            self.assertEqual((result.returncode, result.stdout), (2, ''))
            (root/'bad.toml').unlink()
            self.assertEqual(before, tree())
            # Default invocation still renders the existing board entry point.
            result = subprocess.run([str(ROOT/'doit')], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ns._clean('a\nb\rc\x1b\u0085d'), 'a b c  d')
        with patch.object(fold, 'read_events', return_value=[]), \
             patch.object(fold, 'fold', return_value=({}, {})), \
             patch.object(ns, 'datetime') as clock, \
             tempfile.TemporaryDirectory() as temporary, \
             patch.object(fold, 'EVENTS', Path(temporary)), \
             redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            clock.now.side_effect = ValueError('clock unavailable')
            self.assertEqual(ns.main([]), 2)
            self.assertEqual(out.getvalue(), '')
            self.assertIn('clock unavailable', err.getvalue())



class Retry1672Tests(unittest.TestCase):
    def chain(self, action):
        rows = [event('spec-written', -200)]
        if action == 'rebuild':
            rows += [event('build-blocked', -190),
                     event('decision', -180, actor='thinker', rework='yes')]
        elif action in ('grade', 'regrade', 'review'):
            rows += [event('build-done', -190, actor='builder', status='DONE', ready_sha='new')]
            if action == 'regrade':
                rows += [event('decision', -180, actor='thinker', regrade='yes')]
            elif action == 'review':
                rows += [event('verdict', -180, confirmed=True)]
        return rows

    def one(self, rows, now=NOW, specs=None):
        return ns.step_for(rows, specs or specs_for(rows), SID, now, lr.load())

    def test_retry1672_AC1_role_history_matrix(self):
        import attempts
        roles = dict(build='builder', rebuild='builder', grade='grader',
                     regrade='grader', review='reviewer')
        outputs = dict(builder=['build-done', 'build-blocked'],
                       grader=['verdict', 'cannot-assess'], reviewer=['review'])
        for action, role in roles.items():
            rows = self.chain(action)
            for failure in ('spawn-failed', 'autodispatch-failed', 'spawn-stale', 'spawn-done'):
                with self.subTest(action=action, failure=failure):
                    spawn = f'L-{role}-9001'
                    failed = rows + [event('spawn-started', -100, role=role, spawn=spawn),
                                     event(failure, -90, role=role, spawn=spawn, reason='unknown cause')]
                    if failure == 'autodispatch-failed':
                        failed = rows + [event(failure, -90, role=role, reason='unknown cause')]
                    with patch.object(attempts, 'history', wraps=attempts.history) as helper:
                        step = self.one(failed)
                    self.assertTrue(helper.called)
                    self.assertEqual((step.action, step.attempt), (action, 2))
                    self.assertEqual(step.state, 'action')
            dead = rows + [event('spawn-started', -80, role=role, spawn=f'L-{role}-9001',
                                 waiter_pid=99999999, waiter_host=ns.autodispatch.socket.gethostname())]
            with patch.object(ns.autodispatch.panes, '_is_live', return_value=False):
                self.assertEqual((self.one(dead).state, self.one(dead).attempt), ('action', 2))
            live = rows + [event('spawn-started', -1, role=role, spawn=f'L-{role}-9001')]
            self.assertEqual(self.one(live).state, 'hold')
            for output in outputs[role] + ['queue-add', 'merge-gate-clean']:
                with self.subTest(action=action, output=output):
                    done = live + [event(output, 0, spawn=f'L-{role}-9001')]
                    step = self.one(done)
                    self.assertFalse(step.action == action and step.state == 'action')
            unrelated = rows + [event('spawn-failed', -20, role='spec-writer', spawn='L-spec-writer-9001'),
                                event('spawn-failed', -10, subject='L-spec-9002', role=role, spawn=f'L-{role}-9002')]
            self.assertEqual(self.one(unrelated).attempt, 1)
            older = [event('spawn-started', -300, role=role, spawn=f'L-{role}-9000')] + rows
            older += [event('spawn-failed', -20, role=role, spawn=f'L-{role}-9000')]
            self.assertEqual(self.one(older).attempt, 1)
            for terminal in ('shipped', 'spec-killed', 'spec-closed'):
                self.assertNotEqual(self.one(rows + [event(terminal, -1)]).state, 'action')

    def test_retry1672_AC2_boundaries_exhaustion_reset_serialization(self):
        for action, role in dict(build='builder', rebuild='builder', grade='grader',
                                 regrade='grader', review='reviewer').items():
            rows = self.chain(action)
            first = event('autodispatch-failed', -60, role=role, reason='first')
            rows.append(first)
            for delta, state in ((timedelta(minutes=5, microseconds=-1), 'hold'),
                                 (timedelta(minutes=5), 'action')):
                step = self.one(rows, ns._time(first['ts']) + delta)
                self.assertEqual((step.state, step.attempt), (state, 2), action)
                self.assertEqual(step.since, self.chain(action)[-1]['ts'])
                self.assertEqual(step.trigger, self.chain(action)[-1]['_src'])
                if state == 'hold':
                    self.assertIn('retry-wait', step.reason)
                    self.assertEqual(ns._time(step.retry_at), ns._time(first['ts']) + timedelta(minutes=5))
                self.assertEqual(json.loads(json.dumps(asdict(step)))['attempt'], 2)
            second = event('autodispatch-failed', -50, role=role, reason='second')
            rows.append(second)
            for delta, state in ((timedelta(minutes=15, microseconds=-1), 'hold'),
                                 (timedelta(minutes=15), 'action')):
                self.assertEqual(self.one(rows, ns._time(second['ts']) + delta).state, state)
            third = event('autodispatch-failed', -30, role=role, reason='third')
            rows.append(third)
            for now in (ns._time(third['ts']), NOW + timedelta(days=2)):
                step = self.one(rows, now)
                self.assertEqual((step.state, step.attempt, step.retry_at), ('hold', 3, None))
                self.assertIn('retries-exhausted', step.reason)
            reset = event('decision', -10, actor='thinker',
                          **({'rework': 'yes'} if action == 'rebuild' else
                             {'regrade': 'yes'} if action == 'regrade' else {}))
            self.assertEqual(self.one(rows + [reset]).attempt, 1)
            self.assertEqual(self.one(rows + [event('note', -1)]).attempt, 3)
            if action != 'rebuild':
                for kind in ('unblocked', 'spec-amended', 'spec-written'):
                    reset = event(kind, -10, actor='operator')
                    step = self.one(rows + [reset])
                    self.assertEqual((step.state, step.attempt, step.trigger), ('action', 1, reset['_src']))
                    self.assertEqual(self.one(rows + [reset]), step)  # polling is stable
            if role == 'grader':
                bundle = event('evidence-bundle', -10, sha='new')
                self.assertEqual((self.one(rows + [bundle]).attempt,
                                  self.one(rows + [bundle]).trigger), (1, bundle['_src']))

    def test_retry1672_AC3_newest_bundle_refusal_strict_readiness(self):
        for action in ('grade', 'regrade'):
            rows = self.chain(action)
            self.assertEqual(self.one(rows).action, action)  # bootstrap
            preparatory = event('autodispatch-failed', -100, role='grader', reason='packet failed')
            self.assertEqual(self.one(rows + [preparatory]).attempt, 2)
            refusal = event('spawn-failed', -80, role='grader', spawn='L-grader-9001', reason='bundle-pending')
            refused = rows + [refusal]
            self.assertEqual((self.one(refused).state, self.one(refused).attempt), ('hold', 1))
            for kind in ('evidence-bundle', 'evidence-bundle-failed'):
                for minute, sha, ready in ((-90, 'new', False), (-80, 'new', False),
                                           (-70, 'old', False), (-70, 'new', True)):
                    with self.subTest(action=action, kind=kind, minute=minute, sha=sha):
                        step = self.one(refused + [event(kind, minute, sha=sha)])
                        self.assertEqual(step.state, 'action' if ready else 'hold')
                        self.assertEqual(step.attempt, 1)
            newest = event('build-done', -20, actor='builder', status='DONE', ready_sha='newer')
            self.assertEqual(self.one(refused + [newest]).action, 'grade')

    def test_retry1672_AC3_tick_real_gate_and_checker_publication(self):
        import argparse
        import grader_packet
        import models
        import tick
        with tempfile.TemporaryDirectory(prefix='retry1672-bootstrap-') as temporary, ExitStack() as stack:
            top = Path(temporary)
            root, scratch = top / 'root', top / 'scratch'
            repo = root / 'repos/demo'
            repo.mkdir(parents=True)
            for name in ('events', 'content', 'worktrees/demo/l-spec-9001'):
                (root / name).mkdir(parents=True)
            def git(*argv):
                return subprocess.check_output(['git', '-C', str(repo), *argv],
                                               text=True, stderr=subprocess.DEVNULL).strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / 'probe.py').write_text("print('local evidence ready')\n")
            git('add', '.')
            git('commit', '-qm', 'fixture')
            ready = git('rev-parse', 'HEAD')
            (root / 'content' / (SID + '.md')).write_text(
                '## Acceptance criteria\nAC1 [backend]: local fixture.\n'
                'review_path: run `/usr/bin/python3 probe.py`\n')
            (root / 'events/L-planner-fixture.jsonl').write_text(json.dumps(dict(
                v=1, type='spec-written', subject=SID, project='demo',
                ts=(NOW - timedelta(hours=2)).isoformat())) + '\n')
            (root / 'events/L-builder-fixture.jsonl').write_text(json.dumps(dict(
                v=1, type='build-done', subject=SID, project='demo', status='DONE',
                ready_sha=ready, base_sha=ready, ts=NOW.isoformat())) + '\n')
            packet = root / 'packet.md'
            packet.write_text('fixture packet awaiting evidence')
            env = dict(os.environ, DOIT_ROOT=str(root), DOIT_NO_POKE='1',
                       DOIT_SCRATCH=str(scratch), DOIT_MAIN='main')
            for key in list(env):
                if key.startswith('SUPABASE_DB_URL') or key in ('DOIT_PROJECT', 'DOIT_LEDGER_FILE'):
                    del env[key]
            stack.enter_context(patch.dict(os.environ, env, clear=True))
            stack.enter_context(patch.multiple(fold, ROOT=root, EVENTS=root / 'events', PROJECT=None))
            stack.enter_context(patch.multiple(dispatch, ROOT=root, EVENTS=root / 'events',
                                               CONTENT=root / 'content'))
            stack.enter_context(patch.object(dispatch, 'ensure_room', return_value=True))
            stack.enter_context(patch.object(models, 'resolve', return_value=dict(
                backend='codex', model='fixture', source='fixture')))
            paid = [stack.enter_context(patch.object(dispatch, name, side_effect=AssertionError('metered entry')))
                    for name in ('run_codex', 'run_seat')]
            # Record the refusal ten seconds before the real checker publishes,
            # without sleeping or admitting equal-time readiness.
            stamp = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
            stack.enter_context(patch.object(dispatch, 'now', return_value=stamp))
            checker_calls = []
            defer_checker = [True]
            real_popen = subprocess.Popen
            def popen(argv, *args, **kwargs):
                if defer_checker[0] and len(argv) > 1 and argv[1] == 'run-checks':
                    checker_calls.append(argv)
                    return None  # Run synchronously after the refusal is durable.
                return real_popen(argv, *args, **kwargs)
            stack.enter_context(patch.object(grader_packet.subprocess, 'Popen', side_effect=popen))
            def runner(argv):
                self.assertIsInstance(argv, list)
                if argv[:2] == ['doit', 'packet']:
                    return dict(code=0, stdout=str(packet), stderr='')
                self.assertEqual(argv[:5], ['doit', 'dispatch', '--detach', 'grader', SID])
                args = argparse.Namespace(role='grader', subject=SID, packet=str(packet), path=None,
                    cwd=str(repo), charter=None, project='demo', mcp_config=None,
                    timeout=None, max_usd=None, seat=False)
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    try:
                        dispatch.main(args)
                    except SystemExit:
                        pass
                return dict(code=1, stdout='', stderr='bundle-pending')
            events = fold.read_events(use_cache=False)
            lines = tick.run_steps(events, fold.fold(events)[0], datetime.now(timezone.utc), dispatcher=runner)
            self.assertTrue(any(line.startswith('failed ') for line in lines), lines)
            self.assertEqual(len(checker_calls), 1)
            self.assertEqual(checker_calls[0][1:], ['run-checks', SID, '--sha', ready])
            fresh = fold.read_events(use_cache=False)
            refusal = [e for e in fresh if e['type'] == 'spawn-failed']
            self.assertEqual([e['reason'] for e in refusal], ['bundle-pending'])
            step = ns.step_for(fresh, fold.fold(fresh)[0], SID, datetime.now(timezone.utc), lr.load())
            self.assertEqual((step.state, step.reason, step.attempt),
                             ('hold', 'not-ready: bundle-pending', 1))
            # Execute the actual local checker and its production append path.
            defer_checker[0] = False
            checked = subprocess.run(checker_calls[0], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            reader = subprocess.run(['/usr/bin/python3', '-c',
                'import fold,json; print(json.dumps(fold.read_events(use_cache=False)))'],
                cwd=ROOT / 'src', env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(reader.returncode, 0, reader.stderr)
            fresh = json.loads(reader.stdout)
            published = [e for e in fresh if e['type'] == 'evidence-bundle']
            self.assertEqual([e['sha'] for e in published], [ready])
            due = tick.run_steps(fresh, fold.fold(fresh)[0], datetime.now(timezone.utc),
                                 dispatcher=runner, dry_run=True)
            self.assertIn(f'would act {SID} grade', due)
            self.assertEqual([entry.call_count for entry in paid], [0, 0])

    def test_retry1672_AC4_releases_and_protected_holds(self):
        for blocked in ('build-blocked', 'build-done', 'window-dropped'):
            for release in ('unblocked', 'spec-amended', 'spec-written'):
                rows = [event('spec-written', -200), event(blocked, -100, reason='owner', status='BLOCKED'),
                        event(release, -90, actor='operator')]
                self.assertEqual(self.one(rows).action, 'rebuild')
                older = self.one([rows[0], event(release, -110), rows[1]])
                if blocked == 'window-dropped':
                    self.assertEqual(older.trigger, rows[1]['_src'])
                else:
                    self.assertNotEqual(older.action, 'rebuild')
                completed = event('build-done', -80, actor='builder', status='DONE', ready_sha='new')
                self.assertNotEqual(self.one(rows + [completed]).action, 'rebuild')
                for hold in (event('question', -50),
                             event('escalation-blocking', -50),
                             event('dependency-hold', -50, waits_for='L-spec-9999')):
                    self.assertEqual(self.one(rows + [hold]).state, 'hold')
                held = event('escalation-blocking', -50)
                for release_hold in ('decision', 'unblocked'):
                    released = rows + [held, event(release_hold, -40, actor='operator')]
                    self.assertEqual(self.one(released).action, 'rebuild')
                for state in ('dropped', 'killed', 'shipped'):
                    steps = ns.next_steps(rows, {SID: dict(state=state)}, NOW, lr.load())
                    self.assertFalse(any(s.state == 'action' for s in steps))
        # A capability release belongs to grading, not builder recovery.
        rows = self.chain('grade') + [event('capability-hold', -100, subject='capability:browser:' + SID),
                                      event('unblocked', -90, subject='capability:browser:' + SID, actor='operator')]
        self.assertEqual(self.one(rows).action, 'regrade')

if __name__ == '__main__':
    unittest.main()
