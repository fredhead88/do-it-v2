#!/usr/bin/env python3
"""Hermetic stopgap dates, runtime expiry and preserved wake behaviour."""
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/executor-wake.sh"
HEADER = re.compile(r"# STOPGAP ([a-z0-9-]+) expires (\d{4}-\d{2}-\d{2}) \(([^()]+)\)$")


def headers(text, today):
    """Delimiters, independent of date syntax, cannot hide undated blocks."""
    result, paired = {}, False
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(("# ---", "# BLOCK")):
            if i + 1 >= len(lines) or not HEADER.fullmatch(lines[i + 1]):
                raise ValueError("undated " + line)
            paired = True
        elif line.startswith("# STOPGAP"):
            match = HEADER.fullmatch(line)
            if not match:
                raise ValueError("malformed " + line)
            name, expiry, _ = match.groups()
            try:
                expires = date.fromisoformat(expiry)
            except ValueError:
                raise ValueError("malformed date " + name)
            if (expires - today).days > 30:
                raise ValueError("more than 30 days " + name)
            if name in result:
                raise ValueError("duplicate " + name)
            result[name] = expires
            paired = False
        elif paired:
            raise ValueError("undated " + line)
    return result


def proof(ac, body):
    body = json.dumps(body, sort_keys=True)
    print(json.dumps({"url": "cli://executor-wake/" + ac, "status": 0,
                      "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "body_excerpt": body}))


class WakeTests(unittest.TestCase):
    def test_ac8_dated_blocks(self):
        text = SCRIPT.read_text()
        today = date.fromisoformat(os.environ.get("EXECUTOR_WAKE_TODAY", date.today().isoformat()))
        blocks = headers(text, today)
        self.assertEqual(set(blocks), {"relay-nudge", "planner-idle", "executor-wake"})
        self.assertIn("# --- Executor\n# STOPGAP executor-wake", text)
        for bad in ("# --- undated\necho unsafe", "# BLOCK undated\necho unsafe",
                    "# STOPGAP malformed expires 2026-13-01 (replacement)"):
            with self.assertRaisesRegex(ValueError, "undated|malformed"):
                headers(bad, today)
        future = today + timedelta(days=31)
        with self.assertRaisesRegex(ValueError, "too-far"):
            headers(f"# --- too-far\n# STOPGAP too-far expires {future} (replacement)", today)
        for dead in ("atlas-shadow-active", "codex-composer", "flow:1.0"):
            self.assertNotIn(dead, text)
        # Reject all of the removed Codex blocks by their state variables.
        # The freeze flag's literal belongs exclusively to the freeze module.
        self.assertNotRegex(text, r"(?m)^(?:CFLAG|CSTATE|SFLAG|SSTATE|QSTATE)=")
        self.assertNotIn("/exit", "\n".join(s for s in text.splitlines() if not s.lstrip().startswith("#")))
        self.assertLess(text.index("has-session"), text.index("# ---"))
        self.assertLess(text.index("pane_dead"), text.index("# ---"))
        proof("AC8", {k: str(v) for k, v in blocks.items()})

    def fixture(self, folder):
        root = Path(folder)
        (root / "bin").mkdir()
        (root / "seat").mkdir()
        tmux = root / "bin/tmux"
        tmux.write_text('''#!/usr/bin/env python3
import json,os,pathlib,sys
r=pathlib.Path(os.environ['DOIT_ROOT'])
a=sys.argv[1:]
with (r/'tmux.calls').open('a') as f: f.write(json.dumps(a)+'\\n')
if a[0]=='has-session': sys.exit(int(os.environ.get('NO_SESSION','0')))
if a[0]=='display': print('1' if os.environ.get('DEAD') and a[a.index('-t')+1]=='flow:2.3' else '0')
if a[0]=='list-panes': print('%9 bash doit relay')
if a[0]=='capture-pane':
 t=a[a.index('-t')+1]
 print('L-relay-9003' if t=='%9' else 'done 1:00 AM' if t=='flow:2.1' else '❯ idle')
''')
        say = root / "bin/pane-say.sh"
        say.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$DOIT_ROOT/say.calls"\n')
        tmux.chmod(0o755)
        say.chmod(0o755)
        return root

    def run_script(self, root, today="2026-10-27", **env):
        result = subprocess.run([str(SCRIPT)], env={**os.environ, "DOIT_ROOT": str(root),
                        "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
                        "EXECUTOR_WAKE_TODAY": today, **env}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(s) for s in (root / "tmux.calls").read_text().splitlines()]
        for argv in calls:
            if argv[0] == "send-keys":
                self.assertFalse(any("/exit" in arg for arg in argv))
        return (root / "logs/executor-wake.log").read_text()

    def test_ac9_runtime_behaviour(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.fixture(folder)
            self.assertIn("idle-1", self.run_script(root))
            self.assertFalse((root / "say.calls").exists())
            self.assertIn("woke", self.run_script(root))
            says = (root / "say.calls").read_text().splitlines()
            self.assertEqual(len(says), 1)
            self.assertIn("flow:2.3 Wake (executor-wake.sh,", says[0])
        blocks = headers(SCRIPT.read_text(), date(2026, 10, 27))
        for name, expiry in blocks.items():
            with tempfile.TemporaryDirectory() as folder:
                root = self.fixture(folder)
                packet = root / "seat/packet.cmd.json"
                packet.write_text('{"cmd":["claude"]}')
                import time
                os.utime(packet, (time.time() - 600, time.time() - 600))
                log = self.run_script(root, str(expiry + timedelta(days=1)))
                self.assertIn("expired " + name, log)
                if name in ("relay-nudge", "executor-wake"):
                    self.assertFalse((root / "say.calls").exists())
                if name == "planner-idle":
                    self.assertFalse((root / "state/planner-wake.idle").exists())
        for env, message in (({"NO_SESSION": "1"}, "no-tmux"), ({"DEAD": "1"}, "pane-dead-or-missing")):
            with tempfile.TemporaryDirectory() as folder:
                root = self.fixture(folder)
                self.assertIn(message, self.run_script(root, **env))
                self.assertFalse((root / "say.calls").exists())
                self.assertEqual(list((root / "state").iterdir()), [])
        proof("AC9", {"two_runs_one_wake": True, "expired_blocks": list(blocks), "preamble_cases": 2})


if __name__ == "__main__":
    unittest.main()
