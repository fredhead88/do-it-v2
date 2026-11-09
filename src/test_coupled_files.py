#!/usr/bin/env python3
"""Non-test coupled files: rule scan, hook, hold and release, against disposable repos and real ledgers."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import coupled_files as cf
import effective_writes
import spec_preflight as sp

SOURCE = Path(__file__).resolve().parent
PINNED = [cf.SCRATCH, cf.FINGERPRINT, cf.ISOLATION, cf.ALLOWLIST]


def spec(writes, prose=""):
    return ("project: albert-scott\n" + prose + "\n## Interfaces\nWrites:\n" + "".join(f"- {w}\n" for w in writes)
            + "\n## Acceptance criteria\nAC1 [backend]: works\n## Verification\n```bash\n"
            "/usr/bin/python3 -c 'import json'\n```\n")


class Repo(unittest.TestCase):
    sid = "L-spec-9001"
    project = "albert-scott"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="coupled-files-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "ledger"
        self.repo = self.root / "repos" / self.project
        self.repo.mkdir(parents=True)
        (self.root / "content").mkdir()
        (self.root / "events").mkdir()
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
                    "DOIT_ROOT": str(self.root), "DOIT_PROJECT": "", "DOIT_READ_CACHE": "0",
                    "PYTHONPATH": str(SOURCE), "DOIT_SCRATCH": str(Path(self.temp.name) / "scratch")}
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"DOIT_SCRATCH": self.env["DOIT_SCRATCH"]}).start()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        for path in PINNED:
            self.file(path, "x\n")
        self.file("api/app/routers/old.py", "x = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.now = datetime.now(timezone.utc)
        self.serial = 0

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True,
                              check=True, timeout=30).stdout.strip()

    def file(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def written(self, text):
        self.serial += 1
        path = self.root / "content" / (self.sid + ".md")
        path.write_text(text)
        stamp = datetime.now(timezone.utc) - (timedelta(minutes=10) if self.serial == 1 else timedelta())
        self.now = datetime.now(timezone.utc) + timedelta(seconds=1)
        event = dict(type="spec-written", subject=self.sid, project=self.project,
                     path=str(path), footprint=[], ts=stamp.isoformat())
        with (self.root / "events/L-spec-writer-fixture.jsonl").open("a") as out:
            out.write(json.dumps(event) + "\n")

    def read(self):
        code = "import json, fold; print(json.dumps(fold.fold(fold.read_events())[3]))"
        out = subprocess.run(["/usr/bin/python3", "-c", code], capture_output=True, text=True,
                             check=True, timeout=30, env=self.env).stdout
        return json.loads(out).get(self.sid, [])

    def tick(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(sp.tick(root=self.root, now=self.now), 0)

    def of(self, kind):
        return [e for e in self.read() if e["type"] == kind]


class ScanTests(Repo):
    def rows(self, text, project="albert-scott"):
        return [(r["rule"], r["file"], r["mode"]) for r in cf.scan(text, project, self.repo)]

    def test_scratch_marked_test_is_granted(self):
        self.assertEqual(self.rows(spec(["tests/test_x.py"], "Uses pg_scratch.")),
                         [("pg-scratch-inventory", cf.SCRATCH, "grant")])
        self.assertEqual(self.rows(spec(["api/tests/test_x.py"], "pg_scratch")),
                         [("pg-scratch-inventory", cf.SCRATCH, "grant")])
        self.assertEqual(self.rows(spec(["tests/test_x.py"], "no marker")), [])
        self.assertEqual(self.rows(spec(["src/x.py"], "pg_scratch")), [])

    def test_migration_grants_fingerprint_and_asks_isolation(self):
        self.assertEqual(self.rows(spec(["api/alembic_supabase/versions/0001_x.py"])),
                         [("migration-fingerprint", cf.FINGERPRINT, "grant"),
                          ("migration-branch-isolation", cf.ISOLATION, "ask")])

    def test_new_router_asks_but_existing_router_does_not(self):
        self.assertEqual(self.rows(spec(["api/app/routers/new.py"])),
                         [("router-auth-allowlist", cf.ALLOWLIST, "ask")])
        self.assertEqual(self.rows(spec(["api/app/routers/old.py"])), [])

    def test_satisfied_rule_does_not_fire(self):
        text = spec(["api/alembic_supabase/versions/0001_x.py", cf.FINGERPRINT, cf.ISOLATION,
                     "tests/test_x.py", cf.SCRATCH, "api/app/routers/new.py", cf.ALLOWLIST], "pg_scratch")
        self.assertEqual(self.rows(text), [])
        self.assertEqual(self.rows(spec(["api/alembic_supabase/versions/0001_x.py", cf.FINGERPRINT])),
                         [("migration-branch-isolation", cf.ISOLATION, "ask")])

    def test_project_without_rules_is_empty(self):
        self.assertEqual(self.rows(spec(["api/alembic_supabase/versions/0001_x.py"]), "do-it-v2"), [])

    def test_missing_file_is_undetermined_never_granted(self):
        (self.repo / cf.SCRATCH).unlink()
        rows = cf.scan(spec(["tests/test_x.py"], "pg_scratch"), "albert-scott", self.repo)
        self.assertEqual([(r["file"], r["mode"]) for r in rows], [(cf.SCRATCH, "undetermined")])
        rows = cf.scan(spec(["tests/test_x.py"], "pg_scratch"), "albert-scott", None)
        self.assertEqual([r["mode"] for r in rows], ["undetermined"])


class HookTests(Repo):
    def seed(self):
        self.written(spec(["tests/test_x.py"], "pg_scratch"))

    def test_grant_precedes_build_started_once_per_revision(self):
        self.seed()
        self.tick()
        self.tick()
        grants = self.of("coupled-files-granted")
        self.assertEqual(len(grants), 1)
        grant = grants[0]
        self.assertEqual(grant["actor"], "spec-preflight")
        self.assertEqual([f["file"] for f in grant["files"]], [cf.SCRATCH])
        self.assertEqual(grant["files"][0]["rule"], "pg-scratch-inventory")
        self.assertTrue(grant["why"].strip())
        written = self.of("spec-written")[-1]
        self.assertEqual(grant["spec_written_src"], written["_src"])
        self.assertFalse(self.of("build-started"))
        self.assertEqual(self.of("spec-preflight")[-1].get("refused"), None)
        grant_text = spec(["tests/test_x.py"], "pg_scratch")
        self.assertIn(cf.SCRATCH, effective_writes.effective_grant(self.sid, grant_text, self.read()))

    def test_ask_row_is_refused_not_granted_and_not_duplicated(self):
        self.written(spec(["api/app/routers/new.py"]))
        self.tick()
        self.tick()
        self.assertEqual(self.of("coupled-files-granted"), [])
        preflights = self.of("spec-preflight")
        self.assertEqual(len(preflights), 1)
        refused = preflights[0]["refused"]
        self.assertEqual([(r["check"], r["id"], r["seconds"]) for r in refused],
                         [("coupled-files", cf.ALLOWLIST, 0.0)])
        self.assertIn("router-auth-allowlist", refused[0]["reason"])
        self.assertIn("Writes", refused[0]["reason"])

    def test_hook_registered_and_exception_leaves_preflight_undetermined(self):
        self.assertTrue(any(h.__name__ == "_coupled_files_hook" for h in sp.after_run))
        self.seed()
        with mock.patch.object(cf, "scan", side_effect=RuntimeError("fixture")):
            self.tick()
        self.assertEqual(self.of("coupled-files-granted"), [])
        self.assertTrue(any(u.startswith("hook ") for u in self.of("spec-preflight")[-1]["undetermined"]))

    def test_missing_file_is_undetermined_and_not_granted(self):
        (self.repo / cf.SCRATCH).unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "drop")
        self.seed()
        self.tick()
        self.assertEqual(self.of("coupled-files-granted"), [])
        self.assertTrue(any("coupled-files" in u for u in self.of("spec-preflight")[-1]["undetermined"]))

    def test_new_revision_grants_again(self):
        self.seed()
        self.tick()
        self.written(spec(["tests/test_x.py"], "pg_scratch again"))
        self.tick()
        grants = self.of("coupled-files-granted")
        self.assertEqual(len(grants), 2)
        self.assertNotEqual(grants[0]["spec_written_src"], grants[1]["spec_written_src"])


class HoldTests(Repo):
    def test_ask_holds_then_amend_releases(self):
        self.written(spec(["api/app/routers/new.py"]))
        self.assertIsNone(cf.held_reason(self.read(), self.sid))
        self.tick()
        self.assertEqual(cf.held_reason(self.read(), self.sid),
                         f"held: spec-preflight refused (coupled-files: {cf.ALLOWLIST})")
        self.written(spec(["api/app/routers/new.py", cf.ALLOWLIST]))
        self.assertIsNone(cf.held_reason(self.read(), self.sid))
        self.tick()
        self.assertIsNone(cf.held_reason(self.read(), self.sid))
        self.assertEqual(self.of("spec-preflight")[-1].get("refused"), None)

    def test_clean_spec_is_never_held(self):
        self.written(spec(["src/x.py"]))
        self.tick()
        self.assertIsNone(cf.held_reason(self.read(), self.sid))


if __name__ == "__main__":
    unittest.main()
