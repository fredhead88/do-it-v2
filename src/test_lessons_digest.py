#!/usr/bin/env python3
"""Synthetic acceptance tests; all persistence uses an isolated DOIT_ROOT."""
import datetime as dt
import hashlib
import importlib
import json
import os
import resource
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
sys.path.insert(0, str(REPO / 'src'))
import lessons_digest as digest

UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC).replace(microsecond=0)


def occurrence(i, slug='blocked', **fields):
    return dict(type='lesson', actor='planner', subject=f'L-spec-{i % 2}',
                problem=slug, ts=(NOW - dt.timedelta(hours=4, minutes=i)).isoformat(),
                _src=f'L-planner-fixture.jsonl:{i + 1}', **fields)


def rows(slug='blocked', start=0):
    return [occurrence(i, slug) for i in range(start, start + 3)]


def brief(slug='blocked', **fields):
    return dict(type='brief', actor='digest', subject='problem:' + slug,
                ts=NOW.isoformat(), path='missing.md', **fields)


class LessonsDigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lessons-digest-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ('DOIT_PROJECT', 'DOIT_LEDGER_FILE')}
        self.env.update(DOIT_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE='1')
        self.patch = patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        os.environ.pop('DOIT_PROJECT', None)
        import fold
        import problems
        self.fold = importlib.reload(fold)
        self.problems = importlib.reload(problems)
        root_patch = patch.object(digest, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.log = self.root / 'digest.md'
        self.cwd = self.root / 'unrelated-directory'
        self.cwd.mkdir()

    def evidence(self, ac, **facts):
        body = json.dumps(facts, sort_keys=True)
        print(json.dumps(dict(url='scratch://lessons-digest/' + ac, status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                              body_excerpt=body), sort_keys=True))

    def seed(self, events, actor='planner'):
        folder = self.root / 'events'
        folder.mkdir(exist_ok=True)
        target = folder / f'L-{actor}-fixture.jsonl'
        with target.open('a') as stream:
            for event in events:
                stream.write(json.dumps({k: v for k, v in event.items()
                                         if k not in ('actor', '_src')}) + '\n')
        return target

    def cli(self, project=None, days=14):
        env = dict(self.env)
        if project is not None:
            env['DOIT_PROJECT'] = project
        return subprocess.run([sys.executable, '-B', str(REPO / 'scripts/lessons_digest.py'),
                               '--log', str(self.log), '--days', str(days)],
                              cwd=self.cwd, env=env, capture_output=True, text=True, timeout=30)

    def read_back(self, project=None):
        code = '''import json, sys
sys.path.insert(0, sys.argv[1])
import fold
from pathlib import Path
es = fold.read_events(use_cache=False)
bs = [e for e in es if e['type'] == 'brief']
print(json.dumps({'briefs': bs, 'triage': [e['subject'] for e in fold.triage_briefs(es)],
                  'bodies': {e['subject']: Path(e['path']).read_text() for e in bs}}))
'''
        env = dict(self.env)
        if project:
            env['DOIT_PROJECT'] = project
        result = subprocess.run([sys.executable, '-B', '-c', code, str(REPO / 'src')],
                                env=env, cwd=self.cwd, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_ac1_qualification_and_daily_cap(self):
        fixtures = [rows(), rows(), rows()]
        fixtures[1][2]['type'] = 'problem-grouped'
        for event in fixtures[2]:
            event['type'] = 'problem-grouped'
        for fixture in fixtures:
            self.assertEqual(self.problems.occurrence_count(fixture, 'blocked'), 3)
            self.assertEqual([p['subject'] for p in digest.package(fixture, NOW)], ['problem:blocked'])
        negatives = [rows()[:2], [dict(e, subject='same') for e in rows()],
                     rows()[:2] + [dict(rows()[0])]]
        for value in ('unassigned', 'stuck-over-1h', '', None):
            negatives.append([dict(e, problem=value) for e in rows()])
        missing = rows()
        for event in missing:
            del event['problem']
            self.assertEqual(self.problems._slug_of(event), 'unassigned')
        negatives.append(missing)
        for changed in (dict(verdict='working'), dict(ts='bad'),
                        dict(ts=(NOW + dt.timedelta(seconds=1)).isoformat()),
                        dict(ts=(NOW - dt.timedelta(days=14, seconds=1)).isoformat()),
                        dict(subject='')):
            negatives.append([dict(e, **changed) for e in rows()])
        for fixture in negatives:
            self.assertEqual(digest.package(fixture, NOW), [], fixture)
        boundary = [dict(e, ts=(NOW - dt.timedelta(days=14)).isoformat()) for e in rows()]
        self.assertEqual(len(digest.package(boundary, NOW)), 1)
        for actor in ('thinker', 'digest'):
            old = brief(actor_override=actor, project='other')
            old['actor'] = actor
            old['ts'] = (NOW - dt.timedelta(days=100)).isoformat()
            # Existing exact subjects suppress creation even after routing/answering.
            evs = rows() + [old, dict(type='brief-routed', ref='prior'),
                            dict(type='brief-answered', ref='prior')]
            self.assertFalse(any(p['kind'] == 'brief' for p in digest.package(evs, NOW)))
        self.assertEqual(len(digest.package(
            rows() + [dict(brief('blocked-longer'), actor='thinker')], NOW)), 1)
        four = sum((rows(slug, start=n * 3) for n, slug in enumerate(('a', 'b', 'c', 'd'))), [])
        first = digest.package(four, NOW)
        self.assertEqual(len(first), 2)
        filed = [dict(brief(p['subject'][8:]), path='/scratch/brief.md') for p in first]
        self.assertEqual(digest.package(four + filed, NOW), [])
        later = digest.package(four + filed, NOW + dt.timedelta(days=1))
        self.assertEqual(len(later), 2)
        self.assertTrue(set(p['subject'] for p in first).isdisjoint(p['subject'] for p in later))
        self.assertEqual(len(digest.package(four + [brief('other', project='elsewhere')], NOW)), 1)
        self.evidence('AC1', occurrence_fixtures=3, negative_fixtures=len(negatives),
                      register_counts=[3, 3, 3], daily_cap=[2, 0, 2], boundary_inclusive=True)

    def test_ac2_brief_content(self):
        fixture = rows()
        fixture[0].update(statement='The gate repeatedly failed. Extra sentence.', step='merge',
                          file='src/gate.py', workaround='retry locally', cost_min='2.5', cost_tokens=20,
                          ref='evidence:1')
        fixture[1].update(workaround='wait for retry', cost_min='3', cost_tokens='30')
        fixture[2].update(cost_min='NaN', cost_tokens=-1)
        result = digest.package(fixture, NOW)[0]
        self.assertNotIn('requirement', result)
        text = result['text']
        for value in ('The gate repeatedly failed.', 'merge', 'src/gate.py', 'evidence',
                      'retry locally', 'wait for retry', '5.5 min', '50 tokens', '1 unmeasured'):
            self.assertIn(value, text)
        self.assertNotIn('Extra sentence', text)
        self.assertEqual(text.count('\n- ref: '), 3)
        history = [dict(occurrence(9), ts=(NOW - dt.timedelta(days=40)).isoformat(), cost_min=10)]
        self.assertIn('15.5 min', digest.package(fixture + history, NOW)[0]['text'])
        for cost in ('inf', '-inf', 'bad', None, [], True, -1, '1e999999999999999999'):
            sparse = [dict(e, cost_min=cost, cost_tokens=cost) for e in rows()]
            content = digest.package(sparse, NOW)[0]['text']
            for marker in ('unknown', 'none recorded', '3 unmeasured'):
                self.assertIn(marker, content)
        self.evidence('AC2', refs=3, cost_min=5.5, cost_tokens=50,
                      unmeasured_per_unit=1, historical_cost_min=15.5, no_requirement=True)

    def test_ac3_recurrence(self):
        self.seed(rows())
        self.assertEqual(self.cli().returncode, 0)
        data = self.read_back()
        path = Path(data['briefs'][0]['path'])
        prefix = (self.root / 'events/L-digest-local.jsonl').read_bytes()
        original = path.read_bytes()
        for count in (1, 2):
            event = dict(occurrence(10 + count), ts=dt.datetime.now(UTC).isoformat())
            self.seed([event])
            self.assertEqual(self.cli().returncode, 0)
            once = path.read_bytes()
            self.assertIn(f'recurred {count} times since filed'.encode(), once)
            self.assertEqual(self.cli().returncode, 0)
            self.assertEqual(path.read_bytes(), once)
            self.assertEqual(once.count(b'recurred '), 1)
            self.assertTrue(once.startswith(original))
            self.assertEqual((self.root / 'events/L-digest-local.jsonl').read_bytes(), prefix)
        # Missing canonical content is a visible CLI failure, never a replacement event.
        backup = path.with_suffix('.backup')
        path.rename(backup)
        self.assertNotEqual(self.cli().returncode, 0)
        self.assertEqual(prefix, (self.root / 'events/L-digest-local.jsonl').read_bytes())
        backup.rename(path)
        old = dict(occurrence(99), ts=(NOW - dt.timedelta(days=20)).isoformat())
        filed = dict(data['briefs'][0], ts=(NOW - dt.timedelta(days=30)).isoformat())
        self.assertIn('recurred 4 times', digest.package(rows() + [old, filed], NOW)[0]['line'])
        for bad_path in (self.root / 'content/missing.md', self.root / 'content'):
            with self.assertRaises((OSError, ValueError)):
                digest.persist([dict(kind='recurrence', path=str(bad_path),
                                     subject='problem:blocked', line='recurred 1 times since filed')])
        self.assertEqual(prefix, (self.root / 'events/L-digest-local.jsonl').read_bytes())
        self.evidence('AC3', recurrence_counts=[1, 2, 4], ledger_bytes_unchanged=True,
                      rerun_identical=True, missing_content_failed=True)

    def test_ac4_real_round_trip(self):
        hot = self.seed(rows(), actor='relay')
        # Fold's compacted-member reader, not a private glob, supplies these occurrences.
        payload = hot.read_bytes()
        segments = self.root / 'events/segments'
        segments.mkdir()
        segment = segments / 'digest-fixture.jsonl'
        hot.rename(segment)
        sha = hashlib.sha256(payload).hexdigest()
        (segments / 'digest-fixture.idx.json').write_text(json.dumps(dict(
            version=1, segment=segment.name, bytes=len(payload), sha256=sha,
            members=[dict(name=hot.name, offset=0, length=len(payload), lines=3,
                          sha256=sha, ends_nl=True)])))
        # In-process reader clears PROJECT even when fold is already imported.
        self.fold.PROJECT = 'albert-scott'
        os.environ['DOIT_PROJECT'] = 'albert-scott'
        self.assertEqual(len(digest.events()), 3)
        self.assertIsNone(self.fold.PROJECT)
        result = self.cli(project='do-it-v2')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.read_back()
        self.assertEqual(len(data['briefs']), 1)
        event = data['briefs'][0]
        self.assertEqual((event['actor'], event['project'], event['subject']),
                         ('digest', 'do-it-v2', 'problem:blocked'))
        self.assertNotIn('requirement', event)
        self.assertEqual(data['triage'], ['problem:blocked'])
        self.assertIn('problem:blocked', self.read_back('do-it-v2')['bodies'])
        self.assertEqual(self.cli(project='albert-scott').returncode, 0)
        self.assertEqual(len(self.read_back()['briefs']), 1)
        filed = dt.datetime.fromisoformat(event['ts'])
        # Subsecond occurrence after the filed second is already in the past.
        after = min(dt.datetime.now(UTC), filed + dt.timedelta(microseconds=1))
        self.seed([dict(occurrence(50), ts=after.isoformat())])
        ledger = (self.root / 'events/L-digest-local.jsonl').read_bytes()
        self.assertEqual(self.cli().returncode, 0)
        self.assertIn('recurred 1 times since filed', self.read_back()['bodies']['problem:blocked'])
        self.assertEqual((self.root / 'events/L-digest-local.jsonl').read_bytes(), ledger)
        # Other-project briefs participate in both global dedupe and the daily allowance.
        self.seed(rows('suppressed', 60))
        historical = self.root / 'content/historical.md'
        historical.write_text('Historical brief\n')
        self.seed([dict(brief('suppressed', project='other'), path=str(historical))], actor='thinker')
        self.assertEqual(self.cli(project='do-it-v2').returncode, 0)
        self.assertEqual(len(self.read_back()['briefs']), 2)
        self.seed(rows('pending', 70) + rows('also-pending', 80))
        self.seed([dict(brief('spent-slot', project='other'), path=str(historical))], actor='digest')
        self.assertEqual(self.cli(project='do-it-v2').returncode, 0)
        self.assertEqual(len(self.read_back()['briefs']), 3)
        # Concurrent first runs use a second isolated root, sharing one real ledger.
        with tempfile.TemporaryDirectory(prefix='lessons-overlap-') as temporary:
            root = Path(temporary)
            (root / 'events').mkdir()
            fixture = sum((rows(slug, start=i * 3) for i, slug in enumerate(('a', 'b', 'c'))), [])
            (root / 'events/L-planner-fixture.jsonl').write_text('\n'.join(json.dumps(e) for e in fixture) + '\n')
            env = dict(self.env, DOIT_ROOT=str(root), DOIT_PROJECT='other')
            command = [sys.executable, '-B', str(REPO / 'scripts/lessons_digest.py'), '--log', str(root / 'log')]
            processes = [subprocess.Popen(command, env=env, cwd=self.cwd, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True) for _ in range(2)]
            for process in processes:
                out, err = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, err)
            stored = [json.loads(line) for line in (root / 'events/L-digest-local.jsonl').read_text().splitlines()]
            self.assertEqual(len(stored), 2)
            self.assertEqual(len({e['subject'] for e in stored}), 2)
        # A real append destination failure leaves content and can be retried.
        with tempfile.TemporaryDirectory(prefix='lessons-retry-') as temporary:
            root = Path(temporary)
            (root / 'events').mkdir()
            (root / 'events/L-planner-fixture.jsonl').write_text('\n'.join(json.dumps(e) for e in rows()) + '\n')
            obstruction = root / 'events/L-digest-local.jsonl'
            obstruction.write_text('\n' * 4096)
            env = dict(self.env, DOIT_ROOT=str(root))
            command = [sys.executable, '-B', str(REPO / 'scripts/lessons_digest.py'), '--log', str(root / 'log')]
            def limit_file_size():
                resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))
            failed = subprocess.run(command, env=env, capture_output=True, text=True,
                                    preexec_fn=limit_file_size)
            self.assertNotEqual(failed.returncode, 0)
            self.assertTrue(list((root / 'content').glob('*.md')))
            self.assertEqual(obstruction.read_bytes(), b'\n' * 4096)
            retry = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertEqual(len([line for line in obstruction.read_text().splitlines() if line.strip()]), 1)
            self.assertEqual(len(list((root / 'content').glob('*.md'))), 1)
        self.evidence('AC4', actor='digest', project='do-it-v2', subject='problem:blocked',
                      fresh_readers=True, triage=True, cross_project_cap=True, compacted_occurrences=3,
                      concurrent_briefs=2, recurrence_read_back=True, append_failure_retry=True)

    def test_ac5_digest_and_untrusted_input(self):
        fixture = [occurrence(i) for i in range(1000)]
        content = digest.render(fixture, 14, now=NOW)
        self.assertEqual(content.count('| blocked |'), 1)
        self.assertLessEqual(len(content.splitlines()), 30)
        self.assertIn('1000', content)
        self.assertIn('pending-cap', content)
        self.assertNotIn('Signal counts', content)
        self.assertNotIn('L-spec-', content)
        manual = b'Before\r\n' + digest.START.encode() + b'old' + digest.END.encode() + b'\r\nAfter\r\n'
        self.log.write_bytes(manual)
        self.seed(rows())
        self.assertEqual(self.cli(days=1).returncode, 0)
        actual = self.log.read_bytes()
        self.assertTrue(actual.startswith(b'Before\r\n' + digest.START.encode()))
        self.assertTrue(actual.endswith(digest.END.encode() + b'\r\nAfter\r\n'))
        self.assertIn(b'last 1 days', actual)
        old = [dict(e, ts=(NOW - dt.timedelta(days=2)).isoformat()) for e in rows()]
        self.assertNotIn('| blocked |', digest.render(old, 1, now=NOW))
        self.assertIn('| blocked |', digest.render(old, 14, now=NOW))
        sentinel = self.root / 'sentinel'
        sentinel.write_text('untouched')
        hostile = '../../sentinel;$(touch injected)|[click](https://invalid)<script>'
        produced = digest.package(rows(hostile), NOW)
        produced[0]['text'] += '\n'  # Exercise text-only persistence, never a shell.
        digest.persist(produced)
        self.assertEqual(sentinel.read_text(), 'untouched')
        self.assertFalse((self.cwd / 'injected').exists())
        generated = digest.render(rows(hostile), 14, now=NOW)
        self.assertNotIn('<script>', generated)
        self.assertNotIn('[click]', generated)
        self.assertEqual(len([l for l in generated.splitlines() if l.startswith('| ')]), 2)
        link = self.root / 'content/escape.md'
        link.symlink_to(sentinel)
        for target in (str(sentinel), '../sentinel', str(link), 'content/../sentinel'):
            with self.assertRaises((OSError, ValueError)):
                digest.persist([dict(kind='recurrence', subject='problem:blocked', path=target,
                                     line='recurred 1 times since filed')])
        self.assertEqual(sentinel.read_text(), 'untouched')
        self.evidence('AC5', occurrences=1000, offender_rows=1, generated_lines=len(content.splitlines()),
                      manual_bytes_preserved=True, days_respected=True, escapes_rejected=4)

    def test_ac6_writer_guidance(self):
        blocks = []
        for role in ('planner', 'executor', 'relay', 'thinker'):
            body = (REPO / f'agents/{role}.md').read_text()
            block = body[body.index('## Lessons'):].split('\n## ', 1)[0]
            for marker in ('dysfunction', 'workaround=', 'cost_min=', 'ref=', 'problem=',
                           'never blocks', 'Thinker', 'brief', 'status', 'Working'):
                self.assertIn(marker, block, role)
            self.assertNotIn('verdict=working|', block)
            if role != 'executor':
                blocks.append(block)
        self.assertEqual(blocks[0], blocks[1])
        self.assertEqual(blocks[1], blocks[2])
        planner = (REPO / 'agents/planner.md').read_bytes().split(b'## Lessons', 1)[0]
        original = subprocess.check_output(['git', 'show', 'HEAD:agents/planner.md'], cwd=REPO)
        self.assertEqual(planner, original.split(b'## Lessons', 1)[0])
        executor = (REPO / 'agents/executor.md').read_text()
        trial = executor.split('## Sonnet trial', 1)[1].split('\n## ', 1)[0]
        self.assertNotIn('`lesson', trial)
        self.assertIn('14 days', trial)
        self.assertIn('decision', trial)
        code = '''import json, sys
sys.path.insert(0, sys.argv[1])
import fold
fold.append(['lesson', 'L-spec-historical', 'text=Historical dysfunction', 'project=do-it-v2'])
es = fold.read_events(use_cache=False)
print(json.dumps([e for e in es if e['type'] == 'lesson']))
'''
        result = subprocess.run([sys.executable, '-B', '-c', code, str(REPO / 'src')],
                                env=dict(self.env, DOIT_LEDGER_FILE='L-planner-legacy.jsonl'),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        event = json.loads(result.stdout)[0]
        self.assertNotIn('workaround', event)
        self.evidence('AC6', guidance_files=4, pane_blocks_identical=True,
                      legacy_missing_workaround_accepted=True, trial_preserved=True)


if __name__ == '__main__':
    unittest.main()
