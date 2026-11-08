#!/usr/bin/env python3
"""Spec text prerequisites block both automatic admissions until they ship."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_ROOT = tempfile.TemporaryDirectory(prefix='text-hold-import-')
os.environ.update(DOIT_ROOT=_ROOT.name, DOIT_NO_POKE='1')
import autodispatch
import dependency_holds as holds
import dispatch
import fold
import nextstep
import tick

NOW = datetime(2026, 11, 1, 5, tzinfo=timezone.utc)
SID, DEP = 'L-spec-9001', 'L-spec-9002'


def event(kind, minute=0, **fields):
    return dict(type=kind, subject=SID, actor='operator', project='fixture',
                ts=(NOW + timedelta(minutes=minute)).isoformat(), **fields)


class TextHoldTests(unittest.TestCase):
    def test_single_id_and_reject_list_or_comma(self):
        for text in (f'waits_for={DEP}', f'waits_for="{DEP}"',
                     f"waits_for='{DEP}'", f'waits_for="{DEP}". Exactly one prerequisite id, as a string.'):
            self.assertEqual(holds.from_spec_text('Dependency-hold: ' + text), DEP)
        for text in ('waits_for=[]', f'waits_for=["{DEP}"]', f'waits_for={DEP},L-spec-9003',
                     f'waits_for="{DEP}", "L-spec-9003"', 'waits_for=7', 'waits_for=""',
                     'waits_for=" L-spec-9002"', 'waits_for=true'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                holds.from_spec_text('Dependency-hold: ' + text)
        with self.assertRaises(ValueError):
            holds.from_spec_text(f'Dependency-hold: waits_for={DEP}\nDependency-hold: waits_for={DEP}')
        self.assertIsNone(holds.from_spec_text(f'```\nDependency-hold: waits_for={DEP}\n```'))

    def test_automatic_build_rechecks_unrecorded_text_hold(self):
        with tempfile.TemporaryDirectory(prefix='text-hold-') as tmp:
            root = Path(tmp)
            (root / 'content').mkdir()
            (root / 'events').mkdir()
            (root / 'content' / (SID + '.md')).write_text(f'Dependency-hold: waits_for="{DEP}"\n')
            events = [event('spec-written')]
            class Transport:
                dry_run = False
                def __call__(self, argv):
                    raise AssertionError('a held build reached transport')
            transport = Transport()
            transport.events = events
            step = nextstep.Step(SID, 'spec', 'planned', 'action', 'build', 'tick',
                                 None, 'ready', None, events[0]['ts'])
            with patch.object(dispatch, 'CONTENT', root / 'content'), patch.object(dispatch, 'ROOT', root), \
                 patch.object(fold, 'ROOT', root), patch.object(fold, 'EVENTS', root / 'events'):
                self.assertIn(DEP, autodispatch._automatic_hold(events, events))
                self.assertIsNone(tick.launch(step, dispatcher=transport))
                self.assertIsNone(autodispatch._automatic_hold(events, events + [event('shipped') | {'subject': DEP}]))
                released = events + [event('unblocked', 1, ref='spec-text:' + SID)]
                self.assertIsNone(autodispatch._automatic_hold(released, released))
                # A newer recorded hold stacks with the text declaration.
                explicit = events + [event('dependency-hold', 1, waits_for='L-spec-9003', why='new prerequisite')]
                reason = autodispatch._automatic_hold(explicit, explicit)
                self.assertEqual(set(autodispatch.open_dependencies(explicit, explicit)), {DEP, 'L-spec-9003'})
                self.assertIn(DEP, reason)
                shipped = explicit + [event('shipped', 2) | {'subject': 'L-spec-9003'}]
                self.assertIn(DEP, autodispatch._automatic_hold(explicit, shipped))


if __name__ == '__main__':
    unittest.main()
