#!/usr/bin/env python3
"""Master-full questions preserve attribution and render as inert inbox text."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_ISO = tempfile.TemporaryDirectory(prefix="master-inbox-tests-")
os.environ["DOIT_ROOT"] = _ISO.name
os.environ["HOME"] = str(pathlib.Path(_ISO.name) / "home")
import thinker_inbox as inbox


class TestMasterFullInbox(unittest.TestCase):
    def test_full_question_rendering_is_inert_and_untruncated(self):
        ask = "failed: tests/test_full.py::test_red; " + "merge subject " * 25 + "END\n\x1b[31m\u2028command"
        item = dict(subject="master-full:albert-scott:1", kind="master-full-red", type="question",
                    src="L-base-red-local.jsonl:1", ask=ask)
        rendered = inbox.short(item)
        self.assertIn("END", rendered)
        self.assertNotIn("\n", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u2028", rendered)
        self.assertIn("tests/test_full.py::test_red", rendered)

    def test_literal_send_and_answer_ref_preserved(self):
        with patch.object(inbox.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                patch.object(inbox.time, "sleep"):
            self.assertTrue(inbox.type_into("%1", "$(untrusted) ; subject"))
        self.assertEqual(run.call_args_list[0].args[0],
                         ["tmux", "send-keys", "-t", "%1", "-l", "$(untrusted) ; subject"])
        self.assertEqual(run.call_args_list[1].args[0][-1], "Enter")

    def test_list_contains_complete_attribution(self):
        item = dict(owner="thinker", subject="master-full:albert-scott:1", kind="master-full-red",
                    type="question", src="L-base-red-local.jsonl:1", ask="x" * 200 + " merge: last-tested-head")
        import contextlib
        import io
        output = io.StringIO()
        with patch.object(inbox, "open_items", return_value=[item]), \
                patch.object(sys, "argv", ["thinker_inbox.py", "--list"]), \
                contextlib.redirect_stdout(output):
            inbox.main()
        self.assertIn("merge: last-tested-head", output.getvalue())
        self.assertIn("1 open", output.getvalue())

    def test_delivery_keeps_all_master_full_details(self):
        items = [dict(owner="thinker", subject=f"master-full:albert-scott:{n}", kind="master-full-red",
                      type="question", src=f"L-base-red-local.jsonl:{n}",
                      ask="detail " * 180 + f" final-merge-{n}") for n in range(4)]
        with patch.object(inbox, "open_items", return_value=items), \
                patch.object(inbox, "load_state", return_value={"delivered": {}, "tg": {}}), \
                patch.object(inbox, "pane", return_value="%1"), patch.object(inbox, "pane_idle", return_value=True), \
                patch.object(inbox, "type_into", return_value=True) as send, \
                patch.object(inbox, "save_state"), patch.object(inbox, "log"), \
                patch.object(sys, "argv", ["thinker_inbox.py"]):
            inbox.main()
        message = send.call_args.args[1]
        for n in range(4): self.assertIn(f"final-merge-{n}", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
