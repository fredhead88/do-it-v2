#!/usr/bin/env python3
"""Disposable-ledger proofs for the read-only master safety gate."""
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Import only after isolating the root; no production ledger is consulted.
_imports = tempfile.TemporaryDirectory(prefix="guards-import-")
with patch.dict(os.environ, {"DOIT_ROOT": _imports.name}):
    import guards_live as gate

NOW = datetime(2026, 10, 30, 4, 40, 7, tzinfo=timezone.utc)
DOIT = Path(__file__).resolve().parents[1] / "doit"


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="guards-live-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "events").mkdir()
        for project in ("example", "other"):
            (self.root / "repos" / project).mkdir(parents=True)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(gate.fold, "ROOT", self.root))
        self.stack.enter_context(patch.dict(os.environ, {"DOIT_ROOT": str(self.root),
                                                        "PYTHONDONTWRITEBYTECODE": "1"}))
        self.sequence = 0
        self.rows = []
        self.last = None

    def row(self, guard, age=24, **changes):
        self.sequence += 1
        row = dict(type=gate.STREAMS[guard], project="example", sha="a" * 40,
                   ts=(NOW - timedelta(hours=age)).isoformat(), ids=[])
        if guard == "r1":
            row.update(run_id=self.sequence, green=True)
        else:
            row.update(run_ids=[self.sequence], complete=True)
            if guard == "r2": row["green"] = True
            else: row["suite"] = "nightly"
        row.update(changes)
        return row

    def history(self, age=24):
        return [self.row(guard, age) for guard in gate.STREAMS]

    def write(self, rows, name="L-base-red-local.jsonl"):
        self.rows = rows
        path = self.root / "events" / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def check(self, project="example", now=NOW):
        answer = gate.check(project, now)
        self.assertEqual(set(answer), {"r1", "r2", "r3", "live", "why"})
        self.assertTrue(all(type(answer[key]) is bool for key in ("r1", "r2", "r3", "live")))
        self.assertEqual(answer["live"], all(answer[key] for key in gate.STREAMS))
        self.assertEqual(bool(answer["why"]), not answer["live"])
        self.assertTrue(all(isinstance(reason, str) for reason in answer["why"]))
        self.last = answer
        return answer

    def cli(self, *args):
        return subprocess.run([str(DOIT), "guards-live", "--project", *args],
                              env=dict(os.environ), capture_output=True, text=True, timeout=20)

    def tearDown(self):
        # A signed local response for each criterion's actual synthetic history.
        body = dict(test=self.id(), history=self.rows, result=self.last)
        encoded = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url="scratch://guards-live/" + self.id(), status=200,
                              body_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                              body_excerpt=body), sort_keys=True))


class StreakTests(Fixture):
    def test_independent_boundaries_and_solitary_observation(self):
        for guard in gate.STREAMS:
            for age, expected in ((24 - 1/3600, False), (24, True), (25, True)):
                with self.subTest(guard=guard, age=age):
                    rows = [self.row(g, age if g == guard else 25) for g in gate.STREAMS]
                    self.write(rows)
                    answer = self.check()
                    self.assertEqual(answer[guard], expected)
                    self.assertTrue(all(answer[g] for g in gate.STREAMS if g != guard))

    def test_jitter_uses_age_not_observation_span(self):
        rows = self.history()
        rows += [self.row("r3", 1/3600)]
        self.write(rows)
        self.assertTrue(self.check()["r3"])
        self.assertFalse(self.check(now=NOW - timedelta(seconds=1))["r3"])

    def test_reset_recovery_isolation_and_order(self):
        for guard in gate.STREAMS:
            with self.subTest(guard=guard):
                rows = self.history(72)
                red = dict(ids=["test_nightly::red"]) if guard == "r3" else dict(green=False)
                rows += [self.row(guard, 48, **red), self.row(guard, 24)]
                rows += [self.row(guard, 0, project="other", **red)]
                self.write(list(reversed(rows)))
                self.assertTrue(self.check()["live"])
                self.assertFalse(self.check(now=NOW - timedelta(seconds=1))[guard])
                rows += [self.row(guard, 1, **red)]
                self.write(rows)
                answer = self.check()
                self.assertFalse(answer[guard])
                self.assertTrue(all(answer[g] for g in gate.STREAMS if g != guard))

    def test_equal_time_red_wins(self):
        for guard in gate.STREAMS:
            rows = self.history(72)
            bad = self.row(guard, 25, **(dict(ids=["red"]) if guard == "r3" else dict(green=False)))
            green = self.row(guard, 25)
            for tail in ([bad, green], [green, bad]):
                self.write(rows + tail)
                self.assertFalse(self.check()[guard])

    def test_known_red_canary_and_red_nightly(self):
        rows = self.history()
        rows[0]["ids"] = ["test_known::red"]
        self.write(rows)
        self.assertTrue(self.check()["live"])
        rows[2]["ids"] = ["test_known::red"]
        self.write(rows)
        self.assertFalse(self.check()["r3"])


class EvidenceTests(Fixture):
    def test_missing_evidence_and_registration(self):
        self.assertFalse(self.check()["live"])
        self.write(self.history())
        for guard, kind in gate.STREAMS.items():
            with patch.dict(gate.fold.EMITS):
                gate.fold.EMITS.pop(kind)
                answer = self.check()
                self.assertFalse(answer[guard])
                self.assertIn(f"{guard}: missing producer registration", answer["why"])

    def test_invalid_projects_and_clocks(self):
        self.write(self.history())
        for project in (None, 1, "", "../example", "example;echo hostile", "missing", "a/b"):
            self.assertFalse(self.check(project)["live"])
        for clock in ("now", 1, datetime(2026, 10, 30)):
            self.assertFalse(self.check(now=clock)["live"])

    def test_invalid_and_future_timestamps(self):
        for guard in gate.STREAMS:
            for stamp in (None, "invalid", "2026-10-29T00:00:00", (NOW + timedelta(seconds=1)).isoformat()):
                self.write(self.history(72) + [self.row(guard, ts=stamp)])
                self.assertFalse(self.check()[guard])

    def test_bad_fields_and_undetermined_reset_then_recovery(self):
        for guard in gate.STREAMS:
            changes = [dict(sha="invalid"), dict(ids=None), dict(ids=[1]), dict(ids=[""])]
            changes += [dict(green="true")] if guard == "r1" else [dict(complete=False), dict(complete="true")]
            changes += [dict(run_id=None)] if guard == "r1" else [dict(run_ids=[])]
            if guard == "r2": changes += [dict(green="false")]
            if guard == "r3": changes += [dict(suite=s) for s in (None, "undeclared", "window", "local-hermetic")]
            for change in changes:
                with self.subTest(guard=guard, change=change):
                    rows = self.history(96) + [self.row(guard, 48, **change)]
                    self.write(rows)
                    self.assertFalse(self.check()[guard])
                    rows += [self.row(guard, 24)]
                    self.write(rows)
                    self.assertTrue(self.check()[guard])

    def test_source_missing_unreadable_and_corrupt(self):
        path = self.write(self.history())
        path.chmod(0)
        self.assertFalse(self.check()["live"])
        path.chmod(0o600)
        for text in ("{broken", "[]\n", "null\n", path.read_text() + '{"sentinel":"DO_NOT_ECHO"'):
            path.write_text(text)
            answer = self.check()
            self.assertFalse(answer["live"])
            self.assertNotIn("DO_NOT_ECHO", json.dumps(answer))
        path.unlink()
        (self.root / "events").rmdir()
        self.assertFalse(self.check()["live"])

    def test_filename_admission_and_operator_nightly(self):
        rows = self.history()
        for guard, kind in gate.STREAMS.items():
            self.assertIn(gate.fold.actor_of("L-base-red-local.jsonl"), gate.fold.EMITS[kind])
        self.write(rows[:2])
        self.write(rows[2:], "L-operator-local.jsonl")
        self.assertTrue(self.check()["live"])
        for guard in gate.STREAMS:
            bad = self.row(guard, 0, actor="base-red", green=False, complete=False)
            self.write([bad], "L-untrusted-local.jsonl")
            self.assertTrue(self.check()["live"])
        self.write([], "L-base-red-local.jsonl")
        self.write([], "L-operator-local.jsonl")
        self.write(rows, "L-untrusted-local.jsonl")
        self.assertFalse(self.check()["live"])

    def test_duplicates_and_contradictory_runs(self):
        for guard in gate.STREAMS:
            rows = self.history(72)
            green = next(row for row in rows if row["type"] == gate.STREAMS[guard])
            self.write(rows + [dict(green)])
            self.assertTrue(self.check()["live"])
            bad = dict(green, **(dict(ids=["red"]) if guard == "r3" else dict(green=False)))
            self.write(rows + [bad])
            self.assertFalse(self.check()[guard])
            bad["ts"] = (NOW - timedelta(hours=48)).isoformat()
            self.write(rows + [bad, self.row(guard, 24)])
            self.assertTrue(self.check()[guard])

    def test_corrected_evidence_before_project_filter(self):
        rows = self.history()
        rows[0].update(project="other", green=False)
        self.write(rows)
        correction = dict(type="correction", ts=NOW.isoformat(), ref="L-base-red-local.jsonl:1",
                          set=dict(project="example", green=True, actor="untrusted"))
        self.write([correction], "L-operator-local.jsonl")
        self.assertTrue(self.check()["live"])
        correction["set"] = dict(green=False)
        self.write([correction], "L-operator-local.jsonl")
        self.assertFalse(self.check()["r1"])

    def test_compacted_history_matches_loose_and_corrupt_index_fails(self):
        path = self.write(self.history())
        expected = self.check()
        data = path.read_bytes()
        segment = self.root / "events/segments"
        segment.mkdir()
        (segment / "history.jsonl").write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        index = dict(version=1, segment="history.jsonl", bytes=len(data), sha256=digest,
                     members=[dict(name=path.name, offset=0, length=len(data), lines=3,
                                   sha256=digest, ends_nl=True)])
        idx = segment / "history.idx.json"
        idx.write_text(json.dumps(index))
        self.assertEqual(self.check(), expected)  # exact loose/packed overlap
        path.unlink()
        self.assertEqual(self.check(), expected)
        idx.write_text("{broken")
        self.assertFalse(self.check()["live"])


class CliTests(Fixture):
    def test_green_false_missing_and_undetermined_json(self):
        for rows, expected in ((self.history(1000), 0), ([], 1),
                               (self.history(1000) + [self.row("r2", 999, green=False)], 1),
                               (self.history(1000) + [self.row("r3", 999, suite=None)], 1)):
            self.write(rows)
            proc = self.cli("example")
            self.assertEqual(proc.returncode, expected, proc.stderr)
            self.assertEqual(proc.stderr, "")
            body = json.loads(proc.stdout)
            self.assertEqual(body, self.check(now=datetime.now(timezone.utc)))
            self.assertEqual(body["live"], expected == 0)
            self.last = dict(stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode)

    def test_usage_and_default_clock(self):
        proc = self.cli()
        self.assertEqual(proc.returncode, 2)
        self.write(self.history(1000))
        self.assertTrue(gate.check("example")["live"])


def snapshot(root):
    result = []
    for path in sorted(root.rglob("*")):
        kind = "symlink" if path.is_symlink() else "dir" if path.is_dir() else "file"
        content = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
        result.append((str(path.relative_to(root)), kind, content))
    return result


class ReadOnlyTests(Fixture):
    def test_entire_root_unchanged_with_both_cache_modes(self):
        self.write(self.history(1000))
        (self.root / "link").symlink_to("repos/example", target_is_directory=True)
        for cache in (False, True):
            if cache:
                (self.root / "cache").mkdir()
                (self.root / "cache/existing").write_bytes(b"preserve me")
            for setting in (None, "1"):
                with patch.dict(os.environ):
                    os.environ.pop("DOIT_READ_CACHE", None)
                    if setting is not None: os.environ["DOIT_READ_CACHE"] = setting
                    before = snapshot(self.root)
                    self.assertTrue(self.check()["live"])
                    self.assertEqual(snapshot(self.root), before)
                    proc = self.cli("example")
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(snapshot(self.root), before)

    def test_library_no_external_execution_or_network(self):
        self.write(self.history())
        with patch.object(subprocess, "Popen", side_effect=AssertionError("external process")), \
             patch.object(os, "system", side_effect=AssertionError("shell")), \
             patch.object(socket, "socket", side_effect=AssertionError("network")):
            self.assertTrue(self.check()["live"])

    def test_cli_no_acquisition_from_python(self):
        self.write(self.history(1000))
        # Start the real dispatcher, then intercept all acquisition in its Python
        # processes (including the dispatcher's initial path-resolution Python).
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "sitecustomize.py").write_text(
            "import os, socket, subprocess, sys\n"
            "def audit(event, args):\n"
            "    if event.startswith(('socket.', 'subprocess.', 'os.system', 'os.exec', 'os.spawn')):\n"
            "        raise AssertionError('external acquisition')\n"
            "sys.addaudithook(audit)\n")
        with patch.dict(os.environ, {"PYTHONPATH": str(hooks)}):
            before = snapshot(self.root)
            proc = self.cli("example")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(snapshot(self.root), before)

    def test_safe_diagnostics_and_project_isolation(self):
        secret = "DO_NOT_ECHO_DIAGNOSTIC"
        self.write([dict(row, project="other", detail=secret) for row in self.history(1000)])
        self.assertFalse(self.check()["live"])
        self.assertNotIn(secret, json.dumps(self.last))
        for project in ("../other", "example;touch INJECTED", "$(echo " + secret + ")"):
            before = snapshot(self.root)
            self.assertFalse(self.check(project)["live"])
            proc = self.cli(project)
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn(secret, proc.stdout + proc.stderr)
            self.assertEqual(snapshot(self.root), before)
        self.write(self.history(1000) + [self.row("r1", 999, ids=[secret], green="bad:" + secret)])
        self.assertNotIn(secret, json.dumps(self.check()))
        proc = self.cli("example")
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn(secret, proc.stdout + proc.stderr)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        _imports.cleanup()
