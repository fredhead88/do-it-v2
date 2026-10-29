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
        'trigger', 'reason', 'clears', 'since'}
ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / 'src/fixtures/nextstep'


def event(kind, minutes=-30, subject=SID, **extra):
    return dict(type=kind, ts=(NOW + timedelta(minutes=minutes)).isoformat(),
                subject=subject, _src=f'L-operator-fixture.jsonl:{minutes + 20000}', actor='operator') | extra


def specs_for(events):
    return {sid: dict(state='written') for sid in {e['subject'] for e in events}
            if sid.startswith('L-spec-')}


def replay(name):
    directory = FIXTURES / name
    events = [json.loads(l) for l in (directory / 'events.jsonl').read_text().splitlines()]
    lines = (directory / 'source.txt').read_text().splitlines()
    now = datetime.fromisoformat(lines[0].split('cut: ')[1])
    # The caller supplies fold state; replay time never comes from fold.NOW.
    events += [dict(type='spawn-done', subject=e['subject'], ts=e['ts'],
                    actor='spec-auditor', bad_cut=False, _src='L-spec-auditor-fixture.jsonl:1')
               for e in list(events) if e['type'] == 'spec-written']
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

    def test_vanished_first_build_keeps_original_trigger_and_backoff(self):
        written = event('spec-written', -90)
        start = event('build-started', -10, spawn='L-builder-9861', role='builder')
        failed = event('spawn-failed', -5, spawn=start['spawn'], role='builder',
                       reason='vanished', actor='tick') | {'_src': 'L-tick-local.jsonl:1'}
        evs = [written, start, failed]
        self.assertEqual(ns._failure(evs, self.rules)[1], 'retry')
        step = self.one(evs, {SID: dict(state='building')})
        self.assertEqual((step.action, step.since), ('build', written['ts']))
        waiting = self.one(evs, {SID: dict(state='building')}, now=NOW-timedelta(seconds=1))
        self.assertIsNone(waiting.action)
        self.assertIn('next_retry_at', waiting.reason)

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
                               'condition met', None, since)
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
                step = self.one(events + [event(kind, -30, findings=text)])
                self.assertEqual((step.state, step.owner), ('hold', 'executor'))
                self.assertTrue(step.reason.startswith(prefix))
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


class AuditStepTests(unittest.TestCase):
    setUp = NextStepTests.setUp
    steps = NextStepTests.steps
    one = NextStepTests.one
    # Use the inherited snapshot helpers without repeating the historical cases.
    def planner(self, minutes=-31, **kw):
        return event('spec-written', minutes, actor='spec-writer', **kw)

    def signed(self, ac, body):
        payload = json.dumps(body, sort_keys=True, default=str)
        print(json.dumps(dict(url='cli://audit-nextstep/' + ac, status=0,
            body_sha256=hashlib.sha256(payload.encode()).hexdigest(), body_excerpt=payload)))

    def test_ac2_audit_grace_and_precedence(self):
        written = self.planner(-30)
        evs = [written]
        for offset, expected in ((-1, None), (0, 'audit')):
            now = NOW + timedelta(seconds=offset)
            step = self.one(evs, now=now)
            self.assertEqual(step.action, expected)
            if expected:
                self.assertEqual((step.owner, step.stage), ('tick', 'planned'))
                self.assertEqual(ns.audit_due(evs, now)['since'], written['ts'])
            else:
                self.assertTrue(step.reason.startswith('audit-wait:'))
            self.signed('AC2-boundary', asdict(step))
        self.assertTrue(self.one(evs + [self.planner(-10)]).reason.startswith('audit-wait:'))
        clean = event('spawn-done', -1, actor='spec-auditor', bad_cut=False)
        self.assertEqual(self.one(evs + [clean]).action, 'build')
        for exempt in ([event('spec-written')], evs + [event('spec-carried')]):
            self.assertEqual(self.one(exempt).action, 'build')
        for blocked in (event('question'), event('spawn-started', -1, spawn='L-spec-auditor-9001', role='spec-auditor')):
            self.assertIsNone(self.one(evs + [blocked]).action)
        dependency = event('spec-written', -40, subject='L-spec-9002')
        with patch.object(ns.autodispatch, 'held_by_dependency', return_value='L-spec-9002'):
            self.assertEqual(self.one(evs, specs={SID: dict(state='written')}).action, 'audit')
        bad = event('spawn-done', -2, actor='spec-auditor', bad_cut=True)
        step = self.one(evs + [bad])
        self.assertEqual(step.owner, 'executor')
        self.assertTrue(step.reason.startswith('audit-bad-cut:'))
        self.assertEqual(self.one(evs + [bad, self.planner(-1)]).action, 'build')
        self.assertIsNone(ns._build(dict(evs=evs)))
        self.signed('AC2-bad-cut', asdict(step))

    def test_ac3_real_attempts_and_failure_hold_order(self):
        import attempts
        evs = [self.planner(-90)]
        start = event('spawn-started', -10, spawn='L-spec-auditor-9001', role='spec-auditor')
        self.assertEqual(ns.audit_state(evs + [start], NOW)['phase'], 'live')
        self.assertIsNone(ns.audit_due(evs + [start], NOW))
        refusal = event('spawn-failed', -20, role='builder', spawn='L-builder-9001', reason='unaudited')
        self.assertEqual(ns.classify_failure('unaudited', 1), 'unknown')
        self.assertEqual(self.one(evs + [refusal]).action, 'audit')
        fail = event('spawn-failed', -5, role='spec-auditor', spawn='L-spec-auditor-9001', reason='audit fixture failed')
        retry = evs + [refusal, start, fail]
        for seconds, phase in ((-1, 'retry-wait'), (0, 'due'), (1, 'due')):
            now = NOW + timedelta(seconds=seconds)
            state = ns.audit_state(retry, now)
            self.assertEqual(state['phase'], phase)
            step = self.one(retry, now=now)
            if phase == 'due':
                self.assertEqual(ns.audit_due(retry, now)['attempt'], 2)
                self.assertEqual(step.action, 'audit')
            else:
                self.assertIsNone(ns.audit_due(retry, now))
                self.assertTrue(step.reason.startswith('audit-wait:'))
            self.signed('AC3-backoff', dict(state=state, step=asdict(step)))
        stale = event('spawn-stale', -5, role='spec-auditor', spawn='L-spec-auditor-9001')
        self.assertEqual(ns.audit_due(evs + [start, stale], NOW)['attempt'], 2)
        failures = [event('spawn-failed', -30 + n, role='spec-auditor',
                         spawn=f'L-spec-auditor-{9100+n}', reason=f'audit failure {n}')
                    for n in range(attempts.MAX_ATTEMPTS)]
        exhausted = evs + failures
        self.assertEqual(ns.audit_state(exhausted, NOW)['phase'], 'exhausted')
        self.assertIsNone(ns.audit_due(exhausted, NOW))
        step = self.one(exhausted)
        self.assertTrue(step.reason.startswith('audit-exhausted:'))
        for n in range(attempts.MAX_ATTEMPTS):
            self.assertIn(f'audit failure {n}', step.reason)
        self.assertEqual(ns.audit_state(exhausted + [self.planner(-10)], NOW + timedelta(minutes=20))['phase'], 'due')
        with patch.object(attempts, 'history', side_effect=OSError('ledger unavailable')):
            self.assertEqual(ns.audit_state(evs, NOW)['phase'], 'undetermined')
            self.assertIsNone(ns.audit_due(evs, NOW))
        def unreadable():
            raise OSError('slice unavailable')
            yield
        self.assertEqual(ns.audit_state(unreadable(), NOW)['phase'], 'undetermined')
        self.signed('AC3-exhausted', asdict(step))


class VanishedRetryTests(unittest.TestCase):
    setUp = NextStepTests.setUp
    steps = NextStepTests.steps
    one = NextStepTests.one

    def fixture(self, action):
        evs = [event('spec-written', -240, actor='spec-writer' if action == 'audit' else 'operator')]
        if action == 'rebuild':
            evs += [event('build-blocked', -230), event('decision', -220, rework='yes')]
        elif action in ('grade', 'regrade', 'review'):
            evs += [event('build-done', -230, actor='builder', status='DONE')]
            if action == 'regrade':
                evs += [event('decision', -220, regrade='yes')]
            elif action == 'review':
                evs += [event('verdict', -220, actor='grader', confirmed=True)]
        role = dict(build='builder', rebuild='builder', grade='grader',
                    regrade='grader', review='reviewer', audit='spec-auditor')[action]
        return evs, role, evs[-1]

    def failure(self, evs, role, number, minute):
        spawn = f'L-{role}-{9860+number}'
        start = event('spawn-started', minute-1, spawn=spawn, role=role)
        evs.append(start)
        if role == 'builder':
            evs.append(dict(start, type='build-started'))
        evs += [event('spawn-stale', minute-.5, spawn=spawn, role=role),
                event('spawn-failed', minute, spawn=spawn, role=role, reason='vanished', actor='tick')
                | {'_src': f'L-tick-local.jsonl:{number}'}]

    def report(self, label, step, evs, trigger, role, now):
        import attempts
        h = asdict(attempts.history(ns._history_events(evs), trigger, role, now=now))
        h['next_retry_at'] = str(h['next_retry_at'])
        body = json.dumps(dict(case=label, step=asdict(step), history=h), sort_keys=True)
        print(json.dumps(dict(url='cli://vanished-nextstep/' + label, status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def test_six_actions_all_retry_boundaries_and_new_trigger(self):
        import attempts
        self.assertEqual((attempts.MAX_ATTEMPTS, attempts.BACKOFF_MIN), (3, (5, 15, 45)))
        for action in ('build', 'rebuild', 'grade', 'regrade', 'review', 'audit'):
            with self.subTest(action=action):
                evs, role, trigger = self.fixture(action)
                zero = self.one(evs)
                self.assertEqual(zero.action, action)
                self.assertEqual(zero.owner, 'tick')
                self.report(action+'-zero', zero, evs, trigger, role, NOW)
                for n, minute in ((1, -120), (2, -60), (3, -10)):
                    self.failure(evs, role, n, minute)
                    deadline = NOW + timedelta(minutes=minute + (5, 15, 45)[n-1])
                    for offset in (-1, 0):
                        now = deadline + timedelta(seconds=offset)
                        step = self.one(evs, now=now)
                        if n == 3:
                            self.assertIsNone(step.action)
                            self.assertEqual(step.owner, 'executor')
                            self.assertIn('exhaust', step.reason)
                            self.assertIn('operator decision', step.clears)
                        elif offset == -1:
                            self.assertIsNone(step.action)
                            self.assertIn('next_retry_at', step.reason)
                        else:
                            self.assertEqual((step.action, step.owner, step.since), (action, 'tick', trigger['ts']))
                            self.assertEqual(step.trigger, trigger['_src'])
                        self.report(action+f'-{n}-{offset}', step, evs, trigger, role, now)
                new = dict(trigger, ts=event('x', 1)['ts'], _src='L-operator-new.jsonl:1')
                renewed = self.one(evs+[new], now=NOW+timedelta(minutes=32))
                self.assertEqual(renewed.action, action)
                self.assertEqual(renewed.since, new['ts'])
                # A late reconciliation belonging to an older attempt cannot charge the new trigger.
                late = dict(evs[-1], ts=event('x', 2)['ts'])
                renewed = self.one(evs+[new, late], now=NOW+timedelta(minutes=32))
                self.assertEqual(renewed.action, action)
                self.report(action+'-new-trigger-late-end', renewed, evs+[new, late], new, role, NOW+timedelta(minutes=32))

    def test_stronger_lifecycle_predicates_and_refusals(self):
        import attempts
        for action in ('build', 'rebuild', 'grade', 'regrade', 'review', 'audit'):
            with self.subTest(action=action):
                evs, role, trigger = self.fixture(action)
                self.failure(evs, role, 1, -20)
                live = event('spawn-started', -1, spawn=f'L-{role}-9899', role=role)
                self.assertIn('in flight', self.one(evs+[live]).reason)
                output = dict(builder='build-done', grader='verdict', reviewer='review',
                              **{'spec-auditor': 'spawn-done'})[role]
                done = event(output, -10, actor=role, spawn=evs[-1]['spawn'], role=role,
                             status='DONE', confirmed=True, n_blocking=0, bad_cut=False)
                self.assertNotEqual(self.one(evs+[done]).action, action)
                late = dict(evs[-1], ts=event('x', -1)['ts'])
                self.assertNotEqual(self.one(evs+[done, late]).action, action)
                for terminal in ('queue-add', 'shipped', 'spec-killed'):
                    result = self.steps(evs+[event(terminal, -1)])
                    self.assertFalse(any(s.item == SID and s.action == action for s in result))
                question = event('escalation-blocking', -1)
                self.assertIn('judgment', self.one(evs+[question]).reason)
                if action != 'audit':
                    hold = event('dependency-hold', -1, waits_for='L-spec-9898')
                    with patch.object(ns.autodispatch, 'held_by_dependency', return_value='L-spec-9898'):
                        self.assertIsNone(self.one(evs+[hold]).action)
                clean, _, _ = self.fixture(action)
                for reason in ('bundle-pending', 'duplicate-dispatch', 'dispatch-in-progress'):
                    refusal = event('spawn-failed', -2, spawn=f'L-{role}-9898', role=role, reason=reason)
                    history = attempts.history(ns._history_events(clean+[refusal]), trigger, role, now=NOW)
                    self.assertEqual((history.consumed, history.n_failed), (False, 0))
                    refused = self.one(clean+[refusal])
                    if reason == 'bundle-pending':
                        self.assertIsNone(refused.action)
                    else:
                        self.assertEqual(refused.action, action)
                    self.report(action+'-refusal-'+reason, refused, clean+[refusal], trigger, role, NOW)

    def test_structured_authority_only_and_text_classifier_unchanged(self):
        import grading_env
        base = event('spawn-failed', reason='vanished', actor='tick') | {'_src': 'L-tick-local.jsonl:1'}
        self.assertEqual(ns._failure([base], self.rules)[1], 'retry')
        for change in (dict(reason='vanished later'), dict(reason='prose vanished'),
                       dict(type='autodispatch-failed'), dict(_src='L-builder-9861.jsonl:1'),
                       dict(_src=None)):
            self.assertNotEqual(ns._failure([dict(base, **change)], self.rules)[1], 'retry')
        self.assertEqual(ns.classify_failure('vanished', 1), 'unknown')
        with patch.object(ns, 'classify_failure', return_value='retry'):
            self.assertEqual(grading_env.failure_kind('vanished', 1), 'unknown')
        self.assertEqual(ns._attempt_role(dict(spawn='L-spec-auditor-9861')), 'spec-auditor')
        self.assertEqual(ns._attempt_role(dict(spawn='L-spec-writer-9861')), 'spec-writer')

    def test_same_timestamp_start_and_reconciliation_has_no_live_hold(self):
        for action in ('build', 'rebuild', 'grade', 'regrade', 'review', 'audit'):
            evs, role, _ = self.fixture(action)
            self.failure(evs, role, 1, -5)
            # Second-resolution appends from different sources can sort an end
            # before a start. Terminal identity still wins over that ordering.
            for e in evs[-4:] if role == 'builder' else evs[-3:]:
                e['ts'] = event('x', -5)['ts']
            steps = self.steps(evs)
            self.assertFalse(any(s.item_kind == 'attempt' for s in steps))
            self.assertEqual(next(s for s in steps if s.item == SID).action, action)


class RulingMixin:
    setUp = NextStepTests.setUp
    steps = NextStepTests.steps
    one = NextStepTests.one

    def ruling(self, action='close', **kw):
        return event('decision', action=action, **kw)

    def due(self, events, **kw):
        return [s for s in self.steps(events, **kw)
                if s.item_kind == 'spec' and s.reason.startswith('ruling-unexecuted:')]

    def signed(self, ac, body):
        payload = json.dumps(body, sort_keys=True, default=str)
        print(json.dumps(dict(url='cli://ruling-nextstep/' + ac, status=0,
            body_sha256=hashlib.sha256(payload.encode()).hexdigest(), body_excerpt=payload)))


class RulingTimingTests(RulingMixin, unittest.TestCase):
    def test_boundary_matrix(self):
        results = []
        for action in ('close', 'rework', 'hold', 'recut'):
            for legacy in ({}, {'rework': 'yes'}, {'ref': 'L-operator-fixture.jsonl:19950'}) if action == 'rework' else ({},):
                evs = [event('spec-written', -60), event('build-blocked', -50),
                       self.ruling(action, **legacy, **({'waits_for': 'L-spec-1642'} if action == 'hold' else {}))]
                ruling = evs[-1]
                for seconds in (-1, 0, 1):
                    step = self.one(evs, now=NOW + timedelta(seconds=seconds))
                    self.assertEqual(len([s for s in self.steps(evs) if s.item == SID]), 1)
                    if legacy:
                        self.assertEqual((step.action, step.owner, step.trigger), ('rebuild', 'tick', ruling['_src']))
                    elif seconds < 0:
                        self.assertFalse(step.reason.startswith('ruling-unexecuted:'))
                    else:
                        self.assertEqual((step.state, step.action, step.owner, step.trigger, step.since),
                                         ('hold', None, 'executor', ruling['_src'], ruling['ts']))
                        self.assertEqual(step.reason, f'ruling-unexecuted: {action} ({ruling["_src"]})')
                        self.assertEqual(step.clears, {'close': 'spec-killed', 'rework': 'rework dispatch',
                            'hold': 'dependency-hold waits_for=L-spec-1642',
                            'recut': 'recut resolution recorded as spec-killed'}[action])
                    results.append(dict(action=action, legacy=legacy, age_seconds=1800+seconds, step=asdict(step)))
        self.signed('AC1-timing', results)

    def test_precedence_and_scoped_errors(self):
        ruling = self.ruling()
        written = event('spec-written', -60)
        variants = [[], [event('build-started', -10, spawn='L-builder-9001')],
                    [event('merge-gate-rework', -10, findings='ENOSPC')],
                    [event('dependency-hold', -40, waits_for='L-spec-1642')],
                    [event('question', -10)], [event('verdict', -10, confirmed=True)],
                    [event('review', -10, n_blocking=0)]]
        for extra in variants:
            for actor in ('operator', 'spec-writer'):
                evs = [dict(written, actor=actor), ruling, *extra]
                self.assertEqual(len(self.due(evs)), 1)
        for changed in [dict(ruling, ts='invalid'), dict(ruling, action='hold')]:
            self.assertTrue(self.one([written, changed]).reason.startswith('undetermined:'))
        malformed = deepcopy(self.rules)
        malformed.pop('build')
        self.assertTrue(self.one([written, ruling], rules=malformed).reason.startswith('undetermined:'))
        conflict = [written, ruling, event('spec-killed', -20), event('shipped', -10)]
        self.assertTrue(self.one(conflict).reason.startswith('undetermined:'))
        self.signed('AC1-precedence', dict(variants=len(variants)*2, scoped_errors=4))


class RulingExecutionTests(RulingMixin, unittest.TestCase):
    def test_execution_matrix(self):
        results = []
        for action, kind in [('close', 'spec-killed'), ('recut', 'spec-killed'),
                             ('hold', 'dependency-hold'), ('rework', 'build-started'),
                             ('rework', 'spawn-started'), ('rework', 'autodispatched')]:
            ruling = self.ruling(action, **({'waits_for': 'L-spec-1642'} if action == 'hold' else {}))
            execution = event(kind, -20, spawn='L-builder-9001', role='builder', waits_for='L-spec-1642')
            if kind == 'autodispatched':
                execution.update(actor='tick', _src='L-tick-fixture.jsonl:1')
            evs = [event('spec-written', -60), ruling]
            for change, clears in [({}, True), ({'ref': ruling['_src']}, True),
                                   ({'ref': 'elsewhere:1'}, False),
                                   ({'subject': 'L-spec-9999'}, False),
                                   ({'ts': event('x', -40)['ts']}, False)]:
                # A terminal subject stays terminal even with an unrelated ref.
                if kind == 'spec-killed' and (change.get('ref') == 'elsewhere:1' or 'ts' in change):
                    continue
                sample = evs + [dict(execution, **change)]
                if action == 'hold' and kind == 'dependency-hold' and sample[-1]['subject'] == SID:
                    # Dependency explanation takes precedence even when this
                    # event does not execute the older ruling's own obligation.
                    step = self.one(sample)
                    self.assertEqual(step.state, 'hold')
                    self.assertIn('L-spec-1642', step.reason)
                    context = dict(sid=SID, evs=ns._snapshot(sample, {})[1][SID],
                                   spec=dict(state='written'), now=NOW)
                    self.assertEqual(ns._ruling_step(context, {}, {}) is not None, not clears)
                    continue
                self.assertEqual(bool(self.due(sample)), not clears, (action, change))
                results.append(dict(action=action, change=change, cleared=clears))
            if action == 'hold':
                sample = evs + [dict(execution, waits_for='other')]
                self.assertIn('waits for other', self.one(sample).reason)
                context = dict(sid=SID, evs=ns._snapshot(sample, {})[1][SID],
                               spec=dict(state='written'), now=NOW)
                self.assertIsNotNone(ns._ruling_step(context, {}, {}))
            if action == 'rework':
                for role in ('grader', 'reviewer'):
                    self.assertTrue(self.due(evs + [dict(execution, role=role)]))
                self.assertTrue(self.due(evs + [dict(execution, spawn=None)]))
        self.signed('AC2-execution', results)

    def test_acceptance_correlated_across_complete_snapshot(self):
        results = []
        ruling = self.ruling('rework')
        base = [event('spec-written', -60), ruling]
        start = event('build-started', -20, spawn='L-builder-9001', role='builder')
        failure = event('spawn-failed', -10, spawn='L-builder-9001')
        queued = event('seat-queued', -19, subject='L-builder-9001', spawn='L-builder-9001', actor='relay')
        claim = event('seat-claimed', -18, subject='L-builder-9001', spawn='L-builder-9001',
                      actor='relay', _src='L-relay-fixture.jsonl:1')
        done = event('spawn-done', -17, spawn='L-builder-9001', role='builder',
                     actor='builder', _src='L-builder-fixture.jsonl:1')
        for tail, cleared in [([], True), ([failure], False), ([queued, failure], False),
                              ([claim, failure], True), ([done, failure], True),
                              ([{k: v for k, v in claim.items() if k != 'spawn'}, failure], True),
                              ([dict(claim, spawn='L-builder-9999'), failure], False),
                              ([dict(done, spawn='L-builder-9999'), failure], False),
                              ([dict(claim, _src='L-builder-forged.jsonl:1'), failure], False)]:
            sample = base + [start] + tail
            self.assertEqual(bool(self.due(sample)), not cleared)
            shuffled = list(sample)
            random.Random(72).shuffle(shuffled)
            self.assertEqual(self.steps(sample), self.steps(shuffled))
            results.append(dict(tail=tail, cleared=cleared))
        for kind in ('spawn-failed', 'autodispatch-failed', 'decision'):
            self.assertTrue(self.due(base + [event(kind, -20)]))
        self.signed('AC2-acceptance', results)

    def test_same_act_rebuild_and_refused_launch_at_threshold(self):
        results = []
        for legacy in ({'rework': 'yes'}, {'ref': 'L-operator-fixture.jsonl:19950'}):
            base = [event('spec-written', -60), event('build-blocked', -50),
                    self.ruling('rework', **legacy)]
            refused = [event('build-started', -20, spawn='L-builder-9001'),
                       event('seat-queued', -19, spawn='L-builder-9001'),
                       event('spawn-failed', -18, spawn='L-builder-9001', reason='bundle-pending')]
            for seconds in (-1, 0, 1):
                now = NOW + timedelta(seconds=seconds)
                eligible = self.one(base, now=now)
                self.assertEqual((eligible.action, eligible.owner, eligible.trigger),
                                 ('rebuild', 'tick', base[-1]['_src']))
                rejected = self.one(base + refused, now=now)
                self.assertEqual(rejected.reason.startswith('ruling-unexecuted:'), seconds >= 0)
                results.append(dict(legacy=legacy, age_seconds=1800+seconds,
                                    eligible=asdict(eligible), refused=asdict(rejected)))
        self.signed('AC2-same-act-refusal', results)

    def test_newest_actor_legacy_terminal_and_order(self):
        base = [event('spec-written', -60), self.ruling()]
        fresh = self.ruling('recut', minutes=-5)
        self.assertFalse(self.due(base + [fresh]))
        self.assertIn('recut', self.due(base + [fresh], now=NOW + timedelta(minutes=25))[0].reason)
        self.assertTrue(self.due(base + [event('decision', -5, why='rework it')]))
        for state in dispatch.SETTLED_STATES:
            self.assertFalse(self.due(base, specs={SID: dict(state=state)}))
        self.assertFalse(self.due([base[0], event('spec-killed', -50), base[1]]))
        for changed in [dict(base[1], action='unknown'), dict(base[1], action=None),
                        dict(base[1], _src='L-builder-forged.jsonl:1', actor='thinker')]:
            self.assertFalse(self.due([base[0], changed]))
        # Same source append order decides equal timestamps, independent of input order.
        ruling = self.ruling('hold', waits_for='L-spec-1642', _src='L-operator-fixture.jsonl:2')
        for line, cleared in [(1, False), (3, True)]:
            done = event('dependency-hold', waits_for='L-spec-1642', _src=f'L-operator-fixture.jsonl:{line}')
            sample = [base[0], done, ruling]
            step = self.one(sample)
            self.assertEqual(step.state, 'hold')
            self.assertIn('L-spec-1642', step.reason)
            context = dict(sid=SID, evs=ns._snapshot(sample, {})[1][SID],
                           spec=dict(state='written'), now=NOW)
            self.assertEqual(ns._ruling_step(context, {}, {}) is not None, not cleared)
            self.assertEqual(self.steps(sample), self.steps(list(reversed(sample))))
        self.signed('AC2-order', dict(newest_clock=True, provenance=True, canonical_order=True))


class RulingReplayTests(RulingMixin, unittest.TestCase):
    def test_six_redacted_replays_and_adapters(self):
        import tick
        replayed = []
        for sid, line, action in [('1555', 9044, 'close'), ('1557', 9093, 'close'),
                                  ('1660', 9140, 'close'), ('1745', 9640, 'rework'),
                                  ('1743', 9684, 'rework'), ('1641', 9848, 'hold')]:
            subject = 'L-spec-' + sid
            ruling = self.ruling(action, subject=subject, actor='thinker',
                _src=f'L-thinker-0004.jsonl:{line}', **({'waits_for': 'L-spec-1642'} if action == 'hold' else {}))
            evs = [event('spec-written', -60, subject=subject), ruling]
            specs = specs_for(evs)
            original = deepcopy((evs, specs, self.rules))
            step = self.steps(evs)[0]
            self.assertEqual(step.owner, 'executor')
            self.assertEqual(ns.step_for(evs, specs, subject, NOW, self.rules), step)
            self.assertEqual(ns.flow_counts(evs, specs, NOW, self.rules)['planned'], 1)
            self.assertEqual(fold.step_row(step, self.rules, NOW)['spec'], subject)
            self.assertEqual(tick._steps(evs, specs, NOW), [step])
            self.assertFalse([s for s in tick._steps(evs, specs, NOW) if s.state == 'action' and s.owner == 'tick'])
            execution = event({'close': 'spec-killed', 'hold': 'dependency-hold', 'rework': 'build-started'}[action],
                              -20, subject=subject, waits_for='L-spec-1642', spawn='L-builder-9001', role='builder')
            after = self.steps(evs + [execution], specs={subject: dict(
                state='killed' if action == 'close' else 'written')})
            self.assertFalse(any(s.reason.startswith('ruling-unexecuted:') for s in after))
            if action == 'close':
                self.assertFalse(any(s.item == subject for s in after))
            self.assertEqual((evs, specs, self.rules), original)
            replayed.append(dict(subject=subject, original_ref=ruling['_src'], action=action,
                                 before=asdict(step), after=[asdict(s) for s in after]))
        self.signed('AC3-replays', replayed)

    def test_cli_readonly(self):
        with tempfile.TemporaryDirectory(prefix='ruling-cli-') as directory:
            root = Path(directory)
            (root/'events').mkdir()
            events = [event('spec-written', -60), self.ruling()]
            path = root/'events/L-operator-fixture.jsonl'
            path.write_text(''.join(json.dumps(e)+'\n' for e in events))
            before = {p.name: p.read_bytes() for p in (root/'events').iterdir()}
            env = dict(os.environ, DOIT_ROOT=str(root), PYTHONDONTWRITEBYTECODE='1')
            for key in ('DOIT_LEDGER_FILE', 'DOIT_LANE_RULES', 'DOIT_PROJECT'):
                env.pop(key, None)
            outputs = []
            for args in [('--json',), (SID,), ()]:
                result = subprocess.run([str(ROOT/'doit'), 'next', *args], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                if '--json' in args:
                    obj = json.loads(result.stdout)
                    self.assertEqual(set(obj), {'counts', 'steps'})
                    self.assertEqual(set(obj['steps'][0]), KEYS)
                    self.assertEqual(obj['steps'][0]['owner'], 'executor')
                self.assertIn('ruling-unexecuted: close', result.stdout)
                outputs.append(result.stdout)
            after = {p.name: p.read_bytes() for p in (root/'events').iterdir()}
            self.assertEqual(before, after)
            self.signed('AC3-cli', dict(outputs=outputs, unchanged_bytes=True,
                event_sha256=hashlib.sha256(path.read_bytes()).hexdigest()))


class RulingConventionTests(RulingMixin, unittest.TestCase):
    def test_sd10_contract(self):
        text = (ROOT/'agents/thinker.md').read_text()
        base = (ROOT/'agents/relay.md').read_text()
        marker = '## Lessons'
        self.assertEqual(text[text.index(marker):], base[base.index(marker):])
        headings = [line for line in text.splitlines() if line.startswith('## ')]
        self.assertTrue(headings[-1].startswith(marker))
        convention = text[text.index('## SD10'):text.index(marker)]
        for phrase in ['action=close|rework|hold|recut', 'waits_for=', '30 minutes', 'executor',
                       'spec-killed', 'dependency-hold', 'rework dispatch', 'same-act',
                       'action-less', 'filename', 'original', 'still open', 'settled']:
            self.assertIn(phrase, convention)
        for number in ('1555', '1557', '1660', '1745', '1743', '1641', '1560', '1730', '1672', '1768'):
            self.assertIn('L-spec-' + number, convention)
        for phrase in ('doit alloc charter', 'doit think --land', 'doit think --discard', 'review_path',
                       'Covers:', 'You spawn nothing', 'NEVER deletes', 'blocked_me'):
            self.assertIn(phrase, text)
        self.signed('AC4-convention', dict(shared_tail_unchanged=True, legacy_subjects=10, spawns_nothing=True))


class NoSilentUndeterminedTests(unittest.TestCase):
    setUp = NextStepTests.setUp
    steps = NextStepTests.steps
    one = NextStepTests.one
    signed = AuditStepTests.signed
    diagnostic = 'packet: incomplete criterion AC6 from /home/albert/.do-it/content/L-spec-1706.md\n'

    def built(self):
        return [event('spec-written', -90), event('build-done', -80, actor='builder', status='DONE')]

    def context(self, events):
        return dict(evs=events, spec=dict(state='graded'), dependency=False,
                    capabilities={}, sid=SID)

    def test_packet_classification(self):
        positives = [self.diagnostic, self.diagnostic.rstrip('\n'),
                     'packet: incomplete criterion AC6 from spec.md\n',
                     'packet: incomplete criterion AC6 from spec.md',
                     self.diagnostic.replace('AC6', 'AC42')]
        for text in positives:
            with self.subTest(text=text):
                self.assertEqual(ns.classify_failure(text, 1), 'spec-defect')
                self.assertEqual(ns.classify_failure(text, None), 'unknown')
                for other in ('ENOSPC', 'FAILED', 'ENOSPC\nAssertionError'):
                    self.assertEqual(ns.classify_failure(text + '\n' + other, 1), 'unknown')
        for text in ('incomplete criterion', 'packet: unrelated error', '',
                     'mentioned packet: incomplete criterion AC6 from spec.md',
                     'packet: incomplete criterion AC6',
                     'packet: incomplete criterion AC6 from '):
            self.assertEqual(ns.classify_failure(text, 1), 'unknown')
        for text, code, expected in (('ENOSPC', 1, 'infra'), ('FAILED', 1, 'work'),
                                     ('ENOSPC\nFAILED', 1, 'unknown'), ('', 124, 'infra'),
                                     ('invalid_json_schema', 1, 'infra'),
                                     ('ledger cache: lock not acquired within 120s', 1, 'infra')):
            self.assertEqual(ns.classify_failure(text, code), expected)
        self.signed('L-spec-1853/AC1', dict(positive_classes=[ns.classify_failure(t, 1) for t in positives],
                                         conservative_mixed_evidence=True))

    def test_spec_defect_hold_lifecycle(self):
        for field in ('findings', 'why', 'error', 'reason'):
            failure = event('autodispatch-failed', -60, **{field: self.diagnostic})
            events = self.built() + [failure]
            with self.subTest(field=field):
                self.assertEqual(ns._failure(events, self.rules), (failure, 'spec-defect'))
                step = ns.step_for(events, specs_for(events), SID, NOW, self.rules)
                self.assertEqual(self.steps(events), [step])
                self.assertEqual((step.state, step.action, step.owner), ('hold', None, 'executor'))
                self.assertTrue(step.reason.startswith('spec-defect:'))
                self.assertIn('incomplete criterion AC6', step.reason)
                self.assertRegex(step.clears, 'spec.*(amend|rewrite)')
                self.assertIn('executor', step.clears)
                self.assertIn('reconsider', step.clears)
                self.assertEqual((step.trigger, step.since), (failure['_src'], failure['ts']))
                self.assertEqual(step, self.one(events, now=NOW + timedelta(days=1)))
                self.assertEqual(step, self.one(events + [event('evidence-bundle', -50)]))
                for kind in ('spec-written', 'decision'):
                    cleared = events + [event(kind, -40, actor='executor')]
                    self.assertEqual(ns._failure(cleared, self.rules), (None, None))
                    self.assertEqual(self.one(cleared).action, 'grade')
                # Diagnostic holds precede questions, busy attempts, and rework.
                for extra in (event('question', -55),
                              event('autodispatched', -55, role='builder'),
                              event('spawn-started', -70, spawn='L-builder-9001', role='builder')):
                    self.assertEqual(self.one(events + [extra]), step)
                for prefix in ns.NOT_READY_PREFIXES:
                    refused = event('spawn-failed', -50, reason=prefix + ': fixture')
                    self.assertEqual(self.one(events + [refused]), step)
        self.signed('L-spec-1853/AC2', asdict(step))

    def test_stale_gate_review_and_controls(self):
        original = self.built() + [event('verdict', -70, confirmed=True),
                                  event('review', -60, n_blocking=0),
                                  event('merge-gate-clean', -50)]
        for kind in ('verdict', 'cannot-assess'):
            judgment = event(kind, -40, **({'confirmed': True} if kind == 'verdict' else {}))
            events = original + [judgment]
            with self.subTest(kind=kind):
                self.assertIsNone(ns._enqueue(self.context(events)))
                step = ns.step_for(events, specs_for(events), SID, NOW, self.rules)
                self.assertEqual(self.steps(events), [step])
                self.assertEqual((step.state, step.action, step.owner), ('action', 'review', 'tick'))
                self.assertEqual((step.trigger, step.since), (judgment['_src'], judgment['ts']))
                no_gate = [e for e in events if e['type'] != 'merge-gate-clean']
                self.assertEqual(self.one(no_gate).action, 'review')
                # No intervening build: the old gate cannot authorize this review.
                reviewed = events + [event('autodispatched', -30, role='reviewer'),
                                     event('review', -20, n_blocking=0)]
                self.assertNotIn('confirmed', reviewed[-1])
                self.assertIsNone(ns._enqueue(self.context(reviewed)))
                self.assertEqual(ns._gate(self.context(reviewed))['since'], reviewed[-1]['ts'])
                gate_step = self.one(reviewed)
                self.assertEqual(gate_step.action, 'gate')
                self.assertEqual((gate_step.trigger, gate_step.since),
                                 (reviewed[-1]['_src'], reviewed[-1]['ts']))
                current = reviewed + [event('merge-gate-clean', -10)]
                self.assertEqual(ns._enqueue(self.context(current))['since'], reviewed[-1]['ts'])
                self.assertEqual(self.one(current).action, 'enqueue')
                for terminal in ('queue-add', 'window-dropped', 'merge-conflict', 'shipped'):
                    suppressed = current + [event(terminal, -5, project='fixture', branch='fixture')]
                    self.assertIsNone(ns._enqueue(self.context(suppressed)))
                    self.assertFalse(any(s.action == 'enqueue' for s in self.steps(suppressed)))
                self.signed('L-spec-1853/AC3-' + kind, dict(review=asdict(step), gate=asdict(gate_step),
                                                         enqueue=asdict(self.one(current))))
        negative = original + [event('verdict', -40, confirmed=False)]
        self.assertIsNone(ns._enqueue(self.context(negative)))
        self.assertNotEqual(self.one(negative).action, 'enqueue')
        for gate_time in (-40, -30):
            malformed = self.built() + [event('verdict', -40, confirmed=True),
                                        event('merge-gate-clean', gate_time)]
            with self.subTest(gate_time=gate_time):
                # Equal-time gates remain current; both inputs lack a clean review.
                with self.assertRaisesRegex(ValueError, 'clean review timestamp'):
                    ns._enqueue(self.context(malformed))
                self.assertTrue(self.one(malformed).reason.startswith('undetermined:'))
        # A gate that predates its clean review cannot authorize that review.
        late_review = original + [event('review', -20, n_blocking=0)]
        self.assertIsNone(ns._enqueue(self.context(late_review)))

    def test_compatibility_and_purity(self):
        import grading_env
        import tick
        rules = self.rules
        events = self.built() + [event('autodispatch-failed', -60, error=self.diagnostic + '\x1b\n')]
        review_events = self.built() + [event('verdict', -70, confirmed=True),
                                       event('review', -60, n_blocking=0),
                                       event('merge-gate-clean', -50),
                                       event('cannot-assess', -40)]
        specs = specs_for(events)
        originals = deepcopy((events, review_events, specs, rules))
        with ExitStack() as stack:
            for hook in ('builtins.open', 'io.open', 'pathlib.Path.open', 'pathlib.Path.write_text',
                         'subprocess.run', 'subprocess.Popen', 'dispatch.emit', 'fold.read_events',
                         'time.time'):
                stack.enter_context(patch(hook, side_effect=AssertionError(hook)))
            stack.enter_context(patch.object(tick, 'launch', side_effect=AssertionError('launch')))
            self.assertEqual(grading_env.failure_kind(self.diagnostic), 'unknown')
            held = ns.step_for(events, specs, SID, NOW, rules)
            review = ns.step_for(review_events, specs, SID, NOW, rules)
            self.assertEqual((held.owner, held.action), ('executor', None))
            self.assertEqual(review.action, 'review')
            self.assertEqual(set(asdict(held)), KEYS)
            for invalid in (False, '0', 0.0, None):
                malformed = review_events + [event('review', -20, n_blocking=invalid)]
                self.assertIsNone(ns._clean_review(self.context(malformed)))
            for kind in ('rejected-criterion', 'must-fix'):
                rejected = review_events + [event('review', -20, n_blocking=0),
                                             event(kind, -10, criterion='AC1')]
                self.assertIsNone(ns._clean_review(self.context(rejected)))
            self.assertNotIn('\x1b', ns._clean(held.reason))
            self.assertNotIn('\n', ns._clean(held.reason))
            self.assertEqual(json.loads(json.dumps(asdict(held))), asdict(held))
        # The real launch guard must return before any dispatcher or ledger access.
        with patch.object(dispatch, 'emit', side_effect=AssertionError('emit')), \
                patch.object(fold, 'read_events', side_effect=AssertionError('read')):
            self.assertIsNone(tick.launch(held, dispatcher=lambda argv: self.fail('dispatch')))
        self.assertEqual((events, review_events, specs, rules), originals)
        self.signed('L-spec-1853/AC4', dict(grading_failure_kind='unknown', pure=True,
                                         hold=asdict(held), review=asdict(review)))


class R7BaseRefreshTests(unittest.TestCase):
    setUp = NextStepTests.setUp
    steps = NextStepTests.steps
    one = NextStepTests.one

    def signed(self, name, body):
        payload = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url='cli://r7/' + name, status=0,
            body_sha256=hashlib.sha256(payload.encode()).hexdigest(), body_excerpt=body)))

    def test_failure_class_and_step(self):
        import grading_env
        for name in ('stale-base', 'branch-not-descendant', 'pinned-base-mismatch'):
            for diagnostic in (name, 'reason=' + name, 'problem:' + name + ' — refresh'):
                self.assertEqual(ns.classify_failure(diagnostic, 1), 'base-refresh', diagnostic)
                self.assertEqual(grading_env.failure_kind(diagnostic), 'unknown')
            for diagnostic in ('prefix-' + name, name + '-suffix', 'question about ' + name,
                               'problem:' + name + '-suffix', 'reason=' + name + '-suffix'):
                self.assertEqual(ns.classify_failure(diagnostic, 1), 'unknown', diagnostic)
            kind = 'build-blocked' if name == 'pinned-base-mismatch' else 'spawn-failed'
            events = [event('spec-written', -60, actor='operator'),
                      event(kind, -20, actor='builder', spawn='L-builder-9774', reason='problem:' + name + ' — refresh')]
            step = self.one(events)
            self.assertEqual((step.owner, step.state, step.action), ('executor', 'hold', None))
            for word in ('base-refresh', 'regenerate', 'redispatch', 'safe auto-rebase'):
                self.assertIn(word, step.reason)
            self.assertIn('effective base', step.clears)
            self.assertIn('packet', step.clears)
            self.assertEqual(self.one(events), step)
            self.signed('R7-' + name, asdict(step))
            started = events + [event('build-started', -10, spawn='L-builder-9775')]
            self.assertNotIn('base-refresh', self.one(started).reason)
        self.assertNotIn('build-blocked', ns.FAILURES)
        for text, expected in (('FAILED fixture', 'work'), ('No space left on device', 'infra'),
                               ('FAILED No space left on device', 'unknown'), ('other', 'unknown')):
            self.assertEqual(ns.classify_failure(text, 1), expected)
            self.assertEqual(grading_env.failure_kind(text), expected)
        baseline = [event('spec-written', -90, actor='operator'),
                    event('build-blocked', -40, actor='builder', spawn='L-builder-9774', reason='needs judgment'),
                    event('question', -30, actor='builder', spawn='L-builder-9774', asks='Which option?')]
        original = self.one(baseline)
        incidental = deepcopy(baseline)
        incidental[-1]['asks'] += ' stale-base pinned-base-mismatch'
        incidental[-2]['why'] = 'reason=pinned-base-mismatch'
        self.assertEqual(self.one(incidental), original)
        answered = baseline + [event('decision', -10, actor='operator', ref=baseline[-1]['_src'])]
        self.assertEqual(self.one(answered).action, 'rebuild')
        self.signed('R7-unrelated-blocked', dict(original=asdict(original), answered=asdict(self.one(answered))))
        exhausted = [event('spec-written', -90, actor='operator')]
        exhausted += [event('spawn-failed', -60 + n * 10, actor='tick', role='builder',
                            spawn=f'L-builder-{9800 + n}', reason='vanished') for n in range(3)]
        exhausted += [event('spawn-failed', -10, actor='builder', spawn='L-builder-9803', reason='stale-base')]
        held = self.one(exhausted)
        self.assertIn('retry exhausted: 3', held.reason)
        self.assertEqual((held.owner, held.action), ('executor', None))
        self.signed('R7-bounded-exhaustion', asdict(held))


if __name__ == '__main__':
    unittest.main()
