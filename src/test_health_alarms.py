#!/usr/bin/env python3
"""Health alarms: synthetic boundaries and hermetic store/CLI/probe regressions."""
import contextlib
import copy
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

_ISO = tempfile.TemporaryDirectory(prefix="health-alarms-test-")
os.environ["DOIT_ROOT"] = str(pathlib.Path(_ISO.name) / "root")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold
import health_alarms as health

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
DOIT = pathlib.Path(__file__).resolve().parent.parent / "doit"


def event(kind, age=0, **fields):
    return dict(type=kind, ts=(NOW - timedelta(seconds=age)).isoformat(), **fields)


def healthy():
    return dict(used_fraction=0.5, backup=dict(ts=NOW.isoformat(), age_s=0, ok=True), mirror_ahead_s=0,
                release_log_failed=[], locks={k: dict(held=False, since=None)
                                              for k in ("deploy", "release")},
                disk_free_gb={"/": 100, "scratch": 100})


class HealthTests(unittest.TestCase):
    def alarm(self, name, *, ev=(), probes=None):
        rows = health.evaluate(list(ev), NOW, probes=healthy() if probes is None else probes)
        for row in rows:
            self.assertEqual(set(row), {"alarm", "severity", "reading", "line"})
            self.assertIn(row["alarm"], health.ALARMS)
            self.assertIn(row["severity"], ("summary", "immediate"))
            self.assertIsInstance(row["reading"], dict)
            self.assertTrue(row["line"])
            self.assertNotIn("\n", row["line"])
            self.assertLessEqual(len(row["line"]), 200)
        return next((r for r in rows if r["alarm"] == name), None)

    def test_ac1(self):
        p = healthy()
        original = copy.deepcopy(p)
        self.assertEqual(health.evaluate([], NOW, probes=p), [])
        evs = [event("backup-failed", 1800), {}, {"ts": "bad", "type": []},
               {"ts": NOW.isoformat(), "type": []}, None, "bad"]
        result = health.evaluate(evs, NOW, probes=p)
        self.assertEqual(result, health.evaluate(evs, NOW, probes=p))
        shifted = [dict(evs[0], ts="2020-10-18T11:30:00+00:00")]
        self.assertEqual(result, health.evaluate(shifted, NOW.replace(year=2020), probes=p))
        self.assertEqual(p, original)
        feeds = {"backup": "mirror-stale", "mirror_ahead_s": "mirror-stale",
                 "release_log_failed": "release-failed", "locks": "deploy-lock-stale",
                 "disk_free_gb": "disk-low"}
        for probe, name in feeds.items():
            for absent in (True, False):
                p = healthy()
                if absent:
                    del p[probe]
                else:
                    p[probe] = None
                row = self.alarm(name, probes=p)
                self.assertEqual(row["severity"], "summary")
                self.assertIn(probe, str(row["reading"]["undetermined"]))
        print("AC1 ok")

    def test_ac2(self):
        for age, expected in ((3600, None), (3601, "summary"), (21601, "immediate"), (None, "summary")):
            for field in ("backup", "mirror_ahead_s"):
                p = healthy()
                if field == "backup":
                    p[field]["age_s"] = age
                else:
                    p[field] = age
                row = self.alarm("mirror-stale", probes=p)
                self.assertEqual(row and row["severity"], expected)
                if age is None:
                    self.assertIn("undetermined", row["reading"])
        row = self.alarm("mirror-stale", ev=[event("backup-failed", 1800)])
        self.assertEqual(row["severity"], "summary")
        self.assertEqual(row["reading"]["backup_failed_1h"], 1)
        self.assertIsNone(self.alarm("mirror-stale", ev=[event("backup-failed", 5400)]))
        print("AC2 ok")

    def test_ac3(self):
        for fields, expected in ((dict(rollback="absent", why="check never reported the sha"), "summary"),
                                 (dict(rollback="ran"), "immediate"), (dict(rollback="failed"), "immediate"),
                                 (dict(rollback="absent", why="TIMEOUT after 600s"), "immediate"),
                                 (dict(why="Killed -9"), "immediate")):
            e = event("deploy-failed", 60, **fields)
            row = self.alarm("release-failed", ev=[e])
            self.assertEqual(row["severity"], expected)
            self.assertEqual(row["reading"]["key"], e["ts"])
        self.assertIsNone(self.alarm("release-failed", ev=[event("deploy-failed", 90000)]))
        p = healthy()
        p["release_log_failed"] = [event("log", 120)]
        self.assertEqual(self.alarm("release-failed", probes=p)["severity"], "summary")
        p["release_log_failed"] = None
        self.assertIn("undetermined", self.alarm("release-failed", probes=p)["reading"])
        print("AC3 ok")

    def test_ac4(self):
        self.assertEqual(health.LOCK_STALE_S, 4200)
        for lock in ("deploy", "release"):
            for held, age, expected in ((True, 4201, "immediate"), (True, 4200, None),
                                        (True, 1700, None), (False, 9000, None),
                                        (None, 9000, "summary"), (True, None, "summary")):
                p = healthy()
                p["locks"][lock] = dict(held=held, since=event("start", age)["ts"] if age else None)
                row = self.alarm("deploy-lock-stale", probes=p)
                self.assertEqual(row and row["severity"], expected)
                if expected == "summary":
                    self.assertIn("undetermined", row["reading"])
        print("AC4 ok")

    def test_ac5(self):
        for unobservable, total, fires in ((4, 10, True), (3, 10, False), (5, 5, True), (4, 4, False)):
            evs = [event("owed-unobservable") for _ in range(unobservable)]
            evs += [event("owed-met" if i % 2 else "owed-failed") for i in range(total - unobservable)]
            row = self.alarm("sweeper-unobservable", ev=evs)
            self.assertEqual(bool(row), fires)
            if row:
                self.assertEqual(row["reading"], dict(share=unobservable / total, unobservable=unobservable, total=total))
        self.assertIsNone(self.alarm("sweeper-unobservable", ev=[event("owed-unobservable", 90000)] * 5))
        print("AC5 ok")

    def test_ac6(self):
        for root, scratch, expected in ((20, 20, None), (14.9, 20, "summary"), (20, 4.9, "immediate"),
                                         (5, 20, "summary"), (15, 15, None), (None, 20, "summary"),
                                         (None, 4, "summary")):
            p = healthy()
            p["disk_free_gb"] = {"/": root, "scratch": scratch}
            row = self.alarm("disk-low", probes=p)
            self.assertEqual(row and row["severity"], expected)
            if root is None:
                self.assertIn("undetermined", row["reading"])
        print("AC6 ok")

    def test_ac7(self):
        evs = [event("grading-preflight-failed", capability="runner")] * 4
        row = self.alarm("preflight-refusals", ev=evs)
        self.assertEqual(row["reading"], dict(count=4, capabilities={"runner": 4}))
        self.assertIsNone(self.alarm("preflight-refusals", ev=evs[:3]))
        for field in ("reason", "why", "capability"):
            self.assertIsNone(self.alarm("preflight-refusals", ev=evs[:3] + [event("evidence-bundle-failed", **{field: "bundle-pending: fixture"})]))
        self.assertIsNone(self.alarm("preflight-refusals", ev=evs[:3] + [event("grading-preflight-failed", 3660)]))
        self.assertIsNotNone(self.alarm("preflight-refusals", ev=evs[:2] + [event("evidence-bundle-failed")] * 2))
        print("AC7 ok")

    def test_ac10(self):
        evs = [event("health-alarm", 7200, alarm="mirror-stale", severity="summary", line="old"),
               event("health-alarm", 60, alarm="mirror-stale", severity="immediate", line="new"),
               event("health-alarm", 60, alarm="disk-low", severity="summary", line="disk"),
               event("health-alarm", 90000, alarm="release-failed", severity="summary", line="old")]
        self.assertEqual(health.summary_lines(evs, NOW), ["HEALTH disk-low summary: disk", "HEALTH mirror-stale immediate: new"])
        for ids in ([f"AC{i}" for i in range(7)], {"suite": [f"AC{i}" for i in range(7)]}):
            base = event("base-red", project="fixture", sha="fixture-revision-long", complete=True, ids=ids)
            line = health.summary_lines([base], NOW)[0]
            self.assertIn("HEALTH BASE-RED fixture fixture-revi:", line)
            self.assertIn("AC4", line)
            self.assertNotIn("AC5", line)
            self.assertIn("+2 more", line)
            self.assertEqual(health.summary_lines([base, event("base-red", -60, project="fixture", complete=True, ids=[])], NOW + timedelta(minutes=1)), [])
        print("AC10 ok")

    def test_ac12(self):
        with tempfile.TemporaryDirectory(prefix="health-lock-") as tmp:
            lock = pathlib.Path(tmp) / "lock"
            lock.touch()
            stat = lock.stat()
            token = f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}:{stat.st_ino}"
            self.assertTrue(health.lock_held(lock, f"1: FLOCK ADVISORY WRITE 123 {token} 0 EOF"))
            self.assertFalse(health.lock_held(lock, ""))
            self.assertIsNone(health.lock_held(lock, None))
            self.assertFalse(health.lock_held(lock.with_name("missing"), ""))
            child_code = "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('held',flush=True); sys.stdin.readline()"
            child = subprocess.Popen([sys.executable, "-c", child_code, str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "held")
                self.assertTrue(health.lock_held(lock))
                import fcntl
                with lock.open("a") as other:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                child.communicate("release\n", timeout=5)
            self.assertFalse(health.lock_held(lock))
            log = pathlib.Path(tmp) / "release.log"
            log.write_text("[2026-10-18T00:41:32Z] FAILED: daily-release exited rc=1\n[2026-10-18T01:00:00Z] SKIPPED: busy\n[2026-10-18T02:00:00Z] START release\n")
            records, start = health.release_log(log, NOW)
            self.assertEqual([r["ts"] for r in records], ["2026-10-18T00:41:32Z"])
            self.assertEqual(start, "2026-10-18T02:00:00Z")
            self.assertEqual(health.release_log(log.with_name("missing"), NOW), (None, None))
            with patch.object(pathlib.Path, "read_text", side_effect=PermissionError):
                self.assertEqual(health.release_log(log, NOW), (None, None))
                self.assertIsNone(health.lock_held(lock))
        source = pathlib.Path(health.__file__).read_text()
        self.assertNotRegex(source, r"fcntl\.(flock|lockf)|LOCK_EX|st_mtime")
        # Every external of the assembled probe is stubbed.
        with patch.object(health.backup, "last_push", return_value=healthy()["backup"]), \
             patch.object(health, "mirror_ahead", return_value=0), \
             patch.object(health, "lock_held", return_value=False), \
             patch.object(health, "release_log", return_value=([], None)), \
             patch.object(fold, "read_events", return_value=[event("deploy-started", 1700)]), \
             patch.object(health.scratch, "root", return_value=pathlib.Path(_ISO.name)), \
             patch.object(health.shutil, "disk_usage", return_value=type("Disk", (), {"free": 100 * 1024**3, "used": 100 * 1024**3, "total": 200 * 1024**3})()):
            probes = health.live_probes(NOW)
            self.assertEqual(probes["locks"]["deploy"]["since"], event("deploy-started", 1700)["ts"])
            self.assertEqual(health.evaluate([], NOW, probes=probes), [])
        print("AC12 ok")


    def test_flow_health_grouping(self):
        rows = [dict(type='health-alarm', alarm='flow-health', severity='summary',
                     measure=measure, day='2026-10-26', line=measure,
                     ts=(NOW - timedelta(minutes=5-i)).isoformat())
                for i, measure in enumerate(('M1', 'M2', 'M3', 'M5'))]
        rows += [dict(rows[0], line='newest M1', ts=NOW.isoformat()),
                 dict(rows[0], day='2026-10-25', line='other day'),
                 dict(rows[0], day='2026-10-24', line='expired', ts=(NOW - timedelta(hours=25)).isoformat())]
        lines = health.summary_lines(rows, NOW)
        self.assertEqual(len(lines), 5)
        self.assertIn('HEALTH flow-health summary: newest M1', lines)
        self.assertNotIn('HEALTH flow-health summary: M1', lines)
        self.assertIn('HEALTH flow-health summary: other day', lines)
        self.assertFalse(any('expired' in line for line in lines))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="health-run-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.events = self.root / "events"
        self.events.mkdir()
        for obj, name, value in ((fold, "ROOT", self.root), (fold, "EVENTS", self.events), (fold, "PROJECT", None)):
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"DOIT_ROOT": str(self.root), "HOME": str(self.root / "home")})
        p.start()
        self.addCleanup(p.stop)
        self.calls = []

    def send(self, argv):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "200\n", "")

    def run_alarm(self, probes, minutes=0, sender=None, **kwargs):
        return health.run(now=NOW + timedelta(minutes=minutes), probes=probes,
                          sender=sender or self.send, **kwargs)

    def rows(self):
        return fold.read_events()

    def test_ac8(self):
        p = healthy()
        p["disk_free_gb"]["/"] = 14
        self.assertEqual(self.run_alarm(p), 0)
        self.assertEqual([f.name for f in self.events.iterdir()], ["L-health-local.jsonl"])
        raw = json.loads((self.events / "L-health-local.jsonl").read_text())
        self.assertNotIn("actor", raw)
        self.assertEqual(raw["subject"], "health-disk-low")
        self.assertEqual(raw["telegram"], "none")
        script = "import fold,json; e=fold.read_events(); print(json.dumps([(r['actor'], fold.admits(r,'health')) for r in e]))"
        result = subprocess.check_output([sys.executable, "-c", script], env={**os.environ, "PYTHONPATH": str(DOIT.parent / "src")}, text=True)
        self.assertEqual(json.loads(result), [["health", None]])
        self.assertEqual(self.run_alarm(p, 10), 0)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.run_alarm(p, 361), 0)
        self.assertEqual(len(self.rows()), 2)
        before = (self.events / "L-health-local.jsonl").read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.run_alarm(p, 800, dry_run=True), 0)
        self.assertEqual(set(json.loads(out.getvalue())), {"alarms", "telegrams"})
        self.assertEqual((self.events / "L-health-local.jsonl").read_bytes(), before)
        with patch.object(fold, "read_events", side_effect=OSError("unreadable")), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_alarm(p), 1)
        self.assertIn("unreadable", err.getvalue())
        self.assertEqual((self.events / "L-health-local.jsonl").read_bytes(), before)
        self.assertEqual(self.calls, [])
        print("AC8 ok")

    def test_ac9(self):
        p = healthy()
        p["locks"]["deploy"] = dict(held=True, since=event("start", 4201)["ts"])
        for minutes, count in ((0, 1), (10, 1), (361, 2)):
            self.assertEqual(self.run_alarm(p, minutes), 0)
            self.assertEqual(len(self.calls), count)
        self.assertTrue(self.calls[0][1].startswith("HEALTH deploy-lock-stale"))
        self.assertLessEqual(len(self.calls[0][1]), 1000)
        self.assertEqual(self.calls[0][0], str(self.root / "telegram/tg-send.sh"))
        p = healthy()
        p["disk_free_gb"]["/"] = 14
        self.assertEqual(self.run_alarm(p), 0)
        self.assertEqual(len(self.calls), 2)
        p["disk_free_gb"]["/"] = 4
        failed = lambda argv: subprocess.CompletedProcess(argv, 1, "200\n", "")
        self.assertEqual(self.run_alarm(p, sender=failed), 0)
        self.assertEqual([r for r in self.rows() if r["alarm"] == "disk-low" and r["severity"] == "immediate"][-1]["telegram"], "failed")
        self.assertEqual(self.run_alarm(p, 30), 0)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.run_alarm(p, 61), 0)
        retry = [r for r in self.rows() if r.get("retry")]
        self.assertEqual(len(retry), 1)
        self.assertEqual(retry[0]["telegram"], "sent")
        before = len(self.rows())
        self.assertEqual(self.run_alarm(p, 181), 0)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.rows()), before)
        # A new release key cannot bypass the per-alarm notification window.
        with patch.object(health, "evaluate", return_value=[dict(alarm="release-failed", severity="immediate", reading={"key": "one"}, line="failure")]):
            self.assertEqual(self.run_alarm(p), 0)
        with patch.object(health, "evaluate", return_value=[dict(alarm="release-failed", severity="immediate", reading={"key": "two"}, line="failure")]):
            self.assertEqual(self.run_alarm(p, 1), 0)
        self.assertEqual(len(self.calls), 4)
        print("AC9 ok")

    def test_sender_failures_and_environment(self):
        p = healthy()
        p["disk_free_gb"]["/"] = 4
        for minutes, stdout in ((0, "429\n"), (61, ""), (122, "200\n500\n")):
            sender = lambda argv: subprocess.CompletedProcess(argv, 0, stdout, "")
            self.assertEqual(self.run_alarm(p, minutes, sender=sender), 0)
        def broken(argv):
            raise OSError("sender unavailable")
        self.assertEqual(self.run_alarm(p, 183, sender=broken), 0)
        self.assertTrue(all(r["telegram"] == "failed" for r in self.rows()))
        with patch.object(health.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "200\n", "")) as send:
            self.assertEqual(health.run(now=NOW + timedelta(minutes=244), probes=p), 0)
            self.assertEqual(send.call_args.kwargs["env"]["TG_SENDER"], "health-alarms")

    def test_ac11(self):
        import crons
        rows = crons.rows()
        row = next(r for r in rows if r["name"] == "health-alarms")
        self.assertEqual(sum(r["name"] == "health-alarms" for r in rows), 1)
        self.assertEqual((row["where"], row["schedule"], row["owner"], row["project"]), ("user", "*/10 * * * *", "thinker", "do-it-v2"))
        self.assertEqual(row["command"], f"{DOIT} cron-run health-alarms -- {DOIT} health-alarms >> {self.root}/logs/health-alarms.log 2>&1")
        for other in rows:
            self.assertEqual(bool(re.search(row["sig"], crons.print_line(other["name"]))), other["name"] == "health-alarms")
        # sitecustomize stubs every external read in the actual CLI process.
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "sitecustomize.py").write_text("""import backup, pathlib, scratch, shutil, subprocess, types
scratch.root = lambda: pathlib.Path("unused-scratch")
backup.last_push = lambda root: dict(age_s=0, ts=None, ok=True)
shutil.disk_usage = lambda path: types.SimpleNamespace(free=100 * 1024**3, used=100 * 1024**3, total=200 * 1024**3)
_read = pathlib.Path.read_text
def read(path, *args, **kwargs):
    if str(path) in ('/proc/locks', '/var/log/albert-scott/daily-release.log'):
        return ''
    return _read(path, *args, **kwargs)
pathlib.Path.read_text = read
_run = subprocess.run
def run(argv, *args, **kwargs):
    if argv[0] == 'git':
        return subprocess.CompletedProcess(argv, 0, '', '')
    raise AssertionError('unexpected external command')
subprocess.run = run
""")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(hooks), str(DOIT.parent / "src"))),
               "DOIT_SCRATCH": str(self.root / "scratch")}
        result = subprocess.run([str(DOIT), "health-alarms", "--dry-run"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"alarms": [], "telegrams": []})
        self.assertEqual(list(self.events.iterdir()), [])
        self.assertIn("doit health-alarms [--dry-run]", subprocess.check_output([str(DOIT), "help"], text=True))
        print("AC11 ok")


    def test_disk_boundaries_1496(self):
        import hashlib
        table = []
        for value in (0.0, 0.5, 0.8999, 0.90, 0.95, 1.0, None, "x", float("nan"), -0.1, 1.5):
            row = health.disk_alarm(value)
            valid = isinstance(value, (int, float)) and value == value and 0 <= value <= 1
            if valid and value < .9:
                self.assertIsNone(row)
                outcome = "none"
            else:
                self.assertEqual(row["alarm"], "disk-used")
                if not valid:
                    self.assertTrue(row["reading"]["undetermined"])
                outcome = "alarm" if valid else "undetermined"
            table.append([str(value), outcome])
        self.assertIn("disk-used", health.ALARMS)
        body = json.dumps(table)
        print(json.dumps(dict(url="cli://health/AC8", status=0, body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))
        print("L1496-AC8 ok")

    def test_disk_window_1496(self):
        import hashlib
        p = healthy()
        p["used_fraction"] = .93
        calls = []
        def sender(argv):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "200\n", "")
        table = []
        for minutes, count in ((0, 1), (360, 1), (720, 1), (1439, 1), (1441, 2)):
            self.assertEqual(health.run(now=NOW + timedelta(minutes=minutes), probes=p, sender=sender), 0)
            ev = [e for e in fold.read_events(use_cache=False) if e["type"] == "health-alarm" and e["alarm"] == "disk-used"]
            self.assertEqual(len(ev), count)
            self.assertEqual(len(calls), count)
            table.append([minutes, len(ev), len(calls)])
        p["used_fraction"] = .5
        p["disk_free_gb"]["/"] = 10
        for minutes in (0, 361):
            self.assertEqual(health.run(now=NOW + timedelta(minutes=minutes), probes=p, sender=sender), 0)
        self.assertEqual(len([e for e in fold.read_events(use_cache=False) if e.get("alarm") == "disk-low"]), 2)
        alarm = dict(alarm="release-failed", severity="immediate", reading={"key": "same"})
        prior = dict(type="health-alarm", ts=NOW.isoformat(), telegram="sent", **alarm)
        self.assertEqual(health._plan(alarm, [prior], NOW + timedelta(hours=12)), (False, False))
        self.assertEqual(health._plan(alarm, [prior], NOW + timedelta(minutes=1441)), (True, True))
        body = json.dumps(dict(disk_used=table, disk_low="again at 361 minutes", release_failed="86400 seconds"))
        print(json.dumps(dict(url="cli://health/AC9", status=0, body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))
        print("L1496-AC9 ok")


if __name__ == "__main__":
    unittest.main()
