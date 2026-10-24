#!/usr/bin/env python3
"""Hermetic maintenance regressions: real git objects, deterministic resource seams."""
import contextlib
import hashlib
import io
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

import backup

REAL_GIT = shutil.which("git")
CAPS = ["pack.windowMemory=256m", "pack.threads=1",
        "pack.deltaCacheSize=64m", "core.packedGitLimit=256m"]
NOT_KEEPING_UP = "backup-maintain: not keeping up (see ledger-repack.log)\n"

# The stub always delegates to real git except for the requested failure. It
# records argv and chunk input independently of maintenance's implementation.
STUB = r'''#!/usr/bin/python3
import json, os, pathlib, subprocess, sys, time
c = json.loads(pathlib.Path(os.environ['STUB_CONFIG']).read_text())
a = sys.argv[1:]
i = 0
while a[i] in ('-C', '-c'):
    i += 2
cmd = a[i]
data = sys.stdin.read() if cmd in ('pack-objects', 'cat-file') else None
with open(c['log'], 'a') as f:
    f.write(json.dumps({'cmd': cmd, 'args': a, 'oids': data}) + '\n')
if cmd == c.get('fail'):
    print('stub diagnostic', file=sys.stderr)
    sys.exit(9)
if cmd == 'prune-packed' and c.get('fail_after_pack') and pathlib.Path(c['packed']).exists():
    sys.exit(8)
if cmd == c.get('sleep'):
    if cmd == 'pack-objects':
        packdir = pathlib.Path(a[-1]).parent
        for name in ('tmp_stub', 'pack-' + 'a'*40 + '.pack', 'pack-' + 'a'*40 + '.rev'):
            (packdir / name).write_text('partial')
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        pathlib.Path(c['child']).write_text(str(child.pid))
    if c.get('clock'):
        time.sleep(.2)
        pathlib.Path(c['clock']).write_text('11')
    time.sleep(60)
if cmd == 'cat-file' and c.get('late'):
    pathlib.Path(c['clock']).write_text('8')
p = subprocess.run([c['git'], *a], input=data, text=True)
if cmd == 'pack-objects' and p.returncode == 0:
    pathlib.Path(c['packed']).touch()
    if c.get('drop_free'):
        pathlib.Path(c['free']).write_text('0')
if cmd == 'prune-packed' and c.get('after_clock') and pathlib.Path(c['packed']).exists():
    pathlib.Path(c['clock']).write_text('11')
if cmd == c.get('drop_memory_after'):
    pathlib.Path(c['meminfo']).write_text('MemAvailable: 1 kB\n')
sys.exit(p.returncode)
'''


class MaintainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="backup-maintain-")
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.repo = self.base / "clone"
        self.repo.mkdir()
        self.env = {
            "DOIT_BACKUP_MAINTAIN_TEST": "1", "DOIT_BACKUP_LOOSE_MAX": "1",
            "DOIT_BACKUP_MAINTAIN_CHUNK": "3", "DOIT_BACKUP_MAINTAIN_CHUNK_MIN": "1",
            "DOIT_BACKUP_MAINTAIN_CHUNK_BYTES": str(128 * 1024**2),
            "DOIT_BACKUP_MAINTAIN_TIMEOUT_S": "10",
            "DOIT_BACKUP_MAINTAIN_LOCK_WAIT_S": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test",
            "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test",
        }
        self.mem = self.base / "meminfo"
        self.mem.write_text("MemAvailable: 99999999 kB\n")
        self.env["DOIT_BACKUP_MAINTAIN_TEST_MEMINFO"] = str(self.mem)
        self.free = self.base / "free"
        self.free.write_text(str(100 * 1024**3))
        self.env["DOIT_BACKUP_MAINTAIN_TEST_FREE"] = str(self.free)
        self.git("init", "-q")
        self.git("config", "gc.auto", "0")
        self.git("config", "maintenance.auto", "false")
        self.config = {"git": REAL_GIT, "log": str(self.base / "calls"),
                       "packed": str(self.base / "packed"), "free": str(self.free),
                       "child": str(self.base / "child")}

    def git(self, *args, data=None):
        p = subprocess.run([REAL_GIT, "-C", str(self.repo), *args], input=data,
                           capture_output=True, env={**os.environ, **self.env})
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.decode().strip()

    def seed(self, count=6, size=200):
        for i in range(count):
            (self.repo / f"file-{i}").write_bytes(os.urandom(size))
            self.git("add", ".")
            self.git("commit", "-qm", f"commit-{i}")

    def objects(self):
        return set(line.split()[0] for line in self.git(
            "rev-list", "--objects", "--all", "--reflog", "--indexed-objects").splitlines())

    def loose(self):
        return {p.relative_to(self.repo).as_posix(): p.read_bytes()
                for p in (self.repo / ".git/objects").glob("[0-9a-f][0-9a-f]/*")}

    def packs(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (self.repo / ".git/objects/pack").iterdir()}

    def stub(self, **options):
        self.config.update(options)
        (self.base / "stub.json").write_text(json.dumps(self.config))
        bindir = self.base / "bin"
        bindir.mkdir(exist_ok=True)
        exe = bindir / "git"
        exe.write_text(STUB)
        exe.chmod(0o755)
        self.env.update(PATH=str(bindir) + os.pathsep + os.environ["PATH"],
                        STUB_CONFIG=str(self.base / "stub.json"))

    def clock(self):
        clock = self.base / "clock"
        clock.write_text("0")
        self.env["DOIT_BACKUP_MAINTAIN_TEST_CLOCK"] = str(clock)
        return str(clock)

    def calls(self, cmd=None):
        p = pathlib.Path(self.config["log"])
        rows = [json.loads(s) for s in p.read_text().splitlines()] if p.exists() else []
        return [r for r in rows if cmd is None or r["cmd"] == cmd]

    def state(self, **values):
        p = self.repo / "backup-maintain-state.json"
        if values:
            p.write_text(json.dumps(values))
        return json.loads(p.read_text())

    def run_maintain(self, rc=0, stopped="none"):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = backup.maintain(self.repo)
        self.assertEqual(result, rc, (out.getvalue(), err.getvalue()))
        summary = [s for s in out.getvalue().splitlines() if s.startswith("backup-maintain:")][-1]
        fields = dict(word.split("=", 1) for word in summary.split()[1:])
        self.assertEqual(fields["stopped"], stopped)
        if rc == 3:
            self.assertEqual(err.getvalue(), NOT_KEEPING_UP)
        elif rc == 0:
            self.assertEqual(err.getvalue(), "")
        else:
            self.assertEqual(len(err.getvalue().splitlines()), 1)
        return fields, out.getvalue(), err.getvalue()

    def test_real_chunks_preserve_objects_and_existing_packs(self):
        self.seed(1)
        self.git("repack", "-d")
        old = self.packs()
        self.seed(6)
        unreachable = self.git("hash-object", "-w", "--stdin", data=b"unreachable")
        path = self.repo / ".git/objects" / unreachable[:2] / unreachable[2:]
        before = self.objects()
        self.stub()
        fields, out, _ = self.run_maintain()
        self.assertIn("unlisted_loose=1 reason=outside reachable-unpacked listing; left untouched", out)
        self.assertGreaterEqual(int(fields["chunks_done"]), 2)
        self.assertLess(int(fields["loose_after"]), int(fields["loose_before"]))
        self.assertEqual(before, self.objects())
        self.git("fsck", "--connectivity-only")
        self.assertTrue(path.exists())
        self.assertTrue(all(self.packs()[p] == digest for p, digest in old.items()))
        for call in self.calls():
            self.assertEqual([call["args"][i+1] for i, a in enumerate(call["args"]) if a == "-c"], CAPS)

    def test_threshold_empty_listing_and_opening_prune(self):
        self.seed(1)
        duplicates = self.loose()
        self.git("repack", "-d")
        for name, data in duplicates.items():
            (self.repo / name).parent.mkdir(exist_ok=True)
            (self.repo / name).write_bytes(data)
        self.env["DOIT_BACKUP_LOOSE_MAX"] = "9999"
        packs = self.packs()
        self.assertEqual(self.run_maintain()[0]["chunks_done"], "0")
        self.assertEqual(self.packs(), packs)
        self.assertEqual(self.loose(), {})
        self.git("update-ref", "-d", "HEAD")
        self.git("reflog", "expire", "--expire=now", "--all")
        (self.repo / ".git/index").unlink()
        self.git("hash-object", "-w", "--stdin", data=b"unreachable")
        self.env["DOIT_BACKUP_LOOSE_MAX"] = "1"
        self.state(history=[1, 2, 3])
        fields, _, _ = self.run_maintain()
        self.assertEqual((fields["chunks_done"], fields["behind"]), ("0", "no"))

    def test_commits_last_and_budget_resume_and_growth(self):
        self.seed()
        before = self.objects()
        self.state(chunk=2, chunk_bytes=64 * 1024**2)
        self.stub(clock=self.clock(), after_clock=True)
        fields, _, _ = self.run_maintain(stopped="budget")
        self.assertEqual(fields["chunks_done"], "1")
        self.assertGreater(int(fields["chunks_left"]), 0)
        self.assertEqual((self.state()["chunk"], self.state()["chunk_bytes"]), (3, 128 * 1024**2))
        for oid in self.calls("pack-objects")[0]["oids"].splitlines():
            self.assertNotEqual(self.git("cat-file", "-t", oid), "commit")
        remaining = self.git("rev-list", "--objects", "--all", "--unpacked")
        self.assertTrue(remaining)
        pathlib.Path(self.config["clock"]).write_text("0")
        self.stub(after_clock=False)
        self.run_maintain()
        self.assertEqual(self.objects(), before)
        self.assertEqual(self.git("rev-list", "--objects", "--all", "--unpacked"), "")

    def test_budget_at_default_bounds_and_cli_entry(self):
        self.seed()
        self.stub(clock=self.clock(), after_clock=True)
        fields, _, _ = self.run_maintain(stopped="budget")
        self.assertEqual((fields["chunk"], fields["chunk_bytes"]), ("3", str(128 * 1024**2)))
        pathlib.Path(self.config["clock"]).write_text("0")
        self.stub(after_clock=False)
        p = subprocess.run([sys.executable, str(pathlib.Path(backup.__file__)), "maintain"],
                           env={**os.environ, **self.env, "DOIT_ROOT": str(self.repo)},
                           cwd=self.base, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("chunks_left=0", p.stdout)
        self.assertEqual(self.git("rev-list", "--objects", "--all", "--unpacked"), "")

    def test_space_stops_and_tunes(self):
        self.seed()
        self.free.write_text("0")
        self.run_maintain(3, "space")
        self.assertEqual(self.state()["chunk"], 1)
        self.assertEqual(self.state()["chunk_bytes"], 64 * 1024**2)
        self.assertEqual(self.packs(), {})
        self.free.write_text(str(100 * 1024**3))
        self.state(chunk=3, chunk_bytes=128 * 1024**2)
        self.stub(drop_free=True)
        fields, _, _ = self.run_maintain(stopped="space")
        self.assertEqual(fields["chunks_done"], "1")
        self.assertEqual(self.state()["chunk"], 1)

    def test_memory_before_any_git_and_gated_seams(self):
        self.seed()
        self.mem.write_text("MemAvailable: 1 kB\n")
        self.stub()
        self.run_maintain(3, "memory")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.state()["chunk"], 3)
        with patch.dict(os.environ, self.env):
            with patch.dict(os.environ, {"DOIT_BACKUP_MAINTAIN_TEST": "0"}):
                self.assertIsNone(backup._maintain_test_path("MEMINFO"))

    def test_memory_rechecked_before_listing_and_each_chunk(self):
        self.seed()
        for cmd in ("count-objects", "pack-objects"):
            self.mem.write_text("MemAvailable: 99999999 kB\n")
            self.stub(drop_memory_after=cmd, meminfo=str(self.mem))
            fields, _, _ = self.run_maintain(3 if cmd == "count-objects" else 0, "memory")
            self.assertEqual(fields["chunks_done"], "0" if cmd == "count-objects" else "1")
            self.assertEqual(self.state()["chunk"], 3)
            if cmd == "count-objects":
                self.assertEqual(self.calls("rev-list"), [])

    def test_git_failures_and_internal_exception(self):
        self.seed()
        for cmd in ("rev-list", "pack-objects", "count-objects", "cat-file", "prune-packed"):
            self.stub(fail=cmd)
            self.assertEqual(self.run_maintain(5)[2], f"backup-maintain: {cmd} failed rc=9\n")
        self.stub(fail="", fail_after_pack=True)
        self.assertEqual(self.run_maintain(5)[2], "backup-maintain: prune-packed failed rc=8\n")
        with patch.object(backup, "_maintain_locked", side_effect=ValueError("fixture")):
            self.assertEqual(self.run_maintain(5)[2], "backup-maintain: internal error ValueError\n")

    def test_listing_and_prune_timeouts(self):
        self.seed()
        self.env["DOIT_BACKUP_MAINTAIN_TIMEOUT_S"] = "0.3"
        for cmd, stopped in (("rev-list", "listing"), ("prune-packed", "prune")):
            self.state(history=[1, 2, 3])
            self.stub(sleep=cmd)
            self.run_maintain(3, stopped)
            self.assertEqual(self.state()["chunk"], 3)
            self.assertEqual(self.state()["history"], [1, 2, 3])

    def test_killed_group_cleanup_and_late_chunk(self):
        self.seed(1)
        self.git("repack", "-d")
        self.seed()
        loose = self.loose()
        packs = self.packs()
        for late, expected in ((False, 1), (True, 3)):
            self.state(chunk=3, chunk_bytes=128 * 1024**2)
            self.stub(sleep="pack-objects", clock=self.clock(), late=late)
            self.run_maintain(3, "killed")
            self.assertEqual(self.state()["chunk"], expected)
            self.assertEqual(self.packs(), packs)
            self.assertEqual(self.loose(), loose)
            pid = pathlib.Path(self.config["child"]).read_text()
            status = pathlib.Path("/proc") / pid / "stat"
            try:
                self.assertIn(status.read_text().split()[2], {"Z", "X"})
            except FileNotFoundError:
                pass

    def test_rss_kill(self):
        self.seed()
        rss = self.base / "rss"
        rss.write_text(str(2 * 1024**3))
        self.env["DOIT_BACKUP_MAINTAIN_TEST_RSS"] = str(rss)
        self.stub(sleep="pack-objects")
        self.run_maintain(3, "rss")
        self.assertEqual(self.state()["chunk"], 1)
        self.assertEqual(self.packs(), {})

    def test_behind_and_no_progress_precedence(self):
        self.seed()
        self.state(history=[1, 2, 3])
        fields, _, _ = self.run_maintain(3)
        self.assertEqual(fields["behind"], "yes")
        self.assertGreater(int(fields["chunks_done"]), 0)
        self.seed(7)
        self.state(history=[1, 2, 3])
        self.free.write_text("0")
        self.assertEqual(self.run_maintain(3, "space")[0]["behind"], "yes")

    def test_lock_wait_and_timeout_leave_history_and_objects(self):
        self.seed()
        for duration, expected in ((2, 0), (10, 3)):
            self.state(history=[1, 2, 3], chunk=3, chunk_bytes=128 * 1024**2)
            holder = subprocess.Popen([sys.executable, "-c",
                "import fcntl,sys,time; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX); "
                "print('locked',flush=True); time.sleep(float(sys.argv[2]))",
                str(self.repo / "backup.lock"), str(duration)], stdout=subprocess.PIPE, text=True)
            self.addCleanup(holder.stdout.close)
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            loose, packs = self.loose(), self.packs()
            self.env["DOIT_BACKUP_MAINTAIN_LOCK_WAIT_S"] = "4" if expected == 0 else "1"
            try:
                if expected == 0:
                    self.state(history=[])
                started = time.monotonic()
                self.run_maintain(expected, "lock" if expected else "none")
                self.assertGreaterEqual(time.monotonic() - started, 1)
                if expected:
                    self.assertEqual(self.state()["history"], [1, 2, 3])
                    self.assertEqual((self.loose(), self.packs()), (loose, packs))
            finally:
                holder.terminate()
                holder.wait()

    def test_byte_bounds_and_recovery(self):
        self.seed(3, 700_000)
        self.env["DOIT_BACKUP_MAINTAIN_CHUNK_BYTES"] = "1000000"
        self.env["DOIT_BACKUP_MAINTAIN_CHUNK"] = "99"
        sizes = {name.split("/")[-2] + name.split("/")[-1]: len(data)
                 for name, data in self.loose().items()}
        self.stub()
        self.run_maintain()
        totals = [sum(sizes[oid] for oid in call["oids"].splitlines()) for call in self.calls("pack-objects")]
        self.assertGreater(len(totals), 1)
        self.assertTrue(all(total <= 1_000_000 for total in totals))
        self.seed(4)
        self.env["DOIT_BACKUP_MAINTAIN_CHUNK"] = "2"
        self.env["DOIT_BACKUP_MAINTAIN_CHUNK_BYTES"] = str(128 * 1024**2)
        self.state(chunk=1, chunk_bytes=64 * 1024**2)
        self.run_maintain()
        self.assertEqual((self.state()["chunk"], self.state()["chunk_bytes"]), (2, 128 * 1024**2))

    def test_byte_bound_space_stop_makes_next_chunk_smaller(self):
        self.seed(5, 700_000)
        self.env.update(DOIT_BACKUP_MAINTAIN_CHUNK="99", DOIT_BACKUP_MAINTAIN_CHUNK_BYTES=str(128 * 1024**2),
                        DOIT_BACKUP_MAINTAIN_TIMEOUT_S="120")
        # Use enough incompressible loose blobs to cross the production 64 MiB
        # floor, without committing huge working-tree files into the fixture.
        for i in range(5):
            oid = self.git("hash-object", "-w", "--stdin", data=os.urandom(14 * 1024**2))
            self.git("update-index", "--add", "--cacheinfo", f"100644,{oid},large-{i}")
        sizes = {name.split("/")[-2] + name.split("/")[-1]: len(data)
                 for name, data in self.loose().items()}
        self.free.write_text(str(2 * 1024**3 + 65 * 1024**2))
        self.stub()
        self.run_maintain(3, "space")
        self.assertEqual(self.state()["chunk_bytes"], 64 * 1024**2)
        self.free.write_text(str(100 * 1024**3))
        self.run_maintain()
        totals = [sum(sizes[oid] for oid in call["oids"].splitlines()) for call in self.calls("pack-objects")]
        self.assertGreater(len(totals), 1)
        self.assertTrue(all(total <= 64 * 1024**2 for total in totals))


if __name__ == "__main__":
    unittest.main()
