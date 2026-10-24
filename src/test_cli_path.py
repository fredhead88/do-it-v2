#!/usr/bin/env python3
"""Isolated launcher resolution: never reach the operator's installed CLI."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cli_path


class ClaudePathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        env = patch.dict(os.environ, {"HOME": str(self.home), "PATH": str(self.bin)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DOIT_CLAUDE_BIN", None)

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return str(path)

    def test_pin_wins(self):
        self.executable(self.bin / "claude")
        pin = self.executable(self.home / "pinned" / "claude")
        os.environ["DOIT_CLAUDE_BIN"] = pin
        self.assertEqual(cli_path.claude_bin(), pin)
        self.assertEqual(cli_path.claude_bin_or_raise(), pin)

    def test_bad_pin_never_falls_back(self):
        self.executable(self.bin / "claude")
        for name, executable in (("claude", False), ("2.1.310", True)):
            pin = self.home / "pinned" / name
            self.executable(pin)
            if not executable:
                pin.chmod(0o644)
            os.environ["DOIT_CLAUDE_BIN"] = str(pin)
            self.assertIsNone(cli_path.claude_bin())
            with self.assertRaises(FileNotFoundError) as raised:
                cli_path.claude_bin_or_raise()
            self.assertIn(str(pin), str(raised.exception))
            self.assertIn("not executable" if not executable else "not named claude", str(raised.exception))

    def test_which_then_home_launcher_not_symlink_target(self):
        target = Path(self.executable(self.home / "versions" / "2.1.310"))
        fallback = self.home / ".local/bin/claude"
        fallback.parent.mkdir(parents=True)
        fallback.symlink_to(target)
        self.assertEqual(cli_path.claude_bin(), str(fallback))
        preferred = self.executable(self.bin / "claude")
        self.assertEqual(cli_path.claude_bin(), preferred)

    def test_missing_names_all_probes(self):
        self.assertIsNone(cli_path.claude_bin())
        with self.assertRaises(FileNotFoundError) as raised:
            cli_path.claude_bin_or_raise()
        for probe in ("DOIT_CLAUDE_BIN", "PATH", str(self.home / ".local/bin/claude")):
            self.assertIn(probe, str(raised.exception))

    def test_probe_oserror_does_not_escape(self):
        with patch.object(cli_path.shutil, "which", side_effect=PermissionError):
            self.assertIsNone(cli_path.claude_bin())


if __name__ == "__main__":
    unittest.main(verbosity=2)
