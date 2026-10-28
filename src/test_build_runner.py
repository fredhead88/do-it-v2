#!/usr/bin/env python3
"""Hermetic CLI regressions; git and the append/fold store are real."""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import build_runner
import node_modules

HERE = pathlib.Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "build_runner"
DOIT = HERE.parent / "doit"


class BuildRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="build-runner-test-")
        self.addCleanup(self.temp.cleanup)
        self.top = pathlib.Path(self.temp.name)
        self.root = self.top / "root"
        self.repo = self.root / "repos" / "demo"
        self.repo.mkdir(parents=True)
        (self.root / "content").mkdir()
        (self.root / "events").mkdir()
        self.env = dict(os.environ, DOIT_ROOT=str(self.root), HOME=str(self.top / "home"),
                        DOIT_SCRATCH=str(self.top / "scratch"), DOIT_PROJECT="demo",
                        DOIT_LEDGER_FILE="L-executor-0001.jsonl", DOIT_MAIN="main")
        self.env.pop("PYTHONPATH", None)
        self.bin = self.top / "bin"
        self.bin.mkdir()
        for name in ("npm", "git"):
            (self.bin / name).symlink_to(FIX / name)
        for name in ("gh", "ssh", "telegram", "notify"):
            (self.bin / name).symlink_to(FIX / "blocked")
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env.get("PATH", "/usr/bin:/bin")
        self.log = self.top / "externals.log"
        self.env["FIXTURE_LOG"] = str(self.log)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "fixture")
        self.git("config", "user.email", "fixture@localhost")
        (self.repo / "probe.py").write_text("print('fixture')\n")
        self.base = self.commit("base")
        (self.repo / "later").write_text("later")
        self.sha = self.commit("tip")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.sid = "L-spec-9000"
        self.spec("python3 probe.py")
        self.build_done()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args],
                                       text=True, stderr=subprocess.DEVNULL).strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def spec(self, command=None, verification=None, text=None):
        if text is None:
            text = "## Acceptance criteria\nAC1 [backend]: fixture command.\n"
            if command:
                text += f"review_path: run `{command}`\n"
            if verification:
                text += f"\n## Verification\n```bash\n{verification}\n```\n"
        (self.root / "content" / f"{self.sid}.md").write_text(text)

    def build_done(self, sha=None, base=None):
        event = {"v": 1, "ts": "2026-10-18T00:00:00Z", "subject": self.sid,
                 "type": "build-done", "project": "demo", "status": "DONE",
                 "ready_sha": sha or self.sha, "base_sha": base or self.base}
        (self.root / "events" / "L-builder-fixture.jsonl").write_text(json.dumps(event) + "\n")

    def cli(self, *args, driver=False, **env):
        argv = ([sys.executable, str(FIX / "driver.py")] if driver else
                [str(DOIT), "run-checks"])
        result = subprocess.run([*argv, self.sid, *args], env=dict(self.env, **env),
                                capture_output=True, text=True, timeout=20)
        return result

    def bundle(self, *args, **kw):
        result = self.cli(*args, **kw)
        self.assertEqual(result.returncode, 0, result.stderr)
        path = pathlib.Path(result.stdout.strip())
        return json.loads(path.read_text()), path

    def tail(self, row):
        return pathlib.Path(row["tail"]).read_bytes()

    def events(self):
        path = self.root / "events" / "L-build-runner-local.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def provisioned(self):
        venv = self.repo / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "alembic").symlink_to(FIX / "alembic")
        (self.repo / "api").mkdir()
        (self.repo / "api" / ".keep").touch()
        dashboard = self.repo / "dashboard"
        dashboard.mkdir()
        (dashboard / "package-lock.json").write_text('{"lockfileVersion":3}')
        (dashboard / "package.json").write_text('{"name":"fixture"}')
        self.base = self.commit("provisioning")
        (self.repo / "tip-only").touch()
        self.sha = self.commit("provisioned tip")
        self.build_done()
        config = self.top / "grading.toml"
        config.write_text('[project.demo]\nvenv = ' + json.dumps(str(venv)) + '\n'
                          'node_dirs = ["dashboard"]\n'
                          'db_env = ["SUPABASE_DB_URL", "DATABASE_URL", "TEST_DB"]\n'
                          'admin_env = ["AS_LOCAL_PG_ADMIN"]\n'
                          '[project.demo.db_setup]\ncwd = "api"\n'
                          'argv = ["{venv}/bin/alembic", "upgrade", "head"]\n')
        self.env["DOIT_GRADING_TOML"] = str(config)
        return dashboard

    def lines(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_clean(self):
        runner = self.top / "scratch" / "runner"
        self.assertEqual(list(runner.iterdir()) if runner.exists() else [], [])
        lines = self.lines()
        self.assertEqual(sorted(s[6:] for s in lines if s.startswith("start ")),
                         sorted(s[5:] for s in lines if s.startswith("stop ")))

    def folded(self):
        code = ('import fold,hashlib,json,pathlib; '
                'ev=fold.read_events(); result=fold.fold(ev); '
                'print(json.dumps({"kept":result[3].get("L-spec-9000",[]), '
                '"ignored":result[2], "digests":{e["path"]:hashlib.sha256('
                'pathlib.Path(e["path"]).read_bytes()).hexdigest() for e in ev '
                'if e["type"]=="evidence-bundle"}}))')
        result = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_fresh_clone_uncommitted_absent_clone_removed(self):
        (self.repo / "probe.py").write_text("raise RuntimeError('builder-input-marker')\n")
        (self.repo / "uncommitted").write_text("builder-input-marker")
        self.spec("python3 -c 'import os,pathlib,subprocess; print(os.getcwd()); "
                  "print(subprocess.check_output([\"git\",\"rev-parse\",\"HEAD\"],text=True).strip()); "
                  "print(pathlib.Path(\"uncommitted\").exists(),pathlib.Path(\"later\").exists())'")
        bundle, _ = self.bundle("--sha", self.base)
        row = bundle["commands"][0]
        cwd = pathlib.Path(row["cwd"])
        self.assertEqual(cwd.parent, self.top / "scratch" / "runner")
        self.assertRegex(cwd.name, self.sid + "-" + self.base[:12] + r"-\d+$")
        self.assertEqual(self.tail(row).decode().splitlines(), [str(cwd), self.base, "False False"])
        self.assertFalse(cwd.exists())
        self.assertNotIn("builder-input-marker", json.dumps(bundle))

    def test_env_prod_dsns_unset_env_db_and_admin_node_cache_fill_reuse(self):
        dashboard = self.provisioned()
        for name in ("SUPABASE_DB_URL", "SUPABASE_DB_URL_DIRECT", "SUPABASE_DB_URL_RO",
                     "SUPABASE_DB_URL_MIGRATIONS", "AS_LOCAL_PG_ADMIN", "PGJUNK", "CUSTOM_DSN",
                     "DATABASE_JUNK"):
            self.env[name] = "CALLER_SENTINEL_" + name
        self.spec("python3 -c 'import os,json,pathlib; print(json.dumps(dict(os.environ))); "
                  "p=pathlib.Path(\"dashboard/node_modules\"); print(p.is_symlink()); "
                  "(p/\"marker\").write_text(\"clone-write\")'")
        bundle, path = self.bundle(driver=True)
        output = self.tail(bundle["commands"][0]).decode().splitlines()
        private = json.loads(output[0])
        self.assertFalse(any("CALLER_SENTINEL" in value for value in private.values()))
        self.assertTrue(all(private[n] == "private-db-fixture"
                            for n in ("SUPABASE_DB_URL", "DATABASE_URL", "TEST_DB")))
        self.assertTrue(private["AS_LOCAL_PG_ADMIN"] == "private-admin-fixture")
        self.assertEqual(output[1], "False")
        key = hashlib.sha256((dashboard / "package-lock.json").read_bytes()).hexdigest()[:16]
        self.assertEqual(bundle["env"], {"node_modules": "cache:" + key, "pg": "private",
                                         "admin_pg": "loopback", "prod_dsns": "unset"})
        cache = self.top / "scratch" / "node-cache" / key / "node_modules" / "marker"
        self.assertEqual(cache.read_text(), "cache-original")
        self.assertEqual(sum(s.startswith("npm ") for s in self.lines()), 1)
        self.assertEqual(sum(s.startswith("setup ") for s in self.lines()), 2)
        self.assertTrue(all("/api" in s for s in self.lines() if s.startswith("setup ")))
        prior = path.read_bytes(), self.lines(), self.events()
        self.assertEqual(self.bundle(driver=True)[1], path)
        self.assertEqual((path.read_bytes(), self.lines(), self.events()), prior)
        # A distinct criteria set runs both clones but reuses the lockfile cache.
        self.bundle("--criteria", "AC1", driver=True)
        self.assertEqual(sum(s.startswith("npm ") for s in self.lines()), 1)
        (dashboard / "package-lock.json").write_text('{"lockfileVersion":3,"changed":true}')
        self.sha = self.commit("new lockfile")
        self.build_done()
        self.bundle(driver=True)
        self.assertEqual(sum(s.startswith("npm ") for s in self.lines()), 2)
        self.assert_clean()

    def test_env_none_row(self):
        bundle, _ = self.bundle(driver=True)
        self.assertEqual(bundle["env"], {"node_modules": "none", "pg": "none",
                                         "admin_pg": "none", "prod_dsns": "unset"})
        self.assertEqual(self.lines(), [])
        self.assert_clean()

    def live_fixture(self, relative, source):
        self.provisioned()
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        self.sha = self.commit("live_db fixture source")
        self.build_done()
        # Execute the real shell argv/environment without requiring app pytest
        # dependencies or connecting to any database in this regression suite.
        executable = self.bin / "pytest"
        executable.write_text("#!" + sys.executable + "\n"
            "import json, os, sys\n"
            "assert os.environ['SUPABASE_DB_URL'] == 'private-db-fixture'\n"
            "assert os.environ['AS_LOCAL_PG_ADMIN'] == 'private-admin-fixture'\n"
            "assert not os.environ.get('SUPABASE_DB_URL_RO')\n"
            "print(json.dumps({'args': sys.argv[1:], 'pg': 'private', 'admin_pg': 'loopback'}))\n")
        executable.chmod(0o755)
        return path

    def test_1418_genesis_live_db_verification_uses_private_pg(self):
        self.live_fixture("api/tests/test_562_genesis_drift.py",
                          'import os\nadmin = os.environ.get("AS_LOCAL_PG_ADMIN")\n')
        line = ("pytest api/tests/test_562_genesis_drift.py -m live_db "
                "-k rebuild_fingerprint_equals_committed")
        self.spec(verification="# AC1\n" + line)
        for args in ((), ("--criteria", "AC1")):
            bundle, _ = self.bundle(*args, driver=True)
            self.assertEqual(bundle["env"]["pg"], "private")
            self.assertEqual(bundle["env"]["admin_pg"], "loopback")
            self.assertEqual(bundle["env"]["prod_dsns"], "unset")
            self.assertTrue(all(row["exit"] == 0 for row in bundle["commands"]))
            output = json.loads(self.tail(bundle["commands"][0]))
            self.assertEqual(output["args"], line.split()[1:])
        self.assert_clean()

    def test_admin_live_db_suite_allowed_even_for_observed_data(self):
        self.live_fixture("api/tests/test_admin.py",
                          'import os\nadmin = os.environ.get("AS_LOCAL_PG_ADMIN")\n')
        self.spec(text="## Acceptance criteria\nAC1 [observed-data]: scratch schema.\n"
                       "review_path: `pytest api/tests/test_admin.py -m live_db`\n")
        bundle, _ = self.bundle("--criteria", "AC1", driver=True)
        self.assertEqual(bundle["env"]["pg"], "private")
        self.assertEqual(bundle["env"]["admin_pg"], "loopback")
        self.assertEqual(bundle["commands"][0]["exit"], 0)
        self.assert_clean()

    def test_1026_observed_data_refused_before_private_pg_or_cached_evidence(self):
        relative = "api/tests/profit_v2/test_cash_cogs_live_crosscheck_0050.py"
        source = ('def _resolve_dsn(environ):\n'
                  ' for name in ("SUPABASE_DB_URL_RO", "SUPABASE_DB_URL"):\n'
                  '  if environ.get(name): return environ[name]\n')
        path = self.live_fixture(relative, source)
        command = ("cd \"$(git rev-parse --show-toplevel)/api\" && "
                   "pytest tests/profit_v2/test_cash_cogs_live_crosscheck_0050.py "
                   "-m live_db -k mouthwatchers_april -rA")
        # The same test file can contain hermetic backend checks too.
        self.spec(text="## Acceptance criteria\nAC3 [backend]: hermetic resolver test.\n"
                       f"review_path: `{command}`\n")
        _, cached = self.bundle("--criteria", "AC3", driver=True)
        before = self.lines()
        self.spec(text="## Acceptance criteria\nAC3 [observed-data]: mouthwatchers April 2026 ASIN rows.\n"
                       f"review_path: go to `{command}`; failed if it fails or skips.\n")
        # Inspect the requested commit, not a modified live working copy.
        path.write_text('admin = "AS_LOCAL_PG_ADMIN"\n')
        result = self.cli("--criteria", "AC3", driver=True,
                          SUPABASE_DB_URL="WRITE_DSN_SENTINEL", SUPABASE_DB_URL_RO="RO_DSN_SENTINEL")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stderr.strip(), "run-checks: " + build_runner.PROD_DATA_UNAVAILABLE)
        self.assertEqual(self.lines(), before)
        self.assertTrue(cached.exists())
        self.assertEqual(self.events()[-1]["env"], build_runner.NO_PROD_ENV)
        self.assertNotIn("DSN_SENTINEL", json.dumps(self.events()))
        self.assert_clean()

    def test_full_verification_never_hits_production_guard_and_ro_provenance_counts(self):
        text = ("## Acceptance criteria\nAC3 [observed-data]: rows.\n"
                "## Verification\n```bash\n# AC3\n"
                'SUPABASE_DB_URL="$SUPABASE_DB_URL" pytest tests/test_live.py -m live_db\n```\n')
        with patch.object(build_runner, "_process", side_effect=AssertionError("unexpected process")):
            self.assertFalse(build_runner._missing_production_data(
                text, "demo", None, self.repo, self.sha, {}, time.monotonic() + 10))
            self.assertFalse(build_runner._missing_production_data(
                text, "demo", {"AC3"}, self.repo, self.sha,
                {"SUPABASE_DB_URL_RO": "ro-fixture"}, time.monotonic() + 10, db_target="prod_ro"))
            self.assertTrue(build_runner._missing_production_data(
                text, "demo", {"AC3"}, self.repo, self.sha,
                {"SUPABASE_DB_URL": "private-fixture"}, time.monotonic() + 10, db_target="private"))
        commands, _ = build_runner._commands(text, "demo", None)
        self.assertTrue(any(criterion is None and "-m live_db" in command for criterion, command in commands))

    def test_chain_uses_configured_venv_bin_before_inherited_path(self):
        for relative in (False, True):
            with self.subTest(relative=relative):
                configured = ".venv" if relative else str(self.top / "project-venv")
                venv = self.repo / configured
                (venv / "bin").mkdir(parents=True)
                for name in ("alembic", "python"):
                    for directory, marker in ((venv / "bin", "venv"), (self.bin, "inherited")):
                        binary = directory / name
                        binary.write_text(f"#!/bin/sh\nprintf '%s\\n' '{marker}-{name}'\n")
                        binary.chmod(0o755)
                (self.repo / "venv-fixture").write_text(str(relative))
                self.base = self.commit("venv command fixtures")
                (self.repo / "venv-tip").write_text(str(relative))
                self.sha = self.commit("venv command tip")
                self.build_done()
                config = self.top / "grading.toml"
                config.write_text('[project.demo]\nvenv = ' + json.dumps(configured) + '\n')
                self.env["DOIT_GRADING_TOML"] = str(config)
                self.spec(None, 'cd / &&\ncommand -v alembic &&\ncommand -v python &&\n'
                          'alembic &&\npython &&\nprintf "%s\\n" "$PATH" "$TMPDIR"')
                bundle, path = self.bundle()
                for row in bundle["commands"] + bundle["base"]:
                    # Relative venvs follow db_setup's disposable-tree resolution.
                    expected = pathlib.Path(row["cwd"]) / configured / "bin"
                    self.assertEqual(row["exit"], 0)
                    self.assertEqual(self.tail(row).decode().splitlines(), [
                        str(expected / "alembic"), str(expected / "python"),
                        "venv-alembic", "venv-python",
                        str(expected) + os.pathsep + self.env["PATH"],
                        str(pathlib.Path(row["cwd"] + "-env") / "tmp")])
                # The next config must run again even if its git tip is unchanged.
                path.unlink()
                shutil.rmtree(path.with_suffix(""))
                self.assert_clean()

    def test_chain_without_configured_venv_keeps_inherited_path(self):
        for name in ("alembic", "python"):
            binary = self.bin / name
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
        self.spec(None, 'command -v alembic &&\ncommand -v python &&\nprintf "%s\\n" "$PATH"')
        for config_text in ("", '[project.demo]\nnode_dirs = []\n',
                            '[project.demo]\nvenv = ""\n'):
            with self.subTest(config=config_text):
                config = self.top / "grading.toml"
                config.write_text(config_text)
                self.env["DOIT_GRADING_TOML"] = str(config)
                bundle, path = self.bundle()
                for row in bundle["commands"] + bundle["base"]:
                    self.assertEqual(row["exit"], 0)
                    self.assertEqual(self.tail(row).decode().splitlines(), [
                        str(self.bin / "alembic"), str(self.bin / "python"), self.env["PATH"]])
                path.unlink()
                shutil.rmtree(path.with_suffix(""))
                self.assert_clean()

    def test_rewrite_live_path_missing_left_venv_kept(self):
        actual = self.top / "actual"
        self.repo.rename(actual)
        self.repo.symlink_to(actual, target_is_directory=True)
        venv = self.repo / ".venv"
        (actual / ".venv").mkdir()
        # A committed venv path exists in both clones, so the runner's absolute
        # guard is what prevents rewriting it.
        (actual / ".venv" / "pin").write_text("pin")
        self.sha = self.commit("venv marker")
        self.build_done()
        config = self.top / "grading.toml"
        config.write_text('[project.demo]\nvenv = ' + json.dumps(str(venv)) + '\n')
        self.env["DOIT_GRADING_TOML"] = str(config)
        self.spec(None, f'cd "{self.repo}" &&\ncd "{actual}" &&\n'
                  f'test "{venv}/pin" = "{venv}/pin" &&\ntest -f {self.repo}/absent')
        bundle, _ = self.bundle()
        row = bundle["commands"][0]
        self.assertNotEqual(row["exit"], 0)
        self.assertIn(str(self.repo / "absent"), row["cmd"])
        self.assertEqual(row["cmd"].count(str(venv / "pin")), 2)
        changes = bundle["rewrites"]
        self.assertTrue(any(r["from"] == str(self.repo) for r in changes))
        self.assertTrue(any(r["from"] == str(actual) for r in changes))
        self.assertFalse(any(str(venv) in r["from"] for r in changes))

    def test_commands_exact_criteria_filter_prior_cd_carried(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "pin").touch()
        self.sha = self.commit("subdir")
        self.build_done()
        text = ('## Acceptance criteria\nAC1 [backend]: root.\nreview_path: run `pwd`\n'
                'AC2 [backend]: tagged.\nAC3 [observed-data]: production.\n'
                'AC4 [ui]: visual.\nAC5 [backend]: human judgment.\n'
                '## Verification\n```bash\ncd sub &&\n# AC2\ntest -f pin\n```\n')
        # `pwd` alone is not a review command in the registry (two words are
        # required); use its recognized absolute shell form.
        text = text.replace('run `pwd`', 'run `bash -c pwd`')
        self.spec(text=text)
        bundle, _ = self.bundle()
        self.assertEqual([r["criterion"] for r in bundle["commands"]], ["AC1", None])
        self.assertEqual(bundle["commands"][-1]["covers"], ["AC2"])
        self.assertEqual(bundle["judgment_only"], ["AC3", "AC4", "AC5"])
        self.assertTrue(all(r["exit"] == 0 for r in bundle["commands"]))
        for cid, runner in (("AC1", "build"), ("AC2", "build"), ("AC3", "prod"),
                            ("AC4", "prod-browser"), ("AC5", "judgment")):
            print(cid, runner, "ran", "yes" if cid in ("AC1", "AC2") else "no")
        selected, _ = self.bundle("--criteria", "AC2,AC1")
        self.assertEqual([r["criterion"] for r in selected["commands"]], ["AC1", None])
        self.assertEqual(self.tail(selected["commands"][0]).decode().strip(), selected["commands"][0]["cwd"])

    def test_verification_prose_never_runs_or_gets_tagged(self):
        (self.repo / "dashboard").mkdir()
        (self.repo / "dashboard" / "pin").touch()
        self.sha = self.commit("dashboard verification fixture")
        self.build_done()
        marker = self.top / "prose-executed"
        code = 'cd dashboard &&\n# AC1\n/usr/bin/test -f pin &&\n# AC2\n/usr/bin/test -d .\n'
        clean = ('## Acceptance criteria\nAC1 [backend]: first.\nAC2 [backend]: second.\n'
                 '## Verification\n```bash\n' + code + '```\n')
        prose = f'/usr/bin/touch {marker}\nThese notes mention AC99 and must not run.\n'
        for suffix in (prose, '&&\n' + prose, '## 9b. Notes on the checks\n' + prose):
            with self.subTest(suffix=suffix):
                self.spec(text=clean + suffix)
                bundle, _ = self.bundle()
                rows = bundle["commands"]
                self.assertEqual([row["criterion"] for row in rows], [None])
                self.assertEqual([row["exit"] for row in rows], [0])
                self.assertEqual(rows[-1]["cmd"], code)
                self.assertEqual(rows[-1]["covers"], ["AC1", "AC2"])
                self.assertTrue(all("touch" not in row["cmd"] for row in rows))
                self.assertFalse(marker.exists())
                selected, _ = self.bundle("--criteria", "AC2")
                self.assertEqual([row["criterion"] for row in selected["commands"]], [None])
                self.assertEqual(selected["commands"][0]["exit"], 0)
                self.assertIn("cd dashboard &&", selected["commands"][0]["cmd"])

    def test_compare_split_base_from_build_done_classify_merge(self):
        (self.repo / "probe.py").write_text("print('NEW failed: tests/a.py::same'); raise SystemExit(1)\n")
        self.base = self.commit("base failure")
        (self.repo / "probe.py").write_text("print('NEW failed: tests/a.py::same'); "
                                            "print('NEW failed: tests/a.py::new'); raise SystemExit(1)\n")
        self.sha = self.commit("tip failure")
        self.build_done()
        bundle, _ = self.bundle(driver=True, FIXTURE_CLASSIFY="complete")
        self.assertEqual(bundle["base_sha"], self.base)
        self.assertEqual(bundle["comparison"], {"pre_existing": ["ci-extra::known", "tests/a.py::same"],
                                                "new": ["tests/a.py::new"], "undetermined": []})
        other, _ = self.bundle("--criteria", "AC1", driver=True, FIXTURE_CLASSIFY="incomplete")
        self.assertEqual(other["comparison"]["pre_existing"], ["tests/a.py::same"])
        self.assertEqual(len(other["base"]), len(other["commands"]))

    def test_base_merge_base_project(self):
        self.git("update-ref", "refs/remotes/origin/main", self.sha)
        self.git("branch", "master", self.base)  # clone-local origin/master is stale
        event_file = self.root / "events" / "L-builder-fixture.jsonl"
        event = json.loads(event_file.read_text())
        del event["base_sha"]
        event_file.write_text(json.dumps(event) + "\n")
        bundle, _ = self.bundle()
        self.assertEqual(bundle["base_sha"], self.sha)

    def test_new_test_file_at_tip_and_existing_base_no_ids(self):
        (self.repo / "new.py").write_text("print('NEW failed: new.py::failure'); raise SystemExit(1)\n")
        self.sha = self.commit("new test")
        self.build_done()
        self.spec("cd . && python3 new.py")
        bundle, _ = self.bundle()
        self.assertEqual(bundle["comparison"], {"pre_existing": [], "new": ["new.py::failure"], "undetermined": []})
        # Existing base target exits without a named id.
        self.base = self.sha
        (self.repo / "new.py").write_text("raise SystemExit(1)\n")
        self.base = self.commit("base no ids")
        (self.repo / "new.py").write_text("print('NEW failed: new.py::failure'); raise SystemExit(1)\n")
        self.sha = self.commit("named failure")
        self.build_done()
        bundle, _ = self.bundle()
        self.assertEqual(bundle["comparison"], {"pre_existing": [], "new": [], "undetermined": [0]})

    def test_undetermined_no_ids_parse_and_base_failed(self):
        self.spec("bash -c 'exit 7'")
        bundle, _ = self.bundle()
        self.assertEqual(bundle["comparison"], {"pre_existing": [], "new": [], "undetermined": [0]})
        self.spec("bash -c 'echo SUMMARY pytest check: FAIL new=2; exit 1'")
        bundle, _ = self.bundle("--criteria", "AC1")
        self.assertEqual(bundle["comparison"], {"pre_existing": [], "new": [], "undetermined": [0]})
        self.provisioned()
        self.spec("bash -c 'echo NEW failed: tests/a.py::x; exit 1'")
        bundle, _ = self.bundle(driver=True, FIXTURE_FAIL="base-pg")
        self.assertEqual(bundle["base"], [])
        self.assertEqual(bundle["comparison"], {"pre_existing": [], "new": [], "undetermined": [0]})
        self.assert_clean()

    def test_digest_exact_tail_cap_bundle_keys(self):
        output = bytes(range(256)) * 781 + bytes(range(64))  # exactly 200000 bytes
        self.spec("python3 -c 'import sys;sys.stdout.buffer.write(bytes(range(256))*781+bytes(range(64)))'")
        bundle, path = self.bundle()
        self.assertEqual(set(bundle), {"spec", "project", "sha", "base_sha", "runner_version", "started_at",
                                       "env", "rewrites", "commands", "base", "comparison", "judgment_only"})
        self.assertRegex(bundle["started_at"], r"^\d{4}-\d\d-\d\dT")
        self.assertTrue(bundle["runner_version"])
        row = bundle["commands"][0]
        self.assertEqual(row["out_bytes"], 200000)
        self.assertEqual(row["out_sha256"], hashlib.sha256(output).hexdigest())
        self.assertEqual(self.tail(row), output[-65536:])
        files = [p for p in path.parent.rglob("*") if p.is_file()]
        self.assertFalse(any(p.read_bytes() == output for p in files))

    def test_dir_cap(self):
        command = "python3 -c 'import sys;sys.stdout.write(\"x\"*70000)'"
        self.spec(text="## Acceptance criteria\n" + "\n".join(
            f"AC{i} [backend]: output.\nreview_path: run `{command}`" for i in range(1, 43)))
        bundle, path = self.bundle()
        directory = path.with_suffix("")
        self.assertEqual(len(bundle["commands"]), 42)
        self.assertLessEqual(sum(p.stat().st_size for p in directory.iterdir()), 5 * 1024 * 1024)
        self.assertTrue(all(p.stat().st_size <= 65536 for p in directory.iterdir()))

    def test_roundtrip_event_foreign_ledger_env_digest_matches_file(self):
        foreign = self.root / "events" / self.env["DOIT_LEDGER_FILE"]
        foreign.touch()
        bundle, path = self.bundle()
        self.assertEqual(foreign.read_bytes(), b"")
        self.assertEqual(self.env["DOIT_LEDGER_FILE"], "L-executor-0001.jsonl")
        event, = self.events()
        self.assertNotIn("actor", event)
        self.assertEqual({k: event[k] for k in ("sha", "base_sha")}, {k: bundle[k] for k in ("sha", "base_sha")})
        self.assertEqual((event["n_commands"], event["n_failed"], event["n_new"]), (1, 0, 0))
        wrong = dict(event)
        (self.root / "events" / "L-executor-wrong.jsonl").write_text(json.dumps(wrong) + "\n")
        folded = self.folded()
        kept = [e for e in folded["kept"] if e["type"] == "evidence-bundle"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(folded["ignored"]), 1)
        self.assertEqual(folded["digests"][str(path)], event["bundle_sha256"])

    def test_reuse_same_key_fresh_other_key_lock_two_processes(self):
        counter = self.top / "counter"
        self.spec(f"python3 -c 'import pathlib,time;p=pathlib.Path(\"{counter}\"); "
                  "f=p.open(\"a\");f.write(\"run\\n\");f.close();time.sleep(.15)'")
        argv = [str(DOIT), "run-checks", self.sid]
        processes = [subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for _ in range(2)]
        try:
            results = [p.communicate(timeout=20) for p in processes]
        finally:
            for p in processes:
                if p.poll() is None:
                    p.kill()
                    p.wait()
        self.assertEqual([p.returncode for p in processes], [0, 0])
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(counter.read_text().splitlines(), ["run", "run"])  # tip + base exactly once
        path = pathlib.Path(results[0][0].decode().strip())
        original = path.read_bytes()
        self.assertEqual(len(self.events()), 1)
        self.bundle()
        self.assertEqual(counter.read_text().splitlines(), ["run", "run"])
        _, selected = self.bundle("--criteria", "AC1")
        self.assertNotEqual(selected, path)
        (self.repo / "other").touch()
        sha = self.commit("other sha")
        _, changed = self.bundle("--sha", sha)
        self.assertNotEqual(changed, path)
        self.assertEqual(path.read_bytes(), original)
        folded = self.folded()
        self.assertEqual(len(self.events()), 3)
        for event in self.events():
            self.assertEqual(event["bundle_sha256"], folded["digests"][event["path"]])

    def test_no_builder_input_no_dsn_in_bundle_command_cannot_write_live_ledger(self):
        # Run the real dispatcher + fold in the clone, with a missing caller HOME.
        shutil.copy2(DOIT, self.repo / "doit")
        shutil.copytree(HERE, self.repo / "src", ignore=shutil.ignore_patterns("fixtures", "test_*", "__pycache__"))
        self.sha = self.commit("real append in clone")
        self.build_done()
        (self.repo / "card-rationale").write_text("builder-input-marker")
        with (self.repo / ".git" / "config").open("a") as handle:
            handle.write("\n# builder-input-marker\n")
        before = {p.name: p.read_bytes() for p in (self.root / "events").iterdir()}
        self.env.pop("HOME", None)
        self.env["DOIT_SUPERVISED"] = "caller-actor"
        for name in ("SUPABASE_DB_URL", "SUPABASE_DB_URL_DIRECT", "SUPABASE_DB_URL_RO", "SUPABASE_DB_URL_MIGRATIONS"):
            self.env[name] = "CALLER_SENTINEL_" + name
        self.spec(None, 'python3 -c \'import os,json;print(json.dumps(dict(os.environ)))\' &&\n'
                  './doit append deploy-failed L-spec-9000 reason=fixture &&\n'
                  'python3 -c \'import os,pathlib;assert "deploy-failed" in '
                  '(pathlib.Path(os.environ["DOIT_ROOT"])/"events"/"L-operator-local.jsonl").read_text()\'')
        bundle, path = self.bundle()
        self.assertEqual(bundle["commands"][0]["exit"], 0)
        after = {p.name: p.read_bytes() for p in (self.root / "events").iterdir()}
        self.assertEqual({k: after[k] for k in before}, before)
        self.assertEqual(set(after) - set(before), {"L-build-runner-local.jsonl"})
        private = json.loads(self.tail(bundle["commands"][0]).decode().splitlines()[0])
        self.assertNotIn("DOIT_LEDGER_FILE", private)
        self.assertNotIn("DOIT_SUPERVISED", private)
        for name in ("HOME", "DOIT_ROOT"):
            self.assertTrue(pathlib.Path(private[name]).is_relative_to(self.top / "scratch" / "runner"))
        for artifact in [path, *path.with_suffix("").iterdir(), self.root / "events" / "L-build-runner-local.jsonl"]:
            self.assertNotIn(b"builder-input-marker", artifact.read_bytes())
            self.assertNotIn(b"CALLER_SENTINEL", artifact.read_bytes())

    def test_exit0_failing_command_and_teardown_all_paths(self):
        self.provisioned()
        self.spec("bash -c 'exit 9'")
        bundle, _ = self.bundle(driver=True)
        self.assertEqual(bundle["commands"][0]["exit"], 9)
        self.assert_clean()

    def test_exit2_matrix_no_command_exit2(self):
        self.provisioned()
        for case in ("clone", "pg", "setup", "npm", "sha", "base", "empty", "cap"):
            with self.subTest(case=case):
                self.log.unlink(missing_ok=True)
                pathlib.Path(str(self.log) + ".base").unlink(missing_ok=True)
                shutil.rmtree(self.top / "scratch" / "node-cache", ignore_errors=True)
                self.spec("python3 probe.py")
                self.build_done()
                extra, args = {}, []
                if case in ("clone", "pg", "setup", "npm"):
                    extra["FIXTURE_FAIL"] = case
                elif case == "sha":
                    args = ["--sha", "0" * 40]
                elif case == "base":
                    self.build_done(base="0" * 40)
                elif case == "empty":
                    self.spec(None)
                else:
                    pidfile = self.top / "hanging-child"
                    self.spec(f"python3 -c 'import os,pathlib,time;pathlib.Path(\"{pidfile}\").write_text(str(os.getpid()));time.sleep(60)'")
                    extra["FIXTURE_CAP"] = "3"
                prior = len(self.events())
                result = self.cli(*args, driver=True, **extra)
                self.assertEqual(result.returncode, 2, "case " + case + ": " + result.stderr)
                self.assertTrue(result.stderr.strip())
                events = self.events()
                self.assertEqual(len(events), prior + 1)
                self.assertEqual(events[-1]["type"], "evidence-bundle-failed")
                self.assertTrue(events[-1]["reason"])
                self.assertFalse(list((self.root / "content").rglob("*.json")))
                self.assertFalse([e for e in self.folded()["kept"] if e["type"] == "evidence-bundle"])
                self.assert_clean()
                if case == "cap":
                    self.assertTrue(pidfile.exists(), "cap must interrupt the hanging command")
                    self.assertFalse(pathlib.Path("/proc", pidfile.read_text()).exists())
                print(case, result.returncode, events[-1]["reason"])


class NodeModulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="node-modules-test-")
        self.addCleanup(self.temp.cleanup)
        self.top = pathlib.Path(self.temp.name)
        self.source = self.top / "source" / "node_modules"
        (self.source / ".bin").mkdir(parents=True)
        (self.source / "probe" / "dist").mkdir(parents=True)
        (self.source / "probe" / "cli.mjs").write_text(
            "import { message } from './dist/cli.mjs'; console.log(message);\n")
        (self.source / "probe" / "dist" / "cli.mjs").write_text(
            "export const message = 'private bin works';\n")
        (self.source / ".bin" / "probe").symlink_to("../probe/cli.mjs")
        (self.source / "probe-alias").symlink_to("probe", target_is_directory=True)
        self.target = self.top / "private" / "node_modules"

    def test_relative_bin_import_and_private_copy(self):
        node_modules.copy_tree(self.source, self.target)
        binary = self.target / ".bin" / "probe"
        self.assertTrue(binary.is_symlink())
        self.assertTrue((self.target / "probe-alias").is_symlink())
        self.assertEqual(binary.resolve(), self.target / "probe" / "cli.mjs")
        # Node is optional in the hermetic suite; containment is always tested.
        node = shutil.which("node")
        if node:
            broken = self.top / "dereferenced"
            shutil.copytree(self.source, broken)
            failure = subprocess.run([node, str(broken / ".bin" / "probe")],
                                     capture_output=True, text=True, timeout=10)
            self.assertNotEqual(failure.returncode, 0)
            self.assertIn("ERR_MODULE_NOT_FOUND", failure.stderr)
            result = subprocess.run([node, str(binary)], capture_output=True,
                                    text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "private bin works")
        private = self.target / "probe" / "dist" / "cli.mjs"
        original = self.source / "probe" / "dist" / "cli.mjs"
        self.assertNotEqual(private.stat().st_ino, original.stat().st_ino)
        private.write_text("private mutation")
        self.assertIn("private bin works", original.read_text())

    def test_escaping_absolute_and_cyclic_links_are_rejected(self):
        outside = self.top / "source" / "outside"
        outside.write_text("must not be copied")
        for destination in ("../outside", "../outside-directory", str(outside),
                            str(self.source / "probe"), "escape"):
            with self.subTest(destination=destination):
                link = self.source / "escape"
                link.symlink_to(destination)
                try:
                    with self.assertRaises(node_modules.UnsafeNodeModules):
                        node_modules.copy_tree(self.source, self.target)
                    self.assertFalse(self.target.exists())
                finally:
                    link.unlink()

    def test_root_symlinks_are_rejected_without_changing_target(self):
        source_link = self.top / "source-link"
        source_link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(node_modules.UnsafeNodeModules):
            node_modules.copy_tree(source_link, self.target)
        self.target.parent.mkdir()
        self.target.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(node_modules.UnsafeNodeModules):
            node_modules.copy_tree(self.source, self.target)
        self.assertTrue(self.target.is_symlink())
        self.assertTrue((self.source / "probe" / "cli.mjs").exists())

    def test_late_escape_removes_the_private_copy(self):
        copytree = shutil.copytree
        def changed_copy(source, target, *args, **kwargs):
            copytree(source, target, *args, **kwargs)
            if pathlib.Path(target) == self.target:
                (pathlib.Path(target) / "escape").symlink_to(self.top / "outside")
        with patch.object(node_modules.shutil, "copytree", side_effect=changed_copy):
            with self.assertRaises(node_modules.UnsafeNodeModules):
                node_modules.copy_tree(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_build_runner_cache_hit_preserves_bins_and_rejects_escape(self):
        dashboard = self.top / "tree" / "dashboard"
        dashboard.mkdir(parents=True)
        lock = dashboard / "package-lock.json"
        lock.write_text('{"lockfileVersion":3}')
        key = hashlib.sha256(lock.read_bytes()).hexdigest()[:16]
        cache_root = self.top / "cache"
        cache_root.mkdir()
        cached = cache_root / key / "node_modules"
        shutil.copytree(self.source, cached, symlinks=True)
        with patch.object(build_runner.scratch, "sub", return_value=cache_root), \
                patch.object(build_runner, "_required", side_effect=AssertionError("npm called")):
            result = build_runner._node(dashboard.parent, {"node_dirs": ["dashboard"]},
                                        {}, time.monotonic() + 10)
            self.assertEqual(result, "cache:" + key)
            binary = dashboard / "node_modules" / ".bin" / "probe"
            self.assertTrue(binary.is_symlink())
            self.assertTrue(binary.resolve().is_relative_to(dashboard / "node_modules"))
            shutil.rmtree(dashboard / "node_modules")
            (cached / "escape").symlink_to(self.top / "outside")
            with self.assertRaisesRegex(build_runner.Undetermined, "escapes private tree"):
                build_runner._node(dashboard.parent, {"node_dirs": ["dashboard"]},
                                   {}, time.monotonic() + 10)
            self.assertFalse((dashboard / "node_modules").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
