#!/usr/bin/env python3
"""Coupled-grant discovery against disposable committed repos and real ledgers."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import spec_preflight as sp

SOURCE = Path(__file__).resolve().parent


class GrantFixture(unittest.TestCase):
    sid = "L-spec-9001"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="coupled-grants-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "ledger"
        self.root.mkdir()
        self.repo = self.root / "repos/do-it-v2"
        self.repo.mkdir(parents=True)
        (self.root / "content").mkdir()
        (self.root / "events").mkdir()
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
                    "DOIT_ROOT": str(self.root), "DOIT_PROJECT": "",
                    "DOIT_READ_CACHE": "0", "PYTHONPATH": str(SOURCE),
                    "DOIT_SCRATCH": str(Path(self.temp.name) / "scratch")}
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"DOIT_SCRATCH": self.env["DOIT_SCRATCH"]}).start()
        self.now = datetime.now(timezone.utc)
        self.serial = 0
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.file("src/widget.py", "VALUE = 1\n")
        self.file("src/test_importer.py", "import widget\n")
        self.file("src/test_reader.py", "from pathlib import Path\np = Path('src/widget.py')\n")
        self.file("src/test_widget.py", "assert True\n")
        self.file("src/test_unrelated.py", "p = 'other/src/widget.py.extra'\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.path = self.root / "content" / (self.sid + ".md")
        self.path.write_text("project: do-it-v2\n## Interfaces\nWrites: src/widget.py\n"
                             "Unrelated prose: src/test_widget.py\n"
                             "## Acceptance criteria\nAC1 [backend]: works\n"
                             "review_path: src/test_importer.py::Case.test_value\n"
                             "## Verification\n```bash\n"
                             "/usr/bin/python3 src/test_reader.py\n```\n")
        self.written()

    def command(self, argv, **kwargs):
        return subprocess.run(argv, capture_output=True, text=True, timeout=30,
                              check=True, **kwargs).stdout.strip()

    def git(self, *args):
        return self.command(["git", "-C", str(self.repo), *args])

    def file(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def written(self):
        self.serial += 1
        stamp = self.now - timedelta(minutes=10) + timedelta(seconds=self.serial)
        if self.serial > 1:
            stamp = datetime.now(timezone.utc)
            self.now = stamp + timedelta(seconds=1)
        event = dict(type="spec-written", subject=self.sid, project="do-it-v2",
                     path=str(self.path), footprint=["src/widget.py"],
                     ts=stamp.isoformat())
        with (self.root / "events/L-spec-writer-fixture.jsonl").open("a") as out:
            out.write(json.dumps(event) + "\n")

    def read(self):
        # A separate interpreter supplies _src and emitter admission canonically.
        code = "import json, fold; print(json.dumps(fold.fold(fold.read_events())[3]))"
        return json.loads(self.command(["/usr/bin/python3", "-c", code], env=self.env)).get(self.sid, [])

    def grants(self):
        return [e for e in self.read() if e["type"] == "writes-granted"]

    def tick(self, expected=0):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(sp.tick(root=self.root, now=self.now), expected)

    def assert_failed_closed(self):
        self.assertEqual(self.grants(), [])
        result = [e for e in self.read() if e["type"] == "spec-preflight"][-1]
        self.assertTrue(result["undetermined"], result)


class GrantDiscoveryTests(GrantFixture):
    def test_real_append_revision_roundtrip_and_retry(self):
        append = sp._append
        with mock.patch.object(sp, "_append", side_effect=ValueError("interrupted later append")):
            self.tick(1)
        self.assertEqual(len(self.grants()), 1)
        with mock.patch.object(sp, "_append", wraps=append):
            self.tick()
        self.tick()
        events = self.read()
        grant = self.grants()[0]
        written = [e for e in events if e["type"] == "spec-written"][-1]
        self.assertEqual(grant["spec_written_src"], written["_src"])
        self.assertEqual(grant["subject"], self.sid)
        self.assertEqual(grant["actor"], "spec-preflight")
        self.assertEqual(grant["project"], "do-it-v2")
        self.assertTrue(grant["why"].strip())
        rows = grant["tests"]
        self.assertEqual(rows, sorted(rows, key=lambda r: (r["test"], r["source"], r["kind"])))
        triples = [(r["test"], r["source"], r["kind"]) for r in rows]
        self.assertEqual(len(triples), len(set(triples)))
        self.assertIn(("src/test_importer.py", "src/widget.py", "import"), triples)
        self.assertIn(("src/test_reader.py", "src/widget.py", "read"), triples)
        self.assertIn(("src/test_widget.py", "src/widget.py", "named"), triples)
        self.assertEqual({r["test"] for r in rows},
                         {"src/test_importer.py", "src/test_reader.py", "src/test_widget.py"})
        for row in rows:
            self.assertEqual(row["ac_bound"], row["test"] != "src/test_widget.py")
        self.assertEqual(len(self.grants()), 1)
        self.written()
        self.tick()
        self.assertEqual(len(self.grants()), 2)
        self.assertNotEqual(self.grants()[0]["spec_written_src"], self.grants()[1]["spec_written_src"])
        body = dict(rows=rows, revision_bound=True, repeated_tick_grants=1, new_revision_grants=2)
        encoded = json.dumps(body, sort_keys=True).encode()
        print(json.dumps(dict(url="cli://coupled-grants/GrantDiscoveryTests", status=0,
                              body_sha256=hashlib.sha256(encoded).hexdigest(), body_excerpt=body)))

    def test_no_hits_is_persisted_once(self):
        self.path.write_text("Writes: src/absent.py\n")
        self.tick()
        self.tick()
        self.assertEqual(len(self.grants()), 1)
        self.assertEqual(self.grants()[0]["tests"], [])

    def test_duplicate_discovery_and_original_text_grants(self):
        self.path.write_text(self.path.read_text().replace(
            "Writes: src/widget.py\n", "Writes: src/widget.py src/test_reader.py src/widget.py\n"))
        self.file("src/test_reader.py", "import widget\nimport widget\np = 'src/widget.py'\n")
        self.file("src/test_recursive.py", "import test_widget\n")
        self.git("add", ".")
        self.git("commit", "-qm", "duplicate fixture")
        self.tick()
        rows = self.grants()[0]["tests"]
        self.assertNotIn("src/test_reader.py", {r["test"] for r in rows})
        self.assertNotIn("src/test_recursive.py", {r["test"] for r in rows})
        self.assertEqual(len(rows), len({(r["test"], r["source"], r["kind"]) for r in rows}))

    def test_suffix_python_test_is_discovered(self):
        self.file("src/widget_test.py", "import widget\n")
        self.git("add", ".")
        self.git("commit", "-qm", "suffix test fixture")
        self.tick()
        self.assertIn(("src/widget_test.py", "import"),
                      {(row["test"], row["kind"]) for row in self.grants()[0]["tests"]})

    def test_malformed_test_prevents_partial_grant(self):
        self.file("src/test_bad.py", "def invalid(\n")
        self.git("add", ".")
        self.git("commit", "-qm", "malformed fixture")
        self.tick()
        self.assert_failed_closed()

    def test_unreadable_test_prevents_partial_grant(self):
        original = Path.read_text

        def read(path, *args, **kwargs):
            if path.name == "test_importer.py":
                raise PermissionError("unreadable fixture")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", read):
            self.tick()
        self.assert_failed_closed()

    def test_scan_failure_prevents_partial_grant(self):
        import build_readiness
        with mock.patch.object(build_readiness, "companion_matches", side_effect=ValueError("scan failed")):
            self.tick()
        self.assert_failed_closed()

    def test_later_hook_failure_never_authorizes_staged_rows(self):
        def broken(*args):
            raise RuntimeError("fixture hook")
        with mock.patch.object(sp, "after_run", [*sp.after_run, broken]):
            self.tick()
        self.assert_failed_closed()

    def test_unsafe_test_symlink_fails_closed(self):
        outside = self.root / "outside.py"
        outside.write_text("import widget\n")
        (self.repo / "src/test_escape.py").symlink_to(outside)
        self.git("add", ".")
        self.git("commit", "-qm", "unsafe fixture")
        self.tick()
        self.assert_failed_closed()


class ConsumerFixture(GrantFixture):
    def setUp(self):
        super().setUp()
        self.tick()
        self.events = self.read()
        self.base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-qb", "fixture-changes")
        for name in ("src/test_importer.py", "src/test_reader.py"):
            self.file(name, (self.repo / name).read_text() + "assert 2 == 2\n")
        self.git("add", ".")
        self.git("commit", "-qm", "permitted assertion edits")

    def receipt(self, body):
        encoded = json.dumps(body, sort_keys=True).encode()
        print(json.dumps(dict(url="cli://coupled-grants/" + self.id(), status=0,
                              body_sha256=hashlib.sha256(encoded).hexdigest(), body_excerpt=body)))

    def ready(self):
        import build_readiness as readiness
        with mock.patch.object(readiness.grading_env, "_project_row", return_value={}):
            return readiness.check(self.sid, "do-it-v2", self.base, self.events, root=self.root)

    def permission(self, cli=()):
        import fold
        import merge_gate
        with mock.patch.object(fold, "ROOT", self.root), \
             mock.patch.object(fold, "read_events", return_value=self.events):
            return merge_gate.grant_for(self.sid, cli)

    def render(self):
        import packet
        import packet_base
        c = packet.Ctx.__new__(packet.Ctx)
        c.a = packet.argparse.Namespace(role="builder", subject=self.sid, project="do-it-v2",
            worktree=str(self.repo), repo=str(self.repo), base_sha=self.base,
            charter=None, produces=None, conventions=None)
        c.evs = self.events
        c.by = {self.sid: self.events}
        c.specs, c.charters = {}, {}
        with mock.patch.object(packet, "ROOT", self.root), \
             mock.patch.object(packet, "CONTENT", self.root / "content"), \
             mock.patch.object(packet_base, "_project_main", return_value="main"), \
             mock.patch.object(sp, "_discover_coupled", side_effect=AssertionError("packet discovery")), \
             mock.patch.object(sp, "_append_grant", side_effect=AssertionError("packet append")):
            return "\n".join(packet.p_builder(c))

    def new_revision(self):
        self.written()
        self.events = self.read()

    def handin(self):
        code = ("import json, handin_check; "
                "r=handin_check.run('.', '" + self.sid + "', main='main'); "
                "print(json.dumps(dict(ok=r.ok, failed_check=r.failed_check, findings=r.findings)))")
        env = dict(self.env, DOIT_LEDGER_FILE="L-builder-fixture.jsonl")
        return json.loads(self.command(["/usr/bin/python3", "-c", code],
                                       cwd=self.repo, env=env).splitlines()[-1])

    def merge(self):
        return subprocess.run(["/usr/bin/python3", str(SOURCE / "merge_gate.py"),
                               "fixture-changes", "main", "--spec", self.sid],
                              cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=30)


class EffectiveConsumersTests(ConsumerFixture):
    def test_current_revision_and_shared_reader(self):
        import effective_writes
        expected = effective_writes.effective_grant(self.sid, self.path.read_text(), self.events)
        with mock.patch.object(effective_writes, "effective_grant",
                               wraps=effective_writes.effective_grant) as reader:
            result = self.ready()
            self.assertEqual(result["status"], "ready", result)
            self.assertEqual(result["notes"], [])  # Real bounded Verification ran successfully.
            reader.assert_called_with(self.sid, self.path.read_text(), self.events)
            reader.reset_mock()
            self.assertEqual(self.permission(), expected)
            reader.assert_called_once_with(self.sid, self.path.read_text(), self.events)
            reader.reset_mock()
            rendered = self.render()
            reader.assert_called_with(self.sid, self.path.read_text(), self.events)
        self.assertEqual({call.args[0] for call in reader.call_args_list}, {self.sid})
        self.assertIn("Writes (the merge grant, = the spec's footprint): " + ", ".join(expected), rendered)
        self.assertTrue(self.handin()["ok"])
        self.assertEqual(self.merge().returncode, 0)
        import dispatch
        import fold
        import lane_driver
        import packet_base
        worktree = self.root / "worktrees/do-it-v2" / self.sid.lower()
        worktree.mkdir(parents=True)
        prepared = []

        def runner(argv):
            prepared.append(argv[1])
            if argv[1] == "readiness":
                result = self.ready()
                self.assertEqual(result["status"], "ready", result)
                return dict(code=0, stdout=json.dumps(result))
            if argv[1] == "packet":
                self.assertIn("src/test_reader.py", self.render())
                return dict(code=0, stdout=str(self.root / "packet.md"))
            # Transport only: no seat or external process is launched.
            self.assertEqual(argv[1], "dispatch")
            return dict(code=0, stdout=json.dumps(dict(detached="fixture-seat")))

        with mock.patch.object(fold, "ROOT", self.root), \
             mock.patch.object(dispatch, "CONTENT", self.root / "content"), \
             mock.patch.object(packet_base, "_project_main", return_value="main"):
            self.assertEqual(lane_driver._dispatch("builder", self.sid, "do-it-v2", None, runner),
                             "fixture-seat")
        self.assertEqual(prepared, ["readiness", "packet", "dispatch"])
        self.receipt(dict(readiness=result["status"], grant=expected))

    def test_stale_unrelated_empty_and_new_matching_grant(self):
        self.new_revision()
        self.assertEqual(self.permission(), ["src/widget.py"])
        self.assertEqual(self.ready()["status"], "bounced")
        self.assertFalse(self.handin()["ok"])
        old = self.grants()[0]
        other = dict(old, subject="L-spec-9999", spec_written_src=self.events[-1]["_src"])
        self.events.append(other)
        self.assertEqual(self.permission(), ["src/widget.py"])
        empty = dict(old, tests=[], spec_written_src=next(
            e["_src"] for e in reversed(self.events) if e["type"] == "spec-written"))
        self.events.append(empty)
        self.assertEqual(self.permission(), ["src/widget.py"])
        self.assertEqual(self.ready()["status"], "bounced")
        self.tick()
        self.events = self.read()
        self.assertIn("src/test_reader.py", self.permission())
        self.assertEqual(self.ready()["status"], "ready")
        self.assertTrue(self.handin()["ok"])

    def test_non_test_companion_still_refuses(self):
        self.file("docs/HOW-IT-WORKS.md", "fixture\n")
        self.git("add", ".")
        self.git("commit", "-qm", "non-test companion")
        self.base = self.git("rev-parse", "HEAD")
        self.path.write_text(self.path.read_text() + "\nnew event type\n")
        result = self.ready()
        self.assertEqual(result["status"], "bounced")
        self.assertIn("docs/HOW-IT-WORKS.md", " ".join(result["findings"]))

    def test_legacy_cli_operator_extensions_and_pure_parser(self):
        import fold
        import merge_gate
        import rulings
        self.assertEqual(merge_gate.grant_from_text(self.path.read_text()), ["src/widget.py"])
        with mock.patch.object(fold, "ROOT", self.root), \
             mock.patch.object(fold, "read_events", side_effect=AssertionError("standalone ledger read")):
            self.assertEqual(merge_gate.writes_grant(self.sid), ["src/widget.py"])
        ruling = dict(kind="grant-extend", spec=self.sid, paths=["src/test_operator.py"])
        with mock.patch.object(rulings, "decisions", return_value=[("fixture", ruling)]):
            grant = self.permission(["src/test_cli.py"])
        self.assertEqual(grant[:2], ["src/test_cli.py", "src/test_operator.py"])
        self.assertIn("src/test_importer.py", grant)
        self.path.write_text("## Goal\nLegacy explicit CLI grant\n")
        self.assertEqual(self.permission(["src/test_cli.py"]), ["src/test_cli.py"])
        self.path.write_text("Writes: prose is not permission\n")
        with self.assertRaises(merge_gate.Undetermined):
            self.permission(["src/test_cli.py"])


class GrantSafetyTests(ConsumerFixture):
    baseline = ("import unittest\nVALUE = 1\n"
                "def test_value():\n    assert VALUE == 1\n"
                "class Case(unittest.TestCase):\n"
                "    def test_method(self):\n        self.assertEqual(VALUE, 1)\n")

    def setUp(self):
        super().setUp()
        # Amend the disposable base, retaining the original real discovery rows.
        self.git("checkout", "main")
        for path in ("src/test_importer.py", "src/test_widget.py"):
            self.file(path, self.baseline)
        self.file("scripts/ci/detect-secrets-changed.sh", "#!/bin/bash\nexit 0\n")
        self.git("add", ".")
        self.git("commit", "-qm", "safety base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("checkout", "fixture-changes")
        self.git("reset", "--hard", self.base)

    def commit_edit(self, path, text):
        self.git("reset", "--hard", self.base)
        if text is None:
            self.git("rm", path)
        else:
            self.file(path, text)
        self.git("add", ".")
        self.git("commit", "-qm", "safety edit")

    def outcomes(self, path, allowed):
        gate = self.merge()
        handin = self.handin()
        self.assertEqual(gate.returncode == 0, allowed, gate.stdout + gate.stderr)
        self.assertEqual(handin["ok"], allowed, handin)
        if not allowed:
            self.assertIn(path, gate.stdout + gate.stderr)
            self.assertIn(path, " ".join(handin["findings"]))
        return dict(merge_exit=gate.returncode, handin=handin)

    def test_committed_diff_matrix(self):
        cases = {
            "fixture": (self.baseline.replace("VALUE = 1", "VALUE = 2"), True),
            "assertion": (self.baseline.replace("VALUE == 1", "VALUE == 2"), True),
            "delete": (None, False),
            "function": (self.baseline.replace("def test_value():\n    assert VALUE == 1\n", ""), False),
            "method": (self.baseline.replace("    def test_method(self):\n        self.assertEqual(VALUE, 1)", "    pass"), False),
            "unittest_skip": (self.baseline.replace("def test_value", "@unittest.skip('reason')\ndef test_value"), False),
            "pytest_skip": ("import pytest\n" + self.baseline.replace("def test_value", "@pytest.mark.skip\ndef test_value"), False),
            "pytest_skipif": ("import pytest\n" + self.baseline.replace("def test_value", "@pytest.mark.skipif(True, reason='reason')\ndef test_value"), False),
            "xfail": ("import pytest\n" + self.baseline.replace("def test_value", "@pytest.mark.xfail\ndef test_value"), False),
            "inline": (self.baseline.replace("assert VALUE == 1", "unittest.skip('reason')"), False),
            "inline_skiptest": (self.baseline.replace("self.assertEqual(VALUE, 1)", "self.skipTest('reason')"), False),
            "malformed": ("def test_value(:\n", False),
        }
        rows = []
        for path in ("src/test_importer.py", "src/test_widget.py"):
            for name, (text, allowed) in cases.items():
                with self.subTest(path=path, case=name):
                    self.commit_edit(path, text)
                    rows.append(dict(path=path, case=name, **self.outcomes(path, allowed)))
            self.git("reset", "--hard", self.base)
            self.git("mv", path, "src/test_renamed.py")
            self.git("commit", "-qm", "renamed test")
            rows.append(dict(path=path, case="rename", **self.outcomes(path, False)))
        self.receipt(dict(outcomes=rows))

    def test_unsafe_event_paths_and_symlink(self):
        # Use the actual append file, preserving admitted actor and revision.
        event_path = next(p for p in (self.root / "events").glob("*.jsonl")
                          if any(json.loads(line).get("type") == "writes-granted"
                                 for line in p.read_text().splitlines()))
        original = event_path.read_text()
        for path in ("/outside/test_escape.py", "../test_escape.py", "src/test_$(touch MARKER).py",
                     "src/widget.py"):
            rows = [json.loads(line) for line in original.splitlines()]
            for row in rows:
                if row.get("type") == "writes-granted":
                    row["tests"] = [dict(test=path, source="src/widget.py", kind="read", ac_bound=False)]
            event_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            self.commit_edit("src/test_importer.py", self.baseline.replace("VALUE = 1", "VALUE = 2"))
            self.outcomes(path, False)
            self.assertFalse((self.repo / "MARKER").exists())
        event_path.write_text(original)
        self.git("reset", "--hard", self.base)
        outside = self.root / "outside.py"
        outside.write_text("raise AssertionError('must not read or execute')\n")
        path = "src/test_importer.py"
        (self.repo / path).unlink()
        (self.repo / path).symlink_to(outside)
        self.git("add", ".")
        self.git("commit", "-qm", "symlink escape")
        self.outcomes(path, False)

    def test_existing_secret_failure_survives(self):
        self.git("checkout", "main")
        self.file("scripts/ci/detect-secrets-changed.sh",
                  "#!/bin/bash\necho '  fixture:1 -- Synthetic finding'\nexit 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "trusted scanner finding")
        self.base = self.git("rev-parse", "HEAD")
        self.git("checkout", "fixture-changes")
        self.commit_edit("src/test_importer.py", self.baseline.replace("VALUE = 1", "VALUE = 2"))
        gate = self.merge()
        result = self.handin()
        self.assertNotEqual(gate.returncode, 0)
        self.assertEqual(result["failed_check"], "secret-scan")


class PacketTests(ConsumerFixture):
    heading = "Granted coupled tests (fixture and assertion updates only)"

    def test_persisted_metadata_ac_warning_and_read_only_rendering(self):
        before = {p.name: p.read_bytes() for p in (self.root / "events").iterdir()}
        rendered = self.render()
        self.assertIn("PINNED_BASE_SHA: " + self.base, rendered)
        section = rendered.split(self.heading, 1)[1].split("## STOP", 1)[0]
        for row in self.grants()[0]["tests"]:
            line = next(line for line in section.splitlines() if row["test"] in line
                        and row["source"] in line and row["kind"] in line)
            self.assertEqual("do not weaken" in line, row["ac_bound"])
        self.assertIn("fixture and assertion updates only", rendered)
        self.assertEqual(rendered, self.render())
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root / "events").iterdir()})
        self.receipt(dict(section=section, ledger_unchanged=True))

    def test_missing_stale_and_empty_grants_are_not_claimed(self):
        self.events = [e for e in self.events if e["type"] != "writes-granted"]
        rendered = self.render()
        self.assertNotIn(self.heading, rendered)
        self.events = self.read()
        self.new_revision()
        rendered = self.render()
        self.assertNotIn(self.heading, rendered)
        writes = next(line for line in rendered.splitlines() if "Writes (the merge grant" in line)
        self.assertNotIn("src/test_reader.py", writes)
        old = self.grants()[0]
        newest = next(e["_src"] for e in reversed(self.events) if e["type"] == "spec-written")
        self.events.append(dict(old, tests=[], spec_written_src=newest))
        self.assertNotIn(self.heading, self.render())


if __name__ == "__main__":
    unittest.main()
