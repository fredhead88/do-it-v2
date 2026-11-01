#!/usr/bin/env python3
"""Queue age must not erase window ownership or a known merge hold."""
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_ISO = tempfile.TemporaryDirectory(prefix='queue-watchdog-')
os.environ['DOIT_ROOT'] = _ISO.name
import merge_queue_watchdog as watch
from nextstep import Step

NOW = datetime(2026, 11, 1, 7, 6, 50, tzinfo=timezone.utc)
WINDOW = 'win-20261030-w36'
OPENED = ['L-spec-' + sid for sid in
          ('1881', '1656', '1809', '1876', '1706', '1901', '1912',
           '1917', '1918', '1704', '1637', '1919')]
RECANDIDATE = [sid for sid in OPENED if sid not in
               {'L-spec-1706', 'L-spec-1901', 'L-spec-1912', 'L-spec-1637'}]


def event(kind, subject, ts, **fields):
    return dict(type=kind, subject=subject, ts='2026-11-01T' + ts + '+00:00',
                project='albert-scott', **fields)


def queued(sid):
    return event('queue-add', sid, '03:17:06', branch=sid.lower())


def held(sid, reason='undetermined: conflicting actions'):
    return Step(sid, 'spec', 'merging', 'hold', None, 'executor', None,
                reason, 'window resolution', '2026-11-01T03:17:06+00:00')


def window_events():
    # Real w36 shape: list-valued membership, no queue-batch, and a later
    # smaller candidate. None of these events releases the open window.
    return [event('window-opened', WINDOW, '05:56:24', specs=OPENED),
            event('window-candidate', WINDOW, '05:56:24', specs=OPENED),
            event('window-candidate', WINDOW, '06:49:49', specs=RECANDIDATE)]


class QueueOwnershipTests(unittest.TestCase):
    def test_0706_w36_members_and_base_held_spec_are_owned(self):
        events = [queued(sid) for sid in ['L-spec-1733'] + OPENED] + window_events()
        steps = [held(sid) for sid in OPENED]
        steps.append(held('L-spec-1733', 'undetermined: base-not-releasable: pg-parity is red'))
        self.assertEqual(watch.due(list(reversed(events)), steps, NOW), [])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch('look._append_watchdog', side_effect=AssertionError('false escalation')):
                self.assertEqual(watch.check(events, {}, NOW, root, steps=steps), [])
            self.assertFalse((root / 'events').exists())

    def test_open_window_owns_members_even_without_spec_steps(self):
        for kind in ('window-opened', 'window-candidate'):
            for members in (OPENED, ','.join(OPENED)):
                with self.subTest(kind=kind, members=type(members).__name__):
                    events = [queued(sid) for sid in OPENED]
                    events.append(event(kind, WINDOW, '05:56:24', specs=members))
                    self.assertEqual(watch.due(events, [], NOW), [])

    def test_named_holds_are_owned_but_unknown_routing_still_alerts(self):
        events = [queued('L-spec-1733')]
        for reason in ('base-not-releasable', 'window-in-progress'):
            for prefix in ('', 'undetermined: '):
                with self.subTest(reason=reason, prefix=prefix):
                    self.assertEqual(watch.due(events, [held('L-spec-1733', prefix + reason)], NOW), [])
        for reason in ('undetermined: project', 'undetermined: conflicting actions'):
            self.assertEqual(len(watch.due(events, [held('L-spec-1733', reason)], NOW)), 1)

    def test_unrelated_window_and_project_do_not_hide_orphan(self):
        orphan = queued('L-spec-1733')
        events = [orphan] + window_events()
        self.assertEqual([r['spec'] for r in watch.due(events, [], NOW)], ['L-spec-1733'])
        wrong_project = dict(queued(OPENED[0]), project='do-it-v2')
        self.assertEqual(len(watch.due([wrong_project] + window_events(), [], NOW)), 1)

    def test_closed_window_releases_ownership_and_recandidate_can_reopen(self):
        for kind in ('window-verified', 'window-merged', 'window-done',
                     'window-dropped', 'window-cancelled', 'window-redcut', 'shipped'):
            with self.subTest(kind=kind):
                events = [queued(OPENED[0])] + window_events()
                events.append(event(kind, WINDOW, '07:00:00'))
                self.assertEqual(len(watch.due(events, [], NOW)), 1)
                events.append(event('window-candidate', WINDOW, '07:01:00', specs=[OPENED[0]]))
                self.assertEqual(watch.due(events, [], NOW), [])

if __name__ == '__main__':
    unittest.main()
