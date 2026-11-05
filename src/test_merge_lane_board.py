"""Scratch ledger and engine-renderer coverage for the merge-lane board seam."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_import_root = tempfile.TemporaryDirectory(prefix='merge-lane-board-import-')
os.environ['DOIT_ROOT'] = _import_root.name
os.environ['DOIT_NO_POKE'] = '1'
import fold
import merge_lane_board as board
import merge_queue


class MergeLaneBoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='merge-lane-board-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(fold, 'ROOT', self.root))
        self.stack.enter_context(patch.object(fold, 'EVENTS', self.root / 'events'))
        self.stack.enter_context(patch.object(fold, 'PROJECT', None))
        self.stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1',
                                           DOIT_READ_CACHE='0'))
        self.start = datetime(2026, 11, 5, tzinfo=timezone.utc)

    def event(self, kind, subject='window-one', project='fixture', minute=0, **fields):
        return dict(type=kind, subject=subject, project=project, actor='executor',
                    ts=(self.start + timedelta(minutes=minute)).isoformat(), **fields)

    def emit(self, kind, subject, **fields):
        merge_queue._train_emit('L-executor-board.jsonl', subject, kind, **fields)

    def test_persisted_three_batch_chain_renders_through_engine_in_fresh_process(self):
        inventory = []
        base = 'a' * 40
        for index, size in enumerate((4, 4, 1)):
            ident = f'window-board-{index}'
            specs = [f'L-spec-{9000 + index * 4 + member}' for member in range(size)]
            candidate = str(index + 1) * 40
            inventory.append(dict(ident=ident, specs=specs, base=base, candidate=candidate))
            self.emit('queue-batch', ident, project='fixture', specs=','.join(specs))
            self.emit('window-rehearsed', ident, project='fixture', specs=specs,
                      base_sha=base, sha=candidate, state='running', rehearsal=True)
            base = candidate
        last = inventory[-1]
        parked = last['specs'][0]
        self.emit('window-rehearsed', last['ident'], project='fixture', specs=last['specs'],
                  base_sha=last['base'], sha=last['candidate'], state='red', rehearsal=True,
                  attribution=dict(proven={parked: ['tests/test_total.py::test_total']},
                                   drop={parked: ['tests/test_total.py::test_total']}))
        self.emit('queue-remove', parked, project='fixture', window=last['ident'],
                  reason='attributed rehearsal red')
        env = dict(os.environ, DOIT_PROJECT='fixture')
        result = subprocess.run([sys.executable, str(Path(fold.__file__)), 'board'],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = result.stdout.split('## MERGE LANE\n', 1)[1].split('\n\n', 1)[0]
        for index, batch in enumerate(inventory):
            self.assertIn(batch['ident'], rendered)
            self.assertIn('specs=' + ','.join(batch['specs']), rendered)
            self.assertIn('chain=' + batch['base'] + ' -> ' + batch['candidate'], rendered)
            self.assertIn('stacked-on=' + batch['base'], rendered)
            self.assertIn('candidate=' + batch['candidate'], rendered)
        self.assertIn('rehearsal=running', rendered)
        self.assertIn('rehearsal=red', rendered)
        self.assertIn('ATTRIBUTED ' + parked, rendered)
        self.assertIn('FALLBACK ' + parked, rendered)
        self.assertIn('disposition=parked', rendered)
        self.assertIn('reason=attributed rehearsal red', rendered)
        body = json.dumps(dict(inventory=inventory, board=rendered), sort_keys=True)
        print(json.dumps(dict(url='cli://doit-v2/merge-lane-board/scratch', status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def test_project_admission_and_terminal_receipts(self):
        batch = self.event('queue-batch', specs='L-spec-9000', rehearsal=True)
        receipt = self.event('window-rehearsed', base_sha='a' * 40, specs=['L-spec-9000'],
                             sha='b' * 40, state='green')
        for terminal in ('window-verified', 'window-redcut', 'escalation-blocking'):
            end = self.event(terminal, minute=1)
            self.assertEqual(board.section([end, receipt, batch]), [])
            if terminal == 'window-redcut':
                self.assertIn('rehearsal=green', '\n'.join(board.section([batch, receipt, dict(end, actor='builder')])))
        with patch.object(fold, 'PROJECT', 'other'):
            self.assertEqual(board.section([batch, receipt]), [])
        self.assertEqual(board.section([dict(batch, actor='builder'), receipt]), [])
        self.assertIn('rehearsal=pending', '\n'.join(board.section([batch, dict(receipt, actor='builder')])))
        self.assertIn('rehearsal=pending', '\n'.join(board.section([batch, dict(receipt, project='other')])))
        self.assertIn('candidate=undetermined', '\n'.join(board.section([batch])))
        self.assertEqual(board.section([dict(batch, rehearsal=False)]), [])

    def test_cancellation_is_not_terminal_until_disposition_commits(self):
        batch = self.event('queue-batch', specs='L-spec-9000', rehearsal=True)
        cancel = self.event('window-cancelled', minute=1, after='previous', reason='rebuilding')
        self.assertTrue(board.section([batch, cancel]))
        self.assertEqual(board.section([batch, dict(cancel, reason='escalation-default')]), [])

    def test_untrusted_fields_cannot_forge_board_lines(self):
        batch = self.event('queue-batch', subject='window\n\x1b[31mFORGED',
                           specs='L-spec-9000\n## FORGED', rehearsal=True)
        lines = board.section([batch])
        self.assertEqual(len(lines), 3)
        self.assertTrue(all('\n' not in line and '\x1b' not in line for line in lines))

    def test_attribution_is_reported_until_a_matching_park_and_requeue_clears_it(self):
        batch = self.event('queue-batch', specs='L-spec-9000', rehearsal=True)
        receipt = self.event('window-rehearsed', base_sha='a' * 40, specs=['L-spec-9000'],
                             state='red', attribution=dict(proven={'L-spec-9000': ['node']}))
        remove = self.event('queue-remove', subject='L-spec-9000', minute=1,
                            window='window-one', reason='attributed rehearsal red')
        add = self.event('queue-add', subject='L-spec-9000', minute=2, branch='fixed')
        self.assertIn('disposition=reported', '\n'.join(board.section([batch, receipt])))
        self.assertIn('disposition=parked', '\n'.join(board.section([batch, receipt, remove])))
        self.assertIn('disposition=reported', '\n'.join(board.section([batch, receipt, remove, add])))
        self.assertNotIn('disposition=parked', '\n'.join(board.section([batch, receipt, dict(remove, project='other')])))
        self.assertIn('disposition=parked', '\n'.join(board.section([batch, receipt, remove,
                           dict(remove, project='other', minute=3)])))

    def test_invalid_attribution_is_visible_as_undetermined(self):
        batch = self.event('queue-batch', specs='L-spec-9000', rehearsal=True)
        for attribution in ('invalid', dict(proven=['invalid'])):
            receipt = self.event('window-rehearsed', base_sha='a' * 40, specs=['L-spec-9000'],
                                 state='red', attribution=attribution)
            self.assertIn('reason=invalid attribution', '\n'.join(board.section([batch, receipt])))


if __name__ == '__main__':
    unittest.main()
