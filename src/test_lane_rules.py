#!/usr/bin/env python3
"""Synthetic regressions for the read-only lane rule contract."""
import ast
import builtins
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import autodispatch
import dispatch
import fold
import tick
import lane_rules as lr

ROOT = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 10, 18, 14, tzinfo=dt.timezone.utc)
SID = 'L-spec-9001'
TARGET = 'L-spec-9002'
NAMES = ['build', 'grade', 'review', 'gate', 'enqueue', 'merge', 'idle',
         'rebuild', 'regrade', 'close', 'wave-held', 'dependency-held']
LIMITS = [30, 15, 15, 30, 60, 120, 60, 15, 15, 60, 10, 10]
ACTIONS = ['dispatch-builder', 'dispatch-grader', 'dispatch-reviewer', 'doit-gate',
           'queue-add', 'window-run', 'none', 'dispatch-builder', 'dispatch-grader',
           'spec-killed', 'none', 'none']
ACTORS = ['tick'] * 5 + ['executor', 'none'] + ['tick'] * 3 + ['none', 'none']
KEYS = {'spec', 'rule', 'action', 'actor', 'since', 'limit_min', 'overdue',
        'reason', 'reason_live', 'blocker'}
ACTING = set(NAMES) - {'idle', 'wave-held', 'dependency-held'}


def event(event_type, minute=-90, subject=SID, actor='operator', **fields):
    return dict(type=event_type, ts=(NOW + dt.timedelta(minutes=minute)).isoformat(),
                subject=subject, actor=actor, **fields)


def specs(events):
    with patch.object(fold, 'NOW', NOW):
        return fold.fold(sorted(events, key=lambda e: e['ts']))[0]


def written(subject=SID, **fields):
    return event('spec-written', subject=subject, actor='planner', project='fixture', **fields)


def fixture(rule):
    ev = [written()]
    trigger = {
        'build': [],
        'grade': [event('build-done', -60, actor='builder', status='DONE')],
        'review': [event('verdict', -60, actor='grader', confirmed=True)],
        'gate': [event('review', -60, actor='reviewer', confirmed=True, n_blocking=0)],
        'enqueue': [event('review', -70, actor='reviewer', confirmed=True, n_blocking=0),
                    event('merge-gate-clean', -60, actor='executor')],
        'merge': [event('queue-add', -60, actor='executor')],
        'rebuild': [event('build-blocked', -70, actor='builder'),
                    event('decision', -60, actor='thinker')],
        'regrade': [event('capability-hold', -70, subject='capability:db:' + SID),
                    event('unblocked', -60, subject='capability:db:' + SID)],
        'close': [event('decision', -60, actor='thinker', close='superseded')],
    }[rule]
    return ev + trigger


class LaneRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'events').mkdir()
        (self.root / 'content').mkdir()
        for module, fields in ((fold, ('ROOT', 'EVENTS')), (dispatch, ('ROOT', 'EVENTS', 'CONTENT'))):
            for field in fields:
                value = self.root if field == 'ROOT' else self.root / field.lower()
                p = patch.object(module, field, value)
                p.start()
                self.addCleanup(p.stop)
        self.queue = patch.dict(sys.modules, {'merge_queue': type(sys)('merge_queue')})
        self.queue.start()
        self.addCleanup(self.queue.stop)
        sys.modules['merge_queue'].queue = lambda ev, project=None: [
            {'spec': e['subject'], 'added_at': e['ts']} for e in ev if e['type'] == 'queue-add'
            and not any(x['subject'] == e['subject'] and x['ts'] > e['ts'] and
                        x['type'] in ('queue-remove', 'shipped', 'window-dropped', 'merge-conflict') for x in ev)]

    def rows(self, ev, now=NOW, **kw):
        rows = lr.due(ev, specs(ev), now, **kw)
        for row in rows:
            self.assertEqual(set(row), KEYS)
            for key in ('spec', 'rule', 'action', 'actor', 'since', 'reason'):
                self.assertIsInstance(row[key], str)
            for key in ('overdue', 'reason_live'):
                self.assertIs(type(row[key]), bool)
            self.assertIs(type(row['limit_min']), int)
            self.assertTrue(row['blocker'] is None or isinstance(row['blocker'], str))
            dt.datetime.fromisoformat(row['since'])
        return rows

    def row(self, ev, rule, **kw):
        return next(r for r in self.rows(ev, **kw) if r['spec'] == SID and r['rule'] == rule)

    def test_table_load(self):
        table = lr.load()
        self.assertEqual(set(table), set(NAMES))
        self.assertEqual(len(table), len(lr.PREDICATES))
        for name, actor, limit, action in zip(NAMES, ACTORS, LIMITS, ACTIONS):
            self.assertEqual(table[name], dict(name=name, actor=actor, limit_min=limit, action=action))
            print(name, actor, limit)
        original = (ROOT / 'lane_rules.toml').read_text()
        bads = [None, 'bad = [', original.replace('limit_min = 30', '', 1),
                original.replace('limit_min = 30', 'limit_min = "30"', 1),
                original.replace('name = "build"', 'name = "unknown"', 1)]
        for i, body in enumerate(bads):
            path = self.root / f'bad-{i}.toml'
            if body is not None:
                path.write_text(body)
            with self.assertRaises(ValueError):
                lr.load(path)
            print('ValueError', path.name)
        with patch.dict(lr.PREDICATES, extra=lambda c: None):
            with self.assertRaises(ValueError):
                lr.load()
        print('ValueError predicate without row')

    def test_conditions_from_events(self):
        suppressors = {
            'build': event('build-started', -30, actor='builder', role='builder'),
            'grade': event('spawn-started', -30, role='grader'),
            'review': event('spawn-started', -30, role='reviewer'),
            'gate': event('merge-gate-failed', -30),
            'enqueue': event('window-dropped', -30),
            'merge': event('queue-remove', -30),
            'rebuild': event('spawn-started', -30, role='builder'),
            'regrade': event('spawn-started', -30, role='grader'),
            'close': event('spec-killed', -30),
        }
        for rule in sorted(ACTING):
            ev = fixture(rule)
            row = self.row(ev, rule)
            idx = NAMES.index(rule)
            self.assertEqual((row['action'], row['actor']), (ACTIONS[idx], ACTORS[idx]))
            self.assertEqual(row['since'], ev[-2 if rule == 'enqueue' else -1]['ts'])
            self.assertEqual([r['rule'] for r in self.rows(ev) if r['spec'] == SID and r['rule'] in ACTING], [rule])
            self.assertFalse(any(r['rule'] == rule and r['spec'] == SID for r in self.rows(ev + [suppressors[rule]])))
            print(rule, 'positive negative')
        for actor, close in [('builder', 'reason'), ('executor', 'reason'), ('thinker', None)]:
            ev = [written(), event('decision', -60, actor=actor, **({'close': close} if close else {}))]
            self.assertFalse(any(r['rule'] == 'close' for r in self.rows(ev)))
        self.assertEqual(self.row([written(), event('cannot-assess', -60, actor='grader')], 'review')['rule'], 'review')
        for rule, suppressor in [('review', event('rejected-criterion', -20, criterion='AC1')),
                                 ('gate', event('must-fix', -20, criterion='AC1'))]:
            self.assertFalse(any(r['rule'] == rule for r in self.rows(fixture(rule) + [suppressor])))
        self.row([written(), event('build-blocked', -70), event('spec-written', -60)], 'rebuild')

    def test_states_not_from_fold(self):
        for rule in ('gate', 'enqueue', 'rebuild'):
            ev = [written(), event('build-done', -85, actor='builder', status='DONE'),
                  event('verdict', -80, actor='grader', confirmed=True)] + fixture(rule)[1:]
            s = specs(ev)
            self.assertIn(s[SID]['state'], ('reviewing', 'graded'))
            self.assertEqual(self.row(ev, rule)['rule'], rule)
            print(s[SID]['state'], rule)
        for terminal in ('shipped', 'spec-killed', 'spec-closed'):
            self.assertFalse(any(r['rule'] in ACTING for r in self.rows(fixture('review') + [event(terminal, -1)])))
        ev = []
        for i in range(21):
            sid = f'L-spec-{9100+i}'
            ev += [written(sid), event('verdict', -60, subject=sid, actor='grader', confirmed=True)]
        rows = self.rows(ev)
        self.assertEqual(len([r for r in rows if r['rule'] == 'review']), 21)
        for row in rows:
            print(row['spec'], row['rule'], specs(ev)[row['spec']]['state'])

    def test_row_contract(self):
        ev = fixture('grade')
        since = dt.datetime.fromisoformat(ev[-1]['ts'])
        for seconds, overdue in ((899, False), (900, False), (901, True)):
            row = self.row(ev, 'grade', now=since + dt.timedelta(seconds=seconds))
            self.assertEqual(row['overdue'], overdue)
            print('boundary', seconds, overdue)
        with patch.dict(lr.PREDICATES, grade=lambda c: 1 / 0):
            row = self.row(ev, 'grade')
            self.assertTrue(row['overdue'])
            self.assertFalse(row['reason_live'])
            self.assertTrue(row['reason'].startswith('undetermined: '))
            print(row['reason'])
        with patch.dict(sys.modules, merge_queue=None):
            row = self.row(fixture('merge'), 'merge')
            self.assertTrue(row['reason'].startswith('undetermined: '))
            print(row['reason'])
        for env in (False, True):
            path = self.root / 'missing.toml'
            if env:
                path.write_text('invalid = [')
            with patch.dict(os.environ, {'DOIT_LANE_RULES': str(path)} if env else {}):
                rows = self.rows([written(), written(TARGET)], **({} if env else {'table': path}))
            self.assertEqual({r['spec'] for r in rows}, {SID, TARGET})
            self.assertTrue(all(r['overdue'] and not r['reason_live'] and r['reason'].startswith('undetermined: ') for r in rows))
            print(rows[0]['reason'])
        ev = [written(), event('spawn-started', -5, role='builder', spawn='L-builder-9999', window_min=10)]
        self.assertTrue(lr._busy(ev, NOW)[0])
        self.assertFalse(lr._busy(ev, NOW + dt.timedelta(minutes=101))[0])
        self.assertFalse(any(r['reason_live'] for r in self.rows(ev, now=NOW + dt.timedelta(minutes=101))))
        esc = event('escalation-blocking', -10, deadline=(NOW + dt.timedelta(minutes=10)).isoformat())
        self.assertTrue(self.row(fixture('merge') + [esc], 'merge')['reason_live'])
        self.assertFalse(self.row(fixture('merge') + [esc], 'merge', now=NOW + dt.timedelta(minutes=11))['reason_live'])
        self.assertTrue(self.row(fixture('review') + [event('spawn-started', -1, role='builder')], 'review')['reason_live'])
        self.assertIn(SID, lr._busy([esc], NOW)[1])
        self.assertNotIn(SID, lr._busy([esc], NOW + dt.timedelta(minutes=11))[1])
        malformed = [written(), dict(event('build-done', actor='builder', status='DONE'), ts='bad')]
        with patch.object(fold, 'NOW', object()):
            rows = lr.due(malformed, {SID: {'state': 'graded', 'evs': malformed}}, NOW)
        self.assertTrue(rows[0]['reason'].startswith('undetermined: '))
        # Mirror look._spec_idle_bad's ownership, human-wait and age exclusions.
        idle = [written(), event('build-blocked', -80, actor='builder')]
        self.assertEqual(self.row(idle, 'idle')['since'], idle[-1]['ts'])
        for extra in (event('spawn-started', -1, role='builder'),
                      event('escalation-blocking', -1),
                      event('question', -70, _src='fixture:1'),
                      event('capability-hold', -1, subject='capability:db:' + SID),
                      event('autodispatch-failed', -1), event('build-blocked', -1)):
            self.assertFalse(any(r['rule'] == 'idle' for r in self.rows(idle + [extra])))
        self.assertFalse(any(r['rule'] == 'idle' for r in self.rows([written()])))
        self.assertFalse(any(r['rule'] == 'idle' for r in self.rows(idle + [event('shipped', -1)])))
        print('idle population and ownership exclusions')

    def test_ladder(self):
        for rule in NAMES:
            row = dict(spec=SID, rule=rule, since=event('x')['ts'], limit_min=10, overdue=False, reason_live=False)
            retry = event('autodispatched', -20, rule=rule, since=row['since'], trigger='retry')
            escalation = event('escalation-blocking', -5, subject='lane-stale:' + SID,
                               kind='lane-stale', rule=rule, since=row['since'])
            self.assertEqual(lr.ladder([], row, NOW), 'act')
            print(rule, 'act')
            row['overdue'] = True
            self.assertEqual(lr.ladder([], row, NOW), 'inbox' if rule in ('merge', 'idle') else 'retry')
            if rule in ('merge', 'idle'):
                row['reason_live'] = True
                self.assertEqual(lr.ladder([], row, NOW), 'act')
                self.assertEqual(lr.ladder([retry], row, NOW), 'act')
                row['reason_live'] = False
            else:
                self.assertEqual(lr.ladder([dict(retry, ts=event('x', -1)['ts'])], row, NOW), 'retry')
                self.assertEqual(lr.ladder([dict(retry, rule='wrong')], row, NOW), 'retry')
                self.assertEqual(lr.ladder([{k: v for k, v in retry.items() if k != 'rule'}], row, NOW), 'retry')
                self.assertEqual(lr.ladder([retry, dict(retry, ts=event('x', -1)['ts'])], row, NOW), 'inbox')
            self.assertEqual(lr.ladder([retry], row, NOW), 'inbox')
            self.assertEqual(lr.ladder([retry, escalation], row, NOW), 'digest')
            row['overdue'] = False
            self.assertEqual(lr.ladder([retry, escalation], row, NOW), 'digest')
            row['since'] = event('x', -1)['ts']
            self.assertEqual(lr.ladder([retry, escalation], row, NOW), 'act')
            print(rule, 'retry inbox digest new-key act' if rule not in ('merge', 'idle') else 'inbox digest new-key act')

    def plan(self):
        (dispatch.CONTENT / 'plan-L-charter-0052.md').write_text(
            '## first\nFootprint:\n- first.py\nWave: 1\n\n## later\nFootprint:\n- later.py\nWave: 2\n')
        (self.root / 'repos').mkdir(exist_ok=True)
        (self.root / 'repos' / 'fixture').symlink_to(ROOT)
        (dispatch.CONTENT / f'{SID}.md').write_text('''# Goal
Fixture.
# Acceptance criteria
AC1 [backend]: fixture. review_path: n/a.
Writes:
- later.py
# Verification
```
true && echo VERIFIED
```
''')
        return [written(TARGET, charter='L-charter-0052', footprint=['first.py']),
                written(charter='L-charter-0052', footprint=['later.py'])]

    def candidates(self, ev, now=NOW):
        with patch.object(tick, 'in_flight', return_value=set()):
            return next(r for r in autodispatch.candidates(ev, specs(ev), now) if r['spec'] == SID)

    def test_holds_lift_on_shipped(self):
        base = self.plan()
        for state in ('shipped', 'shipped-owed-evidence', 'shipped-owed-due', 'shipped-owed-expired'):
            ev = base + [event('shipped', -30, subject=TARGET, actor='executor')]
            if state != 'shipped':
                ev += [event('owed-ac', -60, subject=TARGET, actor='spec-writer', criterion='AC1',
                             wake_at=event('x', 120 if state == 'shipped-owed-evidence' else -60)['ts'], line='fixture')]
            if state == 'shipped-owed-expired':
                ev += [event('owed-unobservable', -10, subject=TARGET, actor='owed-sweeper',
                             criterion='AC1', capability='zzz', why='expired fixture')]
            s = specs(ev)
            self.assertEqual(s[TARGET]['state'], state)
            self.assertFalse(any(r['rule'] == 'wave-held' for r in lr.due(ev, s, NOW)))
            self.assertEqual(self.candidates(ev)['status'], 'dispatchable')
            print('a', state, 'no row dispatchable')
        ev = base + [event('review', -180, subject=TARGET, confirmed=True, n_blocking=0),
                     event('merge-gate-clean', -167, subject=TARGET), event('window-opened', -166),
                     event('merge-gate-clean', -133, subject=TARGET), event('window-opened', -132)]
        hold = self.row(ev, 'wave-held')
        blocker = next(r for r in self.rows(ev) if r['spec'] == TARGET and r['rule'] == 'enqueue')
        self.assertEqual((hold['since'], hold['limit_min']), (blocker['since'], blocker['limit_min']))
        self.assertTrue(hold['overdue'])
        self.assertFalse(hold['reason_live'])
        young = self.row(ev, 'wave-held', now=NOW - dt.timedelta(minutes=150))
        self.assertFalse(young['overdue'])
        self.assertTrue(young['reason_live'])
        print('b enqueue inherited', hold)
        ev = base + [event('build-started', -5, subject=TARGET, role='builder')]
        hold = self.row(ev, 'wave-held')
        self.assertEqual((hold['reason'], hold['since'], hold['limit_min']), ('blocker-open: no-row', ev[-1]['ts'], 10))
        self.assertTrue(hold['reason_live'])
        hold = self.row(ev, 'wave-held', now=NOW + dt.timedelta(minutes=181))
        self.assertTrue(hold['overdue'])
        self.assertFalse(hold['reason_live'])
        print('b fallback', hold)
        ev = [written(), written(TARGET), event('dependency-hold', -20, waits_for=TARGET)]
        self.assertEqual(self.row(ev, 'dependency-held')['blocker'], TARGET)
        ev += [event('shipped', 1, subject=TARGET, actor='executor')]
        self.assertFalse(any(r['rule'] in ('wave-held', 'dependency-held') for r in self.rows(ev, now=NOW + dt.timedelta(minutes=11))))
        self.assertEqual(self.candidates(ev, NOW + dt.timedelta(minutes=11))['status'], 'dispatchable')
        print('c no row dispatchable')
        ev[-1] = event('spec-killed', -1, subject=TARGET, actor='spec-writer')
        row = self.row(ev, 'dependency-held')
        self.assertTrue(row['reason'].startswith('blocker-dead:'))
        self.assertTrue(row['overdue'])
        print('d', row)
        ev = base + [event('decision', -1, actor='thinker', wave_waive='yes')]
        self.assertFalse(any(r['rule'] == 'wave-held' for r in self.rows(ev)))
        print('e no row')
        self.assertEqual(self.candidates([written()])['status'], 'dispatchable')

    def ledger(self):
        ev = self.plan() + [event('dependency-hold', -10, waits_for=TARGET),
            event('spawn-started', -500, spawn='L-builder-9998', role='builder'),
            event('spawn-started', -2, subject=TARGET, spawn='L-builder-9999', role='builder',
                  waiter_pid=99999999, waiter_host=socket.gethostname())]
        path = fold.EVENTS / 'L-operator-fixture.jsonl'
        path.write_text(''.join(json.dumps({k: v for k, v in e.items() if k != 'actor'}) + '\n' for e in ev))
        return ev

    def hashes(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob('*') if p.is_file() and not p.is_symlink() and 'repos' not in p.parts}

    def test_cli_read_only(self):
        self.ledger()
        env = dict(os.environ, DOIT_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE='1')
        env.pop('DOIT_LEDGER_FILE', None)
        env.pop('DOIT_LANE_RULES', None)
        before = self.hashes()
        for args in (['--json'], [], ['--json', '--table', str(self.root / 'absent.toml')]):
            r = subprocess.run([str(ROOT / 'doit'), 'lane-rules', *args], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            if '--json' in args:
                rows = json.loads(r.stdout)
                self.assertTrue(rows)
                self.assertTrue(all(set(row) == KEYS for row in rows))
                if '--table' in args:
                    self.assertTrue(all(row['reason'].startswith('undetermined: ') for row in rows))
                    self.assertEqual(len(r.stderr.splitlines()), 1)
                    self.assertIn('absent.toml', r.stderr)
            else:
                self.assertIn(SID, r.stdout)
                self.assertIn('overdue=', r.stdout)
        r = subprocess.run([str(ROOT / 'doit'), 'lane-rules', '--json'],
                           env=dict(env, DOIT_LANE_RULES=str(self.root / 'absent.toml')), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(all(x['reason'].startswith('undetermined: ') for x in json.loads(r.stdout)))
        self.assertEqual(before, self.hashes())
        print('before hashes', before)
        print('after hashes', self.hashes(), 'equal')
        self.assertIn('lane-rules', subprocess.check_output([str(ROOT / 'doit'), 'help'], text=True))

    def test_purity_and_imports(self):
        ev = self.ledger()
        s = specs(ev)
        before = self.hashes()
        original_open = builtins.open
        def readonly(file, mode='r', *args, **kwargs):
            if any(c in mode for c in 'wax+'):
                raise AssertionError('write')
            return original_open(file, mode, *args, **kwargs)
        class Poison:
            def fail(self, *args):
                raise AssertionError('wall clock')
            __sub__ = __rsub__ = __lt__ = __le__ = __gt__ = __ge__ = fail
        with patch.object(dispatch, 'emit', side_effect=AssertionError('emit')), \
             patch.object(fold, 'append', side_effect=AssertionError('append')), \
             patch.object(fold, 'NOW', Poison()), \
             patch.object(subprocess, 'run', side_effect=AssertionError('subprocess')), \
             patch('builtins.open', readonly):
            rows = lr.due(ev, s, NOW)
            self.assertTrue(rows)
            for row in rows:
                lr.ladder(ev, row, NOW)
            with self.assertRaises(AssertionError):
                tick.in_flight(ev)
        self.assertEqual(before, self.hashes())
        source = (ROOT / 'src/lane_rules.py').read_text()
        tree = ast.parse(source)
        imports = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
        self.assertTrue({'autodispatch', 'dispatch', 'tick'} <= imports)
        forbidden = []
        predicate_tree = ast.Module(body=[n for n in tree.body if not isinstance(n, ast.FunctionDef) or n.name != 'main'], type_ignores=[])
        for n in ast.walk(predicate_tree):
            if isinstance(n, ast.Attribute):
                name = ast.unparse(n)
                if name in ('fold.NOW', 'fold.append', 'dispatch.emit', 'subprocess.run',
                            'datetime.now', 'datetime.utcnow', 'time.time', 'tick.in_flight', 'autodispatch.candidates'):
                    forbidden.append(name)
        self.assertEqual(forbidden, [])
        for text in ('claude' + ' -p', 'ANTHROPIC' + '_API_KEY'):
            self.assertNotIn(text, source)
        for module in ('lane_rules', 'autodispatch'):
            code = 'import sys; sys.path.insert(0, "src"); import ' + module
            if module == 'autodispatch':
                code += '; assert "lane_rules" not in sys.modules'
            subprocess.run(['/usr/bin/python3', '-I', '-c', code], cwd=ROOT, check=True)
            print('import', module)
        print('forbidden calls', forbidden, 'purity byte-identical')


if __name__ == '__main__':
    unittest.main()
