#!/usr/bin/env python3
"""Scratch integration evidence for merge-news; no live transport or cron install."""
from contextlib import ExitStack
from datetime import datetime, timedelta
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import merge_news as news
import tg_say

REAL_FUNCTIONS = {name: getattr(tg_say, name) for name in ('deliver', 'drain_held')}

REPO = Path(__file__).resolve().parents[1]
EVIDENCE = Path(os.environ.get('MERGE_NEWS_EVIDENCE_DIR', '/tmp/doit-L-spec-2174-review'))
NOW = datetime.fromisoformat('2026-11-07T12:00:00+02:00')
MERGE = 'window W merged. shipped=2094, 2017 dropped='
FAIL = 'rehearsal of 2094 found a problem. Set aside: 2094. Picking replacements and rehearsing again.'
REGRESSIONS = ['test_crons', 'test_cron_run', 'test_doit_test', 'test_launch', 'test_up',
    'test_window_gate', 'test_cron_cadence', 'test_install_sync', 'test_look', 'test_retired',
    'test_howto', 'test_build_runner', 'test_packet', 'test_packet_lint', 'test_problems',
    'test_reap_tmp', 'test_resources', 'test_scratch', 'test_shape', 'test_throughput',
    'test_balance', 'test_build_readiness', 'test_ledger_rollup', 'test_pace',
    'test_reap_scratch_db', 'test_reap_worktrees', 'test_spec_preflight', 'test_sweep_owed',
    'test_transcript_scan']


class Evidence(unittest.TestCase):
    ac = 0

    def setUp(self):
        self.fixture = Path(tempfile.mkdtemp(prefix=self._testMethodName + '-',
            dir=self.evidence_parent()))
        self.root = self.fixture / 'root'
        (self.root / 'content').mkdir(parents=True)
        (self.root / 'logs').mkdir()
        (self.root / 'logs/tg-send.jsonl').write_text('')
        for number in (2094, 2017):
            (self.root / f'content/L-spec-{number}.md').write_text(
                '## Goal\n\nIn plain English: Change ' + str(number) + ' makes work clearer.\n')
        self.checks = []
        self.calls = []
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(tg_say, 'deliver', side_effect=self.send))
        self.stack.enter_context(patch.object(tg_say, 'drain_held', side_effect=self.drain))

    def evidence_parent(self):
        parent = EVIDENCE / f'fixtures/AC{self.ac}'
        parent.mkdir(parents=True, exist_ok=True)
        return parent

    def send(self, sender, text, *, label, root=None):
        self.calls.append(dict(call='deliver', sender=sender, text=text, label=label, root=str(root)))
        return tg_say.Delivery('sent', 200)

    def drain(self, now, root=None):
        self.calls.append(dict(call='drain', now=now.isoformat(), root=str(root)))
        return []

    def check(self, name, actual, expected):
        self.checks.append(json.loads(json.dumps(dict(case=name, actual=actual, expected=expected, passed=actual == expected), default=str)))
        self.assertEqual(actual, expected, name)

    def snapshot(self, name='snapshot'):
        rows = news.read(self.root)
        (self.fixture / (name + '.json')).write_text(json.dumps(rows, indent=2))
        return rows

    def record(self, text=MERGE, now=NOW):
        return news.record(text, root=self.root, now=now)

    def flush(self, now=NOW + timedelta(minutes=20)):
        return news.flush(root=self.root, now=now)

    def tearDown(self):
        self.stack.close()
        store = self.root / 'state/merge-news.jsonl'
        if store.exists():
            (self.fixture / 'committed-state.jsonl').write_bytes(store.read_bytes())
        payload = dict(case=self.id(), input='see retained state and checks', checks=self.checks,
                       calls=self.calls, fixture=str(self.fixture.relative_to(EVIDENCE)))
        body = json.dumps(payload, sort_keys=True).encode()
        payload['response'] = dict(url='scratch://merge-news/' + self.id(),
            status=200 if self.checks and all(c['passed'] for c in self.checks) else 500,
            body_sha256=hashlib.sha256(body).hexdigest(), body_excerpt=body[:240].decode())
        casefile = self.fixture / 'result.json'
        casefile.write_text(json.dumps(payload, indent=2))
        cases = sorted(self.evidence_parent().glob('*/result.json'), key=lambda p: p.stat().st_mtime_ns)
        latest = {item['case']: item for p in cases for item in [json.loads(p.read_text())]}
        (EVIDENCE / f'AC{self.ac}.json').write_text(json.dumps(list(latest.values()), indent=2))


class RecordFactsTests(Evidence):
    ac = 1

    def test_census(self):
        rows = [
            ('local rehearsal passed for 2094, L-spec-2017; the full CI rehearsal has been running since the batch started. The window reuses it and lands in minutes.', 'progress', [], ['L-spec-2094', 'L-spec-2017']),
            ('rehearsal of 2094 could not run its tests (L-spec-9999 /local). Not blaming any spec; Thinker is checking.', 'failed', [], ['L-spec-2094']),
            (FAIL, 'failed', ['L-spec-2094'], ['L-spec-2094']),
            (MERGE + '2099', 'merged', ['L-spec-2094', 'L-spec-2017'], []),
            ('window W did not merge: L-spec-9999 exit=7', 'failed', [], []),
            ('full CI rehearsal of 2094 2017 was not green: L-spec-9999. Holding that batch; Thinker is checking.', 'failed', [], ['L-spec-2094', 'L-spec-2017']),
            ('window W started: 2094. CI takes about 40 minutes.', 'progress', [], ['L-spec-2094'])]
        with patch.object(news.merge_loop_state, 'plain_line', side_effect=AssertionError('record fetched description')):
            for text, kind, specs, context in rows:
                for prefix in ('', 'Merge push: '):
                    self.record(prefix + text)
                    row = news.classify(prefix + text)[0]
                    self.check(text, [row['kind'], row['specs'], row['context_specs']], [kind, specs, context])
        self.check('record has no transport or drain', self.calls, [])
        self.check('committed real store', bool(self.snapshot()), True)

    def test_truncation_and_untrusted_input(self):
        for suffix, expected in [('2094, 20', ['L-spec-2094']), ('2094, 2017', ['L-spec-2094']),
                                  ('20', []), ('2094', []), ('2094 dropped=', ['L-spec-2094']),
                                  ('L-spec-2094, L-spec-2017 dropped=', ['L-spec-2094', 'L-spec-2017'])]:
            rows = self.record('window W merged. shipped=' + suffix)
            self.check(suffix, [s for r in rows if r['kind'] == 'merged' for s in r['specs']], expected)
            if 'dropped=' not in suffix:
                self.check('truncation diagnostic', any(r['kind'] == 'unknown' for r in rows), True)
        for aside in ('', '.', '9999', '../2094', '2094; touch /x'):
            text = 'rehearsal of 2094 found a problem. Set aside: ' + aside + '. Picking replacements and rehearsing again.'
            rows = self.record(text)
            self.check('no invented blame ' + aside, [s for r in rows for s in r['specs']], [])
        for text in ('arbitrary L-spec-2094', '$(touch /x)\nmalformed', '/local', ''):
            self.check('unknown remains diagnostic', self.record(text)[0]['disposition'], 'diagnostic')


class BatchFormattingTests(Evidence):
    ac = 2

    def test_boundary_real_formatter_and_release(self):
        self.record()
        self.record('window W did not merge: incidental L-spec-1234 /local')
        self.check('19:59', self.flush(NOW + timedelta(minutes=19, seconds=59))['state'], 'waiting')
        self.check('no early send', [c for c in self.calls if c['call'] == 'deliver'], [])
        with patch.object(news.merge_loop_state, 'plain_line', wraps=news.merge_loop_state.plain_line) as plain:
            self.check('20:00', self.flush()['state'], 'sent')
            self.check('only attributed descriptions', [c.args[0] for c in plain.call_args_list], ['L-spec-2094', 'L-spec-2017'])
        text = [c['text'] for c in self.calls if c['call'] == 'deliver'][0]
        self.check('real formatted clauses', all(x in text for x in ('[MERGED]', 'You: nothing.', 'No release is scheduled.', 'Change 2094')), True)
        self.check('unsafe raw fragments excluded', any(x in text for x in ('dropped=', '/local', 'UTC', '40 minutes', 'Set aside: .')), False)
        self.check('acknowledged once', self.flush()['state'], 'idle')
        self.snapshot()
        self.record(now=NOW + timedelta(hours=1))
        with patch.object(news.merge_loop_state, 'next_release', return_value='2026-11-07 15:00'):
            self.flush(NOW + timedelta(hours=1, minutes=20))
        self.check('release clock consumed', '15:00 Israel time' in self.calls[-2]['text'], True)

    def test_overflow_retention_and_repeat(self):
        for number in range(80):
            self.record('window W merged. shipped=' + str(3000 + number) + ' dropped=')
        failed = self.record(FAIL)[0]
        self.flush()
        rows = self.snapshot('overflow')
        omitted = [r for r in rows if r['disposition'] == 'daily']
        text = self.calls[-2]['text']
        self.check('bounded length', len(text) <= 3500, True)
        self.check('exact omitted fact count', f'and {len(omitted)} more' in text, True)
        self.check('failure was omitted', next(r for r in rows if r['id'] == failed['id'])['disposition'], 'daily')
        self.record(FAIL, NOW + timedelta(hours=1))
        self.check('omitted failure still eligible', self.flush(NOW + timedelta(hours=1, minutes=20))['state'], 'sent')
        self.check('subsequent named send suppresses', self.record(FAIL, NOW + timedelta(hours=2))[0]['disposition'], 'daily')

    def test_formatter_refusal_preserves_pending(self):
        self.record()
        with patch.object(news.merge_loop_state, 'plain_line', return_value='/local unsafe UTC'):
            with self.assertRaises(RuntimeError):
                self.flush()
        self.check('unsafe dependency never bypasses formatter', [c for c in self.calls if c['call'] == 'deliver'], [])
        self.check('still pending', {r['disposition'] for r in self.snapshot()}, {'pending'})


class StoreDeliveryTests(Evidence):
    ac = 3

    def child(self, code, *args):
        env = dict(os.environ, DOIT_ROOT=str(self.root), PYTHONPATH=str(REPO / 'src'), PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run(['/usr/bin/python3', '-c', code, *args], cwd=REPO,
                                env=env, text=True, capture_output=True)
        (self.fixture / ('child-' + str(len(self.checks)) + '.json')).write_text(json.dumps(
            dict(command=code, exit_status=result.returncode, stdout=result.stdout, stderr=result.stderr)))
        self.check('child exit status', result.returncode, 0)
        return result.stdout

    def test_process_commit_restart(self):
        self.child('import merge_news; raise SystemExit(merge_news.main(["record",' + repr(MERGE) + ']))')
        read = self.child('import merge_news,json; print(json.dumps(merge_news.read(merge_news.root_path())))')
        self.check('fresh reader observes committed facts', len(json.loads(read)), 2)
        self.child('''import merge_news as m, tg_say
from datetime import datetime,timedelta
m.tg_say.deliver=lambda *a,**k: tg_say.Delivery('sent',200)
m.tg_say.drain_held=lambda *a,**k: []
now=datetime.now(m.ISRAEL)+timedelta(days=1)
now=now.replace(hour=12)
assert m.flush(now=now)['state']=='sent'
''')
        self.check('reopen after restarted success', {r['disposition'] for r in self.snapshot()}, {'sent'})
        self.check('private store', (self.root / 'state/merge-news.jsonl').stat().st_mode & 0o777, 0o600)

    def test_delivery_states_and_retry(self):
        self.record()
        before = self.snapshot('before')
        for state in ('held', 'failed', 'refused', 'exception'):
            with patch.object(tg_say, 'deliver', side_effect=OSError('test') if state == 'exception' else None,
                              return_value=tg_say.Delivery(state)):
                with self.assertRaises(RuntimeError):
                    self.flush()
            self.check(state + ' preserves pending', self.snapshot(state), before)
        self.check('retry succeeds', self.flush()['state'], 'sent')
        self.check('reopen no duplicate', self.flush()['state'], 'idle')

    def test_corrupt_store_is_visible_and_not_rewritten(self):
        self.record()
        path = self.root / 'state/merge-news.jsonl'
        for contents in (path.read_bytes() + b'{bad\n', b'{}\n'):
            path.write_bytes(contents)
            with self.assertRaises(RuntimeError):
                self.flush()
            self.check('corruption preserved', path.read_bytes(), contents)
        self.check('still drains independently', self.calls[-1]['call'], 'drain')

    def test_concurrent_record_and_overlapping_flush(self):
        self.record()
        entered = threading.Event()
        release = threading.Event()
        errors = []
        def send(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test timed out')
            return self.send(*args, **kwargs)
        def run(fn):
            try:
                fn()
            except Exception as exc:
                errors.append(str(exc))
        with patch.object(tg_say, 'deliver', side_effect=send):
            first = threading.Thread(target=run, args=(self.flush,))
            first.start()
            self.assertTrue(entered.wait(10))
            second = threading.Thread(target=run, args=(self.flush,))
            writer = threading.Thread(target=run, args=(lambda: self.record('window new merged. shipped=3001 dropped=', NOW + timedelta(minutes=20)),))
            second.start()
            writer.start()
            release.set()
            for thread in (first, second, writer):
                thread.join(10)
                self.assertFalse(thread.is_alive())
        self.check('concurrent calls succeed', errors, [])
        self.check('one acknowledged batch', len([c for c in self.calls if c['call'] == 'deliver']), 1)
        self.check('new fact retained pending', [r['specs'] for r in self.snapshot() if r['disposition'] == 'pending'], [['L-spec-3001']])


class DedupBudgetTests(Evidence):
    ac = 4

    def test_reason_whitespace_normalization(self):
        template = 'rehearsal of 2094 could not run its tests ({}). Not blaming any spec; Thinker is checking.'
        self.record(template.format('runner unavailable'))
        self.flush()
        repeated = self.record(template.format('runner  \n unavailable'), NOW + timedelta(hours=1))
        self.check('whitespace-only reason changes stay suppressed', repeated[0]['disposition'], 'daily')
        different = self.record(template.format('runner timed out'), NOW + timedelta(hours=1))
        self.check('substantive reason changes remain eligible', different[0]['disposition'], 'pending')
        self.snapshot()

    def log(self, count):
        rows = []
        for i in range(count):
            row = dict(time=NOW.isoformat(), sender='other', label='[DAILY]', length=100,
                       id=str(i), http=None, **{'class': 'automated'})
            rows.extend([row | dict(event='attempt'), row | dict(event='result', http=200)])
        rows.extend([row | dict(event='result', id='reply', http=200, **{'class': 'reply'}),
                     row | dict(event='result', id='failure', http=500)])
        (self.root / 'logs/tg-send.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))

    def test_budget_pairs_and_merge_exception(self):
        self.log(14)
        self.check('paired rows count once; replies/failures ignored', news.budget(self.root, NOW), 14)
        self.record(FAIL)
        self.check('14 permits failure', self.flush()['state'], 'sent')
        self.log(15)
        self.record(FAIL.replace('2094', '2017'), NOW + timedelta(hours=1))
        self.check('15 routes daily', self.flush(NOW + timedelta(hours=1, minutes=20))['state'], 'daily')
        self.record(FAIL.replace('2094', '2017'), NOW + timedelta(hours=2))
        self.record(MERGE, NOW + timedelta(hours=2))
        self.check('confirmed merge permits eligible failure', self.flush(NOW + timedelta(hours=2, minutes=20))['state'], 'sent')
        self.check('failure described in mixed batch', 'Rehearsal failed for L-spec-2017' in self.calls[-2]['text'], True)
        self.snapshot()

    def test_absent_corrupt_unread_log_crossed_with_batch_type(self):
        original = self.root
        for mode in ('absent', 'corrupt', 'unreadable'):
            for merged in (False, True):
                self.root = original / (mode + str(merged))
                (self.root / 'logs').mkdir(parents=True)
                path = self.root / 'logs/tg-send.jsonl'
                if mode == 'corrupt':
                    path.write_text('{bad\n')
                if mode == 'unreadable':
                    path.mkdir()  # deterministic EISDIR even when tests run as root
                self.record(FAIL)
                if merged:
                    self.record()
                result = self.flush()
                self.check(mode + ' merge=' + str(merged), result['state'], 'sent' if merged else 'daily')
                rows = self.snapshot(mode + str(merged))
                self.check('visible budget diagnostic', any(r['reason'] == 'budget-unread' for r in rows), True)
        self.root = original

    def test_reason_identity_and_israel_day_reset(self):
        self.record(FAIL)
        self.flush()
        self.check('same named reason suppressed', self.record(FAIL, NOW + timedelta(hours=1))[0]['disposition'], 'daily')
        different = 'rehearsal of 2094 could not run its tests (unavailable). Not blaming any spec; Thinker is checking.'
        self.check('different reason eligible', self.record(different, NOW + timedelta(hours=1))[0]['disposition'], 'pending')
        tomorrow = NOW + timedelta(days=1)
        self.check('next Israel date resets', self.record(FAIL, tomorrow)[0]['disposition'], 'pending')
        self.log(15)
        local = datetime.fromisoformat('2026-11-08T00:05:00+02:00')
        self.check('budget resets at Israel midnight', news.budget(self.root, local), 0)
        self.snapshot()


class QuietDrainTests(Evidence):
    ac = 5

    def test_quiet_boundaries_and_morning_override(self):
        for day in ('2026-01-03', '2026-07-03', '2026-03-27', '2026-10-25'):
            for hour, minute, expected in [(22,59,False),(23,0,True),(6,59,True),(7,0,False)]:
                now = datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=news.ISRAEL)
                self.check('Israel boundary ' + now.isoformat(), tg_say.quiet(now), expected)
                self.check('same UTC instant', tg_say.quiet(now.astimezone(__import__('datetime').timezone.utc)), expected)
        early = NOW.replace(hour=6, minute=55)
        self.record(now=early)
        self.check('quiet silent', self.flush(early + timedelta(minutes=4))['state'], 'quiet')
        self.check('no quiet calls', self.calls, [])
        self.check('young overnight due at seven', self.flush(early.replace(hour=7, minute=0))['state'], 'sent')
        self.check('batch then independent drain', [c['call'] for c in self.calls], ['deliver', 'drain'])
        self.snapshot()

    def test_daytime_wait_and_failed_send_drain(self):
        self.flush(NOW.replace(hour=7, minute=0))
        daytime = NOW.replace(hour=7, minute=1)
        self.record(now=daytime)
        self.check('early daytime flush does not confer override', self.flush(daytime + timedelta(minutes=1))['state'], 'waiting')
        with patch.object(tg_say, 'deliver', return_value=tg_say.Delivery('failed')):
            with self.assertRaises(RuntimeError):
                self.flush(daytime + timedelta(minutes=20))
        self.check('failed batch still drains', self.calls[-1]['call'], 'drain')
        self.check('failed facts retained', {r['disposition'] for r in self.snapshot()}, {'pending'})
        self.check('daytime due', self.flush(daytime + timedelta(minutes=20))['state'], 'sent')

    def test_delayed_morning_old_plus_young_and_no_held_file_access(self):
        self.record(now=NOW.replace(hour=23)-timedelta(days=1))
        self.record(FAIL, NOW.replace(hour=6, minute=55))
        original_open = os.open
        def guarded(path, *args, **kwargs):
            if str(path).endswith('tg-held.jsonl'):
                raise AssertionError('merge-news touched tg-core hold')
            return original_open(path, *args, **kwargs)
        with patch('os.open', side_effect=guarded):
            self.check('delayed morning sends whole overnight batch', self.flush(NOW.replace(hour=7, minute=3))['state'], 'sent')
        self.check('all overnight acknowledged', {r['disposition'] for r in self.snapshot()}, {'sent'})
        with patch.object(tg_say, 'drain_held', return_value=[tg_say.Delivery('failed')]):
            with self.assertRaises(RuntimeError):
                self.flush(NOW.replace(hour=8))
        self.check('later empty flush retries drain', self.flush(NOW.replace(hour=9))['state'], 'idle')


class CliCronTests(Evidence):
    ac = 6

    def test_real_signatures(self):
        for module, name, parameters in [(tg_say,'deliver',['sender','text','label','root']),
                (tg_say,'drain_held',['now','root']), (tg_say,'quiet',['now']),
                (news.notification_policy,'format_message',['label','what','real','you','by','doing']),
                (news.merge_loop_state,'plain_line',['spec_id','root']),
                (news.merge_loop_state,'next_release',['now','root'])]:
            function = getattr(module, name)
            if hasattr(function, '_mock_wraps'):
                function = REAL_FUNCTIONS[name]
            self.check('signature ' + name, list(inspect.signature(function).parameters), parameters)

    def test_dispatch_and_expanded_cron(self):
        import crons
        import fold
        shim = self.fixture / 'intercept'
        shim.mkdir()
        (shim / 'sitecustomize.py').write_text("""import json, os
from pathlib import Path
import tg_say
tg_say.quiet = lambda now: False
def transport(sender, text, root):
    with (Path(root) / 'transport-calls.jsonl').open('a') as out:
        out.write(json.dumps(dict(sender=sender, text=text)) + '\\n')
    return tg_say.Delivery('sent', 200)
tg_say._transport = transport
""")
        env = dict(os.environ, DOIT_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE='1',
                   PYTHONPATH=str(shim) + os.pathsep + str(REPO / 'src'))
        def run(args, **kwargs):
            result = subprocess.run(args, cwd=REPO, env=env, capture_output=True, text=True, **kwargs)
            self.calls.append(dict(command=args, status=result.returncode, stdout=result.stdout, stderr=result.stderr))
            return result
        self.check('dispatch record', run([str(REPO/'doit'), 'merge-news', 'record', MERGE]).returncode, 0)
        self.check('dispatch preserves text', {r['text'] for r in self.snapshot()}, {MERGE})
        self.check('invalid arguments preserve status', run([str(REPO/'doit'), 'merge-news', 'record']).returncode, 2)
        self.check('direct CLI invalid command', run(['/usr/bin/python3', str(REPO/'src/merge_news.py'), 'no']).returncode, 2)
        manifest = self.fixture/'crons.toml'
        manifest.write_bytes((REPO/'crons.toml').read_bytes())
        with patch.object(fold, 'ROOT', self.root):
            rows = [r for r in crons.rows(manifest) if r['name']=='merge-news']
        self.check('one cron row', len(rows), 1)
        row = rows[0]
        self.check('minute cadence', crons.interval_s(row['schedule']), 60)
        self.check('row ownership', [row['where'], row['owner'], row['project'], row['path']], ['user','thinker','do-it-v2',str(REPO/'doit')])
        self.check('signature matches flush', bool(__import__('re').search(row['sig'], row['command'])), True)
        # Use a real committed old fact and intercept only transport/quiet time
        # in child interpreters, so cron-run executes a due production flush.
        self.record(now=datetime.now(news.ISRAEL) - timedelta(days=1))
        self.check('expanded manifest command executes', run(row['command'], shell=True).returncode, 0)
        runs = [json.loads(line) for line in (self.root/'state/cron-runs/merge-news.jsonl').read_text().splitlines()]
        self.check('cron-run recorded child completion', [r['ev'] for r in runs], ['start','end'])
        self.check('cron child success', runs[-1]['rc'], 0)
        transports = [json.loads(line) for line in (self.root/'transport-calls.jsonl').read_text().splitlines()]
        self.check('CLI used named intercepted transport', [t['sender'] for t in transports], ['merge-news'])
        self.check('CLI flush acknowledged real state', {r['disposition'] for r in self.snapshot('cli-flush')}, {'sent'})
        blocked = self.fixture/'not-directory'
        blocked.write_text('x')
        env['DOIT_ROOT'] = str(blocked)
        self.check('failed persistence nonzero', run([str(REPO/'doit'), 'merge-news', 'record', MERGE]).returncode, 1)


def review_ac6():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CliCronTests)
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    rows = []
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE/'AC6-regressions.txt').open('w') as out:
        for name in REGRESSIONS:
            command = ['/usr/bin/python3', 'src/' + name + '.py']
            try:
                result = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=300,
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
                row = dict(file=command[-1], command=command, status=result.returncode,
                           stdout=result.stdout, stderr=result.stderr)
            except (OSError, subprocess.TimeoutExpired) as exc:
                row = dict(file=command[-1], command=command, status=1, error=type(exc).__name__)
            rows.append(row)
            out.write(json.dumps(row) + '\n')
            print(row['file'], row['status'], flush=True)
    path = EVIDENCE/'AC6.json'
    payload = json.loads(path.read_text()) if path.exists() else []
    payload.append(dict(case='referencing regression census', results=rows))
    path.write_text(json.dumps(payload, indent=2))
    return 0 if outcome.wasSuccessful() and all(r['status']==0 for r in rows) else 1


if __name__ == '__main__':
    if sys.argv[1:] == ['--review-ac6']:
        raise SystemExit(review_ac6())
    unittest.main()
