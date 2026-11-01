"""Spec amendments invalidate runner caches and blind grader evidence."""
import hashlib
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch
import grader_packet
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
            self.assertIsNotNone(grader_packet.bundle_for(
                [build, *fixture.events()], fixture.sid, fixture.sha))

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
                        launch.assert_called_once()
                    self.assertEqual(emitted, [])


if __name__ == '__main__':
    unittest.main()
