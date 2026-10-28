#!/usr/bin/env python3
"""Scratch-only watchdog regressions; reconstructed dialog until AC13."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import pane_watchdog as watch

REPO = Path(__file__).resolve().parent.parent
DIALOG = "Background work is running\n  1. Wait\n  2. Move to background\n❯ 3. Stay\n"
PANE = {"name": "L-planner-9001", "role": "planner", "target": "%1"}
T0 = datetime(2026, 10, 27, tzinfo=timezone.utc)


def proof(ac, body):
    body = json.dumps(body, sort_keys=True)
    print(json.dumps({"url": "cli://pane-watchdog/" + ac, "status": 0,
                      "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                      "body_excerpt": body}))


class WatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.session(os.getpid(), PANE["name"], PANE["target"])
        self.addCleanup(patch.stopall)
        patch.object(watch.time, "sleep").start()

    def session(self, pid, name, target):
        (self.sessions / f"{pid}.json").write_text(json.dumps({"name": name, "tmux": target}))

    def run_watch(self, text=DIALOG, **kwargs):
        return watch.main(root=self.root, sessions_dir=self.sessions, now=T0,
                          capture=kwargs.pop("capture", lambda _: text),
                          send=kwargs.pop("send", lambda *a: None), **kwargs)

    def heartbeat(self):
        return json.loads((self.root / "state/pane-watchdog.json").read_text())

    def events(self):
        # Separate process, production reader, never the writer's in-memory state.
        code = "import json,fold; print(json.dumps(fold.read_events()))"
        result = subprocess.run([sys.executable, "-c", code], cwd=REPO / "src",
                                env={**os.environ, "DOIT_ROOT": str(self.root)},
                                text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_ac1_recognition(self):
        fixtures = {
            "RECONSTRUCTED": DIALOG,
            "idle": "❯ hello",
            "quoted-with-composer": DIALOG + "❯ write more",
            "outside-tail": DIALOG + "\n".join("old" for _ in range(45)),
            "prose": "We saw Background work is running yesterday\n1. Wait\n3. Stay",
            "no-stay": DIALOG.replace("Stay", "Continue"),
            "dollar-prompt": DIALOG + "$ ",
            "hash-prompt": DIALOG + "# ",
            "codex-composer": DIALOG + "› message",
        }
        for name, text in fixtures.items():
            rows = watch.scan([PANE], lambda _: text)
            print("AC1", name, rows)
            self.assertEqual(len(rows), int(name == "RECONSTRUCTED"))
            if rows:
                self.assertEqual(rows[0]["stay_key"], "3")
                self.assertTrue({"pane", "role", "target", "seen_at"} <= rows[0].keys())
        self.assertEqual(watch.scan([PANE], lambda _: DIALOG.replace("3. Stay", "4. Stay"))[0]["stay_key"], "Escape")
        self.assertEqual(watch.scan([PANE], lambda _: "\x1b[32m" + DIALOG + "\x1b[0m")[0]["stay_key"], "3")
        self.assertEqual(watch.scan([PANE], lambda _: (_ for _ in ()).throw(OSError())), [])
        proof("AC1", list(fixtures) + ["ANSI", "renumbered", "OSError"])

    def test_ac2_safe_keys_and_restart_cap(self):
        inventory = []
        for text in (DIALOG, DIALOG.replace("2. Move to background", "4. Wait").replace("3. Stay", "2. Stay"),
                     DIALOG.replace("Stay", "Continue"), DIALOG, "quoted heading", "❯ idle"):
            for row in watch.scan([PANE], lambda _: text):
                calls = []
                result = watch.resolve(row, send=lambda *args: calls.append(args))
                self.assertEqual(calls, [(PANE["target"], row["stay_key"])])
                self.assertEqual(result["choice"], "stay" if row["stay_key"] == "3" else "stay-esc")
                self.assertTrue({"pane", "role", "prev_scan_at", "seen_at", "resolved_at"} <= result.keys())
                inventory.extend(calls)
        row = watch.scan([PANE], lambda _: DIALOG)[0]
        for bad in ({"stay_key": None}, {"target": "x; touch /tmp/pwned"}, {"target": ""}):
            with self.assertRaises(ValueError):
                watch.resolve({**row, **bad}, send=lambda *args: self.fail(str(args)))
        sentinel = self.root / "pwned"
        with self.assertRaises(ValueError):
            watch.resolve({**row, "target": "x; touch " + str(sentinel)}, send=lambda *args: self.fail(str(args)))
        self.assertFalse(sentinel.exists())
        self.assertTrue(all(key in {"3", "Escape"} for _, key in inventory))
        # Every call is a fresh interpreter, sharing only persisted scratch state.
        code = '''import sys,json
from datetime import datetime
import pane_watchdog as w
calls=[]
w.time.sleep=lambda _:None
rc=w.main(root=sys.argv[1],sessions_dir=sys.argv[2],now=datetime.fromisoformat(sys.argv[3]),
capture=lambda _:sys.argv[4],send=lambda *a:calls.append(a))
print(json.dumps([rc,calls]))'''
        counts = []
        for minute in (0, 5, 29, 30):
            result = subprocess.run([sys.executable, "-c", code, str(self.root), str(self.sessions),
                                     (T0 + timedelta(minutes=minute)).isoformat(), DIALOG],
                                    cwd=REPO / "src", text=True, capture_output=True, check=True)
            rc, sends = json.loads(result.stdout)
            self.assertEqual(rc, 0)
            counts.append(len(sends))
            self.assertEqual(self.heartbeat()["ts"], watch._stamp(T0 + timedelta(minutes=minute)))
        self.assertEqual(counts, [1, 0, 0, 1])
        self.assertEqual(self.events(), [])
        self.assertIn("unresolved-cap", (self.root / "logs/pane-watchdog.log").read_text())
        cap = self.root / "state/pane-watchdog-sends.json"
        cap.write_text("corrupt")
        self.run_watch(send=lambda *a: self.fail("corrupt cap sent"))
        cap.unlink()
        self.run_watch(send=lambda *a: self.fail("deleted cap reset"))
        # Disappeared dialog between scan and the pre-send recapture.
        with tempfile.TemporaryDirectory() as root:
            capture = iter([DIALOG, "❯ composer"])
            self.assertEqual(watch.main(root=root, sessions_dir=self.sessions, now=T0,
                             capture=lambda _: next(capture), send=lambda *a: self.fail("stale send")), 0)
        self.session(os.getpid(), PANE["name"], "x; touch " + str(sentinel))
        self.run_watch(capture=lambda *a: self.fail("invalid session target captured"))
        self.assertEqual((self.heartbeat()["panes_scanned"], self.heartbeat()["unreadable"]), (0, 1))
        self.assertFalse(sentinel.exists())
        proof("AC2", {"keys": inventory, "restart_sends": counts, "corrupt_and_deleted_fail_closed": True})

    def test_ac2_failed_attempts_keep_the_cap(self):
        for failure in ("send", "append"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                captures = iter([DIALOG, DIALOG, "idle"])
                calls = []

                def send(target, key):
                    # The reservation must already be durable when tmux is invoked.
                    caps = json.loads((root / "state/pane-watchdog-sends.json").read_text())
                    self.assertEqual(caps[PANE["name"]], watch._stamp(T0))
                    calls.append((target, key))
                    if failure == "send":
                        raise OSError("failed send")

                with patch.object(watch, "_append", side_effect=RuntimeError("failed append")):
                    self.assertEqual(watch.main(root=root, sessions_dir=self.sessions, now=T0,
                                     capture=lambda _: next(captures), send=send), 1)
                self.assertEqual(calls, [(PANE["target"], "3")])
                self.assertEqual(watch.main(root=root, sessions_dir=self.sessions,
                                 now=T0 + timedelta(minutes=5), capture=lambda _: DIALOG,
                                 send=lambda *a: self.fail("failed attempt reset its cap")), 0)
                self.assertIn("unresolved-cap", (root / "logs/pane-watchdog.log").read_text())
                self.assertFalse((root / "events/L-pane-watchdog-local.jsonl").exists())
        proof("AC2-failures", {"failed_send_and_append_keep_cap": True})

    def tmux_roundtrip(self):
        patch.stopall()
        import shlex
        import time
        server = "watchdog-" + self.root.name
        prefix = ["tmux", "-L", server]

        def tmux(*args):
            return subprocess.check_output([*prefix, *args], text=True).strip()

        self.addCleanup(lambda: subprocess.run([*prefix, "kill-server"], capture_output=True))
        tmux("new-session", "-d", "-s", "proof", "-x", "120", "-y", "40", "bash --noprofile --norc")
        key, idle = self.root / "key", self.root / "idle"
        script = self.root / "dialog.sh"
        script.write_text("#!/bin/bash\nprintf '%s' " + shlex.quote(DIALOG) +
                          "\nprintf ready > " + shlex.quote(str(self.root / "ready")) +
                          "\nread -rsn1 k\nprintf '%s' \"$k\" > " + shlex.quote(str(key)) +
                          "\nclear\nexec bash --noprofile --norc\n")
        tmux("split-window", "-h", "-t", "proof", "cat > " + shlex.quote(str(idle)))
        panes = tmux("list-panes", "-t", "proof", "-F", "#{pane_id} #{pane_pid}").splitlines()
        (target, pid), (other, other_pid) = [s.split() for s in panes]
        tmux("send-keys", "-t", target, "bash " + shlex.quote(str(script)), "Enter")
        for _ in range(200):
            if (self.root / "ready").exists() and idle.exists():
                break
            time.sleep(.05)
        self.assertTrue((self.root / "ready").exists(), "private tmux fixture never ready")
        for p in self.sessions.iterdir():
            p.unlink()
        self.session(pid, "L-planner-9001", target)
        self.session(other_pid, "L-executor-9002", other)
        # Restore real settling for the real round trip.
        patch.stopall()
        self.assertEqual(watch.main(root=self.root, sessions_dir=self.sessions, now=T0, tmux_argv=prefix), 0)
        self.assertEqual(key.read_text(), "3")
        self.assertEqual(idle.read_bytes(), b"")
        self.assertEqual(self.heartbeat()["panes_scanned"], 2)
        self.assertEqual(watch.scan([{"name": "L-planner-9001", "role": "planner", "target": target}],
                                    lambda t: tmux("capture-pane", "-p", "-t", t)), [])
        return self.events()

    def test_ac3_private_tmux_roundtrip(self):
        self.tmux_roundtrip()
        proof("AC3", {"panes_scanned": 2, "dialog_key": "3", "idle_bytes": 0})

    def test_ac4_production_store_and_actor_admission(self):
        events = self.tmux_roundtrip()
        resolved = [e for e in events if e["type"] == "pane-dialog-resolved"]
        lessons = [e for e in events if e["type"] == "lesson"]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(len(lessons), 1)
        event = resolved[0]
        self.assertEqual(event["actor"], "pane-watchdog")
        self.assertEqual(event["subject"], "L-planner-9001")
        self.assertEqual(lessons[0]["problem"], "planner-exit-dialog")
        self.assertEqual(lessons[0]["subject"], event["subject"])
        self.assertTrue({"pane", "role", "prev_scan_at", "seen_at", "resolved_at", "choice"} <= event.keys())
        stored = self.root / "events/L-pane-watchdog-local.jsonl"
        self.assertTrue(all("actor" not in json.loads(s) for s in stored.read_text().splitlines()))
        import fold_emits
        self.assertEqual(fold_emits.EMITS["pane-dialog-resolved"], {"pane-watchdog"})
        self.assertEqual(fold_emits.REQUIRED_ROWS["pane-dialog-resolved"], ("pane", "role", "seen_at", "resolved_at", "choice"))
        wrong = self.root / "events/L-other-local.jsonl"
        fields = [f"{k}:={json.dumps(event[k])}" for k in ("pane", "role", "seen_at", "resolved_at", "choice")]
        for existing in (False, True):
            if existing:
                wrong.write_text("\n")
            before = wrong.read_bytes() if wrong.exists() else None
            result = subprocess.run([str(REPO / "doit"), "append", "pane-dialog-resolved", event["subject"], *fields],
                         env={**os.environ, "DOIT_ROOT": str(self.root), "DOIT_LEDGER_FILE": wrong.name},
                         capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(wrong.read_bytes() if wrong.exists() else None, before)
        proof("AC4", {"resolution_events": 1, "lessons": 1, "strict_wrong_actor_refused": True})

    def test_ac5_heartbeat_and_unreadable(self):
        with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) as live:
            self.session(live.pid, "L-executor-9002", "%2")
            self.run_watch("❯ idle")
            first = self.heartbeat()
            self.assertEqual((first["panes_scanned"], first["dialogs"]), (2, 0))
            self.assertEqual(self.events(), [])
            captured = iter([DIALOG, DIALOG, "❯ idle", "❯ idle"])
            self.assertEqual(watch.main(root=self.root, sessions_dir=self.sessions, now=T0 + timedelta(minutes=5),
                         capture=lambda _: next(captured), send=lambda *a: None), 0)
            self.assertEqual(self.heartbeat()["dialogs"], 1)
            self.assertEqual([e for e in self.events() if e["type"] == "pane-dialog-resolved"][0]["prev_scan_at"], first["ts"])
            def capture(target):
                if target == "%1":
                    raise OSError("fixture")
                return "idle"
            self.run_watch(capture=capture)
            self.assertEqual((self.heartbeat()["panes_scanned"], self.heartbeat()["unreadable"]), (1, 1))
            live.terminate()
        for p in self.sessions.iterdir():
            p.unlink()
        replacements = []
        replace = os.replace
        def observe(source, target):
            self.assertIsInstance(json.loads(Path(source).read_text()), dict)
            replacements.append(Path(target).name)
            replace(source, target)
        with patch.object(watch.os, "replace", side_effect=observe):
            self.run_watch("idle")
        self.assertIn("pane-watchdog.json", replacements)
        self.assertEqual(self.heartbeat()["panes_scanned"], 0)
        for prior in (None, "bad json"):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / "state").mkdir()
                if prior:
                    (root / "state/pane-watchdog.json").write_text(prior)
                self.session(os.getpid(), PANE["name"], "%1")
                captures = iter([DIALOG, DIALOG, "idle"])
                self.assertEqual(watch.main(root=root, sessions_dir=self.sessions, now=T0,
                            capture=lambda _: next(captures), send=lambda *a: None), 0)
                stored = json.loads((root / "events/L-pane-watchdog-local.jsonl").read_text().splitlines()[0])
                self.assertIsNone(stored["prev_scan_at"])
        for sequence, expected_sends in (([DIALOG, OSError()], 0), ([DIALOG, DIALOG, OSError()], 1)):
            with tempfile.TemporaryDirectory() as folder:
                captures, sends = iter(sequence), []
                def recapture(_):
                    item = next(captures)
                    if isinstance(item, Exception):
                        raise item
                    return item
                self.assertEqual(watch.main(root=folder, sessions_dir=self.sessions, now=T0,
                                    capture=recapture, send=lambda *a: sends.append(a)), 0)
                heartbeat = json.loads((Path(folder) / "state/pane-watchdog.json").read_text())
                self.assertEqual((heartbeat["panes_scanned"], heartbeat["unreadable"]), (0, 1))
                self.assertEqual(len(sends), expected_sends)
                self.assertFalse((Path(folder) / "events/L-pane-watchdog-local.jsonl").exists())
        proof("AC5", {"idle": first, "atomic_replaces": replacements, "unreadable_excluded": True})

    def test_ac5_roster_identity_and_all_roles(self):
        import panes
        for role in sorted(panes.ROLES):
            self.session(os.getpid(), f"L-{role}-9001", "%1")
            self.assertEqual(self.run_watch("idle"), 0)
            self.assertEqual(self.heartbeat()["panes_scanned"], 1, role)
        path = self.sessions / f"{os.getpid()}.json"
        for metadata, unreadable in (({}, 0), ({"name": "think-operator", "tmux": "%1"}, 0),
                                     ({"name": PANE["name"]}, 1), ([], 1)):
            path.write_text(json.dumps(metadata))
            self.assertEqual(self.run_watch(capture=lambda _: self.fail("invalid roster captured")), 0)
            self.assertEqual((self.heartbeat()["panes_scanned"], self.heartbeat()["unreadable"]),
                             (0, unreadable))
        self.session(os.getpid(), PANE["name"], "%1")
        with patch.object(watch, "_is_live", return_value=False):
            self.assertEqual(self.run_watch(capture=lambda _: self.fail("dead session captured")), 0)
            self.assertEqual(self.heartbeat()["panes_scanned"], 0)
        proof("AC5-roster", {"roles": sorted(panes.ROLES), "invalid_metadata_excluded": True})

    def test_ac6_import_closure_and_argv(self):
        code = '''import subprocess,sys
def fail(*a,**k): raise AssertionError('import launched subprocess')
subprocess.Popen=fail
import pane_watchdog
assert not {'dispatch','pane_resume','look'} & sys.modules.keys()'''
        subprocess.run([sys.executable, "-c", code], cwd=REPO / "src", check=True)
        import ast
        tree = ast.parse((REPO / "src/pane_watchdog.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module]
                self.assertFalse(any(m.split(".")[0] in {"socket", "urllib", "requests", "anthropic", "dispatch", "pane_resume", "look"} for m in modules))
            if isinstance(node, ast.Call):
                self.assertFalse(any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value for k in node.keywords))
        inventory = []
        for fixture in ("resolved", "idle", "unreadable", "capped", "still-showing"):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                captures = iter([DIALOG, DIALOG, "idle"] if fixture == "resolved" else [DIALOG] * 3)
                if fixture == "capped":
                    (root / "state").mkdir()
                    (root / "state/pane-watchdog-sends.json").write_text("corrupt")
                def run(argv, **kwargs):
                    inventory.append(argv)
                    if argv[0] == "tmux":
                        self.assertEqual(argv[:3], ["tmux", "-L", "private-test"])
                        self.assertIn(argv[3], {"capture-pane", "send-keys"})
                        self.assertTrue(watch._target(argv[argv.index("-t") + 1]))
                        if argv[3] == "capture-pane":
                            self.assertIn("-p", argv)
                            if fixture == "unreadable":
                                raise OSError("fixture")
                            return subprocess.CompletedProcess(argv, 0, "idle" if fixture == "idle" else next(captures))
                        self.assertIn(argv[-1], {"3", "Escape"})
                    else:
                        self.assertEqual(argv[:2], [str(REPO / "doit"), "append"])
                    return subprocess.CompletedProcess(argv, 0, "")
                with patch.object(watch.subprocess, "run", side_effect=run):
                    self.assertEqual(watch.main(root=root, sessions_dir=self.sessions, now=T0,
                                               tmux_argv=("tmux", "-L", "private-test")), 0)
        print("AC6 complete executed argv", json.dumps(inventory))
        with patch.object(watch.tomllib, "load", return_value={"pane_watchdog": {"settle_s": "bad"}}):
            self.assertEqual(watch._config(), watch.DEFAULTS)
        proof("AC6", inventory)

    def test_ac7_manifest_and_config(self):
        import crons
        import tomllib
        rows = crons.rows()
        added = [r for r in rows if r["name"] in {"pane-watchdog", "executor-wake"}]
        self.assertEqual(len(added), 2)
        for row in added:
            self.assertEqual(crons.interval_s(row["schedule"]), 300)
            self.assertEqual(row["where"], "user")
            self.assertTrue(row["owner"] and row["project"])
            self.assertTrue(os.access(row["path"], os.X_OK))
            self.assertIn("cron-run " + row["name"] + " --", row["command"])
        text = "\n".join(r["schedule"] + " " + r["command"] for r in added)
        crond = self.root / "empty-crond"
        crond.mkdir()
        absent = crons.check(crontab_text=text, crond_dir=str(crond))
        self.assertFalse({"pane-watchdog", "executor-wake"} & {r["name"] for r in absent})
        import re
        wake = next(r for r in added if r["name"] == "executor-wake")
        self.assertRegex("*/5 * * * * /home/albert/.do-it/executor-wake.sh", wake["sig"])
        self.assertIn("scripts/executor-wake.sh", wake["command"])
        self.assertIn("src/pane_watchdog.py", next(r for r in added if r["name"] == "pane-watchdog")["command"])
        self.assertEqual(tomllib.loads((REPO / "look.toml").read_text())["pane_watchdog"], watch.DEFAULTS)
        proof("AC7", [{k: r[k] for k in ("name", "schedule", "where")} for r in added])


if __name__ == "__main__":
    unittest.main()
