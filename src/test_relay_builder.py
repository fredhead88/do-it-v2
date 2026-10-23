#!/usr/bin/env python3
"""Builder seat discovery, including dispatch's actual codex fallback sequence."""
import datetime
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

TMP = tempfile.TemporaryDirectory(prefix="relay-builder-")
os.environ["DOIT_ROOT"] = TMP.name
os.environ["DOIT_PROJECT"] = "relay-builder-fixture"

import dispatch, fold, relay, spawns, up  # noqa: E402


class BuilderSeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TMP.name)
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.seat = self.root / "seat"
        self.seat.mkdir()
        self.sid = "L-builder-3158"
        self.start = self.event("build-started", backend="seat")
        (self.seat / f"{self.sid}.packet.md").write_text("builder packet")

    def event(self, kind, minutes=7, **fields):
        return {"type": kind, "spawn": self.sid, "subject": "L-spec-1454",
                "ts": (fold.NOW - datetime.timedelta(minutes=minutes)).isoformat(),
                **fields}

    def pending(self, events):
        return relay.pending_packets(events, self.root, served_by="relay")

    def test_seat_builder_is_pending_and_uses_builder_server(self):
        rows = self.pending([self.start])
        self.assertEqual([row["spawn"] for row in rows], [self.sid])
        self.assertAlmostEqual(rows[0]["age_min"], 7)
        with mock.patch.dict(relay.SERVERS, builder="pane"):
            self.assertEqual(self.pending([self.start]), [])
            self.assertEqual(len(relay.pending_packets([self.start], self.root, served_by="pane")), 1)

    def test_wrapper_terminals_close_builder(self):
        self.assertEqual(relay.TERMINAL, set(spawns.TERMINAL))
        for terminal in spawns.TERMINAL:
            with self.subTest(terminal=terminal):
                events = [self.start, self.event(terminal, minutes=1)]
                self.assertEqual(self.pending(events), [])
                self.assertEqual(relay.unserved(events, self.root), [])

    def test_earlier_terminal_does_not_close_new_start(self):
        for terminal in spawns.TERMINAL:
            with self.subTest(terminal=terminal):
                self.assertEqual(len(self.pending([self.event(terminal, minutes=8), self.start])), 1)

    def test_build_output_event_is_not_wrapper_terminal(self):
        self.assertEqual(len(self.pending([self.start, self.event("build-done", minutes=1)])), 1)

    def test_claim_or_output_excludes_builder(self):
        for suffix in ("claimed", "output.json"):
            with self.subTest(suffix=suffix):
                path = self.seat / f"{self.sid}.{suffix}"
                path.write_text("{}")
                self.assertEqual(self.pending([self.start]), [])
                path.unlink()

    def test_packet_and_dispatch_are_both_required(self):
        self.assertEqual(self.pending([]), [])
        (self.seat / f"{self.sid}.packet.md").unlink()
        self.assertEqual(self.pending([self.start]), [])

    def test_non_seat_builder_is_excluded(self):
        for backend in (None, "codex", "claude-p"):
            with self.subTest(backend=backend):
                self.assertEqual(self.pending([{**self.start, "backend": backend}]), [])

    def test_actual_codex_fallback_is_pending_and_unserved(self):
        events = [{**self.start, "backend": "codex"},
                  self.event("backend-fallback", minutes=6, from_backend="codex",
                             to_backend="seat", to_model="claude-sonnet-5-5")]
        original = json.dumps(events, sort_keys=True)
        self.assertEqual([p["spawn"] for p in self.pending(events)], [self.sid])
        self.assertEqual(self.pending(events)[0]["age_min"], 7)
        rows = relay.unserved(events, self.root)
        self.assertEqual([(r["spawn"], r["role"]) for r in rows], [(self.sid, "builder")])
        self.assertEqual(json.dumps(events, sort_keys=True), original)
        for terminal in spawns.TERMINAL:
            self.assertEqual(self.pending(events + [self.event(terminal, minutes=1)]), [])
        for suffix in ("claimed", "output.json"):
            path = self.seat / f"{self.sid}.{suffix}"
            path.touch()
            self.assertEqual(self.pending(events), [])
            path.unlink()

    def test_fallback_requires_a_start_and_must_not_predate_it(self):
        fallback = self.event("backend-fallback", minutes=8, to_backend="seat")
        self.assertEqual(self.pending([fallback]), [])
        self.assertEqual(self.pending([fallback, {**self.start, "backend": "codex"}]), [])

    def test_spawn_started_discovery_and_terminals_unchanged(self):
        for backend in (None, "seat", "codex"):
            start = {**self.start, "type": "spawn-started", "role": "research", "backend": backend}
            with self.subTest(backend=backend):
                self.assertEqual([p["spawn"] for p in self.pending([start])], [self.sid])
                for terminal in spawns.TERMINAL:
                    self.assertEqual(self.pending([start, self.event(terminal, minutes=1)]), [])

    def test_relay_launcher_receives_builder_id_without_starting_a_process(self):
        # Exercise the real query -> launcher prompt, with all installation and
        # model lookup confined to this fixture. print_only never starts a pane.
        with mock.patch.multiple(fold, ROOT=self.root, EVENTS=self.root / "events"), \
                mock.patch.object(fold, "read_events", return_value=[self.start]), \
                mock.patch.object(up, "install"), \
                mock.patch.object(up.models, "load", return_value=None), \
                mock.patch.object(up.launch, "model_for", return_value=None), \
                mock.patch.object(up.subprocess, "Popen") as popen:
            cmd, env = up.relay_main(print_only=True, max_cycles=1)
        self.assertIn(self.sid, cmd)
        self.assertIn("relay", cmd)
        self.assertNotIn("-p", cmd)
        popen.assert_not_called()

    def test_seat_envelope_keeps_builder_worktree_tools_and_fallback_model(self):
        cwd = self.root / "builder-worktree"
        cwd.mkdir()
        tools = dispatch.frontmatter("builder")["tools"]
        cmd = ["claude", "--model", "claude-sonnet-5-5", "--allowedTools", tools]
        # A local completed result avoids waiting; run_seat only writes files,
        # and must never execute the command it records in cmd.json.
        (self.seat / f"{self.sid}.result.json").write_text("{}")
        with mock.patch.object(dispatch, "SEAT", self.seat), \
                mock.patch("time.sleep"), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(dispatch.subprocess, "Popen") as popen:
            dispatch.run_seat(self.sid, cmd, "builder packet", str(cwd), 60, role="builder")
        envelope = json.loads((self.seat / f"{self.sid}.cmd.json").read_text())
        self.assertEqual(envelope, {"cmd": cmd, "cwd": str(cwd), "path": None})
        self.assertEqual((self.seat / f"{self.sid}.packet.md").read_text(), "builder packet")
        required = {"Write", "Edit", "Bash"}
        self.assertTrue(required <= {t.strip() for t in tools.split(",")})
        relay_tools = dispatch.frontmatter("relay")["tools"]
        self.assertTrue(required <= {t.strip() for t in relay_tools.split(",")})
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
