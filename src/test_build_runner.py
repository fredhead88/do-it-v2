#!/usr/bin/env python3
"""Hermetic CLI regressions; git and the append/fold store are real."""
import hashlib
import contextlib
import io
import json
import os
import pathlib
import shutil
import signal
import socket
import stat
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
        self.aliases = self.top / "aliases"
        self.backing = self.top / "backing"
        self.aliases.mkdir()
        self.backing.mkdir()
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
        argv = self.runner_argv(driver)
        result = subprocess.run([*argv, self.sid, *args], env=dict(self.env, **env),
                                capture_output=True, text=True, timeout=20)
        return result

    def runner_argv(self, driver=False):
        # Set the declared test seams before any startup discovery in children.
        code = ("import sys,pathlib,runpy; sys.path.insert(0,sys.argv.pop(1)); "
                "import build_runner as r; r.SHORT_TMP_ROOT=pathlib.Path(sys.argv.pop(1)); "
                "r.SHORT_TMP_BACKING_ROOT=pathlib.Path(sys.argv.pop(1)); ")
        code += ("runpy.run_path(sys.argv.pop(1),run_name='__main__')" if driver else
                 "sys.exit(r.main(sys.argv[1:]))")
        return [sys.executable, "-c", code, str(HERE), str(self.aliases), str(self.backing),
                *([str(FIX / "driver.py")] if driver else [])]

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
                    output = self.tail(row).decode().splitlines()
                    self.assertEqual(output[:-1], [
                        str(expected / "alembic"), str(expected / "python"),
                        "venv-alembic", "venv-python",
                        str(expected) + os.pathsep + self.env["PATH"]])
                    tmp = pathlib.Path(output[-1])
                    self.assertEqual(tmp.parent, self.aliases)
                    self.assertRegex(tmp.name, r"^dr-[1-9][0-9]*-[a-z0-9]{8}$")
                    self.assertFalse(os.path.lexists(tmp))
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
        argv = [*self.runner_argv(), self.sid]
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


class ShortTmpdirTests(unittest.TestCase):
    """Real allocation/write/socket evidence; all discovery roots are private."""

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.top = pathlib.Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="short-tmp-test-", dir=build_runner.scratch.sub("tests"))))
        self.aliases = pathlib.Path(self.stack.enter_context(
            tempfile.TemporaryDirectory(prefix="drt-", dir="/tmp")))
        self.backing = self.top / ("scratch-backing-" + "x" * 100)
        self.backing.mkdir()
        self.production_root = build_runner.SHORT_TMP_ROOT
        self.stack.enter_context(patch.multiple(build_runner, SHORT_TMP_ROOT=self.aliases,
                                                SHORT_TMP_BACKING_ROOT=self.backing))
        self.stack.enter_context(patch.dict(os.environ, DOIT_SCRATCH=str(self.top / "scratch")))
        self.proc = self.top / "proc"
        (self.proc / "net").mkdir(parents=True)
        (self.proc / "net" / "unix").write_text("Num RefCount Protocol Flags Type St Inode Path\n")
        self.events = []

    def evidence(self, ac, **body):
        text = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url=f"cli://test_build_runner/ShortTmpdirTests.{ac}", status=0,
            body_sha256=hashlib.sha256(text.encode()).hexdigest(), body_excerpt=text)))

    def allocate(self):
        alias = build_runner.short_tmpdir()
        return alias, pathlib.Path(os.readlink(alias))

    def remove(self, alias, backing):
        build_runner._remove_tmp_pair(alias, backing, build_runner._tmp_pair(alias, backing))

    def child(self, code, *args, env=None):
        return subprocess.run([sys.executable, "-c", code, *map(str, args)], env=env,
                              text=True, capture_output=True, check=True, timeout=15).stdout.strip()

    def test_ac1(self):
        self.assertEqual(self.production_root, pathlib.Path("/tmp"))
        allocated = []
        for case in ("first", "consecutive", "delete-reallocate", "long-inherited", "long-scratch"):
            with self.subTest(case=case), patch.dict(os.environ,
                    TMPDIR="/ignored/" + "a" * 200, TMP="/ignored/" + "b" * 200,
                    TEMP="/ignored/" + "c" * 200,
                    DOIT_SCRATCH=str(self.top / ("long-scratch-" + "d" * 180))):
                # Also exercise the real scratch.sub selection with a long root.
                with patch.object(build_runner, "SHORT_TMP_BACKING_ROOT", None):
                    alias, backing = self.allocate()
                self.assertNotIn(alias, allocated)
                allocated.append(alias)
                self.assertEqual(alias.parent, self.aliases)
                self.assertRegex(alias.name, rf"^dr-{os.getpid()}-[a-z0-9]{{8}}$")
                self.assertEqual(backing.name, alias.name)
                self.assertEqual(backing.parent, pathlib.Path(os.environ["DOIT_SCRATCH"]) / "runner-tmp")
                nested = alias / ("s" * 20)
                nested.mkdir()
                sockpath = nested / ".s.PGSQL.65535"
                self.assertLess(len(os.fsencode(sockpath)), 107)
                self.assertLess(len(os.fsencode(pathlib.Path("/tmp") / alias.name /
                                               ("s" * 20) / ".s.PGSQL.65535")), 107)
                with socket.socket(socket.AF_UNIX) as sock:
                    sock.bind(str(sockpath))
                env = dict(os.environ, TMPDIR=str(alias), TMP=str(alias), TEMP=str(alias))
                output = json.loads(self.child(
                    "import os,pathlib,stat,tempfile,json; "
                    "a=pathlib.Path(os.environ['TMPDIR']); st=a.stat(); "
                    "assert st.st_uid==os.geteuid() and stat.S_IMODE(st.st_mode)==0o700; "
                    "f=tempfile.NamedTemporaryFile(delete=False); f.write(b'x'*(1024*1024)); "
                    "f.close(); print(json.dumps([f.name,str(pathlib.Path(f.name).resolve())]))", env=env))
                lexical, canonical = map(pathlib.Path, output)
                self.assertEqual(lexical.parent, alias)
                self.assertEqual(canonical.parent, backing)
                self.assertEqual(self.child(
                    "import pathlib,sys; b=pathlib.Path(sys.argv[1]).read_bytes(); "
                    "assert b==b'x'*(1024*1024); print(len(b))", lexical), "1048576")
                self.assertFalse([p for root, dirs, files in os.walk(self.aliases, followlinks=False)
                                  for p in files if stat.S_ISREG((pathlib.Path(root)/p).lstat().st_mode)])
                self.remove(alias, backing)
                self.assertFalse(os.path.lexists(alias))
                self.assertFalse(backing.exists())
        marker = self.aliases / "unowned"
        marker.write_text("keep")
        with patch.object(pathlib.Path, "symlink_to", side_effect=FileExistsError("occupied")):
            with self.assertRaises(FileExistsError):
                build_runner.short_tmpdir()
        self.assertEqual(list(self.backing.iterdir()), [])
        self.assertEqual(marker.read_text(), "keep")
        marker.unlink()
        # A real exclusive-publication collision preserves the unowned alias.
        name = f"dr-{os.getpid()}-01234567"
        occupied = self.aliases / name
        occupied.write_text("unowned")
        with patch.object(build_runner.secrets, "token_hex", return_value="01234567"):
            with self.assertRaises(FileExistsError):
                build_runner.short_tmpdir()
        self.assertEqual(occupied.read_text(), "unowned")
        self.assertFalse((self.backing / name).exists())
        occupied.unlink()
        self.evidence("AC1", allocations=len(allocated), socket_bind=True, bytes_written_read=1048576,
                      scratch_backed=True, publication_rollback=True)

    def required(self, argv, **kw):
        env = kw["env"]
        self.assertEqual(env["TMPDIR"], env["TMP"])
        self.assertEqual(env["TMPDIR"], env["TEMP"])
        self.assertEqual(pathlib.Path(env["TMPDIR"]).parent, self.aliases)
        if "clone" in argv:
            pathlib.Path(argv[-1]).mkdir()
        return ""

    def invoke(self, sha="a" * 40, row=None):
        tails = self.top / "tails"
        tails.mkdir(exist_ok=True)
        return build_runner._run_tree("L-spec-9000", sha, self.top, row or {},
            [("AC1", "true")], tails, 0, 1000, time.monotonic() + 30)

    def test_ac2(self):
        observed = []
        required_paths = []
        def required(argv, **kw):
            required_paths.append(kw["env"]["TMPDIR"])
            return self.required(argv, **kw)
        def provision(tree, auxiliary, row, env, deadline):
            alias = pathlib.Path(env["TMPDIR"])
            backing = alias.resolve()
            self.assertEqual(pathlib.Path(env["HOME"]), auxiliary / "home")
            self.assertEqual(pathlib.Path(env["DOIT_ROOT"]), auxiliary / "root")
            self.assertEqual(env["PATH"].split(os.pathsep)[0], str(tree / row["venv"] / "bin"))
            output = self.child("import os,tempfile,pathlib,json; "
                "f=tempfile.NamedTemporaryFile(delete=False); f.write(b'fixture'); f.close(); "
                "p=pathlib.Path(f.name); assert p.read_bytes()==b'fixture'; "
                "print(json.dumps([str(p.parent),str(p.resolve().parent)]))", env=env)
            self.assertEqual(json.loads(output), [str(alias), str(backing)])
            observed.append((alias, backing))
            return {}
        def check(*a, **kw):
            self.assertEqual(kw["env"]["TMPDIR"], str(observed[-1][0]))
            return 0, b"fixture"
        with patch.dict(os.environ, TMPDIR="/ignored", TMP="/ignored", TEMP="/ignored"), \
                patch.object(build_runner, "_required", side_effect=required), \
                patch.object(build_runner, "_provision", side_effect=provision), \
                patch.object(build_runner, "_process", side_effect=check), \
                patch.object(build_runner.grading_env, "teardown"):
            parent = dict(os.environ)
            for sha, venv in (("a"*40, ".venv"), ("b"*40, "/fixture-venv"), ("a"*40, ".venv")):
                self.invoke(sha, dict(venv=venv))
            self.assertEqual(dict(os.environ), parent)
        self.assertEqual(len({str(a) for a, b in observed}), 3)
        self.assertEqual(required_paths, [str(a) for a,b in observed for _ in range(2)])
        self.assertTrue(all(not os.path.lexists(a) and not b.exists() for a,b in observed))
        self.evidence("AC2", unique_invocations=3, clone_checkout_provision_check=True,
                      child_tempfile=True, parent_unchanged=True, venv_home_root=True)

    def test_ac3(self):
        outcomes = ("success", "nonzero", "clone", "checkout", "provision", "check",
                    "wallcap", "sigint", "sigterm", "home", "root", "allocation", "teardown")
        recorded = []
        real_allocate = build_runner.short_tmpdir
        marker = self.top / "outside-marker"
        marker.write_text("keep")
        def allocate():
            alias = real_allocate()
            backing = alias.resolve()
            (backing / "nested-link").symlink_to(marker.parent, target_is_directory=True)
            recorded.append((alias, backing))
            return alias
        mkdir = pathlib.Path.mkdir
        for outcome in outcomes:
            with self.subTest(outcome=outcome), contextlib.ExitStack() as stack:
                def required(argv, **kw):
                    if outcome == kw["phase"]:
                        raise OSError("fixture " + outcome)
                    return self.required(argv, **kw)
                def provision(*a):
                    if outcome == "provision":
                        raise RuntimeError("fixture provision")
                    return {}
                def check(*a, **kw):
                    if outcome == "check":
                        raise RuntimeError("fixture check")
                    if outcome == "wallcap":
                        raise build_runner.WallCap("fixture cap")
                    if outcome in ("sigint", "sigterm"):
                        sig = signal.SIGINT if outcome == "sigint" else signal.SIGTERM
                        old = signal.signal(sig, build_runner._interrupted)
                        try:
                            os.kill(os.getpid(), sig)
                        finally:
                            signal.signal(sig, old)
                    return (1 if outcome == "nonzero" else 0), b"fixture"
                def partial(path, *a, **kw):
                    if outcome in ("home", "root") and path.name == outcome:
                        raise OSError("partial fixture setup")
                    return mkdir(path, *a, **kw)
                stack.enter_context(patch.object(build_runner, "short_tmpdir",
                    side_effect=OSError("allocation failed") if outcome == "allocation" else allocate))
                stack.enter_context(patch.object(build_runner, "_required", side_effect=required))
                stack.enter_context(patch.object(build_runner, "_provision", side_effect=provision))
                stack.enter_context(patch.object(build_runner, "_process", side_effect=check))
                teardown = stack.enter_context(patch.object(build_runner.grading_env, "teardown",
                    side_effect=RuntimeError("teardown failed") if outcome == "teardown" else None))
                stack.enter_context(patch.object(pathlib.Path, "mkdir", partial))
                if outcome in ("success", "nonzero"):
                    result = self.invoke()
                    self.assertEqual(result[0][0]["exit"], int(outcome == "nonzero"))
                else:
                    with self.assertRaises((OSError, RuntimeError, build_runner.Undetermined)):
                        self.invoke()
                teardown.assert_called_once()
                self.assertEqual(list(build_runner.scratch.sub("runner").iterdir()), [])
                self.assertTrue(all(not os.path.lexists(a) and not b.exists() for a,b in recorded))
                self.assertEqual(list(self.aliases.iterdir()), [])
                self.assertEqual(list(self.backing.iterdir()), [])
                self.assertEqual(marker.read_text(), "keep")
        auxiliary = build_runner.scratch.sub("runner") / f"L-spec-9000-{'a'*12}-{os.getpid()}-env"
        auxiliary.mkdir()
        (auxiliary / "unowned").write_text("keep")
        with self.assertRaisesRegex(build_runner.Undetermined, "already exists"):
            self.invoke()
        self.assertEqual((auxiliary / "unowned").read_text(), "keep")
        self.evidence("AC3", outcomes=list(outcomes), allocated_pairs=len(recorded), residue=0,
                      unowned_preserved=True, nested_symlink_preserved=True)

    def dead_pair(self, suffix="abcdefgh"):
        name = f"dr-2147483647-{suffix}"
        backing = self.backing / name
        backing.mkdir(mode=0o700)
        alias = self.aliases / name
        alias.symlink_to(backing)
        return alias, backing

    def sweep(self):
        return build_runner.sweep_short_tmpdirs(proc_root=self.proc)

    def test_ac4(self):
        self.recovery_matrix()
        self.real_holders()
        self.startup_errors()
        self.evidence("AC4", matrix=self.events, real_orphans=["cwd", "fd"],
                      real_socket=True, real_pidfile=True, startup_direct_cli_cached=True)

    def recovery_matrix(self):
        # The only PID predicate patched is the shared panes.alive.
        cases = ("safe", "backing-only", "alias-only", "live-owner", "foreign-alias", "foreign-backing",
                 "unexpected-target", "backing-symlink", "liveness-error", "snapshot-error",
                 "socket-missing", "socket-unreadable", "pidfile-unreadable", "pidfile-invalid-text",
                 "candidate-unreadable",
                 "cwd-alias", "cwd-backing", "fd-alias", "fd-backing", "socket-alias", "socket-backing",
                 "live-pidfile", "remove-error", "changed-identity")
        real_pair = build_runner._tmp_pair
        real_lstat = pathlib.Path.lstat
        real_read = pathlib.Path.read_text
        real_remove = build_runner._remove_tmp_pair
        for case in cases:
            with self.subTest(case=case), contextlib.ExitStack() as stack:
                alias, backing = self.dead_pair()
                marker = self.top / "outside"
                marker.mkdir(exist_ok=True)
                (marker / "keep").write_text("keep")
                (backing / "nested").symlink_to(marker, target_is_directory=True)
                if case == "backing-only": alias.unlink()
                if case == "alias-only": shutil.rmtree(backing)
                if case == "unexpected-target":
                    alias.unlink(); alias.symlink_to(marker)
                if case == "backing-symlink":
                    shutil.rmtree(backing); backing.symlink_to(marker)
                if case == "socket-missing": (self.proc / "net" / "unix").unlink()
                if case == "live-pidfile": (backing / "postmaster.pid").write_text(str(os.getpid()))
                if case == "pidfile-unreadable": (backing / "postmaster.pid").write_text("123")
                if case == "pidfile-invalid-text": (backing / "postmaster.pid").write_bytes(b"\xff")
                if case in ("cwd-alias", "cwd-backing", "fd-alias", "fd-backing"):
                    proc = self.proc / str(os.getpid())
                    (proc / "fd").mkdir(parents=True)
                    target = alias if case.endswith("alias") else backing
                    (proc / ("cwd" if case.startswith("cwd") else "fd/9")).symlink_to(target)
                if case in ("socket-alias", "socket-backing"):
                    path = alias if case.endswith("alias") else backing
                    (self.proc / "net" / "unix").write_text("header\n0 0 0 0 0 0 0 " + str(path / "socket") + "\n")
                def alive(pid):
                    if case == "liveness-error": raise PermissionError("uncertain")
                    return pid == os.getpid() or case == "live-owner"
                stack.enter_context(patch.object(build_runner.panes, "alive", side_effect=alive))
                if case == "snapshot-error":
                    stack.enter_context(patch.object(build_runner.reap_tmp, "_own_uid_refs", return_value=(None, 0)))
                def lstat(path, *a, **kw):
                    if path == alias and case == "candidate-unreadable": raise PermissionError("candidate")
                    result = real_lstat(path, *a, **kw)
                    foreign = (case == "foreign-alias" and path == alias or
                               case == "foreign-backing" and path == backing)
                    if foreign:
                        fields = list(result); fields[4] = os.geteuid() + 1
                        return os.stat_result(fields)
                    return result
                stack.enter_context(patch.object(pathlib.Path, "lstat", lstat))
                def read(path, *a, **kw):
                    if (case == "socket-unreadable" and path == self.proc / "net" / "unix" or
                        case == "pidfile-unreadable" and path == backing / "postmaster.pid"):
                        raise PermissionError("evidence")
                    return real_read(path, *a, **kw)
                stack.enter_context(patch.object(pathlib.Path, "read_text", read))
                def remove(a, b, identity):
                    if case == "remove-error": raise PermissionError("removal")
                    if case == "changed-identity":
                        a.unlink(); a.symlink_to(marker)
                    return real_remove(a, b, identity)
                stack.enter_context(patch.object(build_runner, "_remove_tmp_pair", side_effect=remove))
                result = self.sweep()
                safe = case in ("safe", "backing-only", "alias-only")
                self.assertEqual(result["removed"], int(safe), result)
                self.assertEqual(result["retained"], int(not safe), result)
                if not safe:
                    self.assertTrue(os.path.lexists(alias))
                    self.assertTrue(os.path.lexists(backing))
                else:
                    self.assertFalse(os.path.lexists(alias)); self.assertFalse(backing.exists())
                self.assertEqual((marker / "keep").read_text(), "keep")
                self.events.append(case)
            # Fixture-owned cleanup, including substituted paths, never follows links.
            if os.path.lexists(alias): alias.unlink()
            if backing.is_symlink(): backing.unlink()
            elif backing.exists(): shutil.rmtree(backing)
            proc = self.proc / str(os.getpid())
            if proc.exists(): shutil.rmtree(proc)
            (self.proc / "net" / "unix").write_text("header\n")
        current = [self.allocate() for _ in range(2)]
        self.assertEqual(self.sweep(), dict(removed=0, retained=2, errors=0))
        self.assertNotEqual(current[0][0], current[1][0])
        for a,b in current: self.remove(a,b)
        # Unrelated and malformed names are never deleted.
        for name in ("unrelated", "dr-no-pid-abcdefgh", "dr-99999999999-abcdefgh"):
            (self.aliases / name).mkdir()
        self.sweep()
        self.assertEqual(len(list(self.aliases.iterdir())), 3)
        for path in self.aliases.iterdir(): path.rmdir()
        self.events.extend(["current-live", "concurrent", "unrelated", "malformed-pid"])
        # An inspection/removal error on one pair cannot block a safe peer.
        a,b = self.dead_pair("aaaaaaaa")
        c,d = self.dead_pair("bbbbbbbb")
        def remove(a1,b1,identity):
            if a1 == a: raise OSError("fixture removal")
            return real_remove(a1,b1,identity)
        with patch.object(build_runner, "_remove_tmp_pair", side_effect=remove):
            self.assertEqual(self.sweep(), dict(removed=1, retained=1, errors=1))
        self.remove(a,b)
        with patch.object(pathlib.Path, "iterdir", side_effect=OSError("discovery")):
            self.assertEqual(self.sweep()["errors"], 1)
        # A vanished candidate is not reported as removed and repeated sweep is harmless.
        a,b = self.dead_pair()
        # Disappear after discovery has captured the name, before inspection.
        def disappear(*args):
            a.unlink()
            shutil.rmtree(b)
            return None, None
        with patch.object(build_runner, "_tmp_pair", side_effect=disappear):
            self.assertEqual(self.sweep()["removed"], 0)
        self.assertEqual(self.sweep(), dict(removed=0, retained=0, errors=0))

    def owner_code(self):
        return ("import sys,pathlib,time,json,subprocess,os; sys.path.insert(0,sys.argv[1]); "
                "import build_runner as r; r.SHORT_TMP_ROOT=pathlib.Path(sys.argv[2]); "
                "r.SHORT_TMP_BACKING_ROOT=pathlib.Path(sys.argv[3]); a=r.short_tmpdir(); "
                "b=a.resolve(); (b/'marker').write_text('keep'); ")

    def real_holders(self):
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        self.addCleanup(lambda: libc.prctl(36, previous.value, 0, 0, 0))
        # Read the actual holder's /proc links and actual kernel socket table,
        # through an isolated proc snapshot containing only fixture processes.
        (self.proc / "net" / "unix").unlink()
        (self.proc / "net" / "unix").symlink_to("/proc/net/unix")
        for kind in ("cwd", "fd", "socket", "pidfile", "unused"):
            with self.subTest(real=kind):
                ready = self.top / "ready"
                ready.unlink(missing_ok=True)
                holder_code = (
                    "import os,sys,pathlib,time,socket; b=pathlib.Path(sys.argv[1]); kind=sys.argv[2]; "
                    "f=None; s=None; "
                    "os.chdir(b) if kind=='cwd' else None; "
                    "f=(b/'marker').open() if kind=='fd' else None; "
                    "s=socket.socket(socket.AF_UNIX) if kind=='socket' else None; "
                    "s.bind(str(pathlib.Path(sys.argv[4])/'socket')) if s else None; "
                    "(b/'postmaster.pid').write_text(str(os.getpid())) if kind=='pidfile' else None; "
                    "pathlib.Path(sys.argv[3]).write_text(str(os.getpid())); time.sleep(60)")
                code = self.owner_code()
                if kind != "unused":
                    code += ("p=subprocess.Popen([sys.executable,'-c',sys.argv[4],str(b),sys.argv[5],"
                             "sys.argv[6],str(a)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); ")
                code += "print(str(a),flush=True); time.sleep(60)"
                owner = subprocess.Popen([sys.executable, "-c", code, str(HERE), str(self.aliases),
                    str(self.backing), holder_code, kind, str(ready)], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True)
                holder = None
                try:
                    alias = pathlib.Path(owner.stdout.readline().strip())
                    self.assertEqual(alias.parent, self.aliases)
                    backing = alias.resolve()
                    if kind != "unused":
                        end = time.monotonic() + 10
                        while not ready.exists() and time.monotonic() < end: time.sleep(.01)
                        self.assertTrue(ready.exists(), kind)
                        holder = int(ready.read_text())
                        # Make this test the orphan's reaper on Linux, so no zombie
                        # PID can keep a fixture allocation alive after teardown.
                    owner.kill(); owner.wait(timeout=5)
                    if holder:
                        (self.proc / str(holder)).symlink_to(pathlib.Path("/proc") / str(holder))
                        self.assertEqual(self.sweep()["retained"], 1)
                        self.assertTrue(alias.is_symlink() and backing.is_dir())
                        self.assertEqual((backing / "marker").read_text(), "keep")
                        os.kill(holder, signal.SIGTERM)
                        # The subreaper below adopts it; waitpid proves it is dead.
                        os.waitpid(holder, 0)
                        (self.proc / str(holder)).unlink()
                        holder = None
                    self.assertEqual(self.sweep(), dict(removed=1, retained=0, errors=0))
                    self.assertFalse(os.path.lexists(alias)); self.assertFalse(backing.exists())
                finally:
                    if owner.poll() is None: owner.kill(); owner.wait()
                    owner.stdout.close(); owner.stderr.close()
                    if holder:
                        os.kill(holder, signal.SIGKILL)
                        os.waitpid(holder, 0)
                        (self.proc / str(holder)).unlink(missing_ok=True)
                self.events.append("real-" + kind)

    def startup_errors(self):
        # Reuse the hermetic bundle fixture; no external command is added.
        fixture = BuildRunnerTests("test_env_none_row")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, fixture.env))
            stack.enter_context(patch.multiple(build_runner.fold, ROOT=fixture.root,
                                               EVENTS=fixture.root / "events"))
            stack.enter_context(patch.object(build_runner.grading_env, "_project_checkout", return_value=fixture.repo))
            stack.enter_context(patch.object(build_runner.grading_env, "_project_row", return_value={}))
            stack.enter_context(patch.object(build_runner, "_event", side_effect=lambda s,p,k,**kw: self.events.append(k)))
            stack.enter_context(patch.object(build_runner, "_provision", return_value={}))
            real_sweep = build_runner.sweep_short_tmpdirs
            for entry in ("direct", "cli"):
                for failure in ("none", "discovery", "removal"):
                    owner = subprocess.Popen([sys.executable, "-c", self.owner_code() +
                        "print(str(a),flush=True); time.sleep(60)", str(HERE),
                        str(self.aliases), str(self.backing)], stdout=subprocess.PIPE, text=True)
                    try:
                        alias = pathlib.Path(owner.stdout.readline().strip())
                        backing = alias.resolve()
                    finally:
                        owner.kill(); owner.wait(timeout=5); owner.stdout.close()
                    sweep_results = []
                    def sweep():
                        with contextlib.ExitStack() as errors:
                            if failure == "discovery":
                                errors.enter_context(patch.object(pathlib.Path, "iterdir", side_effect=OSError("discovery")))
                            if failure == "removal":
                                errors.enter_context(patch.object(build_runner, "_remove_tmp_pair", side_effect=OSError("removal")))
                            result = real_sweep(proc_root=self.proc)
                        if failure == "none": self.assertEqual(result["removed"], int(not sweep_results))
                        else: self.assertGreaterEqual(result["errors"], 1)
                        sweep_results.append(result)
                        return result
                    with patch.object(build_runner, "sweep_short_tmpdirs", side_effect=sweep) as startup:
                        if entry == "direct":
                            path = build_runner.run(fixture.sid)
                        else:
                            with contextlib.redirect_stdout(io.StringIO()):
                                self.assertEqual(build_runner.main([fixture.sid]), 0)
                            path = next((fixture.root / "content/evidence" / fixture.sid).glob("*.json"))
                        self.assertIn("commands", json.loads(path.read_text()))
                        # Every cached return must still sweep.
                        if entry == "direct":
                            self.assertEqual(build_runner.run(fixture.sid), path)
                        else:
                            with contextlib.redirect_stdout(io.StringIO()):
                                self.assertEqual(build_runner.main([fixture.sid]), 0)
                        self.assertEqual(startup.call_count, 2)
                    if failure != "none": self.remove(alias,backing)
                    path.unlink(); shutil.rmtree(path.with_suffix(""))
            self.assertNotIn("evidence-bundle-failed", self.events)


if __name__ == "__main__":
    unittest.main(verbosity=2)
