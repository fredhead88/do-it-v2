#!/usr/bin/env python3
"""Scratch-only CLI and fresh-reader regressions for explicit check publication."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

_ISO = tempfile.TemporaryDirectory(prefix='check-backfill-import-')
os.environ['DOIT_ROOT'] = str(Path(_ISO.name) / 'root')
os.environ['HOME'] = str(Path(_ISO.name) / 'home')
sys.path.insert(0, str(Path(__file__).resolve().parent))
import checks
import fold

REPO = Path(__file__).resolve().parent.parent
SHA = 'a' * 40
LINE = 'check: data_source=scratch customer_data=false profile=repo-venv timing=pre-merge answered_by=script'
PROD = 'check: data_source=production customer_data=true profile=prod-readonly timing=post-deploy answered_by=script'


def spec(blocks=None, command=None):
    return ('## Acceptance criteria\n' + (blocks or 'AC1 [backend]: fixture\n' + LINE) +
            '\n\n## Verification\n```bash\n' + (command or '# AC1\nprintf fixture') + '\n```\n')


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='check-backfill-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'events').mkdir()
        (self.root / 'content').mkdir()
        self.env = dict(os.environ, DOIT_ROOT=str(self.root), HOME=str(self.root / 'home'),
                        DOIT_READ_CACHE='0', PYTHONDONTWRITEBYTECODE='1')
        self.env.pop('DOIT_PROJECT', None)
        self.env.pop('DOIT_LEDGER_FILE', None)
        self.transcript = []

    def seed(self, sid='L-spec-9001', text=None, project='alpha', sha=SHA, actor='executor', shipped=True):
        (self.root / 'content' / (sid + '.md')).write_text(text or spec())
        self.event('spec-written', sid, actor='spec-writer', project=project)
        if shipped:
            self.event('shipped', sid, actor=actor, project=project, sha=sha)

    def event(self, event_type, sid='L-spec-9001', actor='executor', **fields):
        event = dict(v=1, ts='2020-01-01T00:00:00+00:00', type=event_type, subject=sid) | fields
        with (self.root / 'events' / f'L-{actor}-fixture.jsonl').open('a') as output:
            output.write(json.dumps(event) + '\n')

    def cli(self, *args, env=None):
        result = subprocess.run([str(REPO / 'doit'), 'check-backfill', *args],
                                env=env or self.env, capture_output=True, text=True)
        self.transcript.append(dict(args=args, status=result.returncode,
                                    stdout=result.stdout, stderr=result.stderr))
        return result

    def snapshot(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for directory in ('events', 'content') for p in (self.root / directory).rglob('*') if p.is_file()}

    def fresh(self, sid='L-spec-9001'):
        code = '''import json, sys, fold, checks
history = fold.fold(fold.read_events(use_cache=False))[3].get(sys.argv[1], [])
records = {}
for e in history:
    if e['type'] == 'check-declared':
        record = checks.record_for(history, sys.argv[1], e['criterion'])
        records[e['criterion']] = record.to_event() if record else None
print(json.dumps({'records': records, 'events': [e for e in history if e['type'] == 'check-declared']}))
'''
        result = subprocess.run([sys.executable, '-c', code, sid], cwd=REPO / 'src',
                                env=self.env, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def proof(self, ac, facts):
        body = json.dumps(dict(facts=facts, calls=self.transcript), sort_keys=True)
        print(body)
        print(json.dumps(dict(url='cli://doit/check-backfill/' + ac, status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=facts)))

    def test_ac1_discovery(self):
        empty = self.snapshot()
        out = self.cli()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('declared=0', out.stdout)
        self.assertEqual(empty, self.snapshot())
        for i, state in enumerate(('due', 'blocked', 'failed', 'met', 'waived', 'unverifiable')):
            sid = f'L-spec-{9001+i}'
            self.seed(sid, project='alpha' if i % 2 else 'beta')
            project = 'alpha' if i % 2 else 'beta'
            self.event('owed-ac', sid, actor='spec-writer', criterion='AC1',
                       project=project, wake_at='2020-01-01T00:00:00+00:00')
            if state == 'blocked':
                self.event('owed-unobservable', sid, actor='owed-sweeper', criterion='AC1',
                           project=project, capability='ro-dsn', why='fixture read access unavailable',
                           ts='2020-01-01T00:00:01+00:00')
            elif state != 'due':
                self.event('owed-' + state, sid, actor='operator', criterion='AC1',
                           project=project, kind='unmet', evidence='fixture proof', reason='fixture',
                           ts='2020-01-01T00:00:01+00:00')
        code = '''import json, fold, owed
histories = fold.fold(fold.read_events())[3]
print(json.dumps({sid: owed.checks(history, fold.NOW)[0]['status']
                  for sid, history in histories.items()}))
'''
        states = subprocess.run([sys.executable, '-c', code], cwd=REPO / 'src', env=self.env,
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(states.stdout), {f'L-spec-{9001+i}': state for i, state in
                         enumerate(('due', 'blocked', 'failed', 'met', 'waived', 'unverifiable'))})

        self.seed('L-spec-9010', shipped=False)
        self.seed('L-spec-9011', actor='builder')  # rejected shipped actor
        before = self.snapshot()
        out = self.cli()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('dry-run', out.stdout)
        self.assertIn('declared=6', out.stdout)
        self.assertNotIn('L-spec-9010', out.stdout)
        self.assertNotIn('L-spec-9011', out.stdout)
        self.assertEqual(before, self.snapshot())
        out = self.cli('--spec', 'L-spec-9002')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('L-spec-9002/AC1 declared', out.stdout)
        self.assertNotIn('L-spec-9001', out.stdout)
        for sid in ('L-spec-9010', 'L-spec-9999'):
            self.assertNotEqual(self.cli('--apply', '--spec', sid).returncode, 0)
        scoped = dict(self.env, DOIT_PROJECT='alpha')
        out = self.cli(env=scoped)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('declared=3', out.stdout)
        self.assertNotEqual(self.cli('--apply', '--spec', 'L-spec-9001', env=scoped).returncode, 0)
        self.assertEqual(before, self.snapshot())
        self.proof('AC1', 'PASS: two projects; six owed states; selection and scope; unknown/unshipped rejected; byte hashes unchanged ' + json.dumps(before, sort_keys=True))

    def test_ac2_parsing(self):
        cases = [LINE.replace('data_source=scratch customer_data=false', 'customer_data=false data_source=scratch'),
                 LINE.replace('false', 'maybe'), LINE.replace('pre-merge', 'never'),
                 'review_path: misplaced\n' + LINE, LINE + '\n' + LINE]
        command = '# AC2\nprintf first &&\n# AC2\nprintf second'
        for bad in cases:
            with self.subTest(bad=cases.index(bad)):
                text = spec('AC1 [backend]: malformed\n' + bad + '\nAC2 [backend]: valid\n' + PROD, command)
                self.seed(text=text)
                out = self.cli('--apply')
                self.assertNotEqual(out.returncode, 0)
                self.assertRegex(out.stdout, r'L-spec-9001/AC1 malformed .+')
                self.assertIn('L-spec-9001/AC2', out.stdout)
                fresh = self.fresh()['records']
                self.assertNotIn('AC1', fresh)
                expected = checks.declared(spec('AC2 [backend]: valid\n' + PROD, command), 'L-spec-9001', SHA)[0]
                self.assertEqual(fresh['AC2'], expected.to_event())
                self.assertEqual(fresh['AC2']['argv'], ['/bin/sh', '-c', 'printf first && printf second'])
        self.seed('L-spec-9002', text=spec('AC1 [backend]: no declaration'))
        out = self.cli('--apply', '--spec', 'L-spec-9002')
        self.assertEqual(out.returncode, 0)
        self.assertEqual(self.fresh('L-spec-9002')['records'], {})
        self.seed('L-spec-9003')
        self.assertEqual(self.cli('--apply', '--spec', 'L-spec-9003').returncode, 0)
        self.assertEqual(self.fresh('L-spec-9003')['records']['AC1'], checks.declared(spec(), 'L-spec-9003', SHA)[0].to_event())
        self.proof('AC2', 'PASS: five malformed declarations isolated; scratch and production parser equality; no inference; exact multi-statement argv preserved')

    def test_ac3_round_trip(self):
        text = spec('AC1 [backend]: production\n' + PROD, '# AC1\nro-sql --query "SELECT 1"')
        self.seed(text=text, project='old-project')
        self.event('shipped', project='production-fixture', sha='b' * 40)
        self.assertEqual(self.fresh()['records'], {})
        out = self.cli('--apply')
        self.assertEqual(out.returncode, 0, out.stderr)
        fresh = self.fresh()
        expected = checks.declared(text, 'L-spec-9001', 'b' * 40)[0]
        self.assertEqual(fresh['records']['AC1'], expected.to_event())
        event = fresh['events'][0]
        self.assertEqual(event['subject'], expected.spec)
        self.assertEqual(event['project'], 'production-fixture')
        self.assertEqual(event['actor'], 'check-backfill')
        self.assertTrue(event['_src'].startswith('L-check-backfill-local.jsonl:'))
        envelope = {'ts', 'type', 'subject', 'v', 'spawn', 'ref', 'project', 'why', 'actor', '_src'}
        self.assertEqual(checks.CheckRecord.from_event({k: v for k, v in event.items() if k not in envelope}), expected)
        self.assertIs(type(event['customer_data']), bool)
        self.assertEqual(event['argv'], ['ro-sql', '--query', 'SELECT 1'])
        self.proof('AC3', 'PASS: real CLI append; fresh admitted reader; exact ten-field round trip; shipped project/sha; production prod-readonly; typed argv/boolean')

    def test_ac4_idempotency(self):
        self.seed()
        self.assertEqual(self.cli('--apply').returncode, 0)
        before = self.snapshot()
        out = self.cli('--apply')
        self.assertEqual(out.returncode, 0)
        self.assertIn('already-declared=1', out.stdout)
        self.assertEqual(before, self.snapshot())
        text = spec('AC1 [backend]: existing\n' + LINE + '\nAC2 [backend]: missing\n' + LINE,
                    '# AC1\nprintf fixture &&\n# AC2\nprintf second')
        self.seed(text=text)
        self.assertEqual(self.cli('--apply').returncode, 0)
        self.assertEqual(len(self.fresh()['events']), 2)
        for sid, malformed in [('L-spec-9002', False), ('L-spec-9003', True)]:
            self.seed(sid)
            stale = checks.declared(spec(), sid, 'c' * 40)[0].to_event()
            if malformed:
                stale['customer_data'] = 'false'
            self.event('check-declared', sid, actor='operator', project='alpha', **stale)
            initial = self.fresh(sid)['records']['AC1']
            self.assertTrue(initial is None if malformed else initial['sha'] != SHA)
            self.assertEqual(self.cli('--apply', '--spec', sid).returncode, 0)
            fresh = self.fresh(sid)
            self.assertEqual(fresh['records']['AC1'], checks.declared(spec(), sid, SHA)[0].to_event())
            self.assertEqual(len(fresh['events']), 2)
        self.seed('L-spec-9004')  # interrupted prefix: previous subjects completed
        out = self.cli('--apply')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('already-declared=4', out.stdout)
        self.assertEqual(len(self.fresh('L-spec-9004')['events']), 1)
        before = self.snapshot()
        self.assertEqual(self.cli('--apply').returncode, 0)
        self.assertEqual(before, self.snapshot())
        self.proof('AC4', 'PASS: second apply zero bytes; mixed missing; stale/malformed latest repair; interrupted-prefix completion')

    def test_ac5_safety(self):
        self.assertEqual(fold.EMITS['check-declared'], {'spec-writer', 'executor', 'operator', 'check-backfill'})
        self.assertIsNone(fold.admits({'type': 'check-declared'}, 'check-backfill'))
        for kind in ('owed-met', 'owed-failed', 'owed-waived', 'verdict'):
            self.assertIsNotNone(fold.admits({'type': kind}, 'check-backfill'))
        sentinel = self.root / 'sentinel'
        self.seed(text=spec(command='# AC1\ntouch ' + str(sentinel)))
        for args in ((), ('--apply',)):
            self.assertEqual(self.cli(*args).returncode, 0)
            self.assertFalse(sentinel.exists())
        for sid in ('../outside', '/outside', 'L-spec-9001/../../outside', 'L-spec-9001\nsecret'):
            before = self.snapshot()
            self.assertNotEqual(self.cli('--apply', '--spec', sid).returncode, 0)
            self.assertEqual(before, self.snapshot())
        outside = self.root / 'outside.md'
        outside.write_text(spec())
        self.seed('L-spec-9002')
        content = self.root / 'content/L-spec-9002.md'
        content.unlink()
        content.symlink_to(outside)
        out = self.cli('--apply', '--spec', 'L-spec-9002')
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('L-spec-9002', out.stdout)
        content.unlink()
        self.assertNotEqual(self.cli('--apply', '--spec', 'L-spec-9002').returncode, 0)
        for sid, metadata in [('L-spec-9003', {'sha': ''}), ('L-spec-9004', {'project': ''})]:
            self.seed(sid, **metadata)
            out = self.cli('--apply', '--spec', sid)
            self.assertNotEqual(out.returncode, 0)
            self.assertIn(sid, out.stdout)
        self.seed('L-spec-9006')
        out = self.cli('--apply')
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('L-spec-9006/AC1 declared', out.stdout)
        self.assertEqual(len(self.fresh('L-spec-9006')['events']), 1)
        self.seed('L-spec-9005')
        before = self.snapshot()
        guarded = dict(self.env, DOIT_TEST_MACHINE_ROOT=str(self.root))
        out = self.cli('--apply', '--spec', 'L-spec-9005', env=guarded)
        self.assertEqual(before, self.snapshot())
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('L-spec-9005/AC1', out.stdout)
        self.assertIn('append refused', out.stdout)
        self.proof('AC5', 'PASS: actor authority isolated; sentinel inert; path/symlink escape rejected; missing content/sha/project named; real append refusal nonzero')


if __name__ == '__main__':
    unittest.main()
