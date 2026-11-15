"""Scratch-only agreement between tick readiness, start checks and retry history."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

# Bind defaults before importing readers; no production ledger or network access.
_BOOT = tempfile.TemporaryDirectory(prefix='bundle-agreement-import-')
os.environ['DOIT_ROOT'] = _BOOT.name
os.environ['DOIT_NO_POKE'] = '1'

import attempts
import dispatch
import fold
import grader_packet
import lane_rules
import nextstep
import start_check

SID = 'L-spec-9901'
SHA, MAIN, MOVED = 'a' * 40, 'b' * 40, 'c' * 40
NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


def signed(name, body):
    payload = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='scratch://bundle-identity-agreement/' + name, status=0,
                         body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                         body_excerpt=body)))


class BundleIdentityAgreementTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='bundle-agreement-')
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        (self.root / 'events').mkdir()
        directory = self.root / 'content/evidence' / SID
        directory.mkdir(parents=True)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, HOME=str(self.root),
                                       DOIT_ROOT=str(self.root)))
        stack.enter_context(patch.object(fold, 'ROOT', self.root))
        stack.enter_context(patch.object(fold, 'EVENTS', self.root / 'events'))
        stack.enter_context(patch.object(fold, 'NOW', NOW))
        stack.enter_context(patch.object(dispatch, 'CONTENT', self.root / 'content'))
        stack.enter_context(patch.object(grader_packet, 'root', return_value=self.root))
        spec = self.root / 'content' / (SID + '.md')
        spec.write_text('## Acceptance criteria\nAC1 [backend]: scratch.\n')
        self.emit('spec-written', 'spec-writer', -100, path=str(spec))
        self.emit('spawn-done', 'spec-auditor', -90, bad_cut=False)
        self.emit('build-done', 'builder', -80, status='DONE', ready_sha=SHA)
        path = directory / 'bundle.json'
        path.write_text(json.dumps(dict(spec=SID, sha=SHA, commands=[], comparison={},
                                        master_sha=MAIN, local_master_sha=MAIN)))
        self.emit('evidence-bundle', 'build-runner', -70, sha=SHA, path=str(path),
                  bundle_sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    def emit(self, kind, actor, seconds, **fields):
        with patch.object(dispatch, 'now', return_value=(NOW + timedelta(seconds=seconds)).isoformat()):
            error = dispatch.emit(self.root / 'events' / f'L-{actor}-local.jsonl',
                                  dict(subject=SID, project='demo'), kind, **fields)
        self.assertIsNone(error)

    def test_ac1_matching_moved_and_unknown_main(self):
        events = fold.read_events(use_cache=False)
        specs = fold.fold(events)[0]
        checked = []
        for main, action, reason, refusal in (
                (MAIN, 'grade', 'condition met', None),
                (MOVED, 'bundle', 'bundle evidence required', 'bundle-pending'),
                (None, 'grade', 'evidence ready', 'bundle-pending')):
            with self.subTest(main=main):
                step = nextstep.step_for(events, specs, SID, NOW, lane_rules.load(),
                                         mains={'demo': main, 'other': MOVED})
                rejected = start_check.refusal('grader', SID, events, now=NOW,
                                               root=self.root, main_sha=main)
                self.assertEqual(step.action, action)
                self.assertEqual(step.reason, reason)
                self.assertEqual(rejected.reason if rejected else None, refusal)
                row = dict(reason='not-ready:bundle-pending', retry_hold=True,
                           since=events[-1]['ts'])
                bundle = nextstep._bundle_step(dict(sid=SID, evs=events, events=events,
                                                    now=NOW, mains={'demo': main}), row)
                if main == MOVED:
                    self.assertEqual(bundle.action, 'bundle')
                else:
                    self.assertIsNone(bundle)
                    self.assertEqual(row['reason'], 'evidence ready')
                checked.append(dict(main=main, action=step.action, reason=step.reason,
                                    bundle_reason=bundle.reason if bundle else row['reason'],
                                    refusal=rejected.reason if rejected else None))
        legacy = nextstep.step_for(events, specs, SID, NOW, lane_rules.load())
        self.assertEqual((legacy.action, legacy.reason), ('grade', 'condition met'))
        signed('AC1', checked)

    def test_ac2_bundle_refusals_do_not_consume_attempts(self):
        trigger = dict(subject=SID, ts=(NOW - timedelta(minutes=10)).isoformat())
        checked = []
        for kind, reason, expected in (
                ('autodispatch-failed', 'refused-before-start: bundle-pending\n', 0),
                ('spawn-failed', 'bundle-pending', 0),
                ('autodispatch-failed', 'dispatch error', 3),
                ('autodispatch-failed', 'refused-before-start: duplicate-dispatch', 3),
                ('autodispatch-failed', 'bundle-pending', 3)):
            for grouping in ('ungrouped', 'spawn', 'marker'):
                rows = []
                for i in range(3):
                    fields = dict(type=kind, subject=SID, role='grader', reason=reason,
                                  ts=(NOW - timedelta(minutes=3-i)).isoformat())
                    if grouping == 'spawn':
                        fields['spawn'] = f'L-grader-{9900+i}'
                    elif grouping == 'marker':
                        if kind == 'spawn-failed':
                            fields['spawn'] = f'L-grader-{9900+i}'
                        rows.append(dict(type='autodispatched', subject=SID, role='grader',
                                         spawn=str(9900+i),
                                         ts=(NOW - timedelta(minutes=3-i, seconds=1)).isoformat()))
                    rows.append(fields)
                with self.subTest(kind=kind, reason=reason, grouping=grouping):
                    history = attempts.history(rows, trigger, 'grader', now=NOW)
                    self.assertEqual(history.n_failed, expected)
                    self.assertEqual(history.exhausted, expected == 3)
                    self.assertFalse(history.live)
                    self.assertEqual(history.reasons, (reason,) * expected)
                    self.assertIsNone(history.next_retry_at)
                    checked.append(dict(kind=kind, reason=reason, grouping=grouping,
                                        n_failed=history.n_failed, exhausted=history.exhausted))
        signed('AC2', checked)


if __name__ == '__main__':
    unittest.main()
