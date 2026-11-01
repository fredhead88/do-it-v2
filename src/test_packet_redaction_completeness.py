"""Blindness redaction preserves the criterion completeness check."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import grader_packet
import packet


class PacketRedactionTests(unittest.TestCase):
    def test_identifiers_in_criteria_survive_as_redacted_complete_blocks(self):
        subject = 'L-spec-fixture'
        base, ready = 'a' * 40, 'b' * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, card = root / 'spec.md', root / 'card.md'
            spec.write_text('## Acceptance criteria\nAC1 [backend]: L-builder-9901 verifies '
                            + base + '\nreview_path: inspect evidence at ' + ready + '\n')
            card.write_text('AC1 [backend] done\n')
            build = dict(type='build-done', base_sha=base, ready_sha=ready, ts='2026-11-01T00:00:00Z')
            context = SimpleNamespace(evs=[], a=SimpleNamespace(subject=subject),
                spec_file=lambda: spec, card_file=lambda: card, all_of=lambda kind: [],
                charter_id=lambda: None, worktree=lambda: str(root), project=lambda: 'fixture',
                last=lambda kind: build if kind == 'build-done' else None,
                specs={subject: {'evs': [build]}})
            for bundle in (None, {'bundle': {'base_sha': base}, 'event': {'bundle_sha256': 'fixture'}}):
                with self.subTest(bundled=bundle is not None), \
                        patch.object(packet, 'write_verify_script', return_value=None), \
                        patch.object(packet, 'waiver_line', return_value='none'), \
                        patch.object(packet.models, 'resolve', return_value={'backend': 'codex'}), \
                        patch.object(grader_packet, 'bundle_for', return_value=bundle), \
                        patch.object(grader_packet, 'sections', return_value=[]):
                    result = '\n'.join(packet.p_grader(context))
                    self.assertIn('AC1 [backend]: [this build] verifies [base commit]', result)
                    self.assertNotIn('L-builder-9901', result)
                    self.assertNotIn(base, result)
                    with patch.object(packet, 'criteria', return_value=['AC1 [backend]: truncated']):
                        with self.assertRaisesRegex(SystemExit, 'incomplete criterion AC1'):
                            packet.p_grader(context)


if __name__ == '__main__':
    unittest.main()
