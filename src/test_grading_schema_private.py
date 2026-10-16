#!/usr/bin/env python3
"""R3 transport regressions. All DB processes are stubs; no shared PG access."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import grading_env as grading


class PrivateSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="schema-private-")
        self.addCleanup(self.tmp.cleanup)
        self.view = Path(self.tmp.name)
        (self.view / "tree").mkdir()
        self.dsn = "postgresql://fixture@/scratch?host=/fixture/socket&port=23451"
        self.admin = "postgresql://albert:" + "a" * 32 + "@127.0.0.1:23452/postgres"
        self.row = {"db_env": ["DATABASE_URL"], "admin_env": ["AS_LOCAL_PG_ADMIN"],
                    "db_setup": {"argv": ["schema-fixture"], "cwd": "."}}

    def test_schema_gets_only_proven_endpoints(self):
        ambient = {"AS_LOCAL_PG_ADMIN": "forbidden-shared-admin", "DATABASE_URL": "forbidden-database",
                   "PGHOST": "forbidden-host", "PGPORT": "5432", "PGSERVICE": "forbidden-service",
                   "PGPASSFILE": "/forbidden-password-file", "OTHER_DSN": "forbidden-dsn",
                   "SUPABASE_DB_URL_DIRECT": "forbidden-prod", "SCRATCH_PG_URL": "forbidden-scratch"}
        calls = []
        def run(argv, **kw):
            calls.append((argv, kw))
            env = kw["env"]
            self.assertEqual(env["DATABASE_URL"], self.dsn)
            self.assertEqual(env["AS_LOCAL_PG_ADMIN"], self.admin)
            self.assertTrue(all(value not in env.values() for value in ambient.values()))
            self.assertFalse(any(key.startswith("PG") for key in env))
            return subprocess.CompletedProcess(argv, 0, "four fixture criteria pass\n", "")
        with patch.dict(os.environ, ambient), patch.object(grading, "_run", side_effect=run):
            self.assertEqual(grading._prove_db_schema(self.view, self.row, "fixture", self.dsn,
                                                    admin_dsn=self.admin), (True, None))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["cwd"], str(self.view / "tree"))

    def test_missing_private_endpoint_never_runs_setup(self):
        with patch.object(grading, "_run", side_effect=AssertionError("must not connect")):
            for dsn, admin in ((None, self.admin), (self.dsn, None), (None, None)):
                self.assertFalse(grading._prove_db_schema(self.view, self.row, "fixture", dsn,
                                                         admin_dsn=admin)[0])

    def test_preflight_threads_admin_and_blocks_on_failure(self):
        for fails in (False, True):
            with self.subTest(fails=fails), \
                    patch.object(grading, "_spec_verify_text", return_value=("", "")), \
                    patch.object(grading, "required", return_value=["db", "db-schema"]), \
                    patch.object(grading, "_project_row", return_value=self.row), \
                    patch.object(grading, "_look_toml", return_value={}), \
                    patch.object(grading, "_prove_db", side_effect=[
                        (True, None, self.dsn), (False, "private startup failed", None) if fails
                        else (True, None, self.admin)]) as db, \
                    patch.object(grading, "_prove_db_schema", return_value=(True, None)) as schema:
                result = grading.preflight("grader", "L-spec-fixture", self.view, "fixture", repo=self.view)
                self.assertEqual(db.call_args_list[1].kwargs, {"admin": True, "avoid_port": "23451"})
                if fails:
                    self.assertEqual([cap for cap, _ in result], ["db", "db-schema"])
                    schema.assert_not_called()
                    self.assertNotIn("AS_LOCAL_PG_ADMIN", grading.pane_env(self.view))
                else:
                    self.assertEqual(result, [])
                    schema.assert_called_once_with(self.view, self.row, "fixture", self.dsn, admin_dsn=self.admin)
                    self.assertEqual(grading.pane_env(self.view)["AS_LOCAL_PG_ADMIN"], self.admin)

    def test_private_script_contract(self):
        def run(argv, **kw):
            self.assertEqual(argv, ["/bin/bash", str(grading.DOIT_SRC / "scripts/private-pg.sh"),
                                    str(self.view / "pg-admin"), "loopback"])
            self.assertNotIn("AS_LOCAL_PG_ADMIN", kw["env"])
            self.assertNotIn("PGPORT", kw["env"])
            return subprocess.CompletedProcess(argv, 0, "ADMIN_DSN='" + self.admin + "'\n", "")
        with patch.dict(os.environ, AS_LOCAL_PG_ADMIN="forbidden", PGPORT="5432"), \
                patch.object(grading, "_run", side_effect=run):
            self.assertEqual(grading._prove_db(self.view, admin=True), (True, None, self.admin))


if __name__ == "__main__":
    unittest.main()
