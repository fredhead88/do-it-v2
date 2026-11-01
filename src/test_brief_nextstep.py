"""Dossiers show the tick's actual next action, owner, and clearing condition."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

# Import-time roots also stay disposable when this file runs by itself.
ISOLATION = tempfile.TemporaryDirectory(prefix='brief-nextstep-')
os.environ['DOIT_ROOT'] = str(Path(ISOLATION.name) / 'root')
os.environ['HOME'] = str(Path(ISOLATION.name) / 'home')
import brief
import fold
import tick

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
SID, CID, GID = 'L-spec-9901', 'L-charter-9901', 'L-goal-9901'


class DossierNextStepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dossier-next-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'content').mkdir()
        for item in (SID, CID, GID):
            (self.root / 'content' / f'{item}.md').write_text(
                '# Fixture\n\n## 1. Intent\nA fixture.\n')
        self.events = [self.event('goal-filed', GID, goal=None),
                       self.event('charter-filed', CID, goal=GID, covers='none'),
                       self.event('spec-written', SID, charter=CID, project='fixture'),
                       dict(self.event('spawn-done', SID, spawn='L-spec-auditor-9901'),
                            actor='spec-auditor', bad_cut=False, _src='L-spec-auditor-9901.jsonl:1')]
        

    def event(self, kind, subject=SID, **fields):
        actor = 'spec-writer' if kind == 'spec-written' else 'thinker'
        return dict(type=kind, subject=subject, ts=(NOW - timedelta(hours=1)).isoformat(),
                    actor=actor, _src=f'L-{actor}-fixture.jsonl:{len(getattr(self, "events", []))+1}',
                    **fields)

    def check_same_next(self, extra):
        events = self.events + extra
        specs = fold.fold(events)[0]
        step = next(s for s in tick._steps(events, specs, NOW)
                    if s.item == SID and s.item_kind == 'spec')
        with patch.object(tick, '_steps', wraps=tick._steps) as evaluate, \
             patch.object(fold, 'read_events', side_effect=AssertionError('second ledger read')):
            spec = brief.dossier(SID, events, self.root, NOW)
            self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(spec['next']['owner'], step.owner)
        if step.state == 'hold':
            self.assertIn(step.reason, spec['next']['text'])
            self.assertIn(step.clears, spec['next']['text'])
        else:
            self.assertIn(step.action, spec['next']['text'])
        self.assertIn(spec['next']['text'], spec['catch_me_up'][2])
        charter = brief.dossier(CID, events, self.root, NOW)
        goal = brief.dossier(GID, events, self.root, NOW)
        waiting = f"{spec['next']['text']} ({step.owner})"
        self.assertEqual(charter['children'][0]['waiting_on'], waiting)
        self.assertEqual(charter['next'], spec['next'])
        self.assertEqual(goal['children'][0]['specs'][0]['waiting_on'], waiting)
        self.assertEqual(goal['next'], spec['next'])
        return step, spec

    def test_dependency_hold_and_release(self):
        hold = self.event('dependency-hold', waits_for='L-spec-9902')
        step, dossier = self.check_same_next([hold])
        self.assertEqual(step.state, 'hold')
        self.assertIn('L-spec-9902', dossier['next']['text'])

    def test_judgment_hold_keeps_decision_owner(self):
        self.check_same_next([self.event('decision', action='hold', why='operator prerequisite')])

    def test_routine_action_uses_tick_owner(self):
        step, _ = self.check_same_next([])
        self.assertEqual(step.state, 'action')
        self.assertEqual(step.owner, 'tick')

    def test_unreadable_rules_are_visible(self):
        with patch('lane_rules.load', side_effect=ValueError('fixture rules unavailable')):
            result = brief.dossier(SID, self.events, self.root, NOW)
        self.assertEqual(result['next']['owner'], 'executor')
        self.assertIn('fixture rules unavailable', result['next']['text'])

    def test_terminal_item_needs_no_routine_step(self):
        killed = self.events + [self.event('spec-killed')]
        with patch.object(tick, '_steps', side_effect=AssertionError('terminal evaluation')):
            dossier = brief.dossier(SID, killed, self.root, NOW)
        self.assertEqual(dossier['next'], {'text': 'nothing — done', 'owner': 'nobody'})


if __name__ == '__main__':
    unittest.main()
