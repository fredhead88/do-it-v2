"""Scratch-only watchdog policy and committed-ledger regression tests."""
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

import cron_run
import fold
import look
import notification_policy
import tg_say

NOW = datetime(2031, 1, 1, 12, tzinfo=timezone.utc)


def receipt(ac, body):
    body = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='cli://watchdog-quiet/' + ac, status=200,
                         body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))


class WatchdogQuietTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='heartbeat-quiet-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'events').mkdir()
        self.calls = []
        self.outcome = tg_say.Delivery('sent', 200)
        for stub in (patch.dict(os.environ, DOIT_ROOT=str(self.root), HOME=str(self.root)),
                     patch.object(fold, 'ROOT', self.root), patch.object(fold, 'EVENTS', self.root / 'events'),
                     patch.object(look, '_DRY_RUN', False),
                     patch.object(tg_say, '_transport', side_effect=AssertionError('real transport forbidden')),
                     patch.object(look, '_send_operator', side_effect=AssertionError('legacy transport forbidden')),
                     patch.object(tg_say, 'deliver', side_effect=self.send)):
            stub.start()
            self.addCleanup(stub.stop)

    def send(self, sender, text, *, label, root):
        self.assertEqual((sender, label, Path(root)), ('look', '[DOWN]', self.root))
        notification_policy.validate_message(text, label)
        self.calls.append(text)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def start(self, when, name='look', skip=False):
        path = (cron_run.skip_store_path if skip else cron_run.store_path)(name, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(ev='skip' if skip else 'start', ts=when.isoformat())) + '\n')

    def check(self, when=NOW, name='look'):
        return look.heartbeat_missed(name, when, [], root=self.root)

    def rows(self):
        return look._read_jsonl(self.root / 'events' / look.LEDGER_NAME)

    def test_ac1(self):
        import crons
        self.assertEqual([r['schedule'] for r in crons.rows() if r['name'] == 'look'], ['33 * * * *'])
        self.start(NOW)
        for seconds, missed, sent in ((3600, False, 0), (6000, False, 0), (6001, True, 0),
                                      (10800, True, 0), (10801, True, 1)):
            self.assertEqual(self.check(NOW + timedelta(seconds=seconds)), missed)
            self.assertEqual(len(self.calls), sent)
        self.start(NOW + timedelta(hours=4), skip=True)
        self.assertFalse(self.check(NOW + timedelta(hours=4)))
        self.start(NOW, name='tick')
        self.assertFalse(self.check(NOW + timedelta(minutes=15), name='tick'))
        self.assertTrue(self.check(NOW + timedelta(minutes=15, seconds=1), name='tick'))
        for path in (cron_run.store_path('look', self.root), cron_run.skip_store_path('look', self.root)):
            path.write_text('corrupt\n')
        self.check(NOW + timedelta(hours=5))
        self.check(NOW + timedelta(hours=8))
        self.assertEqual(len(self.calls), 2)
        self.check(NOW + timedelta(hours=8, seconds=1))
        self.assertEqual(len(self.calls), 3)
        self.assertIn('evidence has been unavailable', self.calls[-1])
        self.check(NOW + timedelta(hours=9))
        self.assertEqual(len(self.calls), 3)
        self.check(NOW + timedelta(hours=4))  # backwards clock restarts absence
        self.assertEqual(self.rows()[-1]['first_missing_at'], (NOW + timedelta(hours=4)).isoformat())
        # The observed-absence clock is recovered from committed bytes in a new process.
        at = NOW + timedelta(hours=7, seconds=1)
        code = "import look,tg_say; from datetime import datetime; tg_say.deliver=lambda *a,**k: tg_say.Delivery('held'); look.heartbeat_missed('look',datetime.fromisoformat(%r),[])" % at.isoformat()
        subprocess.check_call([sys.executable, '-c', code], env=dict(os.environ, PYTHONPATH=str(Path(__file__).parent.resolve())))
        self.assertEqual(self.rows()[-1]['delivery'], 'held')
        receipt('AC1', dict(boundaries='60,100,100:01,180,180:01', pages=len(self.calls), rows=len(self.rows())))

    def test_ac2(self):
        self.assertIn('undetermined', look.heartbeat_line([], NOW))
        self.start(NOW)
        self.check()
        healthy = self.rows()[-1]
        self.assertIn('healthy', look.heartbeat_line([healthy], NOW))
        self.assertIn('missed', look.heartbeat_line([healthy], NOW + timedelta(minutes=101)))
        missed = dict(healthy, measured_at=(NOW + timedelta(hours=2)).isoformat(), status='missed')
        recovered = dict(healthy, measured_at=(NOW + timedelta(hours=3)).isoformat(), last_start=(NOW + timedelta(hours=3)).isoformat())
        for rows in ([recovered, missed], [missed, recovered]):
            self.assertIn('healthy', look.heartbeat_line(rows, NOW + timedelta(hours=3)))
        self.assertIn('undetermined', look.heartbeat_line([healthy, dict(healthy, last_start=None)], NOW))
        for clock, hour in ((NOW, '14:00'), (NOW.replace(month=7), '15:00')):
            row = dict(healthy, measured_at=clock.isoformat(), last_start=clock.isoformat())
            with patch.object(look, '_read_cron_store', side_effect=AssertionError('store read')), \
                 patch.object(look, '_append_watchdog', side_effect=AssertionError('write')):
                line = look.heartbeat_line([row], clock)
            self.assertIn(hour, line)
            self.assertNotIn('\n', line)
            self.assertNotIn('+00:00', line)
        receipt('AC2', dict(line=line, recovery=True, ordering=True, pure=True))

    def test_ac4(self):
        clock = NOW
        for outcome in (tg_say.Delivery('sent', 200), tg_say.Delivery('held'), tg_say.Delivery('failed'),
                        tg_say.Delivery('refused'), tg_say.Delivery('sent', 500), RuntimeError('fixture')):
            self.start(clock)
            self.outcome = outcome
            self.calls.clear()
            at = clock + timedelta(hours=4)
            try:
                self.check(at)
            except RuntimeError:
                pass
            self.check(at + timedelta(minutes=59))
            self.assertEqual(len(self.calls), 1)
            if isinstance(outcome, Exception):
                self.outcome = tg_say.Delivery('sent', 200)
            self.check(at + timedelta(hours=1))
            terminal = getattr(outcome, 'state', None) == 'held' or outcome == tg_say.Delivery('sent', 200)
            self.assertEqual(len(self.calls), 1 if terminal else 2)
            row = [r for r in self.rows() if r['kind'] == 'heartbeat-missed'][-1]
            self.assertEqual(row['sent'] == 'yes', row['delivery'] == 'sent')
            # Each case is a new start identity.
            clock += timedelta(days=1)
        receipt('AC4-look', dict(outcomes=6, hour_retry=True, held_once=True))

    def test_ac6(self):
        self.start(NOW)
        self.outcome = tg_say.Delivery('held')
        self.check(NOW + timedelta(hours=4))
        code = "import fold,json; print(json.dumps(fold.read_events()))"
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent.resolve()))
        rows = json.loads(subprocess.check_output([sys.executable, '-c', code], env=env, text=True))
        self.assertEqual({r['kind'] for r in rows}, {'heartbeat-observation', 'heartbeat-missed'})
        self.assertEqual([r['delivery'] for r in rows if r['kind'] == 'heartbeat-missed'], ['held'])
        code = "import look,tg_say; calls=[]; tg_say.deliver=lambda *a,**k: calls.append(a) or tg_say.Delivery('refused'); from datetime import datetime; look.heartbeat_missed('look',datetime.fromisoformat(%r),[]); assert not calls" % (NOW + timedelta(hours=9)).isoformat()
        subprocess.check_call([sys.executable, '-c', code], env=env)
        self.assertEqual(len(self.calls), 1)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file() and 'cache' not in p.parts}
        with patch.object(look, '_DRY_RUN', True):
            self.check(NOW + timedelta(days=1))
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file() and 'cache' not in p.parts})
        receipt('AC6-look', dict(independent_rows=len(rows), held_restart=True, dry_bytes_unchanged=True))


if __name__ == '__main__':
    unittest.main()
