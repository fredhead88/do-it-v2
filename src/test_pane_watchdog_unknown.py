#!/usr/bin/env python3
"""Scratch-only regressions using a reconstructed Dangerous rm operation dialog."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

import pane_watchdog as watch
from test_pane_watchdog import DIALOG, PANE, REPO, T0, proof

PROMPT = "Remove these files?"
UNKNOWN = f"Dangerous rm operation\n{PROMPT}\n  1. Yes\n❯ 2. No\n"


class UnknownDialogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        (self.sessions / f"{os.getpid()}.json").write_text(json.dumps(
            {"name": PANE["name"], "tmux": PANE["target"]}))
        self.sends = []

    def run_watch(self, seconds, text=UNKNOWN, **kwargs):
        return watch.main(root=self.root, sessions_dir=self.sessions,
                          now=T0 + timedelta(seconds=seconds),
                          capture=kwargs.pop("capture", lambda _: text),
                          send=lambda *args: self.sends.append(args), **kwargs)

    def state(self):
        return json.loads((self.root / "state/pane-watchdog-unknown.json").read_text())

    def events(self):
        # Fresh interpreter and production reader, after the real doit append.
        result = subprocess.run([sys.executable, "-c",
                                 "import json,fold; print(json.dumps(fold.read_events()))"],
                                cwd=REPO / "src", env={**os.environ, "DOIT_ROOT": str(self.root)},
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_dwell_and_store_round_trip(self):
        self.assertEqual(self.run_watch(0), 0)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.state()[PANE["name"]],
                         dict(prompt=PROMPT, first_seen_at=watch._stamp(T0), raised=False))
        self.assertEqual(self.run_watch(150), 0)
        events = self.events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["type"], "pane-dialog-unknown")
        self.assertEqual(event["actor"], "pane-watchdog")
        self.assertEqual(event["subject"], PANE["name"])
        fields = dict(pane=PANE["name"], role=PANE["role"], prompt=PROMPT,
                      first_seen_at=watch._stamp(T0),
                      seen_at=watch._stamp(T0 + timedelta(seconds=150)))
        for key, value in fields.items():
            self.assertEqual(event[key], value)
        self.assertTrue(self.state()[PANE["name"]]["raised"])
        self.assertEqual(self.sends, [])
        proof("unknown-AC1", {"passes": [0, len(events)], "sends": self.sends, **fields})
        proof("unknown-AC4", event)

    def test_repeat_clear_and_rearm(self):
        for seconds in (0, 150, 300):
            self.assertEqual(self.run_watch(seconds), 0)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.run_watch(350, "❯ idle"), 0)
        self.assertEqual(self.state(), {})
        for seconds in (400, 519):
            self.assertEqual(self.run_watch(seconds), 0)
            self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.state()[PANE["name"]]["first_seen_at"],
                         watch._stamp(T0 + timedelta(seconds=400)))
        self.assertEqual(self.run_watch(520), 0)
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(self.sends, [])
        proof("unknown-AC2", {"third_pass_items": 1, "cleared": True,
                              "before_new_dwell_items": 1, "after_new_dwell_items": 2,
                              "sends": self.sends})

    def test_known_and_plain_captures(self):
        captures = iter([DIALOG, DIALOG, "❯ idle"])
        with patch.object(watch.time, "sleep"):
            self.assertEqual(self.run_watch(0, capture=lambda _: next(captures)), 0)
        self.assertEqual(self.sends, [(PANE["target"], "3")])
        self.assertEqual([e["type"] for e in self.events()], ["pane-dialog-resolved", "lesson"])
        for seconds, text in ((150, "$ "), (300, "plain text")):
            self.assertEqual(self.run_watch(seconds, text), 0)
        self.assertEqual(self.state(), {})
        self.assertFalse(any(e["type"] == "pane-dialog-unknown" for e in self.events()))
        proof("unknown-AC3", {"sends": self.sends,
                              "event_types": [e["type"] for e in self.events()], "plain_items": 0})

    def test_recognition_and_sanitization(self):
        self.assertEqual(watch._unknown_dialog(UNKNOWN, watch.DEFAULTS), PROMPT)
        for text in (DIALOG, "$ ", "plain text", "1. Yes\n2. No", "Question\n1. Yes",
                     UNKNOWN + "› composer", UNKNOWN + "$ ", UNKNOWN + "# ",
                     UNKNOWN + "❯ composer", UNKNOWN + "old\n" * 40):
            with self.subTest(text=text):
                self.assertIsNone(watch._unknown_dialog(text, watch.DEFAULTS))
        # Unlike Stay recognition, unknown detection covers the whole configured tail.
        self.assertEqual(watch._unknown_dialog(UNKNOWN + "footer\n" * 12, watch.DEFAULTS), PROMPT)
        unsafe = "\x1b[31m" + "x\x00\x07\x7f\x9b" * 220 + "\x1b[0m"
        text = f"Dangerous rm operation\n{unsafe}\n1. Yes\n2. No"
        self.assertEqual(watch._unknown_dialog(text, watch.DEFAULTS), "x" * 200)
        self.assertEqual(self.run_watch(0, text), 0)
        self.assertEqual(self.run_watch(120, text), 0)
        self.assertEqual(self.events()[0]["prompt"], "x" * 200)
        self.assertEqual(self.sends, [])

    def test_prompt_change_restarts_dwell(self):
        self.assertEqual(self.run_watch(0), 0)
        changed = UNKNOWN.replace(PROMPT, "Remove another file?")
        for seconds in (150, 269):
            self.assertEqual(self.run_watch(seconds, changed), 0)
            self.assertEqual(self.events(), [])
        self.assertEqual(self.run_watch(270, changed), 0)
        self.assertEqual(self.events()[0]["prompt"], "Remove another file?")
        self.assertEqual(self.sends, [])

    def test_append_failure_can_retry(self):
        self.assertEqual(self.run_watch(0), 0)
        with patch.object(watch, "_append", side_effect=RuntimeError("refused")):
            self.assertEqual(self.run_watch(150), 1)
        self.assertFalse(self.state()[PANE["name"]]["raised"])
        self.assertEqual(self.run_watch(160), 0)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.sends, [])


if __name__ == "__main__":
    unittest.main()
