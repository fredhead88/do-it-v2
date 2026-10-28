"""Not-ready refusals leave failure scanning and existing lane holds intact."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest
from unittest.mock import patch

import lane_rules
import nextstep as ns


NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
SID = 'L-spec-9001'
SIBLING = 'L-spec-9002'


def event(kind, minutes=-30, subject=SID, **extra):
    return dict(type=kind, ts=(NOW + timedelta(minutes=minutes)).isoformat(),
                subject=subject, _src=f'L-operator-fixture.jsonl:{minutes + 20000}', **extra)


def pre_change_failure(evs, rules):
    """Embedded original scan, used to run the same snapshot before the fix."""
    latest = next((e for e in reversed(evs) if e['type'] in ns.FAILURES | ns.RETRIES), None)
    if latest and latest['type'] in ns.FAILURES:
        text = '\n'.join(str(latest.get(k) or '') for k in ('findings', 'why', 'error', 'reason'))
        return latest, ns.classify_failure(text, latest.get('exit_code', 1), rules)
    return None, None


class LaneNotReadyTests(unittest.TestCase):
    def setUp(self):
        self.rules = lane_rules.load()

    def one(self, events, *, old=False, specs=None, waves=None):
        if specs is None:
            specs = {e['subject']: dict(state='written') for e in events
                     if e['subject'].startswith('L-spec-')}
        if old:
            with patch.object(ns, '_failure', pre_change_failure):
                return ns.step_for(events, specs, SID, NOW, self.rules, waves)
        return ns.step_for(events, specs, SID, NOW, self.rules, waves)

    def was_parked(self, events, **kwargs):
        old = self.one(events, old=True, **kwargs)
        self.assertEqual((old.state, old.owner, old.reason),
                         ('hold', 'executor', 'undetermined: failure classification'))
        return old

    def report(self, ac, label, new, old):
        body = json.dumps(dict(case=label, new=asdict(new), old=asdict(old)), sort_keys=True)
        print(json.dumps(dict(url=f'cli://nextstep/L-spec-1794/AC{ac}', status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                              body_excerpt=body), sort_keys=True))
        print(f'L-spec-1794 AC{ac} ok')

    def test_ac1_wave_sibling_shipped(self):
        events = [event('spec-written', subject=s, charter='L-charter-9001')
                  for s in (SID, SIBLING)]
        events += [event('spawn-failed', -20, reason='wave-order earlier wave open'),
                   event('shipped', -10, subject=SIBLING)]
        waves = {SIBLING: 1, SID: 2}
        specs = {SID: dict(state='written'), SIBLING: dict(state='shipped')}
        old = self.was_parked(events, specs=specs, waves=waves)
        new = self.one(events, specs=specs, waves=waves)
        self.assertEqual((new.state, new.action, new.owner), ('action', 'build', 'tick'))
        self.report(1, 'wave sibling shipped', new, old)

    def test_ac1_wave_sibling_unsettled(self):
        events = [event('spec-written', subject=s, charter='L-charter-9001')
                  for s in (SID, SIBLING)]
        events += [event('spawn-failed', -20, reason='wave-order earlier wave open')]
        waves = {SIBLING: 1, SID: 2}
        old = self.was_parked(events, waves=waves)
        new = self.one(events, waves=waves)
        self.assertEqual((new.state, new.owner, new.clears),
                         ('hold', 'tick', SIBLING + ' shipped'))
        self.assertTrue(new.reason.startswith('wave:'))
        self.report(1, 'wave-held', new, old)

    def test_ac1_bundle_arrived(self):
        events = [event('spec-written', -40),
                  event('build-done', -30, actor='builder', status='DONE'),
                  event('spawn-failed', -20, reason='bundle-pending waiting for evidence'),
                  event('evidence-bundle', -10)]
        specs = {SID: dict(state='building')}
        old = self.was_parked(events, specs=specs)
        new = self.one(events, specs=specs)
        self.assertEqual((new.state, new.action, new.owner), ('action', 'grade', 'tick'))
        self.report(1, 'bundle arrived', new, old)

    def test_ac2_duplicate_still_open(self):
        spawn = 'L-builder-9001'
        events = [event('spec-written', -40),
                  event('spawn-started', -30, spawn=spawn, role='builder'),
                  event('spawn-failed', -20, spawn='L-builder-9002',
                        reason='duplicate-dispatch', why='open spawn ' + spawn)]
        old = self.was_parked(events)
        new = self.one(events)
        self.assertEqual((new.state, new.owner, new.reason),
                         ('hold', 'tick', 'in flight: ' + spawn))
        self.report(2, 'duplicate still open', new, old)

    def test_ac2_other_failures_unchanged(self):
        cases = [('spawn-failed', reason) for reason in
                 ('preflight:worktree-missing', 'timeout', '', 'spec-shape',
                  'fold: recorded-and-ignored spawn-failed by builder')]
        cases += [('merge-gate-rework', 'AssertionError'),
                  ('autodispatch-failed', 'Connection refused')]
        for kind, reason in cases:
            with self.subTest(kind=kind, reason=reason):
                events = [event('spec-written'), event(kind, -20, reason=reason)]
                self.assertEqual(ns._failure(events, self.rules),
                                 pre_change_failure(events, self.rules))
                new, old = self.one(events), self.one(events, old=True)
                self.assertEqual(new, old)
                self.report(2, f'{kind} {reason!r}', new, old)

    def test_ac2_mixed_log_keeps_real_failure(self):
        events = [event('spec-written'), event('spawn-failed', -20, reason='timeout')]
        expected = self.one(events, old=True)
        mixed = events + [event('spawn-failed', -10, reason='wave-order earlier wave open')]
        self.assertEqual(ns._failure(mixed, self.rules), pre_change_failure(events, self.rules))
        new = self.one(mixed)
        self.assertEqual(new, expected)
        self.report(2, 'timeout before wave-order', new, expected)

    def test_ac2_all_prefixes_and_retry_boundary(self):
        for prefix in ('wave-order', 'bundle-pending', 'duplicate-dispatch', 'dispatch-in-progress'):
            for suffix in ('', ' waiting'):
                with self.subTest(prefix=prefix, suffix=suffix):
                    events = [event('spec-written'),
                              event('spawn-failed', -25, reason='timeout'),
                              event('decision', -20),
                              event('spawn-failed', -10, reason=prefix + suffix)]
                    self.assertEqual(ns._failure(events, self.rules), (None, None))
                    self.assertEqual(self.one(events).action, 'build')
                    for kind in ('merge-gate-rework', 'autodispatch-failed'):
                        other = [event('spec-written'), event(kind, -10, reason=prefix + suffix)]
                        self.assertEqual(ns._failure(other, self.rules),
                                         pre_change_failure(other, self.rules))
        print('L-spec-1794 AC2 ok')


if __name__ == '__main__':
    unittest.main()
