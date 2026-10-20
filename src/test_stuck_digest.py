#!/usr/bin/env python3
"""Hermetic daily stuck digest acceptance checks; never sends to the network."""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crons
import fold
import lane_rules
import owed_digest
import stuck_digest as digest

NOW = datetime(2026, 10, 18, 6, 15, tzinfo=timezone.utc)


def fixture():
    def ev(kind, spec, minutes, actor='planner', **kv):
        return dict(type=kind, subject=spec, actor=actor,
                    ts=(NOW - timedelta(minutes=minutes)).isoformat(), **kv)
    return [ev('spec-written', 'L-spec-9001', 90, project='fixture'),
            ev('build-done', 'L-spec-9001', 89, actor='builder', status='DONE'),
            ev('escalation-blocking', 'L-spec-9001', 80, actor='operator',
               asks='blocker\nkeeps moving', deadline=(NOW + timedelta(days=1)).isoformat()),
            ev('spec-written', 'L-spec-9002', 90, project='fixture'),
            ev('spec-written', 'L-spec-9003', 1, project='fixture')]


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='stuck-digest-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = self.root / 'events'
        self.events.mkdir()
        for name, value in [('ROOT', self.root), ('EVENTS', self.events), ('PROJECT', None)]:
            p = patch.object(fold, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'DOIT_ROOT': str(self.root)})
        p.start()
        self.addCleanup(p.stop)
        self.sent = []
        self.install(fixture())

    def install(self, events):
        grouped = {}
        for event in events:
            row = dict(event)
            actor = row.pop('actor')
            grouped.setdefault(actor, []).append(row)
        for actor, rows in grouped.items():
            (self.events / f'L-{actor}-fixture.jsonl').write_text(
                ''.join(json.dumps(e) + '\n' for e in rows))

    def sender(self, argv):
        self.sent.append(argv)
        return subprocess.CompletedProcess(argv, 0, '200\n', '')

    def run_digest(self, **kw):
        return digest.run(now=NOW, sender=self.sender, **kw)

    def produced(self, kind):
        return [e for e in fold.read_events() if e['type'] == kind]

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def test_ac1(self):
        ev = fixture()
        obj = digest.build(ev, NOW)
        self.assertEqual(set(obj), {'date', 'stuck_count', 'listed', 'live_reason_count', 'rows', 'action_rows', 'waiting_rows'})
        self.assertEqual((obj['stuck_count'], obj['listed'], obj['live_reason_count']), (2, 2, 1))
        rows = lane_rules.due(ev, fold.fold(ev)[0], NOW)
        self.assertEqual(obj['rows'], sorted([r for r in rows if r['overdue']], key=lambda r: (r['spec'], r['rule'])))
        with patch.object(digest, 'datetime') as wall:
            wall.now.side_effect = AssertionError('wall clock read')
            with patch('pathlib.Path.open', side_effect=AssertionError('file read')), patch.dict(os.environ, {}, clear=True):
                # The rule table is inherited by due(); build itself reads no files.
                with patch.object(lane_rules, 'due', return_value=rows):
                    self.assertEqual(digest.build(ev, NOW), obj)
        self.assertEqual(digest.build([], NOW), dict(date='2026-10-18', stuck_count=0, listed=0, live_reason_count=0, rows=[], action_rows=[], waiting_rows=[]))
        self.assertEqual(digest.build([], NOW.astimezone(timezone(timedelta(hours=-8))))['date'], obj['date'])
        self.evidence = obj

    def test_ac2(self):
        with patch.object(lane_rules, 'DEFAULT_TABLE', self.root / 'unreadable.toml'):
            obj = digest.build(fixture(), NOW)
        self.assertEqual(obj['stuck_count'], 3)
        self.assertTrue(all(r['reason'].startswith('undetermined:') and not r['reason_live'] for r in obj['rows']))
        for module, name in [(lane_rules, 'due'), (fold, 'read_events')]:
            before = self.snapshot()
            with patch.object(module, name, side_effect=RuntimeError('read unavailable')), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertNotEqual(self.run_digest(send=True), 0)
            self.assertIn('read unavailable', err.getvalue())
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.sent, [])
        with patch.object(fold, 'EVENTS', self.root / 'missing'), contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(self.run_digest(), 0)
        self.evidence = dict(undetermined=obj, failure_side_effects=False, telegrams=self.sent)

    def test_ac3(self):
        ev = fixture()
        rows = digest.build(ev, NOW)['rows']
        rows[0] = dict(rows[0], reason='first\nsecond\rthird')
        with patch.object(lane_rules, 'due', return_value=rows), patch('health_alarms.summary_lines', return_value=['alarm\nline']):
            self.assertEqual(self.run_digest(), 0)
            path = self.root / 'content/L-stuck-digest-2026-10-18.md'
            text = path.read_text()
            self.assertEqual(self.run_digest(), 0)
            self.assertEqual(path.read_text(), text)
        self.assertLess(text.index('## STUCK'), text.index('## OWED'))
        self.assertLess(text.index('## OWED'), text.index('## HEALTH ALARMS'))
        stuck = text.split('## STUCK\n')[1].split('## OWED')[0]
        self.assertIn('stuck: 2 (live reason: 1)', stuck)
        self.assertEqual(len([line for line in stuck.splitlines() if line.startswith('- ')]), 2)
        self.assertIn('first second third', stuck)
        self.assertIn('not-live', stuck)
        self.assertIn('; live', stuck)
        for row in rows:
            for key in ('spec', 'rule', 'action', 'since', 'limit_min'):
                self.assertIn(str(row[key]), stuck)
        expected = owed_digest.render_md(owed_digest.build(fold.read_events(), NOW)).split('## Counts by status and cause\n', 1)[1].split('## NEEDS YOU', 1)[0].strip()
        self.assertEqual(text.split('## OWED\n', 1)[1].split('## HEALTH ALARMS', 1)[0].strip(), expected)
        self.assertIn('- alarm line', text)
        with patch.dict(sys.modules, {'health_alarms': None}):
            unavailable = digest.render_md(digest.build([], NOW), [], NOW)
        self.assertEqual(unavailable.split('## HEALTH ALARMS\n', 1)[1].strip(), 'health alarms: unavailable (module not merged)')
        self.assertIn('none', unavailable.split('## OWED')[0])
        self.evidence = dict(rendered=text, unavailable=unavailable)

    def test_ac4(self):
        self.assertEqual(self.run_digest(), 0)
        self.assertEqual(self.run_digest(), 0)
        raw = [json.loads(line) for line in (self.events / digest.LEDGER_NAME).read_text().splitlines()]
        self.assertEqual(len(raw), 1)
        self.assertNotIn('actor', raw[0])
        event = self.produced('stuck-digest')[0]
        self.assertEqual((event['actor'], event['subject'], event['date']), ('digest', 'stuck-digest', '2026-10-18'))
        self.assertEqual((event['stuck_count'], event['listed'], event['live_reason_count']), (2, 2, 1))
        self.assertTrue(Path(event['path']).is_file())
        self.assertIsNone(fold.admits(event, 'digest'))
        self.assertIn(event, fold.fold(fold.read_events())[3]['stuck-digest'])
        self.assertEqual(self.run_digest(send=True), 0)
        alert = self.produced('operator-alert')[0]
        self.assertIsNone(fold.admits(alert, 'digest'))
        self.assertIn(alert, fold.fold(fold.read_events())[3]['stuck-digest'])
        self.evidence = dict(raw=raw, fresh_event=event, admitted_alert=alert, fold_location=3)

    def test_ac5(self):
        before = self.snapshot()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.run_digest(send=True, dry_run=True), 0)
        self.assertEqual(json.loads(out.getvalue())['digest'], digest.build(fold.read_events(), NOW))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.run_digest(), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.produced('operator-alert'), [])
        for result in [FileNotFoundError('missing sender'), subprocess.CompletedProcess([], 1, '200\n'), subprocess.CompletedProcess([], 0, '429\n'), subprocess.CompletedProcess([], 0, '')]:
            def fail(argv):
                if isinstance(result, Exception):
                    raise result
                return result
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(digest.run(now=NOW, send=True, sender=fail), 0)
            self.assertEqual(self.produced('operator-alert')[-1]['sent'], 'no')
        # Exercise the real subprocess boundary with a local executable.
        sender = self.root / 'telegram/tg-send.sh'
        sender.parent.mkdir()
        sender.write_text('#!/bin/sh\n[ "$TG_SENDER" = stuck-digest ] || exit 3\nprintf "200\\n"\n')
        sender.chmod(0o700)
        self.assertEqual(digest.run(now=NOW, send=True), 0)
        before = self.snapshot()
        self.assertEqual(self.run_digest(send=True), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.sent, [])
        self.assertEqual(sum(e['sent'] == 'yes' for e in self.produced('operator-alert')), 1)
        # Another date exercises the injected successful sender and its argv.
        self.assertEqual(digest.run(now=NOW + timedelta(days=1), send=True, sender=self.sender), 0)
        argv = self.sent[0]
        self.assertEqual(argv[0], str(sender))
        self.assertIn('Nothing needs you today.', argv[1])
        self.assertLess(len(argv[1]), 1000)
        for word in ('stuck_count=', 'live_reason_count=', str(self.root / 'content')):
            self.assertNotIn(word, argv[1])
        self.evidence = dict(alerts=self.produced('operator-alert'), sent_argv=argv, dry_run_writes=False)

    def test_ac6(self):
        rows = {r['name']: r for r in crons.rows()}
        row = rows['stuck-digest']
        self.assertEqual((row['where'], row['schedule']), ('user', '15 6 * * *'))
        self.assertEqual(row['command'], f'{crons.HERE.parent}/doit cron-run stuck-digest -- {crons.HERE.parent}/doit stuck-digest --send >> {self.root}/logs/stuck-digest.log 2>&1')
        child = row['command'].split(' -- ', 1)[1]
        self.assertFalse(any(re.match(r'[A-Za-z_][A-Za-z_0-9]*=', t) for t in child.split()))
        self.assertRegex(row['command'], row['sig'])
        self.assertIsNone(re.search(row['sig'], rows['owed-digest']['command']))
        self.assertEqual(digest.LEDGER_NAME, 'L-digest-local.jsonl')
        before = self.snapshot()
        result = subprocess.run([str(crons.HERE.parent / 'doit'), 'stuck-digest', '--dry-run'], env={**os.environ, 'DOIT_ROOT': str(self.root)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(json.loads(result.stdout)['digest']['stuck_count'], 2)
        self.assertEqual(self.snapshot(), before)
        unknown = subprocess.run([sys.executable, __file__, '-k', 'unknown'], capture_output=True)
        self.assertEqual(unknown.returncode, 2)
        self.evidence = dict(row=row, cli_summary=json.loads(result.stdout), unknown_selection_status=unknown.returncode)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-k', choices=[f'test_ac{i}' for i in range(1, 7)])
    args = parser.parse_args()
    names = [args.k] if args.k else [f'test_ac{i}' for i in range(1, 7)]
    for name in names:
        test = DigestTests(name)
        result = unittest.TextTestRunner().run(unittest.TestSuite([test]))
        if not result.wasSuccessful():
            return 1
        criterion = name.removeprefix('test_').upper()
        body = json.dumps(test.evidence, sort_keys=True, default=str)
        print(f'{criterion} ok')
        print(json.dumps(dict(url=f'cli://stuck-digest/{criterion}', status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=test.evidence)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
