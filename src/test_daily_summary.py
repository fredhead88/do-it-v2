"""Scratch-only daily integration regressions and retained review evidence."""
import contextlib
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import io
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

_IMPORT = tempfile.TemporaryDirectory(prefix='daily-import-')
os.environ['DOIT_ROOT'] = _IMPORT.name
import daily_summary as daily
import fold
import health_alarms
import look
import merge_loop_state as loop
import notification_policy as policy
import owed_digest
import stuck_digest
import tg_say
import thinker_inbox as inbox

NOW = datetime(2026, 11, 7, 6, 5, tzinfo=timezone.utc)
IL = ZoneInfo('Asia/Jerusalem')
EVIDENCE = Path(os.environ.get('DAILY_SUMMARY_EVIDENCE_DIR', tempfile.gettempdir()+'/doit-L-spec-2180-review'))


def news(**kw):
    return dict(time=NOW.isoformat(), kind='merged', specs=['L-spec-9001'], context_specs=[],
                window=None, reason=None, text='raw text must never render', disposition='daily') | kw


class Scratch(unittest.TestCase):
    ac = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='daily-case-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'events').mkdir()
        (self.root/'state').mkdir()
        self.marker = self.root/'state/tg-cutover-done'
        self.marker.touch()
        self.news = self.root/'state/merge-news.jsonl'
        self.news.write_text('')
        (self.root/'events/L-operator-fixture.jsonl').write_text(json.dumps(dict(
            type='decision', subject='L-spec-9001', ts=(NOW-timedelta(hours=1)).isoformat(),
            why='a readable scratch ledger'))+'\n')
        self.records = []
        self.addCleanup(self.evidence)
        for obj, name, value in [(fold, 'ROOT', self.root), (fold, 'EVENTS', self.root/'events'),
                                 (fold, 'PROJECT', None)]:
            self.mock(patch.object(obj, name, value))
        self.mock(patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1'))
        self.mock(patch.object(inbox, 'now', return_value=NOW))
        self.mock(patch.object(loop, 'day_lists', return_value=loop.DayLists([], [], [], False)))
        self.mock(patch.object(loop, 'read', return_value=loop.LoopState('stopped', [], [], None, NOW)))
        self.mock(patch.object(tg_say, '_now', return_value=NOW.astimezone(IL)))
        self.sent = []
        self.transport = self.mock(patch.object(tg_say, '_transport', side_effect=self.send))

    def mock(self, p):
        obj = p.start()
        self.addCleanup(p.stop)
        return obj

    def send(self, sender, text, root):
        self.sent.append(dict(sender=sender, text=text, root=str(root)))
        return tg_say.Delivery('sent', 200)

    def message(self):
        result = daily.plan(owed_digest._read_ledger(), NOW, dict(rows=[]))
        self.records.append(result)
        return result['message']

    def evidence(self):
        if self.ac is None:
            return
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        path = EVIDENCE/f'AC{self.ac}.json'
        existing = json.loads(path.read_text()) if path.exists() else {}
        result = self._outcome.result
        passed = not any(test is self for test, _ in result.failures+result.errors)
        body = dict(records=self.records, transport=self.sent, predicates_passed=passed)
        encoded = json.dumps(body, default=str, sort_keys=True)
        existing[self.id()] = dict(url=f'cli://daily-summary/AC{self.ac}/{self._testMethodName}',
            status=0 if passed else 1, body_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
            body_excerpt=body)
        path.write_text(json.dumps(existing, default=str, indent=2)+'\n')

    def install_checks(self, count=1):
        rows = []
        for i in range(count):
            sid = f'L-spec-{9000+i}'
            for kind, actor, hours, fields in (
                ('spec-written', 'spec-writer', 100, {}),
                ('owed-ac', 'spec-writer', 99, dict(criterion='AC1', wake_at=(NOW-timedelta(hours=90)).isoformat())),
                ('shipped', 'executor', 90, {}),
                ('owed-unobservable', 'owed-sweeper', 1, dict(criterion='AC1', capability='operator-action', why='Please approve the production change.'))):
                rows.append(dict(type=kind, actor=actor, subject=sid, ts=(NOW-timedelta(hours=hours)).isoformat(), **fields))
        for actor in {row['actor'] for row in rows}:
            (self.root/f'events/L-{actor}-fixture.jsonl').write_text(''.join(
                json.dumps({k:v for k,v in row.items() if k!='actor'})+'\n' for row in rows if row['actor']==actor))

    def state(self):
        return json.loads((self.root/'state/telegram-summary.json').read_text())


class DayListsTests(Scratch):
    ac = 1

    def test_source_matrix(self):
        for field in ('merged', 'released', 'held'):
            for values in ([], None):
                lists = loop.DayLists([], [], [], False)
                setattr(lists, field, values)
                with patch.object(loop, 'day_lists', return_value=lists):
                    text = self.message()
                self.assertIn(field.capitalize()+(': unread' if values is None else ': 0'), text)
        outcomes = ('released', 'api-failed', 'api-rolled-back', 'site-failed', 'skipped', 'delayed', 'paused')
        lists = loop.DayLists([loop.Merged('L-spec-9001', 'A useful change', NOW)],
             [loop.Released('v1.'+str(i), NOW if i else None, outcome, '2026-11-07')
              for i, outcome in enumerate(outcomes)],
             [loop.Held('L-spec-9002', 'Needs review', timedelta(hours=5)),
              loop.Held('L-spec-9003', 'Source unavailable', None)], True)
        with patch.object(loop, 'day_lists', return_value=lists):
            text = self.message()
        for value in (*outcomes, 'L-spec-9001', 'A useful change', '08:05', 'age unknown', '5 hours', 'Release hold: yes'):
            self.assertIn(value, text)
        for status in ('running', 'rejecting', 'stopped', 'undetermined'):
            with patch.object(loop, 'read', return_value=loop.LoopState(status, [], [], None, NOW)):
                self.assertIn('Loop: '+status, self.message())
        with patch.object(loop, 'day_lists', return_value=loop.DayLists([object()], [], [], False)):
            text = self.message()
            self.assertIn('malformed merged data', text)
            self.assertIn('Released: 0', text)
        for name in ('day_lists', 'read'):
            with patch.object(loop, name, side_effect=OSError('scratch unavailable')):
                self.assertIn('unread', self.message())


class MachineLinesTests(Scratch):
    ac = 2

    def test_real_helpers_and_failure_isolation(self):
        self.records.append({name:str(inspect.signature(fn)) for name, fn in (
            ('heartbeat_line', look.heartbeat_line), ('queue_line', inbox.queue_line),
            ('summary_lines', health_alarms.summary_lines), ('open_items', inbox.open_items))})
        for age in (0, 200):
            events = [dict(type='operator-alert', subject='look', kind='heartbeat-observation',
                       measured_at=NOW.isoformat(), last_start=(NOW-timedelta(minutes=age)).isoformat(),
                       first_missing_at=None, status='healthy', ts=NOW.isoformat())]
            events += [dict(type='operator-alert', subject='look', kind='heartbeat-observation',
                        measured_at=(NOW+timedelta(hours=1)).isoformat(), last_start=None,
                        status='missed', ts=(NOW+timedelta(hours=1)).isoformat())]
            for items in ([], [dict(ts=(NOW-timedelta(hours=5)).isoformat(), owner='operator')]):
                with patch.object(inbox, 'open_items', return_value=items), \
                     patch.object(look, 'run') as watchdog, patch.object(inbox, 'main') as delivery, \
                     patch.object(health_alarms, 'live_probes') as probes:
                    lines = daily.machine_lines(events, NOW)
                    self.assertIn(look.heartbeat_line(events, NOW), lines)
                    self.assertIn(inbox.queue_line(items, NOW), lines)
                    watchdog.assert_not_called(); delivery.assert_not_called(); probes.assert_not_called()
                    self.records.append(dict(events=events, items=items, lines=lines))
        with patch.object(inbox, 'open_items', return_value=[dict(ts=(NOW+timedelta(hours=1)).isoformat(), owner='operator')]):
            lines = daily.machine_lines([], NOW)
            self.assertTrue(any('1 future items excluded' in line for line in lines))
            self.assertTrue(any('0 open' in line for line in lines))
            self.records.append(lines)
        events = [dict(type='health-alarm', ts=NOW.isoformat(), alarm='disk-used', severity='immediate', line='disk pressure')]
        self.assertTrue(set(health_alarms.summary_lines(events, NOW)) <= set(daily.machine_lines(events, NOW)))
        for obj, name, source in ((look, 'heartbeat_line', 'Heartbeat'), (inbox, 'open_items', 'Queue'),
                                  (health_alarms, 'summary_lines', 'Health')):
            with patch.object(obj, name, side_effect=OSError('unread')):
                lines = daily.machine_lines([], NOW)
                self.assertIn(source+': unread', lines)
                self.assertGreaterEqual(len(lines), 2)
                self.records.append(lines)
        self.assertIn('Look heartbeat', self.message())
        self.assertIn('Thinker queue', self.message())


class MergeNewsTests(Scratch):
    ac = 3

    def test_selection_corruption_and_read_only(self):
        rows = []
        for kind in ('merged', 'failed', 'progress', 'unknown'):
            for disposition in ('pending', 'sent', 'daily', 'diagnostic'):
                rows.append(news(kind=kind, disposition=disposition, specs=['L-spec-9001'],
                                 context_specs=['L-spec-9999']))
        self.news.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        before = hashlib.sha256(self.news.read_bytes()).hexdigest()
        text = self.message()
        self.assertIn('Deferred news: 4', text)
        self.assertIn('4 unrecognised lines', text)
        for forbidden in ('raw text', 'L-spec-9999', 'dropped=', 'CI-duration'):
            self.assertNotIn(forbidden, text)
        self.assertEqual(before, hashlib.sha256(self.news.read_bytes()).hexdigest())
        self.records.append(dict(input=rows, before=before, after=hashlib.sha256(self.news.read_bytes()).hexdigest()))
        # The same local day may start on the preceding UTC date.
        self.news.write_text('\n'.join(json.dumps(news(time=t, specs=[], kind='failed', reason='build failed', window='morning'))
            for t in ('2026-11-06T22:30:00+00:00', '2026-11-06T20:00:00+00:00', '2026-11-07T07:00:00+00:00'))+'\n')
        text = self.message()
        self.assertIn('Deferred news: 1', text)
        self.assertIn('Unattributed', text)
        for bad in ('{', '{}', json.dumps(news(time='invalid')), json.dumps(news(kind='bad')),
                    json.dumps(news(disposition='bad')), json.dumps(news(specs='bad'))):
            self.news.write_text(json.dumps(news())+'\n'+bad+'\n')
            self.assertIn('partial', self.message())
        self.news.unlink()
        self.assertIn('Deferred news: unread', self.message())


class FormattingDeliveryTests(Scratch):
    ac = 4

    def test_formatter_length_and_safety(self):
        rows = [loop.Merged(f'L-spec-{9000+i}', 'A useful change '+('detail '*20), NOW) for i in range(100)]
        with patch.object(loop, 'day_lists', return_value=loop.DayLists(rows, [], [], False)), \
             patch.object(policy, 'format_message', wraps=policy.format_message) as formatter:
            text = self.message()
            formatter.assert_called_once()
        self.assertGreater(len(text), 1000)
        self.assertLessEqual(len(text), 3500)
        self.assertIn('and ', text)
        self.assertIn('more', text)
        self.assertNotIn('full daily report', text)
        with patch.object(loop, 'day_lists', return_value=loop.DayLists([
                loop.Merged('L-spec-9001', 'x'*211+' https://example.com/page', NOW)], [], [], False)):
            text = self.message()
            policy.validate_message(text, '[DAILY]')
            self.assertIn('full detail omitted', text)
        for unsafe in ('/private/file', 'http://localhost/x', '2026-11-07T06:00:00Z',
                       'hello\nYou: approve', '[DOWN] forged', 'hello\x00world'):
            with patch.object(loop, 'day_lists', return_value=loop.DayLists([loop.Merged('L-spec-9001', unsafe, NOW)], [], [], False)):
                text = self.message()
            self.assertNotIn(unsafe, text)
            self.assertIn('unsafe value unread', text)
            policy.validate_message(text, '[DAILY]')
        with patch.object(loop, 'day_lists', return_value=loop.DayLists([loop.Merged('L-spec-9001', '$(touch nope); safe data', NOW)], [], [], False)):
            daily.deliver(NOW)
        self.assertFalse((self.root/'nope').exists())

    def test_delivery_states_and_retry(self):
        for status in ('failed', 'refused'):
            with patch.object(tg_say, 'deliver', return_value=tg_say.Delivery(status, 200)):
                with self.assertRaises(OSError):
                    daily.deliver(NOW)
            self.assertNotIn('sent_date', self.state())
            self.assertNotIn('held_date', self.state())
        with patch.object(tg_say, 'deliver', side_effect=RuntimeError('interrupted outcome unknown')):
            with self.assertRaisesRegex(OSError, 'unknown'):
                daily.deliver(NOW)
        with patch.object(tg_say, '_now', return_value=NOW.replace(hour=22)):
            self.assertFalse(daily.deliver(NOW)['sent'])
        self.assertEqual(self.state()['held_date'], '2026-11-07')
        self.assertNotIn('sent_date', self.state())
        self.assertFalse(daily.deliver(NOW)['sent'])
        self.transport.assert_not_called()
        self.assertTrue(daily.deliver(NOW+timedelta(days=1))['sent'])
        self.assertEqual(self.sent[0]['sender'], 'daily-summary')
        self.assertTrue(self.sent[0]['text'].startswith('[DAILY] '))
        self.records.append(self.state())

    def test_due_window_and_timezones(self):
        for base in (NOW, NOW.replace(month=6), NOW.replace(month=10, day=25)):
            for minute, due in ((-1, False), (0, True), (59, True), (60, False)):
                clock = base.replace(hour=6, minute=0)+timedelta(minutes=minute)
                for representation in (clock, clock.astimezone(IL)):
                    self.assertEqual(bool(daily.preview([], representation)), due)
                    if due:
                        self.assertIn(clock.astimezone(IL).strftime('%Y-%m-%d'), daily.preview([], representation)[0])
                self.records.append(dict(clock=clock, due=due))


# A new interpreter imports the real modules and writes the real stores. Its only
# intercepted boundary is transport, plus deterministic source/clock fixtures.
CHILD = r'''
import sys,json,os
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
import fold,daily_summary,owed_digest,stuck_digest,tg_say,merge_loop_state as loop,thinker_inbox
root=Path(os.environ['DOIT_ROOT']); now=datetime.fromisoformat(sys.argv[1]); mode=sys.argv[2]; who=sys.argv[3]
if mode=='read':
 print(json.dumps({p.name:p.read_text() for p in (root/'state').glob('*.json*')}));sys.exit()
def transport(sender,text,root):
 with (root/'transport.jsonl').open('a') as out:out.write(json.dumps(dict(sender=sender,text=text))+'\n')
 return tg_say.Delivery(mode,200)
clock=now.replace(hour=22) if mode=='held' else now
with patch.object(tg_say,'_now',return_value=clock),patch.object(tg_say,'_transport',side_effect=transport),patch.object(loop,'day_lists',return_value=loop.DayLists([],[],[],False)),patch.object(loop,'read',return_value=loop.LoopState('stopped',[],[],None,now)),patch.object(thinker_inbox,'now',return_value=now):
 module=owed_digest if who=='owed' else stuck_digest
 code=module.run(now=now,**({'alert':True} if who=='owed' else {'send':True}))
 sys.exit(code)
'''


class StoreRoundTripTests(Scratch):
    ac = 5

    def child(self, mode, who='owed', now=NOW, wait=True):
        env = dict(os.environ, DOIT_ROOT=str(self.root), PYTHONPATH=str(Path(__file__).resolve().parent))
        args = ['/usr/bin/python3', '-c', CHILD, now.isoformat(), mode, who]
        if not wait:
            return subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        result = subprocess.run(args, env=env, capture_output=True, text=True)
        self.records.append(dict(mode=mode, who=who, code=result.returncode, output=result.stdout, error=result.stderr))
        return result

    def test_independent_readers_and_shared_lock(self):
        for who in ('owed', 'stuck'):
            result = self.child('sent', who)
            self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads(self.child('read').stdout)
        self.assertEqual(json.loads(snapshot['telegram-summary.json'])['sent_date'], '2026-11-07')
        self.assertEqual(len((self.root/'transport.jsonl').read_text().splitlines()), 1)
        children = [self.child('sent', who, NOW+timedelta(days=1), wait=False) for who in ('stuck', 'owed')]
        for child in children:
            out, err = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, err)
            self.records.append(dict(code=child.returncode, output=out, error=err))
        self.assertEqual(len((self.root/'transport.jsonl').read_text().splitlines()), 2)
        self.assertEqual(self.child('sent', 'stuck', NOW+timedelta(days=2)).returncode, 0)
        self.assertEqual(self.child('sent', 'owed', NOW+timedelta(days=2)).returncode, 0)
        self.assertEqual(len((self.root/'transport.jsonl').read_text().splitlines()), 3)

    def test_real_hold_and_failure_reopen(self):
        self.install_checks()
        for mode in ('failed', 'refused'):
            self.assertEqual(self.child(mode).returncode, 1)
            store = json.loads(json.loads(self.child('read').stdout)['telegram-summary.json'])
            self.assertNotIn('sent_date', store)
            self.assertNotIn('held_date', store)
        failed_rows = store['rows']  # silent retirement is preserved, never an acknowledgement.
        self.assertEqual(self.child('held', 'stuck').returncode, 0)
        snapshot = json.loads(self.child('read').stdout)
        store = json.loads(snapshot['telegram-summary.json'])
        self.assertNotIn('sent_date', store)
        self.assertEqual(store['held_date'], '2026-11-07')
        self.assertEqual(store['rows'], failed_rows)
        self.assertEqual(len(snapshot['tg-held.jsonl'].splitlines()), 1)
        self.assertEqual(self.child('held', 'owed').returncode, 0)
        self.assertEqual(json.loads(self.child('read').stdout)['tg-held.jsonl'], snapshot['tg-held.jsonl'])
        self.assertEqual(self.child('sent', 'owed', NOW+timedelta(days=1)).returncode, 0)
        self.records.append(json.loads(self.child('read').stdout))

    def test_included_asks_only_and_legacy_migration(self):
        self.install_checks(30)
        planned = daily.plan(owed_digest._read_ledger(), NOW, dict(rows=[]))
        self.assertGreater(len(planned['delivered']), 0)
        self.assertLess(len(planned['delivered']), 30)
        self.assertEqual(self.child('sent').returncode, 0)
        snapshot = json.loads(self.child('read').stdout)
        store = json.loads(snapshot['telegram-summary.json'])
        self.assertEqual(set(store['rows']), set(planned['rows']) | set(planned['delivered']))
        self.assertEqual(self.child('sent', 'stuck').returncode, 0)
        self.assertEqual(len((self.root/'transport.jsonl').read_text().splitlines()), 1)
        self.records.append(dict(planned=planned, snapshot=snapshot))
        # The following day's remaining eligible asks are still available.
        pending = daily.plan(owed_digest._read_ledger(), NOW+timedelta(days=1), store)
        self.assertGreater(len(pending['delivered']), 0)
        self.assertTrue(set(pending['delivered']).isdisjoint(planned['delivered']))

    def test_legacy_success_migration_from_each_source(self):
        for source in ('owed', 'event'):
            if source=='owed':
                owed_digest._write_json(self.root/'state/owed-alerted.json', dict(rows=[], streaks=[], version=owed_digest.ALERT_STATE_VERSION, sent_date='2026-11-07'))
                events=[]
            else:
                (self.root/'state/owed-alerted.json').unlink()
                events=[dict(type='operator-alert', kind='stuck-digest', sent='yes', date='2026-11-07', ts=NOW.isoformat())]
            migrated = daily.state(events, NOW)
            self.assertEqual(migrated['sent_date'], '2026-11-07')
            self.records.append(dict(source=source, migrated=migrated))

    def test_invalid_state_fails_closed(self):
        for invalid in ('{', '[]', '{"rows":[] ,"sent_date":"bad"}', '{"rows":[{}]}'):
            (self.root/'state/telegram-summary.json').write_text(invalid)
            with self.assertRaises((ValueError, TypeError)):
                daily.deliver(NOW)
        self.assertEqual(self.sent, [])


class EntryPointTests(Scratch):
    ac = 6

    def test_cutover_has_one_daily_owner(self):
        # The fixture stopgap owns DAILY before the retirement/marker step.
        self.marker.unlink()
        stopgap = self.root/'stopgap-active'
        stopgap.touch()
        census = ['stopgap']
        sent = daily.deliver(NOW, lambda argv: subprocess.CompletedProcess(argv, 0, '200\n'))
        self.assertNotIn('[DAILY]', sent['message'])
        stopgap.unlink()
        self.marker.touch()
        self.assertFalse(daily.deliver(NOW)['sent'])
        self.assertTrue(daily.deliver(NOW+timedelta(days=1))['sent'])
        self.assertFalse(stopgap.exists())
        census.append('repo')
        self.assertEqual(len(self.sent), 1)
        self.records.append(dict(daily_owners_by_day=census))

    def test_modes_revert_and_read_only_previews(self):
        def snapshot():
            return {str(p.relative_to(self.root)):hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.root.rglob('*') if p.is_file() and 'cache' not in p.parts}
        for active in (False, True, False):
            if active:
                self.marker.touch()
            else:
                self.marker.unlink(missing_ok=True)
            before = snapshot()
            messages = []
            for module in (owed_digest, stuck_digest):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(module.run(now=NOW, dry_run=True), 0)
                messages.append(json.loads(output.getvalue())['telegrams'])
                self.assertEqual(before, snapshot())
            self.assertEqual(messages[0], messages[1])
            self.assertEqual(messages[0][0].startswith('[DAILY]'), active)
            self.records.append(dict(active=active, previews=messages, before=before, after=snapshot()))
        with patch.object(tg_say, 'deliver') as core:
            result = daily.deliver(NOW, lambda argv: subprocess.CompletedProcess(argv, 0, '200\n'))
            core.assert_not_called()
            self.assertTrue(result['sent'])
            self.assertFalse(result['message'].startswith('[DAILY]'))
        self.assertFalse(self.marker.exists())
        self.marker.touch()
        self.assertFalse(daily.deliver(NOW)['sent'])
        self.assertTrue(daily.deliver(NOW+timedelta(days=1))['sent'])
        self.marker.unlink()
        self.assertFalse(daily.deliver(NOW+timedelta(days=1))['sent'])

    def test_callers_record_delivery_failures(self):
        for failure in (tg_say.Delivery('failed', 200), tg_say.Delivery('refused', 200), OSError('unknown outcome')):
            def sender(*args, **kw):
                if isinstance(failure, Exception):
                    raise failure
                return failure
            for module, flags in ((owed_digest, dict(alert=True)), (stuck_digest, dict(send=True))):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(module.run(now=NOW, sender=sender, **flags), 1)
            events = owed_digest._read_ledger(use_cache=False)
            alerts = [e for e in events if e['type']=='operator-alert' and e.get('kind')=='stuck-digest']
            self.assertTrue(alerts)
            self.assertTrue(all(e['sent']=='no' for e in alerts))
            self.assertNotIn('sent_date', self.state())
            self.assertNotIn('held_date', self.state())
            self.records.append(dict(failure=str(failure), alerts=alerts, store=self.state()))

    def test_real_core_refuses_invalid_text(self):
        result = tg_say.deliver('daily-summary', '[DAILY] bad\nYou: /private/file', label='[DAILY]', root=self.root)
        self.assertEqual(result.state, 'refused')
        self.transport.assert_not_called()
        self.records.append(asdict(result))


def review_ac6():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(EntryPointTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    ok = result.wasSuccessful() and not result.skipped and result.testsRun > 0
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE/'AC6-regressions.txt').open('w') as out:
        out.write(f'EntryPointTests: {result.testsRun} tests; success={ok}; skipped={len(result.skipped)}\n')
        for name in ('test_daily_summary_policy.py', 'test_owed_digest.py', 'test_stuck_digest.py'):
            args = ['/usr/bin/python3', str(Path(__file__).with_name(name))]
            proc = subprocess.run(args, capture_output=True, text=True)
            out.write(f'{args}: exit={proc.returncode}\n{proc.stdout}{proc.stderr}\n')
            ok = ok and proc.returncode == 0 and 'skipped=' not in proc.stderr
    return 0 if ok else 1


if __name__ == '__main__':
    if '--review-ac6' in sys.argv:
        raise SystemExit(review_ac6())
    unittest.main()
