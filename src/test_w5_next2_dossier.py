"""Regressions for unreadable descriptions and stale backfill output."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ISOLATION = tempfile.TemporaryDirectory(prefix="w5-description-import-")
os.environ["DOIT_ROOT"] = str(Path(ISOLATION.name) / "root")
os.environ["HOME"] = str(Path(ISOLATION.name) / "home")
import brief
import fold
import plain
import plain_backfill

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
SID = "L-spec-9901"
LINE = "This change sends a fresh weekly report to every active client."


class DescriptionSafety(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="w5-description-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.content = self.root / "content"
        self.content.mkdir()
        self.events = self.root / "events/L-planner-fixture.jsonl"
        self.events.parent.mkdir()
        self.enterContext(patch.dict(os.environ, DOIT_ROOT=str(self.root),
                                     HOME=str(self.root / "home")))
        self.enterContext(patch.multiple(fold, ROOT=self.root, EVENTS=self.events.parent, NOW=NOW))
        self.seed(SID)

    def seed(self, sid):
        (self.content / f"{sid}.md").write_text("# Fixture\n\n## Goal\nA useful change.\n")
        with self.events.open("a") as out:
            out.write(json.dumps(dict(v=1, type="spec-written", subject=sid,
                                      project="fixture", ts=NOW.isoformat())) + "\n")

    def apply(self, rows, served):
        packet = self.root / "packet.md"
        packet.write_text("kind: plain-english-backfill-batch\nspecs:\n" +
                          "".join(f"- {sid}\n" for sid in served))
        output = self.root / "output.jsonl"
        output.write_text("".join(json.dumps(row) + "\n" for row in rows))
        before = {p.name: p.read_bytes() for p in self.events.parent.iterdir()}
        env = dict(os.environ)
        for key in ("DOIT_PROJECT", "DOIT_LEDGER_FILE", "DOIT_TEST_LIVE_ROOT"):
            env.pop(key, None)
        result = subprocess.run([sys.executable, str(Path(plain_backfill.__file__)),
                                 "apply", str(output), str(packet)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.events.parent.iterdir()})
        return result

    def test_unreadable_description_sources_degrade(self):
        for item in (SID, "L-charter-9901", "L-goal-9901"):
            with self.subTest(item=item):
                path = self.content / f"{item}.md"
                path.write_bytes(b"\xff")
                self.assertEqual(plain.line(item, self.root)["source"], "missing")
                path.unlink()
                path.mkdir()
                self.assertEqual(plain.line(item, self.root)["source"], "missing")
                path.rmdir()

    def test_corrupt_sidecar_keeps_readable_goal_fallback(self):
        (self.content / f"{SID}.plain.txt").write_bytes(b"\xff")
        result = plain.line(SID, self.root)
        self.assertEqual(result["source"], "goal-section")
        self.assertIn("A useful change.", result["text"])

    def test_dossier_survives_corrupt_description_file(self):
        (self.content / f"{SID}.md").write_bytes(b"\xff")
        events = fold.read_events(use_cache=False) + [dict(
            type="spec-killed", subject=SID, ts=NOW.isoformat(), actor="operator",
            _src="L-operator-fixture.jsonl:1")]
        dossier = brief.dossier(SID, events, self.root, NOW)
        self.assertEqual(dossier["plain"]["source"], "missing")
        self.assertEqual(dossier["title"], SID)
        self.assertEqual(dossier["next"], {"text": "nothing — done", "owner": "nobody"})
        self.assertEqual(brief.render(dossier).splitlines()[1:4], dossier["catch_me_up"])

    def test_backfill_inspection_skips_unreadable_content(self):
        corrupt, directory = "L-spec-9902", "L-spec-9903"
        self.seed(corrupt)
        self.seed(directory)
        (self.content / f"{corrupt}.md").write_bytes(b"\xff")
        (self.content / f"{directory}.md").unlink()
        (self.content / f"{directory}.md").mkdir()
        before = self.events.read_bytes()
        specs = fold.fold(fold.read_events(use_cache=False))[0]
        self.assertEqual(plain_backfill.split_batches(specs, self.root), ([SID], []))
        self.assertEqual(self.events.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["content", "events"])

    def test_apply_rechecks_excluded_state_and_rejection_limit(self):
        for state in sorted(plain_backfill.STATE_EXCLUDED):
            with self.subTest(state=state):
                counts = plain_backfill.apply_rows([{"spec": SID, "line": LINE}],
                                                   {SID: {"state": state}}, self.root, [SID])
                self.assertEqual(counts, {"written": 0, "skipped": 1, "rejected": 0})
                self.assertFalse((self.content / f"{SID}.plain.txt").exists())
        rejection = self.content / plain_backfill.REJECTED_NAME
        rejection.write_text((json.dumps({"spec": SID, "reason": "fixture"}) + "\n") * 2)
        result = self.apply([{"spec": SID, "line": LINE}], [SID])
        self.assertIn("written=0 skipped=1 rejected=0", result.stdout)
        self.assertFalse((self.content / f"{SID}.plain.txt").exists())

    def test_apply_rejects_known_subject_outside_packet(self):
        other = "L-spec-9902"
        self.seed(other)
        result = self.apply([{"spec": other, "line": LINE}], [SID])
        self.assertIn("written=0 skipped=0 rejected=2", result.stdout)
        self.assertFalse((self.content / f"{other}.plain.txt").exists())
        rejects = [json.loads(s) for s in (self.content / plain_backfill.REJECTED_NAME).read_text().splitlines()]
        self.assertEqual({r["reason"] for r in rejects}, {"not in the served packet", "omitted by research"})


if __name__ == "__main__":
    unittest.main()
