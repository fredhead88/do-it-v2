"""Blindness redaction must preserve the completeness proof for AC text."""
import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import fold
import grader_packet
import packet


class BaseShaTests(unittest.TestCase):
    def test_literal_base_sha_in_ac_and_review_path_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = root / 'L-spec-9000.md'
            sha = '2c336dc3' + 'a'*32
            spec.write_text('## Acceptance\nAC1 [backend]: compare against ' + sha + '.\n'
                            'review_path: compare with ' + sha[:7] + '\n'
                            '## Verification\n```bash\ntrue\n```\n')
            card = root / 'L-card-9000.md'
            card.write_text('verify exit 0\n')
            events = [dict(type='spec-written', subject='L-spec-9000', path=str(spec)),
                      dict(type='build-done', subject='L-spec-9000', base_sha=sha, card=str(card))]
            with patch.object(fold, 'read_events', return_value=events), \
                    patch.object(packet.Ctx, 'card_file', return_value=card), \
                    patch.object(packet.Ctx, 'worktree', return_value=str(root)), \
                    patch.object(packet.Ctx, 'charter_id', return_value=None), \
                    patch.object(packet, 'write_verify_script', return_value=None), \
                    patch.object(packet.models, 'resolve', return_value=dict(backend='codex')), \
                    patch.object(grader_packet, 'packet_items', side_effect=lambda ctx, items: items):
                context = packet.Ctx(argparse.Namespace(subject='L-spec-9000', role='grader', charter=None, project='demo'))
                body = '\n'.join(packet.p_grader(context))
                self.assertIn('AC1 [backend]: compare against [base commit]', body)
                self.assertIn('review_path: compare with [base commit]', body)
                self.assertNotIn(sha[:7], body)
                # An actual truncation still fails after normalizing both sides.
                with patch.object(packet, 'criteria', return_value=['AC1 [backend]: compare against ' + sha + '.']):
                    with self.assertRaisesRegex(SystemExit, 'incomplete criterion AC1'):
                        packet.p_grader(context)


if __name__ == '__main__':
    unittest.main()
