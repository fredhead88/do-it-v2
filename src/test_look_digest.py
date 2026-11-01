"""Hermetic daily digest replay, shared tick path, cap and detector exclusions."""
import json
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ['DOIT_ROOT'] = tempfile.mkdtemp(prefix='look-digest-test-')
import fold
import look
import look_digest

NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


class DailyDigestTests(unittest.TestCase):
    def test_replay_and_late_arrivals(self):
        with tempfile.TemporaryDirectory() as name:
            root = pathlib.Path(name)
            events = []
            for offset in range(8):
                now = NOW + timedelta(days=offset)
                d = look_digest.Digest(root, events, now)
                for n in range(600):
                    d.add('condition-' + str(n % 30), 'L-spec-' + str(n), 'executor', {'spec': n})
                written = []
                def emit(brief):
                    written.append(json.loads(json.dumps(brief)))
                    return str(len(events) + len(written))
                d.finish(emit, lambda *args: None)
                self.assertEqual(len(written), 30)
                self.assertTrue(all(len(b['items']) == 20 for b in written))
                events.extend(dict(b, ts=now.isoformat()) for b in written)
                later = look_digest.Digest(root, events, now)
                self.assertIsNone(later.add('condition-0', 'late-spec', 'executor', {}))
                later.finish(emit, lambda *args: None)
                self.assertEqual(len(written), 30)
                saved = json.loads(later.path.read_text())
                self.assertIn('late-spec', saved['conditions']['condition-0']['items'])
                self.assertEqual(saved['suppressed'], 571)
                # Losing derived state must not permit another announcement.
                later.path.unlink()
                recovered = look_digest.Digest(root, events, now)
                self.assertIsNone(recovered.add('condition-0', 'another', 'executor', {}))
                self.assertEqual(recovered.data['emitted'], 30)

    def test_cap_and_dry_run(self):
        with tempfile.TemporaryDirectory() as name:
            root = pathlib.Path(name)
            d = look_digest.Digest(root, [], NOW)
            for n in range(70):
                d.add('condition-' + str(n), 'key', 'operator', {})
            self.assertEqual(len(d.pending), 50)
            self.assertEqual(d.data['suppressed'], 20)
            d.finish(lambda b: b['condition'], lambda *args: None)
            before = d.path.read_bytes()
            dry = look_digest.Digest(root, [], NOW, dry_run=True)
            dry.add('condition-99', 'key', 'operator', {})
            dry.finish(lambda _: self.fail('dry run wrote ledger'), lambda *args: None)
            self.assertEqual(d.path.read_bytes(), before)

    def test_shared_emit_once_is_per_condition_even_after_clear(self):
        with tempfile.TemporaryDirectory() as name, patch.object(fold, 'NOW', NOW):
            root = pathlib.Path(name)
            first = look.emit_once('look-stale', 'a', 'operator', {}, [], root=root)
            self.assertIsNotNone(first)
            self.assertIsNone(look.emit_once('look-stale', 'b', 'operator', {}, [], root=root))
            self.assertIsNone(look._clear('look-stale', 'a', 'fresh', [], root))
            self.assertIsNotNone(look._clear('look-stale', 'b', 'fresh', [], root))
            self.assertIsNone(look.emit_once('look-stale', 'c', 'operator', {}, [], root=root))
            rows = [json.loads(s) for s in (root / 'events' / look.LEDGER_NAME).read_text().splitlines()]
            self.assertEqual(sum(e['type'] == 'brief' for e in rows), 1)

    def test_owed_rolls_up_per_spec(self):
        rows = [{'spec': 'L-spec-0001', 'criterion': 'AC1'},
                {'spec': 'L-spec-0001', 'criterion': 'AC2'},
                {'spec': 'L-spec-0002', 'criterion': 'AC1'}]
        with patch('owed_metrics.summary', return_value={'overdue_24h': rows}), \
             patch.object(look, '_write_brief', return_value=None) as write:
            look._check_owed_overdue([], NOW, pathlib.Path('/unused'), [])
        self.assertEqual(write.call_count, 2)
        self.assertEqual(write.call_args_list[0].args[1], 'L-spec-0001')
        self.assertEqual(write.call_args_list[0].args[3]['criteria'], ['AC1', 'AC2'])

    def test_overdue_escalation_excludes_idle_even_when_classifier_is_stuck(self):
        evs = [{'subject': 'L-spec-0001', 'type': 'escalation-blocking',
                'ts': (NOW - timedelta(days=2)).isoformat(), '_src': 'esc'}]
        with patch.object(fold, 'open_escalations', return_value=evs), \
             patch.object(fold, 'answered', return_value=set()):
            self.assertFalse(look._spec_idle_bad('L-spec-0001', {'state': 'specified'},
                evs, evs, {'L-spec-0001': {'bucket': 'STUCK'}}, set(), set(), NOW))


if __name__ == '__main__':
    unittest.main()
