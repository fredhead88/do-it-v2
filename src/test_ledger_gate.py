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
    (root / 'repos' / 'fixture').mkdir(parents=True)
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


_raw_differences = gate.differences


def differences_with_tick_owner(before, current):
    """Allow only SD6a's /folded/<charter>/proving/items/<n>/owner change."""
    expected = copy.deepcopy(before)
    for cid, charter in expected.get('folded', {}).items():
        if not cid.startswith('L-charter-'):
            continue
        old_items = (charter.get('proving') or {}).get('items', [])
        new_items = (current.get('folded', {}).get(cid, {}).get('proving') or {}).get('items', [])
        for index, item in enumerate(old_items):
            if (index < len(new_items) and item.get('kind') == 'charter-review'
                    and item.get('owner') == 'executor' and new_items[index].get('owner') == 'tick'):
                item['owner'] = 'tick'
    return _raw_differences(expected, current)


class TestCompare(unittest.TestCase):
    def test_only_charter_review_owner_transition_is_expected(self):
        cid = 'L-charter-0001'
        before = answer([cid])
        before['folded'][cid]['proving'] = {
            'items': [{'kind': 'charter-review', 'owner': 'executor', 'text': 'review'}]}
        after = copy.deepcopy(before)
        after['folded'][cid]['proving']['items'][0]['owner'] = 'tick'
        self.assertEqual(differences_with_tick_owner(before, after), [])
        self.assertTrue(gate.differences(before, after))  # production gate remains strict
        for label in ('text', 'kind', 'state', 'brief', 'new-item', 'reverse', 'spec', 'other-kind',
                      'null-proving'):
            with self.subTest(change=label):
                left, right = copy.deepcopy(before), copy.deepcopy(after)
                item = right['folded'][cid]['proving']['items'][0]
                if label in ('text', 'kind'):
                    item[label] = 'changed'
                elif label == 'state':
                    right['folded'][cid]['state'] = 'changed'
                elif label == 'brief':
                    right['brief'][cid]['rendered'] = 'changed'
                elif label == 'new-item':
                    right['folded'][cid]['proving']['items'].append(dict(item))
                elif label == 'reverse':
                    left, right = right, left
                elif label == 'spec':
                    for value in (left, right):
                        value['folded']['L-spec-0001'] = value['folded'].pop(cid)
                elif label == 'null-proving':
                    right['folded'][cid]['proving'] = None
                else:
                    left['folded'][cid]['proving']['items'][0]['kind'] = 'spec-review'
                    item['kind'] = 'spec-review'
                self.assertTrue(differences_with_tick_owner(left, right), label)

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
        def compare_answers(before, current):
            self.comparison_answers = (before, current)
            return differences_with_tick_owner(before, current)
        with patch.object(gate, 'differences', side_effect=compare_answers):
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
    def test_registered_project_names_are_captured_and_replayed_without_repo_contents(self):
        repos = self.root / 'repos'
        external = self.path / 'external-repo'
        external.mkdir()
        (external / 'private.txt').write_text('must not be copied')
        (repos / 'alias').symlink_to(external, target_is_directory=True)
        (repos / 'dangling').symlink_to(self.path / 'missing')
        (repos / 'file').write_text('not a registration')
        snap = gate.capture(self.root)
        self.assertEqual(snap['registered_projects'], ['alias', 'fixture'])
        self.assertFalse(any(name.startswith('repos/') for name in snap['files']))
        (repos / 'alias').unlink()
        changed = gate.capture(self.root)
        self.assertNotEqual(snap['identity'], changed['identity'])
        ids, _ = gate.select_subjects(snap, 20)
        workspace = self.path / 'evaluation'

        def inspect_replay(*args, **kwargs):
            replay = workspace / 'root' / 'repos'
            self.assertEqual(sorted(p.name for p in replay.iterdir()), ['alias', 'fixture'])
            self.assertTrue(all(p.is_dir() and not p.is_symlink() and not list(p.iterdir())
                                for p in replay.iterdir()))
            return answer(ids)

        with patch.object(gate, 'run_child', side_effect=inspect_replay):
            gate.evaluate(REPO, snap, ids, FIXED, workspace)

    def test_private_workspace_under_aliased_tmpdir(self):
        # Compare post-planning advice across revisions, preserving isolation checks.
        (self.root / 'events/L-planner-0001.jsonl').write_text(''.join(
            json.dumps({'type': 'plan-written', 'subject': sid, 'ts': FIXED}) + '\n'
            for sid in self.ids if sid.startswith('L-charter-')))
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
        # Keep the proving/clock fixture planned in both evaluated revisions.
        (self.root / 'events/L-planner-0001.jsonl').write_text(''.join(
            json.dumps({'type': 'plan-written', 'subject': sid, 'ts': FIXED}) + '\n'
            for sid in self.ids if sid.startswith('L-charter-')))
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
        # Exercise unchanged post-planning answers with the full strict comparison.
        (self.root / 'events/L-planner-0001.jsonl').write_text(''.join(
            json.dumps({'type': 'plan-written', 'subject': sid, 'ts': FIXED}) + '\n'
            for sid in self.ids if sid.startswith('L-charter-')))
        r = self.compare()
        self.assertTrue(r['success'], r['uncomparable'])
        self.assertEqual(r['completed_count'], 20)
        self.assertEqual(len(r['surfaces']), 6)
        p = subprocess.run([sys.executable, str(REPO / 'src/ledger_gate.py'), 'compare',
                            '--before', BASE, '--subjects', '20', '--root', str(self.root),
                            '--now', FIXED], cwd=REPO, capture_output=True, text=True)
        # The unmodified production CLI must still report this intentional
        # ownership change. The API comparison above checked its exact paths.
        expected = gate.differences(*self.comparison_answers)
        self.assertEqual(p.returncode, int(bool(expected)), p.stdout + p.stderr)
        result = json.loads(p.stdout)
        self.assertEqual(result['differences'], expected)
        self.assertEqual(result['success'], not expected)
        self.assertEqual(result['completed_count'], 20)

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


class TestSurfaceSelection(Scratch):
    def test_invalid_selection_timeout_and_partial_answers(self):
        for surfaces in ((), ('brief', 'brief'), ('unknown',), 'brief'):
            self.assertFalse(self.compare(surfaces=surfaces)['success'])
        for timeout in (True, 0, -1, float('nan'), float('inf')):
            self.assertFalse(self.compare(timeout=timeout)['success'])
        for value in ({}, {'brief': {}}, {'brief': {self.ids[0]: {}}}):
            with patch.object(gate, 'evaluate', return_value=value):
                result = self.compare(surfaces=('brief',))
                self.assertFalse(result['success'])
                self.assertEqual(result['completed_count'], 0)
                self.assertTrue(result['uncomparable'])

    def test_timeout_forwarded_and_exact_surfaces(self):
        def evaluate(*args, **kw):
            self.assertEqual(kw, {'surfaces': ('brief',), 'timeout': 123})
            return {'brief': answer(args[2])['brief']}
        with patch.object(gate, 'evaluate', side_effect=evaluate) as worker:
            result = self.compare(surfaces=('brief',), timeout=123)
        self.assertEqual(worker.call_count, 2)
        self.assertTrue(result['success'], result)
        self.assertEqual([r['surface'] for r in result['surfaces']], ['brief'])
        print('SURFACES enumeration: ' + ', '.join(gate.SURFACES))

    def test_real_worker_omits_board_and_changed_brief_fails(self):
        revision = self.path / 'revision'
        gate.materialize(REPO, BASE, revision)
        fold = revision / 'src/fold.py'
        # Any CLI invocation of fold (board, states or events) is forbidden.
        fold.write_text(fold.read_text().replace('if __name__ == "__main__":',
            'if __name__ == "__main__":\n    raise RuntimeError("forbidden board sentinel")', 1))
        snap = gate.capture(self.root)
        before_manifest = gate.manifest(self.root)
        selected = self.ids[:2]
        actual = gate.evaluate(revision, snap, selected, FIXED, self.path / 'eval',
                               surfaces=('brief',))
        self.assertEqual(set(actual), {'brief'})
        gate.validate_answers(actual, selected, surfaces=('brief',))
        # Same clock/root, actual CLI versus shared-render answer, in isolation.
        brief = revision / 'src/brief.py'
        original = brief.read_text()
        brief.write_text(original + '\n_original_dossier = dossier\n'
            'def dossier(*args, **kwargs):\n'
            '    d = _original_dossier(*args, **kwargs)\n'
            '    assert _gate_cli_bytes(args[0]) == render(d) + "\\n"\n'
            '    return d\n'
            'def _gate_cli_bytes(subject):\n'
            '    from gate_runtime import _cli\n'
            '    return _cli(__file__, [subject])\n')
        # runpy __main__ does not reach the wrapper until after CLI rendering.
        cli_checked = gate.evaluate(revision, snap, selected, FIXED, self.path / 'eval',
                                    surfaces=('brief',))
        self.assertEqual(actual, cli_checked)
        brief.write_text(original.replace('def render(d):',
            'def render(d):\n    return "changed brief bytes"'))
        result = self.compare(current=revision, surfaces=('brief',))
        self.assertFalse(result['success'])
        self.assertTrue(result['differences'])
        self.assertEqual({d['surface'] for d in result['differences']}, {'brief'})
        self.assertEqual(gate.manifest(self.root), before_manifest)

    def test_actual_each_surface_matches_legacy_and_bounded_child_defaults(self):
        snapshot = gate.capture(self.root)
        ids = self.ids[:1]
        workspace = self.path / 'eval'
        legacy = gate.evaluate(REPO, snapshot, ids, FIXED, workspace)
        self.assertEqual(tuple(legacy), tuple(sorted(gate.SURFACES)))
        for surface in gate.SURFACES:
            with self.subTest(surface=surface):
                selected = gate.evaluate(REPO, snapshot, ids, FIXED, workspace,
                                         surfaces=(surface,))
                self.assertEqual(selected, {surface: legacy[surface]})
        for kw, expected in (({},90), ({'surfaces':('brief',),'timeout':900},900)):
            value = legacy if not kw else {'brief': legacy['brief']}
            with patch.object(gate, 'run_child', return_value=value) as child:
                gate.evaluate(REPO, snapshot, ids, FIXED, workspace, **kw)
            self.assertEqual(child.call_args.kwargs['timeout'],expected)

    def test_requested_isolation_failure_and_timeout_fail_closed(self):
        revision = self.path / 'revision'
        gate.materialize(REPO, BASE, revision)
        brief = revision / 'src/brief.py'
        original = brief.read_text()
        for expression in ('open("/home/forbidden-live-config").read()',
                           '__import__("socket").socket().connect(("127.0.0.1", 1))',
                           '__import__("subprocess").run(["claude", "--version"])',
                           '__import__("time").sleep(2)'):
            brief.write_text(original.replace('def render(d):',
                'def render(d):\n    ' + expression))
            result = self.compare(current=revision, surfaces=('brief',), timeout=.5)
            self.assertFalse(result['success'], expression)
            self.assertTrue(result['uncomparable'])


class JobScratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='job-meter-test-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.root = self.path / 'root'
        seed(self.root, 1)
        self.samples = self.path / 'samples.jsonl'
        self.logs = self.path / 'cron'
        self.logs.mkdir()
        self.current = datetime(2026, 11, 8, tzinfo=timezone.utc)
        self.first = datetime(2026, 11, 7, tzinfo=timezone.utc)
        self.baseline = datetime(2026, 11, 5, tzinfo=timezone.utc)
        self.env = patch.dict(os.environ, {'DOIT_ROOT': str(self.root),
            'LEDGER_GATE_SAMPLES': str(self.samples),
            'LEDGER_GATE_CRON_RUNS_DIR': str(self.logs),
            'LEDGER_GATE_REPO': str(REPO)})
        self.env.start()
        self.addCleanup(self.env.stop)
        # Real admitted canonical ledger provenance, with a locally resolvable parent.
        sha = git(REPO, 'rev-parse', 'HEAD')
        self.merges = [dict(subject='L-spec-9001', charter='L-charter-0086',
                            ts=self.first.isoformat(), sha=sha),
                       dict(subject='L-spec-9002', charter='L-charter-0086',
                            ts=(self.current-timedelta(hours=1)).isoformat(), sha=sha),
                       dict(subject='L-spec-9003', charter='L-charter-0079',
                            ts=(self.current+timedelta(seconds=10)).isoformat(), sha=sha),
                       dict(subject='L-spec-9004', charter='L-charter-0084',
                            ts=(self.current+timedelta(seconds=40)).isoformat(), sha=sha)]
        for merge in self.merges:
            self.append(self.root / 'events/L-spec-writer-0002.jsonl',
                        dict(merge, type='spec-written', project='fixture'))
            self.append(self.root / 'events/L-executor-0001.jsonl',
                        dict(merge, type='shipped', project='fixture'))
        self.rows = []
        for begin, factor in ((self.baseline, 4), (self.first-timedelta(days=1), 2),
                              (self.current, 1)):
            for offset, wall, tick, pre in ((10, 10, 5, 2), (40, 30, 15, 6)):
                self.rows.append(dict(ts=(begin+timedelta(seconds=offset)).isoformat(),
                    wall=wall, by={'ledger-bookkeeping': 500},
                    ledger=[dict(cpu=tick*factor, cmd='python src/tick.py wait')],
                    top=[dict(cpu=tick*factor, cmd='python src/tick.py wait', cat='ledger-bookkeeping'),
                         dict(cpu=pre*factor, cmd='python src/spec_preflight.py')]))
        self.write_rows()
        for job in ('tick', 'spec_preflight'):
            file = self.logs / ('spec-preflight.jsonl' if job == 'spec_preflight' else 'tick.jsonl')
            records = [dict(ev='coverage', start=self.baseline.isoformat(),
                end=(self.current+timedelta(seconds=40)).isoformat(), continuous=True,
                source='operator-preserved fixture')]
            for begin in (self.baseline, self.first-timedelta(days=1), self.current):
                for offset in (1, 11):
                    start = (begin+timedelta(seconds=offset)).isoformat()
                    records += [dict(ev='start', ts=start, pid=offset),
                                dict(ev='end', ts=(begin+timedelta(seconds=offset+3)).isoformat(),
                                     start_ts=start, duration_s=3, rc=0)]
            file.write_text(''.join(json.dumps(r)+'\n' for r in records))

    @staticmethod
    def append(path, row):
        with path.open('a') as stream:
            stream.write(json.dumps(row)+'\n')

    def write_rows(self):
        self.samples.write_text(''.join(json.dumps(r)+'\n' for r in self.rows))

    def report(self):
        return gate.job_meter(self.current.isoformat(),
            (self.current+timedelta(seconds=40)).isoformat(), ['tick', 'spec_preflight'])


class TestJobMeterArithmetic(JobScratch):
    def test_three_windows_weighted_rates_passes_and_canonical_merges(self):
        manifest = gate.manifest(self.path)
        result = self.report()
        self.assertTrue(result['success'], result['reasons'])
        current = result['windows']['current']
        for job, cpu, rate, per_pass in (('tick',20,43200,10), ('spec_preflight',8,17280,4)):
            row = current['jobs'][job]
            self.assertEqual((row['observed_cpu_s'],row['cpu_s_per_86400_sampled_wall'],
                              row['passes'],row['cpu_s_per_pass']), (cpu,rate,2,per_pass))
            self.assertEqual(row['cpu_quality'], 'lower-bound')
            self.assertEqual(result['windows']['baseline']['jobs'][job]['observed_cpu_s'],cpu*4)
            self.assertEqual(result['windows']['control']['jobs'][job]['observed_cpu_s'],cpu*2)
            self.assertEqual(result['changes'][job]['baseline']['percent'], -75)
            self.assertEqual(result['changes'][job]['control']['percent'], -50)
        self.assertEqual(current['sampled_wall_s'],40)
        self.assertTrue(current['coverage']['complete_window'])
        self.assertFalse(result['windows']['baseline']['coverage']['complete_window'])
        self.assertEqual([m['charter'] for m in current['merges']],
                         ['L-charter-0079','L-charter-0084'])
        self.assertEqual(result['provenance']['first_merge']['subject'], 'L-spec-9001')
        self.assertEqual(result['provenance']['last_merge']['subject'], 'L-spec-9002')
        self.assertEqual(result['provenance']['first_merge']['parent_sha'],git(REPO,'rev-parse','HEAD^'))
        self.assertIsNone(result['charter_bar_met'])
        self.assertEqual(gate.manifest(self.path),manifest)
        print('weighted rates: tick=43200/day, 10/pass; spec_preflight=17280/day, 4/pass')

    def test_start_excluded_end_included_and_ring_truncation(self):
        self.rows.append(dict(self.rows[-1], ts=self.current.isoformat(), wall=1))
        self.rows.sort(key=lambda r:r['ts'])
        self.write_rows()
        result=self.report()
        self.assertEqual(result['windows']['current']['sample_count'],2)
        for path in self.logs.iterdir():
            lines=[json.loads(l) for l in path.read_text().splitlines()]
            path.write_text(''.join(json.dumps(r)+'\n' for r in lines
                if r['ev'] != 'coverage' and gate.utc(r['ts']) > self.current))
        result=self.report()
        for row in result['windows']['current']['jobs'].values():
            self.assertEqual(row['known_completions'],2)
            self.assertIsNone(row['passes'])
            self.assertIsNone(row['cpu_s_per_pass'])
        self.assertFalse(result['success'])

    def test_cli_and_legacy_meter(self):
        p=subprocess.run([sys.executable,str(REPO/'src/ledger_gate.py'),'job-meter',
            '--from',self.current.isoformat(),'--to',(self.current+timedelta(seconds=40)).isoformat(),
            '--jobs','tick','spec_preflight'],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr+p.stdout)
        self.assertEqual(json.loads(p.stdout)['windows']['current']['jobs']['tick']['passes'],2)
        self.assertEqual(gate.meter(self.current.isoformat(),
            (self.current+timedelta(seconds=40)).isoformat(), samples=self.samples)['bar'],5350)


class TestJobMeterEvidence(JobScratch):
    def test_full_ledger_still_lower_bound_missing_match_unknown_and_no_double_count(self):
        r=self.report()
        self.assertEqual(r['windows']['current']['jobs']['tick']['observed_cpu_s'],20)
        for row in self.rows:
            row['top']=[e for e in row['top'] if 'tick.py' in e['cmd']]
        self.write_rows()
        r=self.report()
        job=r['windows']['current']['jobs']['spec_preflight']
        self.assertIsNone(job['observed_cpu_s'])
        self.assertEqual(job['cpu_quality'],'unknown')
        self.assertEqual(job['wall_fallback']['wall_s_per_known_completed_pass'],3)
        self.assertEqual(job['wall_fallback']['known_completions'],2)
        self.assertFalse(r['success'])
        self.assertIsNone(r['charter_bar_met'])
        print('command matches remain lower-bound; missing CPU is unknown; wall fallback is separate')

    def test_missing_logs_unmatched_failed_and_zero_complete(self):
        file=self.logs/'tick.jsonl'
        file.unlink()
        r=self.report()['windows']['current']['jobs']['tick']
        self.assertIsNone(r['passes'])
        self.assertIsNone(r['cpu_s_per_pass'])
        start=(self.current-timedelta(seconds=1)).isoformat()
        self.append(file,dict(ev='coverage',start=start,
            end=(self.current+timedelta(seconds=40)).isoformat(),continuous=True,source='fixture'))
        self.append(file,dict(ev='start',ts=self.current.isoformat(),pid=1))
        r=self.report()['windows']['current']['jobs']['tick']
        self.assertEqual(r['passes'],0)
        self.assertEqual(r['unfinished_invocations'],1)
        self.assertIsNone(r['cpu_s_per_pass'])
        self.append(file,dict(ev='end',ts=(self.current+timedelta(seconds=5)).isoformat(),
                            start_ts=start,rc=2,duration_s=6))
        r=self.report()['windows']['current']['jobs']['tick']
        self.assertEqual(r['failed_completions'],1)
        self.assertEqual(r['unmatched_ends'],1)
        self.assertEqual(r['boundary_crossing_completions'],1)

    def test_absent_control_anchor_and_short_coverage(self):
        (self.root/'events/L-executor-0001.jsonl').unlink()
        r=self.report()
        self.assertIsNone(r['windows']['control'])
        self.assertIsNone(r['provenance']['first_merge'])
        self.assertFalse(r['success'])
        self.assertIsNone(r['changes']['tick']['control']['delta'])
        self.rows=self.rows[-1:]; self.rows[0]["wall"]=1; self.write_rows()
        r=self.report()
        self.assertFalse(r['windows']['current']['coverage']['complete_window'])

    def test_bad_samples_fail_clearly(self):
        good=copy.deepcopy(self.rows)
        for field,value in (('wall',True),('wall',0),('wall',-1),('wall',float('inf')),
                            ('ts','2026-11-08T00:00:10'),('ts','bad'),('by',[]),
                            ('ledger',[dict(cpu=-1,cmd='tick.py')]),
                            ('top',[dict(cpu=True,cmd='spec_preflight.py')]),
                            ('ledger',[dict(cpu=float('nan'),cmd='tick.py')])):
            with self.subTest(field=field,value=value):
                self.rows=copy.deepcopy(good); self.rows[-1][field]=value; self.write_rows()
                r=self.report(); self.assertFalse(r['success']); self.assertTrue(r['reasons'])
        for rows in (good+[good[-1]],list(reversed(good))):
            self.rows=rows; self.write_rows(); self.assertFalse(self.report()['success'])
        self.samples.write_text('{bad json\n'); self.assertFalse(self.report()['success'])
        for jobs in ([],['bad'],['tick','tick'],'tick'):
            self.assertFalse(gate.job_meter(self.current.isoformat(),
                (self.current+timedelta(seconds=40)).isoformat(),jobs)['success'])

    def test_schedule_is_separate_estimate_and_explicit_zero_match(self):
        cadence = self.path/'cadence.jsonl'
        for job in ('tick','spec_preflight'):
            self.append(cadence,dict(job=job,start=self.baseline.isoformat(),
                end=(self.current+timedelta(days=1)).isoformat(), cadence_s=10,
                anchor=self.current.isoformat(), source='operator effective schedule fixture',effective=True))
        for path in self.logs.iterdir():
            path.unlink()
        for row in self.rows:
            for obs in row['ledger']+row['top']:
                obs['cpu']=0
        self.write_rows()
        with patch.dict(os.environ,{'LEDGER_GATE_CADENCE':str(cadence)}):
            r=self.report()
        for row in r['windows']['current']['jobs'].values():
            self.assertEqual(row['scheduled_passes'],4)
            self.assertEqual(row['scheduled_quality'],'estimate')
            self.assertEqual(row['cpu_s_per_scheduled_pass'],0)
            self.assertIsNone(row['passes'])
            self.assertIsNone(row['cpu_s_per_pass'])
            self.assertEqual(row['cpu_quality'],'lower-bound')
        self.assertFalse(r['success'])
        self.assertIsNone(r['charter_bar_met'])

    def test_unauthorized_merge_cannot_supply_anchor_and_positive_change_is_retained(self):
        merges=self.root/'events/L-executor-0001.jsonl'
        (self.root/'events/L-builder-0001.jsonl').write_bytes(merges.read_bytes())
        merges.unlink()
        self.assertIsNone(self.report()['provenance']['first_merge'])
        (self.root/'events/L-builder-0001.jsonl').rename(merges)
        for row in self.rows[-2:]:
            for obs in row['ledger']+row['top']:
                obs['cpu']*=8
        self.write_rows()
        r=self.report()
        self.assertTrue(r['success'],r['reasons'])
        self.assertEqual(r['changes']['tick']['baseline']['percent'],100)
        self.assertEqual(r['changes']['tick']['control']['percent'],300)
        self.assertIsNone(r['charter_bar_met'])

    def test_bad_completion_measurements(self):
        file=self.logs/'tick.jsonl'; good=file.read_text()
        for field,value in (('duration_s',True),('duration_s',-1),('duration_s',float('inf')),
                            ('rc',True),('start_ts','bad'),('ts','2026-11-08T00:00:10')):
            rows=[json.loads(l) for l in good.splitlines()]
            rows[-1][field]=value
            file.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            r=self.report(); self.assertFalse(r['success']); self.assertTrue(r['reasons'])
        file.write_text(good+'bad json\n');self.assertFalse(self.report()['success'])


if __name__ == '__main__':
    unittest.main()
