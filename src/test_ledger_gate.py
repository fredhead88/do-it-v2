"""Scratch-only contracts for the answer gate; no live ledger is required."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import ledger_gate as gate

REPO = Path(__file__).resolve().parents[1]
BASE = '4f31d8e4612343c4c1e51d6bb9bf4fc25d61113b'
FIXED = '2026-11-05T18:00:00+00:00'


def git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    return subprocess.check_output(['git', '-C', str(repo), *args], env=env,
                                   stderr=subprocess.PIPE, text=True).strip()


def seed(root, n=20):
    (root / 'events').mkdir(parents=True)
    (root / 'content').mkdir()
    rows = []
    for i in range(n):
        kind = 'spec' if i % 2 else 'charter'
        sid = f'L-{kind}-{i + 1:04d}'
        event = {'type': 'spec-written' if kind == 'spec' else 'charter-filed',
                 'subject': sid, 'ts': f'2026-11-05T17:00:{i:02d}+00:00',
                 'title': sid, 'project': 'fixture', 'charter': None}
        actor = 'spec-writer' if kind == 'spec' else 'operator'
        with (root / 'events' / f'L-{actor}-0001.jsonl').open('a') as f:
            f.write(json.dumps(event) + '\n')
        (root / 'content' / f'{sid}.md').write_text(f'# {sid}\n\nProject: fixture\n')
        rows.append(sid)
    if n:
        with (root / 'events/L-operator-0001.jsonl').open('a') as f:
            f.write(json.dumps({'type': 'l1-complete', 'subject': rows[0],
                                'ts': '2026-11-05T17:01:00+00:00'}) + '\n')
    return rows


def answer(ids):
    return {'brief': {sid: {'rendered': 'brief', 'dossier': {'id': sid}} for sid in ids},
            'events': {sid: {'history': [{'value': 1}], 'display': [{'value': 1}]} for sid in ids},
            'states': 'states\n', 'board': 'board fold @ 20\n',
            'folded': {sid: {'id': sid, 'state': 'written'} for sid in ids},
            'counts': {'spec-written': 20}}


class TestCompare(unittest.TestCase):
    def test_each_surface_and_event_field(self):
        ids = ['L-spec-0001']
        before = answer(ids)
        self.assertEqual(gate.differences(before, copy.deepcopy(before)), [])
        for surface in before:
            with self.subTest(surface=surface):
                after = copy.deepcopy(before)
                if isinstance(after[surface], str):
                    after[surface] += 'changed'
                elif surface == 'counts':
                    after[surface]['spec-written'] = 19
                else:
                    after[surface][ids[0]] = {'changed': True}
                diff = gate.differences(before, after)
                self.assertEqual(diff[0]['surface'], surface)
                self.assertEqual(diff[0]['subject'], ids[0] if surface in gate.SUBJECT_SURFACES else None)
        after = copy.deepcopy(before)
        after['events'][ids[0]]['history'][0]['value'] = 2
        self.assertEqual(after['counts'], before['counts'])
        self.assertEqual(gate.differences(before, after)[0]['surface'], 'events')

    def test_object_order_only_is_immaterial(self):
        self.assertEqual(gate.differences({'counts': {'a': 1, 'b': 2}},
                                          {'counts': {'b': 2, 'a': 1}}), [])
        a, b = answer(['L-spec-0001']), answer(['L-spec-0001'])
        b['events']['L-spec-0001']['history'] = [{'value': 2}, {'value': 1}]
        self.assertTrue(gate.differences(a, b))


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ledger-gate-test-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve()
        self.root = self.path / 'source'
        self.ids = seed(self.root)

    def compare(self, **kw):
        return gate.compare(BASE, 20, root=self.root, repo=REPO, now=FIXED, **kw)


class TestFailure(Scratch):
    def test_bad_revision_and_counts(self):
        self.assertFalse(gate.compare('missing-revision', 1, root=self.root, repo=REPO)['success'])
        for n in (0, -1, True, '20', 21):
            self.assertFalse(gate.compare(BASE, n, root=self.root, repo=REPO)['success'])

    def test_missing_events_content_and_bad_timestamp(self):
        for missing in ('events', 'content'):
            with patch.object(gate, 'capture', side_effect=ValueError('missing ' + missing)):
                self.assertFalse(self.compare()['success'])
        (self.root / 'content' / f'{self.ids[0]}.md').unlink()
        r = self.compare()
        self.assertFalse(r['success'])
        self.assertEqual(r['ineligible'][0]['subject'], self.ids[0])
        (self.root / 'content' / f'{self.ids[0]}.md').write_text('# restored\n')
        file = self.root / 'events' / 'L-operator-0001.jsonl'
        file.write_text(file.read_text().replace('2026-11-05T17:00:00+00:00', 'invalid'))
        self.assertFalse(self.compare()['success'])

    def test_materialize_child_and_partial_failures(self):
        for operation in ('materialize', 'evaluate'):
            with patch.object(gate, operation, side_effect=RuntimeError(operation)):
                self.assertFalse(self.compare()['success'])
        for outcome in ({}, {'brief': {}}, answer(self.ids[:1])):
            with patch.object(gate, 'evaluate', return_value=outcome):
                r = self.compare()
                self.assertFalse(r['success'])
                self.assertTrue(r['uncomparable'])

    def test_child_exit_timeout_and_malformed_output(self):
        with tempfile.TemporaryDirectory(dir=self.path) as scratch:
            work = Path(scratch)
            for code in ('raise SystemExit(3)', 'import time; time.sleep(3)', 'print("bad json")'):
                script = work / 'child.py'
                script.write_text(code)
                with self.assertRaises((RuntimeError, TimeoutError)):
                    gate.run_child([sys.executable, str(script)], cwd=work,
                                   env=os.environ.copy(), timeout=.2)

    def test_cli_failure_is_nonzero(self):
        p = subprocess.run([sys.executable, str(REPO / 'src/ledger_gate.py'), 'compare',
                            '--before', 'missing-revision', '--subjects', '20',
                            '--root', str(self.root)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertFalse(json.loads(p.stdout)['success'])


class TestIsolation(Scratch):
    def test_private_workspace_under_aliased_tmpdir(self):
        alias = self.path / 'tmp-alias'
        alias.symlink_to(self.path, target_is_directory=True)
        with patch.object(tempfile, 'tempdir', str(alias)):
            result = self.compare()
        self.assertTrue(result['success'], result['uncomparable'])
        seed(self.path / 'nested' / 'source', 1)
        with self.assertRaisesRegex(ValueError, 'aliased capture path'):
            gate.capture(alias / 'nested' / 'source')

    def test_capture_is_immutable_and_virtual_provenance_is_preserved(self):
        snap = gate.capture(self.root)
        selected, excluded = gate.select_subjects(snap, 20)
        self.assertEqual(set(selected), set(self.ids))
        self.assertEqual(excluded, [])
        manifest = gate.manifest(self.root)
        with patch.object(gate, 'evaluate', side_effect=RuntimeError('deliberate')):
            self.assertFalse(self.compare()['success'])
        self.assertEqual(gate.manifest(self.root), manifest)
        (self.root / 'events' / 'L-operator-0001.jsonl').write_text('changed after capture')
        self.assertEqual(gate.select_subjects(snap, 20)[0], selected)
        # A sealed member must keep its original actor/line provenance.
        other = self.path / 'packed'
        seed(other, 1)
        member = other / 'events/L-operator-0001.jsonl'
        data = member.read_bytes()
        segments = other / 'events/segments'
        segments.mkdir()
        (segments / 'one.jsonl').write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        index = {'version': 1, 'segment': 'one.jsonl', 'bytes': len(data), 'sha256': sha,
                 'members': [{'name': member.name, 'offset': 0, 'length': len(data),
                              'lines': 2, 'sha256': sha, 'ends_nl': True}]}
        (segments / 'one.idx.json').write_text(json.dumps(index))
        member.unlink()
        packed = gate.capture(other)
        self.assertEqual(packed['sources'][member.name], data.decode().splitlines())

    def test_real_clocks_descendant_and_hostile_environment(self):
        manifest = gate.manifest(self.root)
        live_home = self.path / 'live-home'
        for relative in ('.config/do-it/backup.toml', '.claude/sessions/99999.json'):
            sentinel = live_home / relative
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text('LIVE SENTINEL: must never be read')
        live_manifest = gate.manifest(live_home)
        hostile = {'DOIT_ROOT': str(self.root), 'DOIT_PROJECT': 'wrong',
                   'DOIT_LEDGER_FILE': 'live.jsonl', 'DOIT_READ_CACHE': '1',
                   'PYTHONPATH': '/hostile', 'HOME': str(live_home),
                   'XDG_CONFIG_HOME': str(live_home / '.config'),
                   'DOIT_SESSIONS_DIR': str(live_home / '.claude/sessions'),
                   'V4_LEDGER_DIR': str(self.root), 'DOIT_K': '100'}
        with patch.dict(os.environ, hostile):
            r = self.compare()
        self.assertTrue(r['success'], r['uncomparable'])
        self.assertEqual(gate.manifest(self.root), manifest)
        self.assertEqual(gate.manifest(live_home), live_manifest)
        with tempfile.TemporaryDirectory(dir=self.path) as name:
            space = Path(name)
            revision = space / 'revision'
            gate.materialize(REPO, BASE, revision)
            snap = gate.capture(self.root)
            ids, _ = gate.select_subjects(snap, 20)
            workspace = space / 'evaluation'
            # Evaluation uses identical private path, a fixed external clock,
            # and actual proving_lookup children for the charter briefs.
            a = gate.evaluate(revision, snap, ids, FIXED, workspace)
            with patch.object(gate.time, 'time', return_value=gate.time.time() + 180):
                b = gate.evaluate(revision, snap, ids, FIXED, workspace)
            self.assertEqual(a['board'].encode(), b['board'].encode())
            self.assertIn(FIXED, a['board'])
            self.assertIn('proving', a['brief'][ids[0]]['dossier']['status']['state'].lower())
            # Seed note/backup ages in a scratch capture variant, never live state.
            snap2 = copy.deepcopy(snap)
            note = {'type': 'inbound-note-listed', 'subject': 'https://example.invalid/1', 'ts': FIXED,
                    'source': 'https://example.invalid/1', 'title': 'clock',
                    'opened_at': '2026-11-03T18:00:00+00:00'}
            snap2['files']['events/L-intake-note.jsonl'] = (json.dumps(note) + '\n').encode()
            snap2['files']['backup-state.json'] = json.dumps({
                'ts': '2026-11-05T17:00:00+00:00', 'ok': True,
                'snapshot': 'scratch', 'remote': 'scratch'}).encode()
            c = gate.evaluate(revision, snap2, ids, FIXED, workspace)
            with patch.object(gate.time, 'time', return_value=gate.time.time() + 180):
                d = gate.evaluate(revision, snap2, ids, FIXED, workspace)
            self.assertEqual(c['board'], d['board'])
            self.assertIn('48h', c['board'])
            self.assertIn('mirror: last push 60m ago', c['board'])

    def test_source_read_sentinels_and_symlinks(self):
        with tempfile.TemporaryDirectory(dir=self.path) as name:
            space = Path(name)
            revision = space / 'revision'
            gate.materialize(REPO, BASE, revision)
            snap = gate.capture(self.root)
            ids, _ = gate.select_subjects(snap, 20)
            for module, expression in [('notes', 'root()'), ('carry', 'root()'),
                                       ('intake', 'root()')]:
                path = revision / 'src' / f'{module}.py'
                original = path.read_text()
                path.write_text(original + f'\nassert str({expression}) != {str(self.root)!r}\n')
            # A forbidden read can be caught by production code and still must fail.
            proving = revision / 'src/proving.py'
            proving.write_text(proving.read_text().replace(
                'if __name__ == "__main__":',
                'if __name__ == "__main__":\n    open(' +
                repr(str(self.root / 'content' / (ids[0] + '.md'))) + ').read()\n', 1))
            with self.assertRaisesRegex(RuntimeError, 'isolation'):
                gate.evaluate(revision, snap, ids, FIXED, space / 'eval')
        (self.root / 'content/link.md').symlink_to(self.root / 'content' / f'{self.ids[0]}.md')
        self.assertNotIn('content/link.md', gate.capture(self.root)['files'])
        (self.root / 'events/link.jsonl').symlink_to(self.root / 'events/L-operator-0001.jsonl')
        with self.assertRaises(ValueError):
            gate.capture(self.root)


class MeterScratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ledger-meter-')
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / 'samples.jsonl'
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def rows(self, rows):
        self.file.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def row(self, seconds, wall=30, cpu=30):
        return {'ts': (self.start + timedelta(seconds=seconds)).isoformat(),
                'wall': wall, 'by': {'ledger-bookkeeping': cpu}}

    def meter(self, end, start=None):
        return gate.meter(start or self.start.isoformat(),
                          (self.start + timedelta(seconds=end)).isoformat(), samples=self.file)


class TestMeterArithmetic(MeterScratch):
    def test_weighted_sparse_and_inclusive_offsets(self):
        a, b, c = self.row(0, 10, 5), self.row(30, 30, 60), self.row(90, 60)
        c['by'] = {'other': 9}
        b['ts'] = '2025-12-31T19:00:30-05:00'
        self.rows([a, b, c])
        r = self.meter(90)
        self.assertTrue(r['success'], r)
        self.assertEqual(r['sample_count'], 3)
        self.assertEqual(r['summed_wall_seconds'], 100)
        self.assertEqual(r['bookkeeping_cpu_seconds'], 65)
        self.assertEqual(r['cpu_s_per_hour'], 2340)
        self.assertAlmostEqual(r['percent_change'], 100 * (2340 / 7645 - 1))

    def test_observed_aggregate_is_not_integer_baseline(self):
        self.rows([self.row(33147.8, 33147.8, 70394.75)])
        r = self.meter(33147.8)
        self.assertAlmostEqual(r['cpu_s_per_hour'], 7645.186105865246)
        self.assertEqual(r['baseline'], 7645)
        self.assertGreater(r['percent_change'], 0)
        self.assertFalse(r['success'])

    def test_bar_is_unrounded(self):
        for rate in (5349.99999, 5350, 5350.00001):
            self.rows([self.row(60, 60, rate / 60)])
            r = self.meter(60)
            self.assertEqual(r['at_or_under_bar'], rate <= 5350)
            self.assertEqual(r['success'], rate <= 5350)


class TestMeterFailure(MeterScratch):
    def test_invalid_boundaries_and_missing_file(self):
        for a, b in ((None, None), ('bad', FIXED), ('2026-01-01', FIXED),
                     (FIXED, FIXED), (FIXED, '2025-01-01T00:00:00Z')):
            self.assertFalse(gate.meter(a, b, samples=self.file)['success'])
        self.assertFalse(self.meter(60)['success'])
        self.file.write_text('')
        self.assertFalse(self.meter(60)['success'])
        self.file.write_text('not json\n')
        self.assertFalse(self.meter(60)['success'])

    def test_invalid_rows(self):
        valid = self.row(30)
        bad = [None, [], {}, {**valid, 'ts': '2026-01-01'},
               *[{k: v for k, v in valid.items() if k != missing} for missing in valid],
               *[{**valid, 'wall': v} for v in (0, -1, True, float('nan'), float('inf'), '30')],
               *[{**valid, 'by': v} for v in (None, [], 0)],
               *[{**valid, 'by': {'other': v}} for v in (-1, True, float('nan'), float('inf'), '1')]]
        for row in bad:
            with self.subTest(row=row):
                self.rows([row])
                self.assertFalse(self.meter(60)['success'])
        for rows in ([valid, valid], [self.row(60), valid]):
            self.rows(rows)
            self.assertFalse(self.meter(60)['success'])

    def test_coverage_gap_overlap_and_complete_day(self):
        self.rows([self.row(30), self.row(77), self.row(107)])
        r = self.meter(107)
        self.assertTrue(r['success'], r)
        self.assertEqual(r['total_uncovered_seconds'], 17)
        self.assertEqual(r['maximum_interior_uncovered_gap'], 17)
        self.assertEqual(r['coverage_tolerance_seconds'], 30)
        self.rows([self.row(30), self.row(91), self.row(121)])
        self.assertFalse(self.meter(121)['success'])
        self.rows([self.row(30), self.row(40), self.row(60)])
        self.assertEqual(self.meter(60)['overlap_seconds'], 30)
        self.rows([self.row(s, 3600, 100) for s in range(3600, 86401, 3600)])
        self.assertTrue(self.meter(86400)['complete_day'])
        self.rows([self.row(3600, 3600, 100)])
        r = self.meter(86400)
        self.assertFalse(r['complete_day'])
        self.assertFalse(r['success'])

    def test_meter_cli_exit_codes(self):
        for cpu, expected in ((30, 0), (90, 1)):
            self.rows([self.row(30, 30, cpu)])
            p = subprocess.run([sys.executable, str(REPO / 'src/ledger_gate.py'), 'meter',
                                '--from', self.start.isoformat(), '--to', self.row(30)['ts'],
                                '--samples', str(self.file)], capture_output=True, text=True)
            self.assertEqual(p.returncode, expected, p.stderr)
            self.assertEqual(json.loads(p.stdout)['success'], expected == 0)


class TestRevisionIntegration(Scratch):
    def test_actual_base_and_current_twenty_subjects_api_and_cli(self):
        r = self.compare()
        self.assertTrue(r['success'], r['uncomparable'])
        self.assertEqual(r['completed_count'], 20)
        self.assertEqual(len(r['surfaces']), 6)
        p = subprocess.run([sys.executable, str(REPO / 'src/ledger_gate.py'), 'compare',
                            '--before', BASE, '--subjects', '20', '--root', str(self.root),
                            '--now', FIXED], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)['completed_count'], 20)

    def test_real_changed_answer_and_uncomparable_subject(self):
        with tempfile.TemporaryDirectory(dir=self.path) as name:
            revision = Path(name) / 'revision'
            gate.materialize(REPO, BASE, revision)
            brief = revision / 'src/brief.py'
            text = brief.read_text()
            brief.write_text(text.replace('def render(d):', 'def render(d):\n    return "changed answer"'))
            r = self.compare(current=revision)
            self.assertFalse(r['success'])
            self.assertIn('brief', {d['surface'] for d in r['differences']})
            brief.write_text(text.replace('def dossier(item_id, events, root, now):',
                                         'def dossier(item_id, events, root, now):\n    raise UnknownItem(item_id)'))
            r = self.compare(current=revision)
            self.assertFalse(r['success'])
            self.assertTrue(r['uncomparable'])
            self.assertEqual(r['selected_count'], 20)
            self.assertEqual(r['completed_count'], 0)


class TestForwardSignature(Scratch):
    def test_synthetic_keyword_revisions_keep_full_history_and_cli_observability(self):
        # Clearly synthetic contract commits: no shipped R6 claim.
        dead = {'type': 'tree-reaped', 'subject': 'scratch-dead', 'ts': FIXED}
        (self.root / 'events/L-executor-0001.jsonl').write_text(json.dumps(dead) + '\n')
        with tempfile.TemporaryDirectory(dir=self.path) as name:
            revision = Path(name) / 'synthetic'
            gate.materialize(REPO, BASE, revision)
            git(revision, 'init', '-q')
            git(revision, 'config', 'user.name', 'Scratch fixture')
            git(revision, 'config', 'user.email', 'fixture@example.invalid')
            fold = revision / 'src/fold.py'
            text = fold.read_text().replace('def read_events(use_cache=True, *, snapshot=None):',
                                            'def _reference_reader(use_cache=True, *, snapshot=None):')
            wrapper = '''\ndef read_events(use_cache=True, *, snapshot=None, with_reaped=False):
    rows = _reference_reader(use_cache=use_cache, snapshot=snapshot)
    return rows if with_reaped else [e for e in rows if e.get('type') != 'tree-reaped']
\n'''
            text = text.replace('if __name__ == "__main__":', wrapper + 'if __name__ == "__main__":', 1)
            fold.write_text(text)
            git(revision, 'add', '.')
            git(revision, 'commit', '-qm', 'Synthetic forward reader; displayed count deliberately loses history')
            snap = gate.capture(self.root)
            ids, _ = gate.select_subjects(snap, 20)
            a = gate.evaluate(revision, snap, ids, FIXED, Path(name) / 'eval')
            self.assertEqual(a['counts']['tree-reaped'], 1)
            r = self.compare(current=revision)
            self.assertFalse(r['success'])
            self.assertEqual({d['surface'] for d in r['differences']}, {'board'})
            # Preservation belongs to the synthetic revision's CLI, never the gate.
            text = text.replace('ev = read_events()\n    specs, charters',
                                'ev = read_events(with_reaped=True)\n    specs, charters')
            fold.write_text(text)
            git(revision, 'add', 'src/fold.py')
            git(revision, 'commit', '-qm', 'Synthetic CLI preserves history presentation')
            r = self.compare(current=revision)
            self.assertTrue(r['success'], r)
            fold.write_text(text.replace('rows = _reference_reader(use_cache=use_cache, snapshot=snapshot)',
                                         'raise TypeError("internal reader failure")'))
            r = self.compare(current=revision)
            self.assertFalse(r['success'])
            self.assertTrue(r['uncomparable'])


if __name__ == '__main__':
    unittest.main()
