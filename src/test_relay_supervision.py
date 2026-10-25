#!/usr/bin/env python3
"""Relay ownership, hermetic pane environments, visible failures and retry waits."""
import contextlib
import datetime
import fcntl
import io
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import dispatch
import fold
import look
import tick
import up


class RelaySupervisionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="relay-supervision-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "root"
        (self.root / "events").mkdir(parents=True)
        (self.root / "seat").mkdir()
        self.home = Path(tmp.name) / "home"
        self.bin = Path(tmp.name) / "stock-bin"
        self.bin.mkdir()
        self.home.mkdir()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "HOME": str(self.home), "PATH": str(self.bin), "DOIT_ROOT": str(self.root),
            "DOIT_SUPERVISOR_INTERVAL_SEC": "30", "DOIT_RELAY_WEDGED_MIN": "30",
            "DOIT_TICK_MIN": "5", "DOIT_NO_POKE": "1"}))
        os.environ.pop("DOIT_CLAUDE_BIN", None)
        for obj, key, value in ((fold, "ROOT", self.root), (fold, "EVENTS", self.root / "events"),
                                (dispatch, "EVENTS", self.root / "events")):
            self.stack.enter_context(patch.object(obj, key, value))
        for obj, name, value in ((up, "install", None), (up, "maybe_reexec", None),
                                 (up, "code_sha", None), (up.models, "load", None),
                                 (up.launch, "model_for", None), (up.launch, "record", None),
                                 (up.launch, "ended", None)):
            self.stack.enter_context(patch.object(obj, name, return_value=value))
        self.stack.enter_context(patch.object(up.launch, "child_env", side_effect=lambda role, base: base))
        self.now = datetime.datetime.now(datetime.timezone.utc)
        self.sid = "L-builder-9991"
        (self.root / "seat" / f"{self.sid}.packet.md").touch()
        self.events = [{"type": "spawn-started", "spawn": self.sid, "role": "builder",
                        "backend": "seat", "ts": self.now.isoformat()}]
        self.stack.enter_context(patch.object(fold, "read_events", return_value=self.events))
        self.stack.enter_context(patch.object(up, "_SLEEP"))
        self.launches = []

    def executable(self, path=None):
        path = path or self.home / ".local/bin/claude"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nprintf 'fake-claude-marker'\n")
        path.chmod(0o755)
        return path

    def rows(self):
        return [json.loads(line) for file in sorted((self.root / "events").glob("L-relay-*.jsonl"))
                for line in file.read_text().splitlines()]

    def hold_lock(self):
        fd = os.open(self.root / "relay.lock", os.O_RDONLY | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        return fd

    def ensure(self):
        return up.ensure_relay(self.events, self.now, launcher=self.launches.append)

    def test_held_lock_ignores_heartbeat_age_and_missing_file(self):
        self.hold_lock()
        hb = self.root / "relay.heartbeat"
        for age in (0, 600, -600, None):
            if age is None:
                hb.unlink()
            else:
                hb.touch()
                stamp = self.now.timestamp() - age
                os.utime(hb, (stamp, stamp))
            self.assertEqual(self.ensure(), "serving")
        self.assertFalse(self.launches)

    def test_wedged_recovery_and_invalid_threshold(self):
        self.hold_lock()
        hb = self.root / "relay.heartbeat"
        hb.touch()
        old = self.now.timestamp() - 1900
        os.utime(hb, (old, old))
        for value in ("30", "bogus", "0", "-1", "nan", "inf"):
            os.environ["DOIT_RELAY_WEDGED_MIN"] = value
            self.assertEqual(self.ensure(), "wedged")
        self.assertFalse(list((self.root / "events").iterdir()))
        os.utime(hb, (self.now.timestamp(), self.now.timestamp()))
        self.assertEqual(self.ensure(), "serving")
        self.assertFalse(self.launches)

    def test_sigkill_releases_lock_then_exactly_one_launch(self):
        # The child opens its own fd: no inherited parent reference can retain it.
        ctx = multiprocessing.get_context("fork")
        reader, writer = ctx.Pipe(duplex=False)
        def holder():
            fd = os.open(self.root / "relay.lock", os.O_RDONLY | os.O_CREAT, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX)
            writer.send(True)
            signal.pause()
        child = ctx.Process(target=holder)
        child.start()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        try:
            self.assertTrue(reader.poll(10), "child never acquired lock")
            reader.recv()
            self.assertEqual(self.ensure(), "serving")
        finally:
            child.kill()
            child.join(10)
        self.assertEqual(child.exitcode, -signal.SIGKILL)
        self.assertEqual(self.ensure(), "started")
        self.assertEqual(self.ensure(), "serving")
        self.assertEqual(len(self.launches), 1)

    def test_absent_lock_and_unreadable_inputs(self):
        self.assertFalse((self.root / "relay.lock").exists())
        with patch.object(up.os, "open", side_effect=PermissionError):
            self.assertEqual(self.ensure(), "undetermined")
        with patch.object(Path, "read_bytes", side_effect=PermissionError):
            (self.root / "events/L-fixture.jsonl").touch()
            self.assertEqual(self.ensure(), "undetermined")
        self.assertFalse(self.launches)
        self.assertEqual(self.ensure(), "started")

    def test_second_relay_exits_before_install_or_allocation(self):
        self.hold_lock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertIsNone(up.relay_main(max_cycles=1))
        self.assertEqual(output.getvalue().strip(), "relay: another relay holds relay.lock; exiting")
        up.install.assert_not_called()
        self.assertFalse(list((self.root / "events").iterdir()))

    def test_brief_probe_race_retries_without_using_stub_sleep(self):
        release, released = threading.Event(), threading.Event()
        ready = threading.Event()
        def holder():
            fd = os.open(self.root / "relay.lock", os.O_RDONLY | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                ready.set()
                release.wait(10)
            finally:
                os.close(fd)
                released.set()
        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(ready.wait(10))
        def retry(seconds):
            self.assertEqual(seconds, 0.1)
            release.set()
            self.assertTrue(released.wait(10))
        try:
            with patch.object(up.time, "sleep", side_effect=retry) as sleep:
                up.relay_main(max_cycles=0)
            sleep.assert_called_once()
            self.assertTrue((self.root / "relay.heartbeat").exists())
        finally:
            release.set()
            thread.join(10)

    def test_exception_and_systemexit_release_ownership(self):
        for exc in (RuntimeError("install failed"), SystemExit("missing contract")):
            with patch.object(up, "install", side_effect=exc):
                with self.assertRaises(type(exc)):
                    up.relay_main(max_cycles=0)
            # A second call in this process must acquire a separate description.
            up.relay_main(max_cycles=0)
        with patch.object(dispatch, "AGENTS", self.root / "missing"):
            with self.assertRaises(SystemExit):
                up.relay_main(max_cycles=0)
        up.relay_main(max_cycles=0)

    def test_environment_path_order_pin_and_real_bare_spawn(self):
        cli = self.executable()
        ledger = Path("L-relay-9999.jsonl")
        env = up._child_env(ledger)
        self.assertEqual(env["PATH"].split(":"), [str(up.HERE.parent), str(self.bin), str(cli.parent)])
        result = subprocess.run(["claude"], env=env, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, "fake-claude-marker")
        os.environ["PATH"] += ":" + str(cli.parent)
        self.assertEqual(up._child_env(ledger)["PATH"].split(":").count(str(cli.parent)), 1)
        pin = self.executable(self.home / "pinned/claude")
        os.environ["DOIT_CLAUDE_BIN"] = str(pin)
        os.environ["PATH"] += ":" + str(pin.parent)
        parts = up._child_env(ledger)["PATH"].split(":")
        self.assertEqual(parts[:2], [str(up.HERE.parent), str(pin.parent)])
        self.assertEqual(parts.count(str(pin.parent)), 1)

    def test_missing_and_bad_pin_do_not_break_environment_callers(self):
        for pin in (None, str(self.home / "absent/claude")):
            if pin:
                os.environ["DOIT_CLAUDE_BIN"] = pin
            for name, supervised in (("L-planner-0001.jsonl", True), ("L-executor-0001.jsonl", True),
                                      ("L-relay-0001.jsonl", False)):
                self.assertEqual(up._child_env(Path(name), supervised)["PATH"],
                                 f"{up.HERE.parent}:{self.bin}")
            cmd, env = up.relay_main(print_only=True)
            self.assertEqual(cmd[0], "claude")
            self.assertIn("PATH", env)
        self.assertFalse((self.root / "relay.lock").exists())

    def assert_failed_attempts(self, count, cause):
        rows = self.rows()
        self.assertEqual([r["type"] for r in rows], ["spawn-started", "spawn-failed"] * count)
        for start, failure in zip(rows[::2], rows[1::2]):
            self.assertEqual(start["spawn"], failure["spawn"])
            self.assertIn(cause, failure["why"])
        return rows

    def test_resolution_failures_survive_and_remain_visible_to_look(self):
        with patch.object(up.subprocess, "Popen") as popen:
            up.relay_main(max_cycles=2)
            self.assert_failed_attempts(2, "PATH")
            up.relay_main(max_cycles=1)
        popen.assert_not_called()
        rows = self.assert_failed_attempts(3, "FileNotFoundError")
        with patch.object(look, "_write_brief", return_value={"condition": "repeat-failure"}) as write:
            look._check_dispatch_failures(rows, self.root, [], [])
        args = write.call_args.args
        self.assertEqual(args[:3], ("repeat-failure", "relay", "thinker"))
        self.assertEqual(args[3]["role"], "relay")

    def test_bad_pin_and_environment_failures_are_started_spawns(self):
        os.environ["DOIT_CLAUDE_BIN"] = str(self.home / "missing/claude")
        up.relay_main(max_cycles=2)
        self.assert_failed_attempts(2, os.environ["DOIT_CLAUDE_BIN"])
        self.executable()
        os.environ.pop("DOIT_CLAUDE_BIN")
        for exc in (ValueError("bad role"), up.scratch.ScratchRootError("bad scratch")):
            with patch.object(up.launch, "child_env", side_effect=exc):
                up.relay_main(max_cycles=1)
            self.assertIn(str(exc), self.rows()[-1]["why"])
            self.assertEqual(self.rows()[-2]["type"], "spawn-started")

    def run_failures(self, sequence, interval=30):
        self.executable()
        os.environ["DOIT_SUPERVISOR_INTERVAL_SEC"] = str(interval)
        totals = []
        current = [0]
        first = [True]
        child = types.SimpleNamespace(pid=123, wait=lambda **kw: 0)
        def spawn(*args, **kw):
            if current[0]:
                totals.append(current[0])
                current[0] = 0
            item = sequence.pop(0)
            if item is not None:
                raise item
            return child
        def sleep(seconds):
            current[0] += seconds
            self.assertEqual(self.ensure(), "serving")
            if first[0]:
                first[0] = False
                (self.root / "events/L-fixture.jsonl").write_text('{}\n')
        cycles = len(sequence)
        with patch.object(up.subprocess, "Popen", side_effect=spawn), patch.object(up, "_SLEEP", side_effect=sleep):
            up.relay_main(max_cycles=cycles)
        if current[0]:
            totals.append(current[0])
        return totals

    def test_identical_failure_backoff_and_cap(self):
        self.assertEqual(self.run_failures([FileNotFoundError("fake") for _ in range(5)] + [None]),
                         [30, 60, 120, 120, 120])
        self.assertEqual(sum(r["type"] == "spawn-failed" for r in self.rows()), 5)

    def test_changed_class_and_success_reset_backoff(self):
        self.assertEqual(self.run_failures([FileNotFoundError(), PermissionError(), FileNotFoundError(),
                                           None, FileNotFoundError(), None]), [30, 30, 30, 30])

    def test_zero_interval_floor(self):
        self.assertEqual(self.run_failures([FileNotFoundError()], interval=0), [30])

    def test_dry_queue_does_not_reset_failure_run(self):
        self.executable()
        pending = up._unclaimed_pending
        totals = []
        total = [0]
        def spawn(*args, **kwargs):
            if total[0]:
                totals.append(total[0])
                total[0] = 0
            raise FileNotFoundError("fake")
        def sleep(seconds):
            total[0] += seconds
        with patch.object(up, "_unclaimed_pending", side_effect=[pending([self.sid], self.root), [],
                                                                 pending([self.sid], self.root)]), \
             patch.object(up, "_hold") as hold, \
             patch.object(up.subprocess, "Popen", side_effect=spawn), \
             patch.object(up, "_SLEEP", side_effect=sleep):
            up.relay_main(max_cycles=3)
        totals.append(total[0])
        self.assertEqual(totals, [30, 60])
        hold.assert_called_once()

    def test_acquisition_refreshes_old_heartbeat_before_install(self):
        hb = self.root / "relay.heartbeat"
        hb.touch()
        old = self.now.timestamp() - 3600
        os.utime(hb, (old, old))
        def install(contract):
            self.assertGreater(hb.stat().st_mtime, self.now.timestamp() - 5)
            self.assertEqual(self.ensure(), "serving")
            self.assertFalse(list((self.root / "events").iterdir()))
        with patch.object(up, "install", side_effect=install):
            up.relay_main(max_cycles=0)

    def test_tick_records_wedge_and_age(self):
        hb = self.root / "relay.heartbeat"
        hb.touch()
        old = self.now.timestamp() - 2000
        os.utime(hb, (old, old))
        with contextlib.ExitStack() as stack:
            for target, result in (("intake.run", {}), ("notes.run", None), ("pr_closeout.run", None),
                ("install_sync.run", []), ("push_origin.run", None), ("carry.uncarried", []),
                ("carry.sync_v4", None), ("pane_resume.run", None), ("crons.check", []),
                ("look._read_state", {"last_pass": fold.NOW.isoformat()}), ("look.heartbeat_missed", False),
                ("tree_cleanup.reap_worktrees", ([], [])), ("autodispatch.redispatch_dead", []),
                ("merge_queue.enqueue_due", [])):
                stack.enter_context(patch(target, return_value=result))
            stack.enter_context(patch.object(tick, "_steps", return_value=[]))
            stack.enter_context(patch.object(tick, "run_steps", return_value=[]))
            stack.enter_context(patch.object(fold, "read_events", return_value=[]))
            for present in (True, False):
                if not present:
                    hb.unlink()
                with patch.object(up, "ensure_relay", return_value="wedged") as ensure:
                    tick._record()
                now = ensure.call_args.args[1]
                self.assertEqual(now.tzinfo, datetime.timezone.utc)
                self.assertLess(abs((now - datetime.datetime.now(datetime.timezone.utc)).total_seconds()), 5)
                rows = [json.loads(line) for file in (self.root / "events").glob("*.jsonl")
                        for line in file.read_text().splitlines()]
                row = [r for r in rows if r["type"] == "tick"][-1]
                self.assertEqual(row["relay"], "wedged")
                if present:
                    self.assertAlmostEqual(row["relay_heartbeat_age_s"], 2000, delta=5)
                else:
                    self.assertIsNone(row["relay_heartbeat_age_s"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
