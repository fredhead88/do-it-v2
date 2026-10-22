#!/usr/bin/env python3
"""Verification fence boundaries agree across runners, grading and lint."""
import os
import pathlib
import subprocess
import tempfile
import unittest

# Imports and CLI fixtures must never resolve the operator's live state.
_TEMP = tempfile.TemporaryDirectory(prefix="verification-test-")
os.environ["HOME"] = str(pathlib.Path(_TEMP.name) / "home")
os.environ["DOIT_ROOT"] = str(pathlib.Path(_TEMP.name) / "root")

import build_runner
import checks
import grading_env
import packet
import packet_lint
import validate
import verification

DOIT = pathlib.Path(__file__).resolve().parent.parent / "doit"
CODE = "cd dashboard &&\n# AC1\n/usr/bin/test -f pin &&\n# AC2\n/usr/bin/test -d .\n"
PROSE = "These notes name AC99, npm, git log and live_db.\n"


def spec(code=CODE, heading="## Verification", fence="```bash", closing="```"):
    return ("# Fixture\nWrites: src/probe.py\nIn plain English: check the fixture.\n"
            "## Acceptance criteria\nAC1 [backend]: first check.\nAC2 [backend]: second check.\n"
            + heading + "\n" + fence + "\n" + code + closing + "\n")


class VerificationTests(unittest.TestCase):
    def test_only_first_complete_block_supplies_commands(self):
        for heading in ("# Verification", "## Verification", "## 9. Verification",
                        "## 10. vErIfIcAtIoN", "### Verification"):
            for fence, closing in (("```bash", "```"), ("~~~sh", "~~~"),
                                   ("````shell", "`````"), ("  ```bash", "  ```")):
                with self.subTest(heading=heading, fence=fence):
                    text = spec(heading=heading, fence=fence, closing=closing)
                    self.assertEqual(verification.block(text + PROSE), CODE)
                    self.assertEqual(verification.block(text + "```\nsecond block\n```\n"), CODE)
                    self.assertEqual(verification.block(text + "## After\n" + PROSE), CODE)
                    self.assertEqual(packet.verify_script(text + PROSE).split("set -euo pipefail\n", 1)[1], CODE)

    def test_fence_contents_and_verbatim_text(self):
        code = "# AC1\n# Heading inside code\n/usr/bin/printf '``` inline' &&\ntrue\n"
        text = ("~~~md\n## Verification\n```\nignored\n```\n~~~\n"
                "## Verification\nIntroductory prose is not code.\n```bash\n" + code + "```\n" + PROSE)
        self.assertEqual(verification.block(text), code)
        self.assertEqual(verification.trailing_text(text), PROSE)
        long_fence = "## Verification\n````bash\n```\n~~~\n# AC1\ntrue\n````\n"
        self.assertEqual(verification.block(long_fence), "```\n~~~\n# AC1\ntrue\n")
        self.assertEqual(verification.block("## Verification\r\n```bash\r\ntrue\r\n```\r\n"), "true\r\n")

    def test_missing_empty_and_unclosed_blocks(self):
        for text in ("", "## Verification\nOnly prose\n", "## Verification\n```bash\ntrue\n",
                     "```md\n## Verification\ntrue\n```\n",
                     "## Verification\nNo block\n## Notes\n```\ntrue\n```\n",
                     "## Verification\nNo block\n## Verification\n```\ntrue\n```\n",
                     "## Verification\n```bash\ntrue\n~~~\n"):
            with self.subTest(text=text):
                self.assertIsNone(verification.block(text))
                self.assertIsNone(packet.verify_script(text))
                self.assertEqual(list(checks._verification_statements(text)), [])
                with self.assertRaises(build_runner.Undetermined):
                    build_runner._commands(text, None, None)
        text = "## Verification\n```\n```\n```\ntrue\n```\n"
        self.assertEqual(verification.block(text), "")
        self.assertIsNone(packet.verify_script(text))

    def test_tags_splitting_and_cd_carry_forward_ignore_prose(self):
        clean = spec()
        expected = [(None, "cd dashboard"), ("AC1", "/usr/bin/test -f pin"),
                    ("AC2", "/usr/bin/test -d .")]
        for suffix in (PROSE, "&&\n" + PROSE, "\n" + PROSE + "## Next\nmore prose\n",
                       "\n## 9b. Notes on the checks\n" + PROSE):
            with self.subTest(suffix=suffix):
                text = clean + suffix
                self.assertEqual(list(checks._verification_statements(text)), expected)
                self.assertEqual(checks.records(text, None), checks.records(clean, None))
                self.assertEqual(checks.coverage(text, None), checks.coverage(clean, None))
                self.assertEqual(build_runner._commands(text, None, None),
                                 build_runner._commands(clean, None, None))
                commands, judgment = build_runner._commands(text, None, {"AC2"})
                self.assertEqual(commands, [("AC2", "cd dashboard &&\n/usr/bin/test -d .")])
                self.assertEqual(judgment, [])
                self.assertEqual(grading_env._tool_texts(text, ""), grading_env._tool_texts(clean, ""))
                self.assertEqual(grading_env.criterion_caps(text, "", "fixture"),
                                 grading_env.criterion_caps(clean, "", "fixture"))
                self.assertNotIn(PROSE.strip(), packet.criterion_block(text, "verify"))
        # A separately supplied verification script still contributes capabilities.
        self.assertEqual(grading_env.criterion_caps(clean + PROSE, "&&\n# AC3\n/usr/bin/node check.js", "fixture")["AC3"],
                         ["tools", "node_modules"])

    def test_lint_blocks_trailing_text_and_cli_accepts_notes_heading(self):
        for suffix, blocked in (("", False), ("\n \t\n", False), (PROSE, True),
                                ("\n" + PROSE + "## Next\nother notes\n", True),
                                ("```\nsecond code block\n```\n", True),
                                ("\n## 9b. Notes on the checks\n" + PROSE, False),
                                ("\n### Notes\n" + PROSE, False)):
            for heading in ("## Verification", "## 9. Verification"):
                for fence, closing in (("```bash", "```"), ("~~~sh", "~~~")):
                    with self.subTest(suffix=suffix, heading=heading, fence=fence):
                        text = spec(heading=heading, fence=fence, closing=closing) + suffix
                        findings = packet_lint.run(text, packet_lint.CHECKLIST, "spec")
                        self.assertEqual([f["id"] for f in findings], ["PL-012"] if blocked else [])
                        self.assertEqual(bool(validate.spec_shape(text)), blocked)
                        with tempfile.TemporaryDirectory() as tmp:
                            path = pathlib.Path(tmp) / "spec.md"
                            path.write_text(text)
                            proc = subprocess.run([str(DOIT), "shape", str(path)],
                                                  capture_output=True, text=True)
                        self.assertEqual(proc.returncode, 1 if blocked else 0, proc.stdout + proc.stderr)
                        if blocked:
                            self.assertIn("BLOCK: PL-012:", proc.stdout)
                            self.assertIn("notes heading", proc.stdout)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        _TEMP.cleanup()
