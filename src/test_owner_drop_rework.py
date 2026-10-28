"""Owner drops dispatch builder rework and carry the window's evidence."""
import ast
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import fold
import lane_rules
import nextstep as ns
import packet


ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
SID = 'L-spec-9001'
NODES = ['tests/test_shared.py::test_one', 'tests/test_shared.py::test_two']
RUN = 'https://example.invalid/actions/runs/1790'


def event(kind, minute, subject=SID, **fields):
    return dict(type=kind, subject=subject,
                ts=(NOW + timedelta(minutes=minute)).isoformat(),
                _src=f'L-operator-fixture.jsonl:{minute + 100}', **fields)


def dropped(minute=-10, subject=SID, **fields):
    return event('window-dropped', minute, subject, **dict(
        window='win-fixture', reason='owner', owned_nodes=NODES,
        unattributed_nodes=[], run_url=RUN, **fields))


def prechange(module, name):
    """Execute the actual base function against the same dependencies/fixtures."""
    base = subprocess.check_output(['git', 'merge-base', 'HEAD', 'main'],
                                   cwd=ROOT, text=True).strip()
    source = subprocess.check_output(['git', 'show', f'{base}:src/{module.__name__}.py'],
                                     cwd=ROOT, text=True)
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<pre-change>', 'exec'), namespace)
    return namespace[name]


class OwnerDropReworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_rebuild = staticmethod(prechange(ns, '_rebuild'))
        cls.old_builder = staticmethod(prechange(packet, 'p_builder'))
        cls.rules = lane_rules.load()

    def history(self, subject=SID):
        return [event('spec-written', -60, subject),
                event('build-done', -50, subject, status='DONE'),
                event('review', -40, subject, n_blocking=0),
                event('merge-gate-clean', -30, subject)]

    def step(self, events, subject=SID, old=False, specs=None):
        specs = specs or {e['subject']: dict(state='graded') for e in events}
        with patch.dict(ns.PREDICATES, {'rebuild': self.old_rebuild}) if old else patch.dict(ns.PREDICATES, {}):
            # Other predicates call _rebuild too; replace both references.
            with patch.object(ns, '_rebuild', self.old_rebuild if old else ns._rebuild):
                return ns.step_for(events, specs, subject, NOW, self.rules)

    def test_ac1_owner_rebuild_and_attempt_guards(self):
        events = self.history() + [dropped()]
        current, before = self.step(events), self.step(events, old=True)
        self.assertEqual(current.action, 'rebuild')
        self.assertIsNone(before.action)
        context = dict(evs=events, spec=dict(state='graded'), dependency=None)
        self.assertIsNone(self.old_rebuild(context))
        for kind, extra in [('build-started', {'spawn': 'L-builder-9001'}),
                            ('autodispatched', {'role': 'builder', 'spawn': 'L-builder-9001'})]:
            with self.subTest(kind=kind):
                later = events + [event(kind, -5, **extra)]
                self.assertIsNone(ns._rebuild(dict(context, evs=later)))
                self.assertNotEqual(self.step(later).action, 'rebuild')
        blocked = event('build-blocked', -5)
        self.assertIsNone(ns._rebuild(dict(context, evs=events + [blocked])))
        newer = dropped(-2)
        decision = event('decision', -1, actor='thinker', rework='yes')
        trigger = ns._rebuild(dict(context, evs=events + [blocked, newer, decision]))
        self.assertEqual(trigger['since'], decision['ts'])
        # A lexically earlier timestamp can still be later in absolute time.
        offset = dict(newer, ts='2026-10-28T07:59:00-04:00')
        trigger = ns._rebuild(dict(context, evs=events + [blocked, offset]))
        self.assertEqual(trigger['since'], offset['ts'])
        print(f'AC1 steps: current={current.action}, pre-change={before.action}, after build-started=None')
        print('L-spec-1790 AC1 ok')

    def test_ac2_innocent_coowners_and_holds(self):
        innocent = dict(dropped(), reason='bisect-single')
        events = self.history() + [innocent]
        self.assertEqual(self.step(events), self.step(events, old=True))
        self.assertNotEqual(self.step(events).action, 'rebuild')
        subjects = [SID, 'L-spec-9002', 'L-spec-9003']
        together = [e for sid in subjects for e in self.history(sid) + [dropped(subject=sid)]]
        self.assertEqual([self.step(together, sid).action for sid in subjects], ['rebuild'] * 3)
        context = dict(evs=self.history() + [dropped()], spec=dict(state='graded'), dependency=None)
        for state in ns.dispatch.SETTLED_STATES:
            self.assertIsNone(ns._rebuild(dict(context, spec=dict(state=state))))
        self.assertIsNone(ns._rebuild(dict(context, evs=context['evs'] + [event('spec-killed', -5)])))
        self.assertIsNone(ns._rebuild(dict(context, dependency='L-spec-9002')))
        held = context['evs'] + [event('dependency-hold', -5, waits_for='L-spec-9002')]
        self.assertNotEqual(self.step(held).action, 'rebuild')
        print('AC2: bisect-single identical; three overlapping owners rebuild; terminal/dependency holds respected')
        print('L-spec-1790 AC2 ok')

    def test_ac3_builder_packet_evidence_and_unchanged_cases(self):
        with tempfile.TemporaryDirectory(prefix='owner-drop-rework-') as directory:
            root = Path(directory)
            content = root / 'content'
            content.mkdir()
            spec = content / f'{SID}.md'
            spec.write_text('# fixture\n## Interfaces\nWrites: src/nextstep.py\n'
                            '## Verification\n```\n/usr/bin/true\n```\n')
            events = [event('spec-written', -60, path=str(spec), footprint=['src/nextstep.py'], project='fixture')]

            def render(rows, old=False):
                with patch.object(fold, 'read_events', return_value=rows), \
                     patch.object(packet, 'ROOT', root), \
                     patch.object(packet, 'CONTENT', content), \
                     patch.object(packet, 'PACKETS', root / 'packets'), \
                     patch.dict(os.environ, {'DOIT_ROOT': str(root), 'HOME': str(root / 'home')}), \
                     patch.dict(packet.BUILD, {'builder': self.old_builder if old else packet.p_builder}):
                    path = packet.main(['builder', SID, '--repo', str(ROOT), '--worktree', str(ROOT),
                                        '--project', 'fixture', '--base-sha', 'fixture-base'])
                    return Path(path).read_bytes()

            self.assertEqual(render(events), render(events, old=True))
            old_drop = dropped(-20)
            # Lexically later drop is older in absolute time than build-done.
            old_drop['ts'] = '2026-10-28T13:00:00+02:00'
            done = event('build-done', -30, status='DONE')
            for rows in (events + [old_drop, done], events + [done, dict(dropped(), ts=done['ts'])],
                         events + [dict(dropped(), reason='bisect-single')]):
                self.assertEqual(render(rows), render(rows, old=True))
            owner = dict(dropped(), unattributed_nodes=['tests/test_other.py::test_unknown'])
            rows = events + [done, owner, dict(dropped(-5), reason='bisect-single')]
            text = render(rows).decode()
            heading = 'Dropped from window win-fixture: these test nodes failed with your change and pass without it:'
            self.assertIn(heading, text)
            for node in NODES:
                self.assertIn(node, text)
            self.assertIn(f'Run: {RUN}', text)
            self.assertIn('also failed but were not attributed to this spec', text)
            self.assertIn(owner['unattributed_nodes'][0], text)
            empty = render(events + [dropped()]).decode()
            self.assertNotIn('also failed but were not attributed to this spec', empty)
            newer = dict(dropped(-1), window='win-new', owned_nodes=['tests/test_latest.py::test_new'])
            latest = render(events + [owner, newer]).decode()
            self.assertIn('Dropped from window win-new:', latest)
            self.assertNotIn(NODES[0], latest)
            print(text[text.index(heading):text.index('## STOP')].strip())
        print('AC3: no drop, stale drop, equal-time drop and bisect-single packets byte-identical to pre-change')
        print('L-spec-1790 AC3 ok')


if __name__ == '__main__':
    unittest.main()
