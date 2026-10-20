"""W33: real note classes, answered asks, trends, queued work, send limits.

All events are synthetic or static note fixtures. No live sender or runner.
"""
import contextlib
import io
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

import digest_actions as actions
import owed_digest
import owed_routes
import prod_runner
import stuck_digest

NOW = datetime(2026, 10, 20, 6, 5, tzinfo=timezone.utc)


def event(event_type, subject='L-spec-9001', hours=1, actor='operator', **kv):
    return dict(type=event_type, subject=subject, actor=actor,
                ts=(NOW - timedelta(hours=hours)).isoformat(), **kv)


def open_ask(note='Please approve the production change.'):
    return [event('owed-ac', hours=5, criterion='verify', wake_at=(NOW-timedelta(hours=4)).isoformat()),
            event('shipped', hours=4, actor='executor'),
            event('owed-unobservable', hours=2, actor='owed-sweeper', criterion='verify',
                  capability='operator-action', why=note, _src='L-owed-sweeper-fixture.jsonl:1')]


class ActionableTests(unittest.TestCase):
    def test_real_note_classes(self):
        rows = json.loads((Path(__file__).parent / 'fixtures/digest-notes-2026-10-20.json').read_text())
        # Hand-reviewed acts: approvals, genuine grants and rulings. Everything
        # else is a Thinker task, irrespective of the old capability label.
        operator = {('0621', 'AC11'), ('0624', 'AC15'), ('0633', 'AC13'), ('0756', 'AC6'),
                    ('0796', 'AC8'), ('0988', 'AC13'),
                    ('0996', 'AC9'), ('0999', 'AC9'), ('1042', 'AC6'), ('1121', 'AC8')}
        self.assertEqual(len(rows), 37)
        for row in rows:
            with self.subTest(spec=row['spec'], criterion=row['criterion']):
                expected = 'operator' if (row['spec'].removeprefix('L-spec-'), row['criterion']) in operator else 'thinker'
                classified = owed_routes.classify_ask(row['note'], 'operator-action')
                self.assertEqual(classified['owner'], expected)
                obj = owed_digest.build(open_ask(row['note']), NOW)
                self.assertEqual(len(obj['needs_you']), int(expected == 'operator'))
                self.assertEqual(len(obj['thinker_rows']), int(expected == 'thinker'))
                self.assertIn(row['note'], str(obj))
                if expected == 'operator':
                    line = actions.operator_line(dict(row, ask=row['note'], **classified))
                    prose, ref = line.rsplit(' [', 1)
                    self.assertIn('Recommend', prose)
                    self.assertIn('No reply:', prose)
                    for jargon in ('L-spec-', 'ro-dsn', 'droplet', 'capability', 'cron_runs', 'owed-'):
                        self.assertNotIn(jargon, prose)
                    self.assertEqual(ref, f"{row['spec']}/{row['criterion']}]")
        # The old daily file truncated this real note before its approval gate.
        note = 'Resolves when the nginx-config hold is approved and deploy.sh runs, then integrator runs AC4 commands.'
        classified = owed_routes.classify_ask(note)
        self.assertEqual(classified['owner'], 'operator')
        line = actions.operator_line(dict(spec='L-spec-0810', criterion='AC4', ask=note, **classified))
        self.assertIn('API server access configuration', line)
        self.assertNotIn('nginx', line)

    def test_refusals_do_not_become_operator_asks(self):
        for note in ('read-only command refused', 'no commands', 'no granted wrapper runs pytest',
                     'The local script is permission denied; choose a repository command',
                     'ro-sql returned permission denied for the batch read-only role',
                     'No RO DSN is granted to this batch. Operator: grant one, or supply the query output.',
                     'No review account is provided. Operator: provide the review account.'):
            for cap in ('operator-action', 'ro-dsn-grant', 'repo'):
                self.assertEqual(owed_routes.classify_ask(note, cap)['owner'], 'thinker')
        self.assertEqual(owed_routes.classify_ask(
            'Operator: grant read access to the protected production table.')['owner'], 'operator')
        with patch.object(prod_runner.subprocess, 'run', side_effect=AssertionError('executed')):
            for record in ({'commands': [], 'needs': [], 'validity_h': 24},
                           {'commands': ['rm dangerous'], 'needs': ['repo'], 'validity_h': 24}):
                result = prod_runner.observe({}, record, now=NOW)
                self.assertEqual((result['outcome'], result['capability']), ('unobservable', 'repo'))

    def test_each_answer_kind_suppresses_the_operator_ask(self):
        for kind, fields in [('decision', {}), ('thinker-answer', {}),
                             ('operator-approval', {}), ('owed-met', {'evidence': 'proof'}),
                             ('owed-waived', {'reason': 'approved waiver'}),
                             ('owed-failed', {'evidence': 'fix needed'}),
                             ('owed-corrective', {'corrective': 'L-spec-9002'})]:
            with self.subTest(kind=kind):
                events = open_ask() + [event(kind, hours=1, criterion='verify', **fields)]
                self.assertEqual(owed_digest.build(events, NOW)['needs_you'], [])
        # Different subjects can answer by exact ref; unrelated siblings cannot.
        events = open_ask()
        other = event('decision', 'other', ref=events[-1]['_src'])
        self.assertEqual(owed_digest.build(events + [other], NOW)['needs_you'], [])
        for fields in ({'criterion': 'AC2'}, {'ref': 'unrelated:1'}, {'actor': 'builder'}):
            self.assertEqual(len(owed_digest.build(events + [event('decision', **fields)], NOW)['needs_you']), 1)
        # A later repeated seat note must not resurrect an answered ask.
        repeat = dict(events[-1], ts=(NOW-timedelta(minutes=10)).isoformat())
        self.assertEqual(owed_digest.build(events + [event('decision'), repeat], NOW)['needs_you'], [])

    def test_future_events_cannot_answer_or_change_a_historical_digest(self):
        events = open_ask()
        future = event('owed-met', hours=-1, criterion='verify', evidence='future proof')
        self.assertEqual(owed_digest.build(events + [future], NOW), owed_digest.build(events, NOW))
        self.assertEqual(owed_digest.build(events + [future], NOW + timedelta(hours=2))['needs_you'], [])

    def test_rulings_linked_to_earlier_expiry_and_historical_operator_consent(self):
        events = open_ask()
        expiry = event('escalation-blocking', hours=3, criteria='["verify"]',
                       kind='owed-expired', deadline=(NOW+timedelta(days=1)).isoformat(),
                       default='wait', _src='L-operator-fixture.jsonl:10')
        ruling = event('decision', hours=2.5, actor='thinker', ref=expiry['_src'], why='Keep it paused.')
        self.assertEqual(owed_digest.build(events + [expiry, ruling], NOW)['needs_you'], [])
        unrelated = dict(expiry, criteria='["AC2"]')
        self.assertEqual(len(owed_digest.build(events + [unrelated, ruling], NOW)['needs_you']), 1)
        approved = event('decision', hours=3, why="Operator Telegram: all yes, retire the duplicate rows.")
        self.assertEqual(owed_digest.build(events + [approved], NOW)['needs_you'], [])
        # An ordinary earlier build decision does not answer a later approval ask.
        ordinary = event('decision', hours=3, why='Restart the builder.')
        self.assertEqual(len(owed_digest.build(events + [ordinary], NOW)['needs_you']), 1)

    def test_trend_uses_yesterdays_latest_event_not_todays(self):
        counts = {'met': 972, 'failed': 35, 'blocked': 305}
        events = [event('owed-digest', hours=25, counts={'by_status': {'met': 700, 'failed': 20, 'blocked': 320}}),
                  event('owed-digest', hours=24, counts={'by_status': {'met': 707, 'failed': 26, 'blocked': 318}}),
                  event('owed-digest', hours=0.1, counts={'by_status': {'met': 966, 'failed': 34, 'blocked': 308}})]
        self.assertEqual(actions.trend(events, counts, NOW), {'met': 265, 'failed': 9, 'blocked': -13})
        self.assertEqual(actions.trend([], counts, NOW), dict.fromkeys(('met', 'failed', 'blocked')))

    def test_waiting_specs_are_audited_but_not_presented_as_stuck(self):
        ids = ['L-spec-' + n for n in ('1246', '1249', '1250', '1252', '1253', '1255', '1257', '1260')]
        events = []
        for sid in ids:
            events += [event('spec-written', sid, hours=5, project='fixture'),
                       event('build-done', sid, hours=4, actor='builder', status='DONE'),
                       event('merge-gate-clean', sid, hours=3, actor='executor')]
        events += [event('window-opened', 'win-fixture', actor='executor', specs=ids),
                   event('spec-written', 'L-spec-9002', hours=5, project='fixture'),
                   event('dependency-hold', 'L-spec-9002', waits_for=ids[0], why='wait for that merge')]
        rows = [dict(spec=sid, rule='idle', action='none', actor='thinker', overdue=True,
                     reason_live=False, reason='condition met', since=events[0]['ts'], limit_min=60)
                for sid in ids + ['L-spec-9002']]
        with patch.object(stuck_digest.lane_rules, 'due', return_value=rows):
            obj = stuck_digest.build(events, NOW)
            self.assertEqual(obj['rows'], rows)  # raw audit evidence retained
            self.assertEqual(obj['action_rows'], [])
            self.assertEqual(len(obj['waiting_rows']), 9)
            message = stuck_digest.render_telegram(obj)
            self.assertNotIn('L-spec-', message)
            self.assertIn('Nothing needs you', message)
            # A later failed gate invalidates the queued classification.
            failed = event('merge-gate-failed', ids[0], hours=0.1, actor='executor', why='failed')
            self.assertIn(ids[0], {r['spec'] for r in stuck_digest.build(events + [failed], NOW)['action_rows']})
            question = event('question', ids[1], hours=0.1, asks='Please approve the production change.',
                             deadline=(NOW+timedelta(hours=1)).isoformat(), _src='L-operator-fixture.jsonl:1')
            needs = stuck_digest.build(events + [question], NOW)
            self.assertIn('NEEDS YOU:', stuck_digest.render_telegram(needs))
            approval = event('operator-approval', ids[1], hours=0.05, ref=question['_src'])
            self.assertNotIn('NEEDS YOU:', stuck_digest.render_telegram(
                stuck_digest.build(events + [question, approval], NOW)))

    def test_phone_messages_bound_and_dry_runs_do_not_send(self):
        obj = owed_digest.build(open_ask(), NOW)
        obj['needs_you'] = [dict(obj['needs_you'][0], spec=f'L-spec-{i:04d}') for i in range(100)]
        messages = owed_digest._alerts(obj, {'rows': [], 'streaks': []})
        text, _, keys = messages[0]
        self.assertLess(len(text), 1000)
        self.assertGreater(len(keys), 0)
        for key in keys:
            self.assertIn('[' + '/'.join(key.split('/')[:2]) + ']', text)
        empty = owed_digest.build([], NOW)
        text = owed_digest._alerts(empty, {'rows': [], 'streaks': []})[0][0]
        self.assertNotIn('\n', text)
        self.assertIn('Nothing needs you today.', text)
        for module in (owed_digest, stuck_digest):
            with patch.object(owed_digest, '_read_ledger', return_value=open_ask()), \
                 patch.object(owed_digest, '_alerted', return_value={'rows': [], 'streaks': []}), \
                 patch.object(module.subprocess, 'run', side_effect=AssertionError('real send')), \
                 patch('pathlib.Path.write_text', side_effect=AssertionError('write')), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(module.run(now=NOW, dry_run=True), 0)
                self.assertTrue(json.loads(output.getvalue())['telegrams'])

    def test_legacy_silent_retirement_does_not_hide_remaining_asks(self):
        obj = owed_digest.build(open_ask(), NOW)
        key = owed_digest._row_key(obj['needs_you'][0])
        with tempfile.TemporaryDirectory(prefix='digest-state-') as temporary:
            root = Path(temporary)
            (root / 'state').mkdir()
            state = root / 'state/owed-alerted.json'
            state.write_text(json.dumps(dict(rows=[key], streaks=[], sent_date=NOW.date().isoformat())))
            original = state.read_bytes()
            with patch.object(owed_digest.fold, 'ROOT', root):
                migrated = owed_digest._alerted()
                self.assertEqual(migrated['rows'], [])
                self.assertEqual(migrated['version'], owed_digest.ALERT_STATE_VERSION)
                self.assertEqual(owed_digest._alerts(obj, migrated), [])  # no second send today
                tomorrow = dict(obj, at=(NOW+timedelta(days=1)).isoformat())
                self.assertIn('NEEDS YOU:', owed_digest._alerts(tomorrow, migrated)[0][0])
                self.assertEqual(state.read_bytes(), original)  # read/dry-run never migrates live state
                state.write_text(json.dumps(dict(migrated, rows=[key])))
                self.assertEqual(owed_digest._alerted()['rows'], [key])  # v2 delivery dedup stays authoritative


if __name__ == '__main__':
    unittest.main()
