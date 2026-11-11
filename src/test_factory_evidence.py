"""Spec amendments invalidate runner caches and blind grader evidence."""
import hashlib
from contextlib import ExitStack
from copy import deepcopy
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch
import grader_packet
import build_runner
import evidence_revision
import test_build_runner as fixtures


class EvidenceAmendTests(unittest.TestCase):
    def test_same_sha_rebuild_publishes_evidence_after_latest_build(self):
        # L-spec-1913: a rebuild returned the original ready SHA; the old
        # runner exited zero from cache forever while the grader waited.
        fixture = fixtures.BuildRunnerTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _, old_path = fixture.bundle()
        evidence = fixture.events()
        evidence[-1]['ts'] = '2026-10-18T01:00:00Z'
        (fixture.root / 'events/L-build-runner-local.jsonl').write_text(
            '\n'.join(json.dumps(e) for e in evidence) + '\n')
        build_path = fixture.root / 'events/L-builder-fixture.jsonl'
        build = json.loads(build_path.read_text())
        build['ts'] = '2026-10-19T02:26:28Z'
        build_path.write_text(json.dumps(build) + '\n')
        with patch.object(grader_packet, 'root', return_value=fixture.root):
            self.assertIsNone(grader_packet.bundle_for([build, *evidence], fixture.sid, fixture.sha))
            _, new_path = fixture.bundle()
            self.assertNotEqual(old_path, new_path, 'same-SHA rebuild reused evidence predating build-done')
            self.assertEqual(len(fixture.events()), 2)
            # Reuse publishes a bundle-reused receipt through the runner CLI, which
            # has no scratch ledger here; capture the receipt instead of appending.
            with patch.object(build_runner, '_event') as receipt:
                self.assertIsNotNone(grader_packet.bundle_for(
                    [build, *fixture.events()], fixture.sid, fixture.sha))
            self.assertEqual(receipt.call_args.args[2], 'bundle-reused')

    def test_runner_regenerates_changed_verification_at_same_sha(self):
        fixture = fixtures.BuildRunnerTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        old, old_path = fixture.bundle()
        fixture.spec('python3 -c "print(12345)"')
        new, path = fixture.bundle()
        self.assertNotEqual(old_path, path, 'spec change reused pre-amendment cache')
        self.assertIn('12345', fixture.tail(new['commands'][0]).decode())
        events = fixture.events()
        events.append(dict(type='spec-amended', subject=fixture.sid, ts='2099-01-01T00:00:00Z'))
        with patch.object(grader_packet, 'root', return_value=fixture.root):
            self.assertIsNone(grader_packet.bundle_for(events, fixture.sid, fixture.sha))

    def test_legacy_bundle_and_failure_before_amend_are_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            directory = root / 'content/evidence/L-spec-1'
            directory.mkdir(parents=True)
            path = directory / 'bundle.json'
            raw = json.dumps(dict(spec='L-spec-1', sha='a'*40, commands=[], comparison={})).encode()
            path.write_bytes(raw)
            event = dict(type='evidence-bundle', subject='L-spec-1', sha='a'*40,
                         ts='2026-10-29T20:00:00Z', path=str(path), bundle_sha256=hashlib.sha256(raw).hexdigest())
            amendment = dict(type='spec-amended', subject='L-spec-1', ts='2026-10-29T20:31:00Z')
            with patch.object(grader_packet, 'root', return_value=root):
                self.assertIsNotNone(grader_packet.bundle_for([event], 'L-spec-1', 'a'*40))
                self.assertIsNone(grader_packet.bundle_for([event, amendment], 'L-spec-1', 'a'*40))
                for kind in ('evidence-bundle', 'evidence-bundle-failed'):
                    events = [dict(type='build-done', subject='L-spec-1', ready_sha='a'*40,
                                   ts='2026-10-29T19:00:00Z'), dict(event, type=kind), amendment]
                    emitted = []
                    def fail(message, **fields):
                        raise RuntimeError(fields['reason'])
                    with patch.object(grader_packet.subprocess, 'Popen') as launch:
                        with self.assertRaisesRegex(RuntimeError, 'bundle-pending'):
                            grader_packet.gate(events, 'L-spec-1', '', lambda *a, **kw: emitted.append(kw), fail)
                        launch.assert_not_called()
                    self.assertEqual(emitted, [])


class ReadOnlyBundleTests(unittest.TestCase):
    """Persisted stable identity must be usable without acquiring live refs."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='bundle-snapshot-')
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.sid, self.sha, self.base = 'L-spec-9001', 'a' * 40, 'b' * 40
        self.directory = self.root / 'content/evidence' / self.sid
        self.directory.mkdir(parents=True)
        self.text = '## Acceptance criteria\nAC1 [backend]: fixture.\n'
        (self.root / 'content' / (self.sid + '.md')).write_text(self.text)
        self.tail = self.directory / 'tail.txt'
        self.tail.write_bytes(b'checked fixture\n')
        self.path = self.directory / 'bundle.json'
        self.build = dict(type='build-done', subject=self.sid, ready_sha=self.sha,
                          base_sha=self.base, project='demo', ts='2026-11-06T00:00:00Z')
        key = evidence_revision.bundle_key(self.text, [], self.sid, self.sha, self.base)
        self.data = dict(spec=self.sid, sha=self.sha, project='demo', base_sha=self.base,
                         master_sha='c' * 40, local_master_sha=None,
                         spec_sha256=evidence_revision.digest(self.text), bundle_key=key,
                         criteria=None, runner_version=build_runner.VERSION,
                         commands=[dict(tail=str(self.tail), tail_sha256=hashlib.sha256(
                             self.tail.read_bytes()).hexdigest())], comparison={})
        self.event = dict(type='evidence-bundle', subject=self.sid, sha=self.sha,
                          project='demo', base_sha=self.base, bundle_key=key,
                          ts='2026-11-06T00:01:00Z', path=str(self.path))
        self.publish(self.data)
        self.events = [self.build, self.event]
        self.addCleanup(patch.stopall)
        patch.object(grader_packet, 'root', return_value=self.root).start()

    def publish(self, data):
        raw = json.dumps(data, sort_keys=True).encode()
        self.path.write_bytes(raw)
        self.event['bundle_sha256'] = hashlib.sha256(raw).hexdigest()

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def no_effects(self):
        stack = ExitStack()
        for module, name in ((build_runner, '_event'), (build_runner, '_resolve'),
                             (build_runner, '_bundle_base'),
                             (grader_packet.subprocess, 'Popen'),
                             (grader_packet.subprocess, 'run')):
            stack.enter_context(patch.object(module, name,
                                             side_effect=AssertionError('lookup effect: ' + name)))
        return stack

    def test_stable_snapshot_is_pure_and_accepts_abbreviated_ready(self):
        before = self.snapshot()
        with self.no_effects():
            for ready in (self.sha, self.sha[:9]):
                for identity in (None, (self.base, 'c' * 40, None)):
                    for _ in range(2):
                        result = grader_packet.bundle_for(self.events, self.sid, ready,
                                                          identity=identity, record=False)
                        self.assertIsNotNone(result, (ready, identity))
                        self.assertEqual(result['bundle'], self.data)
                        self.assertIn('checked fixture', result['tails'][0])
        self.assertEqual(before, self.snapshot())
        print('snapshot lookup: 8 stable/abbreviated/supplied-identity reads; bytes unchanged; effects=0')

    def test_snapshot_rejects_inconsistent_or_missing_identity(self):
        cases = {'missing-base': ('base_sha', None), 'missing-master': ('master_sha', None),
                 'invalid-master': ('master_sha', 'invalid'),
                 'invalid-local-master': ('local_master_sha', 'invalid'),
                 'wrong-base': ('base_sha', 'd' * 40),
                 'wrong-key': ('bundle_key', 'invalid'),
                 'wrong-project': ('project', 'another'),
                 'wrong-runner': ('runner_version', 'another'),
                 'wrong-revision': ('spec_sha256', 'invalid'),
                 'wrong-sha': ('sha', 'd' * 40), 'subset': ('criteria', ['AC1'])}
        with self.no_effects():
            for case, (field, value) in cases.items():
                with self.subTest(case=case):
                    data = dict(self.data, **{field: value})
                    self.publish(data)
                    self.assertIsNone(grader_packet.bundle_for(
                        self.events, self.sid, self.sha, record=False))
            for field in ('base_sha', 'master_sha', 'local_master_sha'):
                data = dict(self.data)
                del data[field]
                self.publish(data)
                self.assertIsNone(grader_packet.bundle_for(
                    self.events, self.sid, self.sha, record=False), field)
            self.publish(self.data)
            for changes in ({'base_sha': 'd' * 40}, {'project': 'another'},
                            {'bundle_key': 'invalid'}, {'bundle_sha256': 'invalid'},
                            {'criteria': ['AC1']}, {'subject': 'L-spec-9002'},
                            {'sha': 'd' * 40}, {'path': str(self.directory / 'missing')}):
                events = [self.build, dict(self.event, **changes)]
                self.assertIsNone(grader_packet.bundle_for(events, self.sid, self.sha, record=False))
        print('snapshot rejection: 14 persisted identity cases; 8 receipt mismatch/unreadable cases')

    def test_snapshot_preserves_freshness_containment_and_tail_checks(self):
        with self.no_effects():
            for changes in ({'ts': self.event['ts']}, {'ready_sha': 'd' * 40}):
                self.assertIsNone(grader_packet.bundle_for(
                    [dict(self.build, **changes), self.event], self.sid, self.sha, record=False))
            amended = dict(type='spec-amended', subject=self.sid, ts='2026-11-06T00:02:00Z')
            self.assertIsNone(grader_packet.bundle_for(
                self.events + [amended], self.sid, self.sha, record=False))
            self.tail.write_bytes(b'tampered')
            self.assertIsNone(grader_packet.bundle_for(self.events, self.sid, self.sha, record=False))
            self.tail.unlink()
            self.assertIsNone(grader_packet.bundle_for(self.events, self.sid, self.sha, record=False))
            outside = self.root / 'outside.json'
            outside.write_bytes(self.path.read_bytes())
            self.assertIsNone(grader_packet.bundle_for(
                [self.build, dict(self.event, path=str(outside))], self.sid, self.sha, record=False))
            link = self.directory / 'escape.json'
            link.symlink_to(outside)
            self.assertIsNone(grader_packet.bundle_for(
                [self.build, dict(self.event, path=str(link))], self.sid, self.sha, record=False))
        print('snapshot rejection: same-SHA rebuild, other build, amendment, tail tamper/missing, escape/symlink')

    def test_default_lookup_keeps_live_validation_and_reuse_receipt(self):
        identity = (self.base, 'c' * 40, None)
        with patch.object(build_runner, '_resolve', return_value=self.sha) as resolve, \
                patch.object(build_runner, '_bundle_base', return_value=identity) as live, \
                patch.object(build_runner, '_event') as emit:
            self.assertIsNotNone(grader_packet.bundle_for(self.events, self.sid, self.sha))
            resolve.assert_called_once()
            live.assert_called_once()
            self.assertEqual(emit.call_args.args, (self.sid, 'demo', 'bundle-reused'))
            live.return_value = (self.base, 'e' * 40, None)
            self.assertIsNone(grader_packet.bundle_for(self.events, self.sid, self.sha))
            self.assertEqual(emit.call_count, 1)

    def test_legacy_lookup_keeps_existing_path(self):
        data = dict(spec=self.sid, sha=self.sha, commands=[], comparison={})
        self.publish(data)
        with self.no_effects():
            self.assertIsNotNone(grader_packet.bundle_for(self.events, self.sid, self.sha, record=False))
            self.assertIsNone(grader_packet.bundle_for(
                self.events, self.sid, self.sha, identity=(self.base, 'c' * 40, None), record=False))


if __name__ == '__main__':
    unittest.main()
