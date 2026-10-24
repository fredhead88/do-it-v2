"""Profile, script and real-ledger regressions for the shared check runner."""
import dataclasses
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import checks
import fold
import provision


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="provision-test-")
        self.addCleanup(self.tmp.cleanup)
        self.top = Path(self.tmp.name)
        self.root = self.top / "root"
        self.root.mkdir()
        self.tree = self.top / "tree"
        self.tree.mkdir()
        for target, changes in ((os.environ, {"DOIT_ROOT": str(self.root),
                "DOIT_LEDGER_FILE": "L-operator-local.jsonl"}),):
            self.enterContext(patch.dict(target, changes))
        self.enterContext(patch.object(fold, "ROOT", self.root))
        self.enterContext(patch.object(fold, "EVENTS", self.root / "events"))
        self.enterContext(provision.context(tree=self.tree, row={}))
        self.record = checks.CheckRecord("L-spec-fixture:AC1", "L-spec-fixture", "AC1",
            [sys.executable, "-c", "print('observed')"], "", "scratch", False,
            "repo-venv", "pre-merge", "script")

    def read_events(self):
        result = subprocess.run([sys.executable, "-c",
            "import fold,json; print(json.dumps(fold.read_events(use_cache=False)))"],
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).parent)),
            capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_each_profile(self):
        for profile in ("repo-scratch-pg", "repo-venv", "prod-readonly", "browser-review"):
            with self.subTest(profile=profile):
                record = dataclasses.replace(self.record, profile=profile)
                env = provision.prepare(record, lane="test")
                try:
                    self.assertEqual(env.profile, profile)
                finally:
                    env.cleanup()

    def test_unknown_and_missing_profile_create_nothing(self):
        for record in (dataclasses.replace(self.record, profile="unknown"), {}, None):
            with self.subTest(record=type(record).__name__):
                with self.assertRaises(provision.Undetermined):
                    provision.prepare(record, lane="test")
        self.assertEqual(list(self.tree.iterdir()), [])
        self.assertFalse((self.root / "events").exists())

    def test_scratch_pg_setup_and_cleanup_real_connection(self):
        record = dataclasses.replace(self.record, profile="repo-scratch-pg")
        names = ["SUPABASE_DB_URL", "DATABASE_URL", "PG_PARITY_DB_URL", "SCRATCH_PG_URL"]
        setup = {"db_env": names, "db_setup": {"argv": [sys.executable, "-c",
            "import os; from pathlib import Path; "
            "assert os.environ['DATABASE_URL']==os.environ['AS_LOCAL_PG_ADMIN']; "
            "Path('setup-ran').touch()"]}}
        with provision.context(tree=self.tree, row=setup):
            env = provision.prepare(record, lane="test")
        directory = Path(env.resources[-1])
        try:
            self.assertTrue((self.tree / "setup-ran").exists())
            endpoint = env.env["AS_LOCAL_PG_ADMIN"]
            self.assertTrue(all(env.env[name] == endpoint for name in names))
            result = subprocess.run(["/usr/lib/postgresql/17/bin/psql", endpoint,
                "-Atc", "select 1"], capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, "1"))
            from urllib.parse import urlsplit
            port = urlsplit(endpoint).port
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                pass
            events = self.read_events()
            self.assertTrue(any(e.get("type") == "resource-created" and
                e.get("kind") == "scratch-db" and e.get("ref") == str(directory)
                and e.get("lane") == "test" and e.get("subject") == record.spec for e in events))
            print("resource-created round trip: scratch-db, lane=test, subject=L-spec-fixture")
        finally:
            env.cleanup()
        self.assertFalse(directory.exists())
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1)

    def test_dirty_stop_retains_directory(self):
        record = dataclasses.replace(self.record, profile="repo-scratch-pg")
        env = provision.prepare(record, lane="test")
        directory = Path(env.resources[-1])
        with patch.object(provision, "PG_CTL", "/bin/false"):
            with self.assertRaises(provision.Undetermined):
                env.cleanup()
        self.assertTrue(directory.exists())
        env.cleanup()

    def test_no_wrapper_is_unobservable_round_trip(self):
        record = dataclasses.replace(self.record, profile="prod-readonly",
            data_source="production", argv=["ro-sql", "fixture", "select 1"])
        with patch.object(provision, "SOURCE", self.top / "absent"):
            env = provision.prepare(record, lane="test")
            self.assertEqual(env.unobservable, "no-approved-access")
            verdict = provision.run_script_check(record, env)
            provision.emit_verdict(record, verdict, fold.EVENTS / "L-verifier-local.jsonl")
        events = self.read_events()
        verdicts = [e for e in events if e.get("type", "").startswith("owed-")]
        self.assertEqual(len(verdicts), 1)
        event = verdicts[0]
        self.assertEqual(event["type"], "owed-unobservable")
        self.assertEqual(event["why"], "no-approved-access")
        self.assertTrue(all(event.get(key) for key in ("subject", "criterion", "evidence", "capability")))
        print("owed-unobservable round trip: why=no-approved-access; zero owed-failed")

    def test_script_exit_states_without_seats(self):
        env = provision.prepare(self.record, lane="test")
        with patch("dispatch.emit", side_effect=AssertionError("seat/dispatch")), \
                patch("packet.main", side_effect=AssertionError("packet")):
            for code, outcome in ((0, "met"), (1, "failed"), (2, "unobservable"), (3, "unobservable")):
                record = dataclasses.replace(self.record,
                    argv=[sys.executable, "-c", f"raise SystemExit({code})"])
                with self.subTest(code=code):
                    self.assertEqual(provision.run_script_check(record, env).outcome, outcome)
            for name in ("missing-provision-binary", "keepa-fetch", "scrapingbee-fetch", "portal-e2e",
                         "claude", "codex"):
                with self.subTest(command=name):
                    record = dataclasses.replace(self.record, argv=[name])
                    self.assertEqual(provision.run_script_check(record, env).outcome, "unobservable")
        record = dataclasses.replace(self.record, argv=[sys.executable, "-c", "import time;time.sleep(5)"])
        with patch.object(provision, "CMD_TIMEOUT_S", 0.02):
            self.assertEqual(provision.run_script_check(record, env).outcome, "unobservable")

    def test_script_lanes_emit_real_verdicts_without_dispatch(self):
        import dispatch
        import sweep_owed
        import verifier
        from datetime import datetime, timezone
        row = dict(spec=self.record.spec, criterion="AC1", project="fixture")
        for lane in ("verifier", "sweep"):
            for code, outcome in ((0, "met"), (1, "failed"), (2, "unobservable")):
                record = dataclasses.replace(self.record,
                    argv=[sys.executable, "-c", f"raise SystemExit({code})"])
                with self.subTest(lane=lane, outcome=outcome), \
                        patch.object(checks, "record_for", return_value=record):
                    if lane == "verifier":
                        with patch.object(verifier, "candidates", return_value=[row]), \
                                patch.object(verifier, "dispatch_batch", side_effect=AssertionError("seat")), \
                                patch.object(verifier.owed_timed, "run_pass", return_value=[]), \
                                patch.object(verifier, "file_rework_briefs", return_value=[]):
                            verifier.run()
                    else:
                        with patch.object(sweep_owed, "_resolve_candidates", return_value=([row], 0, [])), \
                                patch.object(sweep_owed.prod_runner, "run", return_value={
                                    "lines": [], "bundle_rows": {}, "held": set(), "to_judgment": set()}), \
                                patch.object(sweep_owed, "dispatch_batch", side_effect=AssertionError("seat")), \
                                patch.object(verifier, "run", return_value=[]) as verify:
                            sweep_owed.run(now=datetime.now(timezone.utc))
                            self.assertEqual(verify.call_count, 1)
                    events = [e for e in self.read_events() if e.get("type") == "owed-" + outcome]
                    self.assertTrue(events)
                    self.assertTrue(all(events[-1].get(k) for k in ("subject", "criterion", "evidence")))
        print("script lanes: real-ledger round trips; zero seats; one verifier pass")

    def test_model_and_unknown_rows_remain_visible_and_never_run_as_scripts(self):
        row = dict(spec=self.record.spec, criterion=self.record.criterion)
        for record in (None, dataclasses.replace(self.record, answered_by="model")):
            with patch.object(checks, "record_for", return_value=record), \
                    patch.object(provision, "prepare", side_effect=AssertionError("script setup")):
                pending, lines = provision.script_rows([row], [], self.root / "events/L-verifier-local.jsonl",
                                                       lane="verifier")
                self.assertEqual(pending, [row])
                self.assertEqual(any("data-source-unknown" in line for line in lines), record is None)

    def test_shell_wrapped_paid_command_is_refused_before_execution(self):
        env = provision.prepare(self.record, lane="test")
        record = dataclasses.replace(self.record, argv=["/bin/sh", "-c", "keepa-fetch fixture"])
        with patch.object(subprocess, "run", side_effect=AssertionError("paid command executed")):
            self.assertEqual(provision.run_script_check(record, env).outcome, "unobservable")

    def test_approved_wrapper_environment_has_no_credentials(self):
        directory = self.top / "scripts/sweep"
        directory.mkdir(parents=True)
        wrapper = directory / "ro-sql"
        wrapper.touch(mode=0o700)
        record = dataclasses.replace(self.record, profile="prod-readonly",
            data_source="production", argv=["ro-sql", "fixture", "select 1"])
        with patch.dict(os.environ, SUPABASE_DB_URL_OTHER="forbidden",
                        CUSTOM_TOKEN="forbidden", DATABASE_URL="forbidden"), \
                patch.object(provision, "SOURCE", self.top):
            env = provision.prepare(record, lane="test")
        self.assertIsNone(env.unobservable)
        self.assertNotIn("forbidden", env.env.values())


if __name__ == "__main__":
    unittest.main(verbosity=2)
