"""Hermetic owed provisioning fixtures; no npm network or real database needed."""
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import build_runner
import sweep_provision as provision


def check_manifest_rows(rows, base):
    """Shared integration assertions for the driver's real git-clone fixture."""
    for row in rows:
        assert row["provisioning"] == {"node_modules": "none", "prod_dsns": "unset"}
        assert "sweep_provision.py" in row["check_runner"]
        observed = subprocess.run(__import__("shlex").split(row["check_runner"]),
            input='test "$BASE" = ' + base + '\nprintf "wrapper passed\\n"',
            capture_output=True, text=True)
        assert observed.returncode == 0 and observed.stdout == "wrapper passed\n", observed.stderr


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.top = pathlib.Path(temporary.name)
        self.tree = self.top / "tree"
        self.dash = self.tree / "dashboard"
        self.dash.mkdir(parents=True)
        self.root = self.top / "root"
        self.live = self.top / "live-dashboard"
        self.live.mkdir()
        self.lock = b'{"lockfileVersion": 3}\n'
        self.key = hashlib.sha256(self.lock).hexdigest()
        (self.dash / "package-lock.json").write_bytes(self.lock)
        (self.dash / "package.json").write_text('{"private":true}')
        self.cache = self.root / "cache/node_modules" / self.key
        self.record = self.top / "check.provisioning.json"
        self.record.write_text(json.dumps({"node_modules": "fixture"}))
        self.pglog = self.top / "pg-path"
        self.stoplog = self.top / "stop-path"
        # Fake cluster lifecycle implements the real helper's output contract.
        self.pg = self.top / "pg.sh"
        self.pg.write_text('set -eu\n'
            'test "$2" = loopback\n'
            'test -z "${DATABASE_URL:-}${SUPABASE_DB_URL:-}${AS_LOCAL_PG_ADMIN:-}${PGHOST:-}"\n'
            'mkdir -p "$1/data"\n'
            'touch "$1/data/postmaster.pid"\n'
            f'printf "%s" "$1" > "{self.pglog}"\n'
            'printf "ADMIN_DSN=\x27postgresql://albert:00000000000000000000000000000000@127.0.0.1:23456/postgres\x27\\n"\n')
        self.ctl = self.top / "pg_ctl"
        self.ctl.write_text('#!/bin/bash\nset -eu\n'
            'while [ "$1" != -D ]; do shift; done\nshift\n'
            f'printf "%s" "$1" > "{self.stoplog}"\n'
            'rm "$1/postmaster.pid"\n')
        self.ctl.chmod(0o755)

    def dashboard(self):
        return provision.dashboard(self.tree, self.root, live=self.live)

    def test_cache_wins_and_is_keyed_by_full_lock_hash(self):
        (self.cache / "node_modules").mkdir(parents=True)
        (self.cache / "package-lock.json").write_bytes(self.lock)
        (self.live / "node_modules").mkdir()
        (self.live / "package-lock.json").write_bytes(self.lock)
        with patch.object(build_runner, "_required") as npm:
            self.assertEqual(self.dashboard(), "cache:" + self.key)
        npm.assert_not_called()
        self.assertEqual((self.dash / "node_modules").resolve(), self.cache / "node_modules")

    def test_matching_live_lock(self):
        (self.live / "node_modules").mkdir()
        (self.live / "package-lock.json").write_bytes(self.lock)
        with patch.object(build_runner, "_required") as npm:
            self.assertEqual(self.dashboard(), "matching-lock:" + str(self.live))
        npm.assert_not_called()
        self.assertEqual((self.dash / "node_modules").resolve(), self.live / "node_modules")

    def test_live_lock_mismatch_never_links_and_failed_install_is_not_cached(self):
        (self.live / "node_modules").mkdir()
        (self.live / "package-lock.json").write_text("different lock")
        with patch.object(build_runner, "_required", side_effect=build_runner.Undetermined("offline")):
            with self.assertRaises(build_runner.Undetermined):
                self.dashboard()
        self.assertFalse((self.dash / "node_modules").is_symlink())
        self.assertFalse(self.cache.exists())

    def test_cache_lock_mismatch_refuses(self):
        (self.cache / "node_modules").mkdir(parents=True)
        (self.cache / "package-lock.json").write_text("different lock")
        with patch.object(build_runner, "_required") as npm:
            with self.assertRaisesRegex(build_runner.Undetermined, "lock mismatch"):
                self.dashboard()
        npm.assert_not_called()
        self.assertFalse((self.dash / "node_modules").exists())

    def test_npm_ci_populates_cache_atomically_then_reuses_it(self):
        def npm(argv, **kw):
            self.assertEqual(argv, ["npm", "ci", "--prefer-offline"])
            self.assertNotIn("DATABASE_URL", kw["env"])
            (kw["cwd"] / "node_modules").mkdir()
            (kw["cwd"] / "node_modules/fixture").write_text("installed")
        with patch.object(build_runner, "_required", side_effect=npm) as install:
            self.assertEqual(self.dashboard(), "cache:" + self.key)
            (self.dash / "node_modules").unlink()
            self.assertEqual(self.dashboard(), "cache:" + self.key)
        self.assertEqual(install.call_count, 1)
        self.assertEqual((self.dash / "node_modules/fixture").read_text(), "installed")

    def test_no_dashboard_lock_does_nothing(self):
        (self.dash / "package-lock.json").unlink()
        with patch.object(build_runner, "_required") as npm:
            self.assertEqual(self.dashboard(), "none")
        npm.assert_not_called()
        self.assertFalse(self.root.exists())

    def run_check(self, command, **kw):
        with patch.object(provision, "PRIVATE_PG", self.pg), \
                patch.object(provision, "PG_CTL", str(self.ctl)):
            return provision.run_check(self.tree, "fixture-base", command, self.record, **kw)

    def assert_cleaned(self):
        pg = pathlib.Path(self.pglog.read_text())
        self.assertEqual(pg.parent, pathlib.Path("/var/tmp"))
        self.assertFalse(pg.exists())
        self.assertEqual(self.stoplog.read_text(), str(pg / "data"))

    def test_no_markers_no_pg_and_ambient_dsns_removed(self):
        with patch.dict(os.environ, DATABASE_URL="forbidden", SUPABASE_DB_URL="forbidden",
                        AS_LOCAL_PG_ADMIN="forbidden", CUSTOM_DSN="forbidden", PGHOST="forbidden"):
            # Assemble names so the test command itself has no PG marker.
            code, output = self.run_check('python3 -c \'import os; '
                'assert not any("DSN" in k or "DATABASE" in k or k.startswith("PG") '
                'or k == "AS_LOCAL_" + "PG_ADMIN" for k in os.environ); '
                'assert os.environ["BASE"] == "fixture-base"\'')
        self.assertEqual((code, output), (0, b""))
        self.assertFalse(self.pglog.exists())
        self.assertEqual(json.loads(self.record.read_text())["pg"], "none")

    def test_each_marker_gets_check_scoped_admin_and_cleanup(self):
        for marker in ("pg_scratch", "live_db", "AS_LOCAL_PG_ADMIN"):
            with self.subTest(marker=marker), patch.dict(os.environ, DATABASE_URL="forbidden",
                    SUPABASE_DB_URL="forbidden", AS_LOCAL_PG_ADMIN="forbidden", PGHOST="forbidden"):
                command = f'# {marker}\npython3 -c \'import os; '
                command += 'assert os.environ["AS_LOCAL_" + "PG_ADMIN"].startswith("postgresql://albert:"); '
                command += 'assert os.environ["DATABASE_URL"] == os.environ["SUPABASE_DB_URL"] == os.environ["AS_LOCAL_" + "PG_ADMIN"]\''
                code, output = self.run_check(command)
                self.assertEqual((code, output), (0, b""))
                self.assertEqual(os.environ["AS_LOCAL_PG_ADMIN"], "forbidden")
                self.assert_cleaned()
                summary = json.loads(self.record.read_text())
                self.assertEqual(summary["pg"], "loopback")
                self.assertNotIn("postgresql", self.record.read_text())

    def test_cleanup_after_failed_check_timeout_and_interrupt(self):
        code, _ = self.run_check('# live_db\nexit 7')
        self.assertEqual(code, 7)
        self.assert_cleaned()
        with self.assertRaises(build_runner.WallCap):
            self.run_check('# pg_scratch\nsleep 30', timeout=0.15)
        self.assert_cleaned()
        original = build_runner._process
        def interrupted(argv, **kw):
            if argv[0] == "/bin/bash" and argv[1] == "-euo":
                os.kill(os.getpid(), signal.SIGTERM)
            return original(argv, **kw)
        with patch.object(build_runner, "_process", side_effect=interrupted):
            with self.assertRaisesRegex(build_runner.Undetermined, "interrupted"):
                self.run_check('# live_db\ntrue')
        self.assert_cleaned()

    def test_cleanup_after_failed_start_and_malformed_endpoint(self):
        for ending in ('exit 1\n', 'echo "invalid output"\n'):
            self.pg.write_text('set -eu\nmkdir -p "$1/data"\ntouch "$1/data/postmaster.pid"\n'
                              f'printf "%s" "$1" > "{self.pglog}"\n' + ending)
            with self.assertRaises(build_runner.Undetermined):
                self.run_check('# live_db\ntrue')
            self.assert_cleaned()

    def test_cleanup_refuses_outside_var_tmp_and_symlinks(self):
        for path in (self.top, pathlib.Path("/var/tmp"), pathlib.Path("/var/tmp/../owed-pg-bad")):
            with self.assertRaisesRegex(build_runner.Undetermined, "unsafe"):
                provision._stop_pg(path, {})
        link = pathlib.Path(tempfile.mktemp(prefix="owed-pg-", dir="/var/tmp"))
        link.symlink_to(self.top)
        self.addCleanup(link.unlink)
        with self.assertRaisesRegex(build_runner.Undetermined, "unsafe"):
            provision._stop_pg(link, {})
        self.assertTrue(self.top.exists())


if __name__ == "__main__":
    unittest.main()
