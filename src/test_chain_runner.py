#!/usr/bin/env python3
"""chain_runner regressions (L-spec-2240). Real git clones, real bwrap shells, real
scratch Postgres and a real temporary ledger; mocks only select failure inputs.

    test_chain_runner.py [TestClass ...]        the merge chain
    test_chain_runner.py --replay-burndown --compare-only [--report P] [--budget-s N]
    test_chain_runner.py --review-live-burndown [--report P]

The two flags are explicit, host-dependent modes and never run by discovery.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import build_runner
import chain_runner
import checks
import dispatch
import fold
import owed
import provision
import sweep_owed
import sweep_provision

SRC = pathlib.Path(__file__).resolve().parent
NOW = datetime.now(timezone.utc).replace(microsecond=0)
EPOCH = (NOW - timedelta(days=3650)).isoformat(timespec="seconds")
WAKE = (NOW - timedelta(hours=1)).isoformat(timespec="seconds")
CD = 'cd "$(git rev-parse --show-toplevel)"'
SCRIPT = "L-owed-sweeper-script.jsonl"


class Fixture(unittest.TestCase):
    """A temporary DOIT_ROOT with a `demo` project repository and ledger."""

    def setUp(self):
        self.top = pathlib.Path(tempfile.mkdtemp(prefix="chain-fix-"))
        self.addCleanup(shutil.rmtree, self.top, True)
        self.root = self.top / "root"
        for name in ("events", "content", "repos", "state"):
            (self.root / name).mkdir(parents=True)
        (self.top / "home").mkdir()
        self.repo = self.root / "repos" / "demo"
        self.repo.mkdir()
        environment = dict(DOIT_ROOT=str(self.root), DOIT_SCRATCH=str(self.top / "scratch"),
                           DOIT_LEDGER_FILE="L-operator-local.jsonl", HOME=str(self.top / "home"))
        self.enterContext(patch.dict(os.environ, environment))
        self.enterContext(patch.object(dispatch, "ROOT", self.root))
        self.enterContext(patch.object(dispatch, "CONTENT", self.root / "content"))
        self.enterContext(patch.object(dispatch, "EVENTS", self.root / "events"))
        self.enterContext(patch.object(fold, "ROOT", self.root))
        self.enterContext(patch.object(fold, "EVENTS", self.root / "events"))
        self.enterContext(patch.object(fold, "PROJECT", None))
        self.git("init", "-q", "-b", "main")
        self.root_sha = self.commit({"README": "root\n"}, "root")
        self.path = self.root / "events" / SCRIPT

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), "-c", "user.name=f", "-c", "user.email=f@x",
                                        *args], text=True, stderr=subprocess.DEVNULL).strip()

    def commit(self, files, message="c"):
        for name, body in files.items():
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def two_shas(self):
        self.commit({"value.txt": "v0\n"}, "base")
        return self.commit({"value.txt": "v1\n"}, "one"), self.commit({"value.txt": "v2\n"}, "two")

    def raw(self, name, spec, *events, ts=EPOCH):
        with (self.root / "events" / name).open("a") as handle:
            for event in events:
                handle.write(json.dumps({"v": 1, "subject": spec, "ts": ts, **event}) + "\n")

    def add_spec(self, spec, verification, sha, *, criteria=(), reviews=None, project="demo"):
        blocks = "".join(f"{c} [backend]: fixture {c}\n" + (f"review_path: {reviews[c]}\n" if reviews and c in reviews else "")
                         for c in criteria)
        text = f"# {spec}\n\n## Acceptance criteria\n{blocks}\n"
        if verification is not None:
            text += f"## Verification\n\n```bash\n{verification}\n```\n"
        (self.root / "content" / (spec + ".md")).write_text(text)
        self.raw("L-spec-writer-fixture.jsonl", spec, {"type": "spec-written", "project": project},
                 *({"type": "owed-ac", "criterion": c, "line": "fixture", "wake_at": WAKE} for c in criteria))
        if sha:
            self.raw("L-executor-fixture.jsonl", spec, {"type": "shipped", "sha": sha})

    def events(self):
        return fold.read_events(use_cache=False)

    def rows(self, *specs):
        by_spec = fold.fold(self.events())[0]
        found = [r for s, v in by_spec.items() for r in owed.checks(v["evs"], NOW) if not specs or s in specs]
        return sorted(found, key=lambda r: (r["spec"], r["criterion"]))

    def chain(self, rows=None, *, limit=8, dry_run=False, events=None):
        rows = self.rows() if rows is None else rows
        return chain_runner.run(rows, self.events() if events is None else events, self.path,
                                limit=limit, dry_run=dry_run, refresh=self.events)

    def verdicts(self):
        return [e for e in self.events() if e.get("runner") == "chain"]

    def outcome(self, spec, criterion):
        found = [e for e in self.verdicts() if e["subject"] == spec and e["criterion"] == criterion]
        self.assertEqual(len(found), 1, found)
        return found[0]

    def verify_chain(self, body):
        return f"{CD} &&\n{body}"

    def scratch_left(self):
        base = self.top / "scratch"
        return sorted(p.name for p in base.rglob("L-chain-*")) if base.exists() else []


# --------------------------------------------------------------------- AC1

class SelectionTests(Fixture):
    def test_verify_runs_whole_chain_at_each_ship_sha(self):
        one, two = self.two_shas()
        chain = self.verify_chain("grep -qx v1 value.txt &&\ntest -f value.txt")
        self.add_spec("L-spec-9001", chain, one, criteria=("verify",))
        self.add_spec("L-spec-9002", chain, two, criteria=("verify",))
        pending, _ = self.chain()
        self.assertEqual(list(pending), [])
        self.assertEqual(self.outcome("L-spec-9001", "verify")["type"], "owed-met")
        failed = self.outcome("L-spec-9002", "verify")
        self.assertEqual((failed["type"], failed["kind"], failed["exit"]), ("owed-failed", "unmet", 1))
        self.assertEqual(self.outcome("L-spec-9001", "verify")["sha"], one)

    def test_head_does_not_substitute_for_ship(self):
        one, _ = self.two_shas()
        self.add_spec("L-spec-9003", self.verify_chain("grep -qx v1 value.txt"), one, criteria=("verify",))
        self.chain()
        self.assertEqual(self.outcome("L-spec-9003", "verify")["type"], "owed-met")

    def test_tagged_ac_uses_only_its_own_commands_in_order(self):
        one, _ = self.two_shas()
        text = self.verify_chain("# AC1\ngrep -qx v1 value.txt &&\n# AC1\ntest -f value.txt &&\n# AC2\nexit 9")
        self.add_spec("L-spec-9004", text, one, criteria=("AC1", "AC2"))
        plan, reason = chain_runner.select(self.rows("L-spec-9004")[0], self.events())
        self.assertIsNone(reason)
        self.assertEqual(plan["provenance"], "tagged:AC1")
        self.assertNotIn("exit 9", plan["commands"])
        self.assertLess(plan["commands"].index("grep"), plan["commands"].index("test -f"))
        self.assertTrue(plan["commands"].startswith(CD))
        self.chain(self.rows("L-spec-9004")[:1])
        self.assertEqual(self.outcome("L-spec-9004", "AC1")["type"], "owed-met")

    def test_review_path_guess_never_substitutes(self):
        one, _ = self.two_shas()
        text = self.verify_chain("# AC1\ntest -f value.txt")
        self.add_spec("L-spec-9005", text, one, criteria=("AC1",), reviews={"AC1": "do run `/usr/bin/false now`"})
        with patch.object(checks, "_review_command", side_effect=AssertionError("review fallback used")):
            self.chain()
        self.assertEqual(self.outcome("L-spec-9005", "AC1")["type"], "owed-met")

    def test_shared_executor_helper_is_the_command_source(self):
        one, _ = self.two_shas()
        self.add_spec("L-spec-9006", self.verify_chain("# AC1\ntrue"), one, criteria=("AC1",))
        with patch.object(build_runner, "_commands", wraps=build_runner._commands) as helper:
            plan, _ = chain_runner.select(self.rows()[0], self.events())
        helper.assert_called_once()
        self.assertEqual(helper.call_args.args[2], None)
        self.assertEqual(plan["command_digest"], chain_runner._digest(plan["commands"]))

    def test_unselectable_rows_stay_pending_without_side_effects(self):
        one, _ = self.two_shas()
        self.add_spec("L-spec-9007", None, one, criteria=("verify",))
        self.add_spec("L-spec-9008", "python3 -c 'pass'", one, criteria=("verify",))
        self.add_spec("L-spec-9009", self.verify_chain("# AC1\ntrue"), one, criteria=("AC1", "AC2"))
        rows = self.rows()
        events = self.events()
        reads, made = [], []
        with patch.object(fold, "read_events", side_effect=lambda *a, **k: reads.append(1)), \
                patch.object(chain_runner, "provision", side_effect=lambda *a: made.append(a)):
            pending, lines = chain_runner.run([r for r in rows if r["spec"] != "L-spec-9009" or r["criterion"] == "AC2"],
                                              events, self.path, limit=8)
        self.assertEqual((reads, made), ([], []))
        self.assertEqual(len(pending), 3)
        self.assertTrue(all(any(r is p for p in pending) for r in rows if r["criterion"] != "AC1"))
        text = "\n".join(lines)
        self.assertIn("no Verification block", text)
        self.assertIn("malformed Verification chain", text)
        self.assertIn("AC2 has no tagged command", text)
        self.assertFalse(self.path.exists())

    def test_missing_shipped_sha_is_a_named_unobservable(self):
        self.two_shas()
        self.add_spec("L-spec-9010", self.verify_chain("true"), "0123456789abcdef0123456789abcdef01234567",
                      criteria=("verify",))
        self.chain()
        event = self.outcome("L-spec-9010", "verify")
        self.assertEqual((event["type"], event["why"], event["exit"]), ("owed-unobservable", "sha-missing", None))


# --------------------------------------------------------------------- AC2

class EnvironmentTests(Fixture):
    def test_one_clone_per_project_sha_and_pristine_start_for_each_row(self):
        one, two = self.two_shas()
        before = (self.git("for-each-ref"), self.git("status", "--porcelain"))
        dirty = self.verify_chain("grep -qx v1 value.txt &&\necho junk > junk.txt &&\necho x >> value.txt")
        clean = self.verify_chain("test ! -e junk.txt &&\ngrep -qx v1 value.txt &&\ntest \"$(wc -l < value.txt)\" = 1")
        other = self.verify_chain("grep -qx v2 value.txt")
        self.add_spec("L-spec-9101", dirty, one, criteria=("verify",))
        self.add_spec("L-spec-9102", clean, one, criteria=("verify",))
        self.add_spec("L-spec-9103", other, two, criteria=("verify",))
        with patch.object(sweep_owed, "make_repo_cwd", wraps=sweep_owed.make_repo_cwd) as clone:
            self.chain()
        self.assertEqual([c.args[1] for c in clone.call_args_list], [one, two])
        for spec in ("L-spec-9101", "L-spec-9102", "L-spec-9103"):
            self.assertEqual(self.outcome(spec, "verify")["type"], "owed-met", spec)
        self.assertEqual(before, (self.git("for-each-ref"), self.git("status", "--porcelain")))
        self.assertEqual(self.scratch_left(), [])

    def test_node_modules_lock_must_match_and_is_linked_read_only(self):
        lock = '{"lockfileVersion": 3}\n'
        sha = self.commit({"dashboard/package-lock.json": lock, "dashboard/package.json": "{}", "value.txt": "v1\n"})
        import hashlib
        key = hashlib.sha256(lock.encode()).hexdigest()
        cache = self.root / "cache/node_modules" / key
        (cache / "node_modules").mkdir(parents=True)
        (cache / "node_modules/marker.js").write_text("m")
        (cache / "package-lock.json").write_text(lock)
        env = chain_runner.provision("demo", sha, "L-chain-env-1")
        self.addCleanup(env.cleanup)
        self.assertIsNone(env.unobservable)
        self.assertEqual(env.summary["node_modules"], "cache:" + key)
        self.assertEqual((env.tree / "dashboard/node_modules").resolve(), (cache / "node_modules").resolve())
        self.add_spec("L-spec-9104", self.verify_chain("test -f dashboard/node_modules/marker.js"), sha, criteria=("verify",))
        self.chain()
        self.assertEqual(self.outcome("L-spec-9104", "verify")["type"], "owed-met")
        (cache / "package-lock.json").write_text("different")
        bad = chain_runner.provision("demo", sha, "L-chain-env-2")
        self.assertEqual(bad.unobservable, "node_modules")
        self.assertIsNone(bad.tree)

    def test_python_resolves_a_prepared_venv(self):
        sha = self.commit({"value.txt": "v1\n"})
        venv = self.top / "tools" / ".venv"
        subprocess.check_call(["/usr/bin/python3", "-m", "venv", "--without-pip", str(venv)])
        py = f"{venv}/bin/python"
        self.add_spec("L-spec-9105", self.verify_chain(f"{py} -c \"import sys; assert sys.prefix.endswith('.venv')\""),
                      sha, criteria=("verify",))
        self.add_spec("L-spec-9106", self.verify_chain(f"{self.top}/absent/.venv/bin/python -c pass"), sha,
                      criteria=("verify",))
        self.chain()
        self.assertEqual(self.outcome("L-spec-9105", "verify")["type"], "owed-met")
        self.assertEqual(self.outcome("L-spec-9106", "verify")["why"], "venv-unavailable")

    def test_scratch_postgres_only_when_required_and_always_cleaned_up(self):
        sha = self.commit({"value.txt": "v1\n"})
        psql = 'psql "$AS_LOCAL_PG_ADMIN" -v ON_ERROR_STOP=1 -qAt'
        pg = self.verify_chain(f"{psql} -c 'create table t(x int)' -c 'insert into t values (7)' &&\n"
                               f"{psql} -c 'select x from t' | grep -qx 7")
        self.add_spec("L-spec-9107", pg, sha, criteria=("verify",))
        self.add_spec("L-spec-9108", self.verify_chain("/usr/bin/python3 -c pass"), sha,
                      criteria=("verify",))
        before = set(pathlib.Path("/var/tmp").glob("owed-pg-*"))
        with patch.object(provision, "repo_scratch_pg", wraps=provision.repo_scratch_pg) as scratch_pg:
            self.chain(self.rows("L-spec-9108"))
            self.assertEqual(scratch_pg.call_count, 0)
        self.chain(self.rows("L-spec-9107"))
        self.assertEqual(self.outcome("L-spec-9107", "verify")["type"], "owed-met")
        self.assertEqual(self.outcome("L-spec-9108", "verify")["type"], "owed-met")
        self.assertEqual(set(pathlib.Path("/var/tmp").glob("owed-pg-*")), before)

    def test_cleanup_on_failure_timeout_and_interruption(self):
        sha = self.commit({"value.txt": "v1\n"})
        self.add_spec("L-spec-9109", self.verify_chain("exit 3"), sha, criteria=("verify",))
        self.add_spec("L-spec-9110", self.verify_chain("sleep 311.25"), sha, criteria=("verify",))
        with patch.object(chain_runner, "ROW_CAP", 3):
            self.chain()
        self.assertEqual(self.outcome("L-spec-9109", "verify")["type"], "owed-failed")
        self.assertEqual(self.outcome("L-spec-9110", "verify")["why"], "timeout")
        self.assertEqual(self.scratch_left(), [])
        self.assertNotEqual(subprocess.run(["pgrep", "-f", "sleep 311.25"], capture_output=True).returncode, 0)
        self.add_spec("L-spec-9111", self.verify_chain("sleep 312.5"), sha, criteria=("verify",))
        threading.Timer(2.0, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        with self.assertRaisesRegex(build_runner.Undetermined, "interrupted"):
            self.chain(self.rows("L-spec-9111"))
        self.assertEqual(self.scratch_left(), [])
        time.sleep(0.3)
        self.assertNotEqual(subprocess.run(["pgrep", "-f", "sleep 312.5"], capture_output=True).returncode, 0)

    def test_unusable_environment_is_named_not_empty(self):
        env = chain_runner.provision("demo", "0123456789abcdef0123456789abcdef01234567", "L-chain-env-3")
        self.assertEqual((env.unobservable, env.tree), ("sha-missing", None))
        env.cleanup()
        self.assertEqual(chain_runner.provision("no such", "abc1234", "x").unobservable, "invalid-identifier")


# --------------------------------------------------------------------- AC3

class VerdictTests(Fixture):
    def read_back(self):
        """A fresh reader over the closed writer's file."""
        events = [e for e in fold.read_events(use_cache=False) if e.get("runner") == "chain"]
        for event in events:
            self.assertIsNone(fold.admits(event, event["actor"]), event)
            self.assertEqual(event["actor"], "owed-sweeper")
            self.assertEqual((event["runner"], event["capability"]), ("chain", "repo"))
            self.assertTrue(pathlib.Path(event["log"]).is_file())
            self.assertEqual(pathlib.Path(event["log"]).stat().st_mode & 0o777, 0o600)
        return {(e["subject"], e["criterion"]): e for e in events}

    def test_exit_codes_map_to_met_failed_and_never_unobservable(self):
        sha = self.commit({"value.txt": "v1\n"})
        for number, body in ((1, "true"), (2, "exit 2"), (3, "exit 1")):
            self.add_spec(f"L-spec-920{number}", self.verify_chain(body), sha, criteria=("verify",))
        self.chain()
        found = self.read_back()
        self.assertEqual(found[("L-spec-9201", "verify")]["type"], "owed-met")
        for spec in ("L-spec-9202", "L-spec-9203"):
            event = found[(spec, "verify")]
            self.assertEqual((event["type"], event["kind"]), ("owed-failed", "unmet"))
        self.assertEqual(found[("L-spec-9202", "verify")]["exit"], 2)
        statuses = {r["spec"]: r["status"] for r in self.rows()}
        self.assertEqual(statuses, {"L-spec-9201": "met", "L-spec-9202": "failed", "L-spec-9203": "failed"})
        self.assertEqual({e["type"] for e in self.events() if e.get("runner") == "chain"}, {"owed-met", "owed-failed"})

    def test_environment_failures_are_named_unobservable_with_evidence(self):
        sha = self.commit({"value.txt": "v1\n", "dashboard/package-lock.json": "{}", "dashboard/package.json": "{}"})
        import hashlib
        cache = self.root / "cache/node_modules" / hashlib.sha256(b"{}").hexdigest()
        (cache / "node_modules").mkdir(parents=True)
        (cache / "package-lock.json").write_text("mismatch")
        self.add_spec("L-spec-9210", self.verify_chain("true"), sha, criteria=("verify",))
        self.add_spec("L-spec-9211", self.verify_chain("true"), "0123456789abcdef0123456789abcdef01234567", criteria=("verify",))
        self.chain(self.rows("L-spec-9210", "L-spec-9211"))
        cache_lock = cache / "package-lock.json"
        cache_lock.write_text("{}")
        failing = self.top / "pg-fail.sh"
        failing.write_text("exit 1\n")
        self.add_spec("L-spec-9212", self.verify_chain("test -n \"$AS_LOCAL_PG_ADMIN\""), sha, criteria=("verify",))
        self.add_spec("L-spec-9213", self.verify_chain(f"{self.top}/none/.venv/bin/python -c pass"), sha, criteria=("verify",))
        self.add_spec("L-spec-9214", self.verify_chain("sleep 313.5"), sha, criteria=("verify",))
        with patch.object(sweep_provision, "PRIVATE_PG", failing), patch.object(chain_runner, "ROW_CAP", 2):
            self.chain(self.rows("L-spec-9212", "L-spec-9213", "L-spec-9214"))
        found = self.read_back()
        causes = {s: found[(s, "verify")]["why"] for s in
                  ("L-spec-9210", "L-spec-9211", "L-spec-9212", "L-spec-9213", "L-spec-9214")}
        self.assertEqual(causes, {"L-spec-9210": "node_modules", "L-spec-9211": "sha-missing",
                                  "L-spec-9212": "scratch-postgres", "L-spec-9213": "venv-unavailable",
                                  "L-spec-9214": "timeout"})
        for event in found.values():
            self.assertEqual(event["type"], "owed-unobservable")
            self.assertIsNone(event["exit"])
            self.assertNotEqual(event["why"].strip(), "")
            self.assertNotIn("environment unavailable", event["why"])
            self.assertIn(event["why"], event["evidence"])

    def test_root_commit_names_base_unavailable_not_a_main_fallback(self):
        self.add_spec("L-spec-9230", self.verify_chain("true"), self.root_sha, criteria=("verify",))
        self.chain()
        self.assertEqual(self.outcome("L-spec-9230", "verify")["why"], "base-unavailable")

    def test_append_refusal_leaves_row_pending_and_is_reported(self):
        sha = self.commit({"value.txt": "v1\n"})
        self.add_spec("L-spec-9220", self.verify_chain("true"), sha, criteria=("verify",))
        with patch.object(dispatch, "emit", return_value="refused: fixture"):
            pending, lines = self.chain()
        self.assertEqual(len(pending), 1)
        self.assertTrue(any("append refused" in line for line in lines))
        self.assertEqual(self.verdicts(), [])


# --------------------------------------------------------------------- AC4

class SweepIntegrationTests(Fixture):
    def specs(self, count=3):
        sha = self.commit({"value.txt": "v1\n"})
        for index in range(count):
            self.add_spec(f"L-spec-930{index}", self.verify_chain("true"), sha, criteria=("verify",))
        return sha

    def test_limit_zero_starts_nothing_and_holds_every_eligible_row(self):
        self.specs()
        with patch.object(chain_runner, "provision", side_effect=AssertionError("provisioned")):
            pending, lines = self.chain(limit=0)
        self.assertEqual((len(pending), len(pending.held)), (3, 3))
        self.assertFalse(self.path.exists())

    def test_budget_defers_rest_and_resolved_rows_disappear(self):
        self.specs()
        pending, lines = self.chain(limit=1)
        self.assertEqual([r["spec"] for r in pending], ["L-spec-9301", "L-spec-9302"])
        self.assertEqual([r["spec"] for r in pending.held], ["L-spec-9301", "L-spec-9302"])
        self.assertEqual(len(self.verdicts()), 1)
        self.assertTrue(any("deferred" in line for line in lines))
        again, _ = self.chain([r for r in self.rows() if r["status"] == "due"], limit=8)
        self.assertEqual(len(self.verdicts()), 3)
        self.assertEqual(list(again), [])

    def test_duplicate_rows_do_not_spend_the_limit_twice(self):
        self.specs()
        rows = self.rows()
        pending, _ = self.chain([rows[0], rows[0], rows[1]], limit=1)
        self.assertEqual(len(self.verdicts()), 1)
        self.assertEqual([r["spec"] for r in pending], ["L-spec-9301"])

    def test_dry_run_has_no_side_effects(self):
        self.specs()
        before = (self.top / "scratch").exists()
        with patch.object(chain_runner, "provision", side_effect=AssertionError("provisioned")), \
                patch.object(dispatch, "emit", side_effect=AssertionError("appended")):
            pending, lines = self.chain(dry_run=True)
        self.assertEqual(len(pending), 3)
        self.assertEqual(len([l for l in lines if "DRY RUN" in l]), 3)
        self.assertEqual((self.top / "scratch").exists(), before)
        self.assertFalse((self.root / "state/chain-logs").exists())

    def test_declared_production_rows_keep_their_route(self):
        sha = self.specs(1)
        spec = "L-spec-9300"
        record = checks.CheckRecord(spec + ":verify", spec, "verify", ["ro-sql", "select 1"], sha,
                                    "production", False, "prod-readonly", "post-deploy", "script")
        import dataclasses
        self.raw("L-spec-writer-fixture.jsonl", spec, {"type": "check-declared", **dataclasses.asdict(record)})
        pending, _ = self.chain()
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.verdicts(), [])

    def test_sweep_run_passes_batch_budget_as_limit_before_script_rows(self):
        self.specs(3)
        order, seen = [], {}
        real_chain, real_script = chain_runner.run, provision.script_rows
        def chain_spy(rows, events, path, *, limit, dry_run=False, refresh=None):
            order.append("chain")
            seen["limit"] = limit
            return real_chain(rows, events, path, limit=limit, dry_run=dry_run, refresh=refresh)
        def script_spy(rows, events, path, *, lane, dry_run=False):
            order.append("script")
            seen["script_keys"] = {r["spec"] for r in rows}
            return real_script(rows, events, path, lane=lane, dry_run=dry_run)
        readings = {"sentinel": 1}
        with patch.object(sweep_owed.verifier, "run", return_value=[]), \
                patch.object(sweep_owed, "dispatch_batch", side_effect=AssertionError("paid model batch")), \
                patch.object(sweep_owed, "open_batch", return_value=False), \
                patch.object(sweep_owed.loadgate, "readings", return_value=readings), \
                patch.object(sweep_owed.loadgate, "admit", return_value=(True, "ok", {})), \
                patch.object(sweep_owed, "batch_budget", return_value=2) as budget, \
                patch.object(chain_runner, "run", side_effect=chain_spy), \
                patch.object(provision, "script_rows", side_effect=script_spy):
            sweep_owed.run(now=NOW, runner=sweep_owed.Runner())
        budget.assert_called_once_with(readings, NOW)
        self.assertEqual((order, seen["limit"]), (["chain", "script"], 2))
        self.assertEqual(len(self.verdicts()), 2)
        self.assertEqual(seen["script_keys"], set())

    def test_row_closed_between_selection_and_append_is_not_appended(self):
        sha = self.specs(1)
        real = chain_runner._execute
        def close_first(plan, env):
            result = real(plan, env)
            self.raw("L-operator-fixture.jsonl", plan["spec"], {"type": "owed-met", "criterion": "verify",
                     "evidence": "operator"}, ts=NOW.isoformat(timespec="seconds"))
            return result
        with patch.object(chain_runner, "_execute", side_effect=close_first):
            pending, lines = self.chain()
        self.assertEqual(self.verdicts(), [])
        self.assertTrue(any("before append" in line for line in lines))

    def test_supplied_snapshot_without_refresh_does_not_read_the_ledger(self):
        self.specs(1)
        rows, events = self.rows(), self.events()
        # No hidden full reader is permitted inside the chain runner. The sweep
        # owns any freshness read and supplies it through its shared snapshot.
        with patch.object(fold, "read_events", side_effect=AssertionError("hidden reader")):
            pending, _ = chain_runner.run(rows, events, self.path, limit=1)
        self.assertEqual(pending, [])
        self.assertEqual(len(self.verdicts()), 1)


# --------------------------------------------------------------------- AC5

class IsolationTests(Fixture):
    SENTINELS = {"ANTHROPIC_API_KEY": "sk-sentinel-anthropic-91", "OPENAI_API_KEY": "sk-sentinel-openai-92",
                 "SUPABASE_DB_URL": "postgresql://u:sentinel93@db.example/prod", "AWS_SECRET_ACCESS_KEY": "sentinel94secret",
                 "SSH_AUTH_SOCK": "/tmp/sentinel95-agent", "CUSTOM_TOKEN": "sentinel96token", "GH_PASSWORD": "sentinel97pw"}

    def test_hostile_identifiers_cannot_inject_or_traverse(self):
        sha = self.commit({"value.txt": "v1\n"})
        marker = self.top / "pwned"
        for number, spec in enumerate(("L-spec-9401; touch " + str(marker), "../../etc/passwd", "L-spec-9402\nrm")):
            row = {"spec": spec, "criterion": "verify", "project": "demo", "status": "due"}
            with patch.object(chain_runner, "provision", side_effect=AssertionError("provisioned")):
                pending, _ = chain_runner.run([row], [], self.path, limit=5)
            self.assertEqual(len(pending), 1)
        row = {"spec": "L-spec-9403", "criterion": "verify", "project": "../demo", "status": "due"}
        self.assertIsNotNone(chain_runner.select(row, [])[1])
        for bad in (("../demo", sha, "x"), ("demo", sha + ";id", "x"), ("demo", sha, "../../escape"), ("demo", sha, "a b")):
            env = chain_runner.provision(*bad)
            self.assertEqual(env.unobservable, "invalid-identifier", bad)
        self.assertFalse(marker.exists())
        self.assertFalse((self.top / "scratch").exists() and any((self.top / "scratch").rglob("escape")))

    def test_credentials_never_reach_the_command_log_or_event(self):
        sha = self.commit({"value.txt": "v1\n"})
        dump = "env &&\n! env | grep -i sentinel &&\ntest -z \"${ANTHROPIC_API_KEY:-}${SSH_AUTH_SOCK:-}${CUSTOM_TOKEN:-}\""
        self.add_spec("L-spec-9410", self.verify_chain(dump), sha, criteria=("verify",))
        with patch.dict(os.environ, self.SENTINELS):
            self.chain()
        event = self.outcome("L-spec-9410", "verify")
        self.assertEqual(event["type"], "owed-met")
        text = json.dumps(event) + pathlib.Path(event["log"]).read_text()
        for value in self.SENTINELS.values():
            self.assertNotIn(value, text)
        with patch.dict(os.environ, {"MY_API_TOKEN": "leakvalue-1234"}):
            self.assertNotIn("leakvalue-1234", chain_runner.redact("x leakvalue-1234 y postgresql://a:b@h/d"))
            self.assertNotIn("postgresql", chain_runner.redact("postgresql://a:b@h/d"))

    def test_executed_code_cannot_write_source_checkout_or_live_ledger(self):
        sha = self.commit({"value.txt": "v1\n"})
        ledger = self.root / "events" / "L-owed-sweeper-hack.jsonl"
        attacks = {"L-spec-9420": f"echo x > {self.repo}/pwned", "L-spec-9421": f"echo x >> {ledger}",
                   "L-spec-9422": f"echo x > {self.root}/events/new.jsonl", "L-spec-9423": "echo x > /usr/pwned",
                   "L-spec-9424": f"echo x > {self.top}/pwned-outside"}
        for spec, body in attacks.items():
            self.add_spec(spec, self.verify_chain(body), sha, criteria=("verify",))
        before = self.git("status", "--porcelain")
        self.chain()
        self.assertEqual(self.git("status", "--porcelain"), before)
        for path in (self.repo / "pwned", ledger, self.root / "events/new.jsonl", pathlib.Path("/usr/pwned"),
                     self.top / "pwned-outside"):
            self.assertFalse(path.exists(), path)

    def test_no_network_and_named_inability(self):
        sha = self.commit({"value.txt": "v1\n"})
        net = "/usr/bin/python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), 2)\""
        self.add_spec("L-spec-9430", self.verify_chain(net), sha, criteria=("verify",))
        self.chain()
        event = self.outcome("L-spec-9430", "verify")
        self.assertEqual((event["type"], event["why"]), ("owed-unobservable", "network-unavailable"))

    def test_timeout_kills_descendants_and_logs_are_private_and_outside_the_clone(self):
        sha = self.commit({"value.txt": "v1\n"})
        self.add_spec("L-spec-9440", self.verify_chain("(sleep 314.75 &) &&\nsleep 314.75"), sha, criteria=("verify",))
        with patch.object(chain_runner, "ROW_CAP", 2):
            self.chain()
        time.sleep(0.3)
        self.assertNotEqual(subprocess.run(["pgrep", "-f", "sleep 314.75"], capture_output=True).returncode, 0)
        event = self.outcome("L-spec-9440", "verify")
        log = pathlib.Path(event["log"])
        self.assertEqual(log.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(str(self.top / "scratch"), str(log))


# --------------------------------------------------------------------- AC6

class ReplayFixtureTests(Fixture):
    """The D-shaped bounded fixture: ten cases through the production path."""

    CASES = [  # spec, criterion, proposal
        ("L-spec-9501", "verify", "MET"), ("L-spec-9502", "verify", "FAILED"), ("L-spec-9503", "verify", "CANNOT"),
        ("L-spec-9504", "AC1", "MET"), ("L-spec-9505", "AC1", "FAILED"), ("L-spec-9506", "AC1", "CANNOT"),
        ("L-spec-9507", "AC1", "CANNOT"), ("L-spec-9508", "verify", "MET"), ("L-spec-9509", "AC1", "MET"),
        ("L-spec-9510", "AC1", "MET")]

    def build(self):
        one, _ = self.two_shas()
        ok, bad = "grep -qx v1 value.txt", "grep -qx v9 value.txt"
        tagged = lambda body: self.verify_chain(f"# AC1\n{body}")
        self.add_spec("L-spec-9501", self.verify_chain(ok), one, criteria=("verify",))
        self.add_spec("L-spec-9502", self.verify_chain(ok), one, criteria=("verify",))  # proposal FAILED, executes met
        self.add_spec("L-spec-9503", self.verify_chain(ok), "0123456789abcdef0123456789abcdef01234567", criteria=("verify",))
        self.add_spec("L-spec-9504", tagged(ok), one, criteria=("AC1",))
        self.add_spec("L-spec-9505", tagged(bad), one, criteria=("AC1",))
        self.add_spec("L-spec-9506", tagged("sleep 315.5"), one, criteria=("AC1",))
        self.add_spec("L-spec-9507", tagged(ok), one, criteria=("AC1", "AC2"))
        self.add_spec("L-spec-9508", self.verify_chain(ok), one, criteria=("verify",))
        self.add_spec("L-spec-9509", tagged(ok), one, criteria=("AC1",))
        self.add_spec("L-spec-9510", tagged(ok), one, criteria=("AC1",))
        self.raw(SCRIPT, "L-spec-9508", {"type": "owed-met", "criterion": "verify", "evidence": "earlier"}, ts=NOW.isoformat())
        self.raw("L-operator-fixture.jsonl", "L-spec-9509", {"type": "owed-waived", "criterion": "AC1", "reason": "fixture"},
                 ts=NOW.isoformat())

    def test_fixture_replay_reconciles_each_case_through_production_paths(self):
        self.build()
        proposals = [c for c in self.CASES if c[0] != "L-spec-9507" or c[1] == "AC1"]
        report = self.top / "report.json"
        real = chain_runner._execute
        def maybe_close(plan, env):
            result = real(plan, env)
            if plan["spec"] == "L-spec-9510":
                self.raw("L-operator-fixture.jsonl", plan["spec"], {"type": "owed-met", "criterion": "AC1",
                         "evidence": "closed first"}, ts=(NOW + timedelta(seconds=5)).isoformat())
            return result
        with patch.object(chain_runner, "ROW_CAP", 3), patch.object(chain_runner, "_execute", side_effect=maybe_close), \
                patch.object(checks, "_review_command", side_effect=AssertionError("no review fallback")):
            rows, remaining = chain_runner.replay(proposals, self.events(), self.path, report, append=True, now=NOW,
                                                  refresh=self.events)
        self.assertEqual(remaining, [])
        got = {k: (v["outcome"], v.get("why")) for k, v in rows.items()}
        self.assertEqual(got["L-spec-9501/verify"], ("met", ""))
        self.assertEqual(got["L-spec-9502/verify"], ("met", ""))
        self.assertEqual(got["L-spec-9503/verify"], ("unobservable", "sha-missing"))
        self.assertEqual(got["L-spec-9504/AC1"], ("met", ""))
        self.assertEqual(got["L-spec-9505/AC1"], ("failed", ""))
        self.assertEqual(got["L-spec-9506/AC1"], ("unobservable", "timeout"))
        self.assertEqual(got["L-spec-9507/AC1"], ("met", ""))
        self.assertIn("proposal FAILED but executed met", rows["L-spec-9502/verify"]["explanation"])
        self.assertIn("proposal CANNOT but executed met", rows["L-spec-9507/AC1"]["explanation"])
        self.assertTrue(rows["L-spec-9508/verify"]["explanation"].endswith("compared only: row already met"))
        self.assertTrue(rows["L-spec-9509/AC1"]["explanation"].endswith("compared only: row already waived"))
        self.assertEqual(rows["L-spec-9510/AC1"]["closed_before_append"], "met")
        # append rules, judged by a separate interpreter session reading the closed ledger
        code = ("import sys, json; sys.path.insert(0, %r); import fold; "
                "print(json.dumps(sorted((e['subject'], e['type']) for e in fold.read_events(use_cache=False) "
                "if e.get('runner') == 'chain')))" % str(SRC))
        out = subprocess.check_output([sys.executable, "-c", code], env={**os.environ, "DOIT_ROOT": str(self.root)}, text=True)
        appended = json.loads(out)
        self.assertEqual(appended, sorted([
            ["L-spec-9501", "owed-met"], ["L-spec-9502", "owed-met"], ["L-spec-9503", "owed-unobservable"],
            ["L-spec-9504", "owed-met"], ["L-spec-9505", "owed-failed"], ["L-spec-9506", "owed-unobservable"],
            ["L-spec-9507", "owed-met"]]))
        # compare-only mode never writes
        report2 = self.top / "report2.json"
        before = self.path.read_text()
        chain_runner.replay([("L-spec-9505", "AC1", "FAILED")], self.events(), self.path, report2, append=False, now=NOW)
        self.assertEqual(self.path.read_text(), before)

    def test_untagged_ac_is_listed_and_resume_reuses_saved_evidence(self):
        self.build()
        report = self.top / "resume.json"
        cohort = [("L-spec-9507", "AC2", "CANNOT"), ("L-spec-9504", "AC1", "MET")]
        rows, remaining = chain_runner.replay(cohort, self.events(), self.path, report, append=False, now=NOW,
                                              budget_s=-1)
        self.assertEqual(len(remaining), 2)
        self.assertFalse(json.loads(report.read_text())["complete"])
        rows, remaining = chain_runner.replay(cohort, self.events(), self.path, report, append=False, now=NOW)
        self.assertEqual(remaining, [])
        self.assertIn("AC2 has no tagged command", rows["L-spec-9507/AC2"]["explanation"])
        with patch.object(chain_runner, "attempt", side_effect=AssertionError("re-executed")):
            chain_runner.replay(cohort, self.events(), self.path, report, append=False, now=NOW)

    def test_audit_is_nonzero_until_complete(self):
        text = (pathlib.Path(__file__).resolve().parent.parent / "docs").exists() and ""
        inventory = [("L-spec-%04d" % i, "verify" if i < 51 else "AC1", "MET") for i in range(155)]
        problems = chain_runner.audit_report(self.top / "none.json", inventory, [])
        self.assertTrue(problems)


# ----------------------------------------------------------------- live modes

LIVE_REPORT = "state/chain-replay-report.json"
BURNDOWN = pathlib.Path.home() / ".do-it/burndown/owed2/verdicts.jsonl"


def live_inventory():
    spec = pathlib.Path(os.environ.get("DOIT_ROOT") or pathlib.Path.home() / ".do-it") / "content/L-spec-2240.md"
    return chain_runner.parse_inventory(spec.read_text())


def main(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--replay-burndown", action="store_true")
    parser.add_argument("--compare-only", action="store_true")
    parser.add_argument("--review-live-burndown", action="store_true")
    parser.add_argument("--report")
    parser.add_argument("--budget-s", type=float, default=24 * 3600)
    args, rest = parser.parse_known_args(argv[1:])
    report = args.report or str(dispatch.ROOT / LIVE_REPORT)
    if args.replay_burndown:
        proposals = [(r["spec"], r["criterion"], r["verdict"]) for r in map(json.loads, BURNDOWN.read_text().splitlines())]
        events = fold.read_events()
        path = fold.EVENTS / SCRIPT
        rows, remaining = chain_runner.replay(proposals, events, path, report, append=not args.compare_only,
                                              budget_s=args.budget_s, refresh=fold.read_events)
        print(f"replay: {len(rows)} compared, {len(remaining)} remaining; report {report}")
        return 0 if not remaining else 1
    if args.review_live_burndown:
        problems = chain_runner.audit_report(report, live_inventory(), fold.read_events())
        for problem in problems[:50]:
            print("audit: " + problem)
        print(f"audit: {len(problems)} problem(s)")
        return 1 if problems else 0
    unittest.main(argv=[argv[0], *rest])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
