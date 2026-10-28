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
                subject=subject, _src=f'L-operator-fixture.jsonl:{minutes + 20000}', **({'actor': 'operator'} | extra))


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


if __name__ == '__main__':
    unittest.main()
