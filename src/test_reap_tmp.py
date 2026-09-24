#!/usr/bin/env python3
"""Checks on reap_tmp — the scratch reaper's owner/age/liveness decision (SD7).
Run: python3 test_reap_tmp.py

Hermetic (SD11): every `root` and `proc_root` here is a tempdir this file
builds and tears down; the real `/tmp`, the real `/proc`, the real crontab and
the real ledger are never touched. A live pid is this file's own child
(`/bin/sleep`, spawned and reaped here); a dead one is `pid_max + 1`, the
`test_panes.py` convention. AC10/AC11 exercise `reap_tmp.main()` in-process
with `reap_tmp.run` monkeypatched; AC14 is the one exception (SD11 permits
it) — it shells out to the real `./doit reap-tmp` to falsify the CLI's own
`exec` target, touching no real `/tmp` or `/proc` path.
"""
import os, pathlib, re, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import reap_tmp  # noqa: E402
import panes  # noqa: E402

TMP = pathlib.Path(tempfile.mkdtemp(prefix="reap-tmp-test-"))
NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
UID = os.getuid()
n = 0
KIDS = []


def ok(cond, why):
    global n
    assert cond, why
    n += 1


def kid():
    """A live pid that is not this process, reaped at the end of the file."""
    p = subprocess.Popen(["/bin/sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    KIDS.append(p)
    return p.pid


def dead_pid():
    """A pid past the kernel's maximum — certainly not running."""
    try:
        return int(pathlib.Path("/proc/sys/kernel/pid_max").read_text().strip()) + 1
    except Exception:
        p = subprocess.Popen(["/bin/true"])
        p.wait()
        return p.pid


def root_and_proc(name):
    """A scanned `root` and a SEPARATE fake `proc_root` — kept apart so the
    fake `/proc` never shows up as an entry of the directory being reaped."""
    r, p = TMP / f"{name}-root", TMP / f"{name}-proc"
    r.mkdir(parents=True, exist_ok=True)
    (p / "net").mkdir(parents=True, exist_ok=True)
    (p / "net" / "unix").write_text("Num RefCount Protocol Flags Type St Inode Path\n")
    return r, p


def add_pid(proc_root, pid, cwd=None):
    d = proc_root / str(pid)
    (d / "fd").mkdir(parents=True, exist_ok=True)
    if cwd is not None:
        (d / "cwd").symlink_to(cwd)
    return d


def add_socket(proc_root, path):
    with open(proc_root / "net" / "unix", "a") as f:
        f.write(f"0000000000000000: 00000002 00000000 00000000 0001 03  1 {path}\n")


def touch(path, when, follow_symlinks=True):
    ts = when.timestamp()
    os.utime(path, (ts, ts), follow_symlinks=follow_symlinks)


def age_all(root_dir, when):
    """`when` on `root_dir` and everything inside it — the depth-3 scan's
    fixture floor, before any individual entry is made fresh again."""
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for name in dirnames + filenames:
            touch(os.path.join(dirpath, name), when)
    touch(root_dir, when)


# ── AC1 · wrong owner, never removed, reason names ownership ─────────────────
root1, proc1 = root_and_proc("ac1")
e1 = root1 / "entry"
e1.mkdir()
(e1 / "f").write_text("x")
age_all(e1, NOW - timedelta(days=1))
res = reap_tmp.run(root=str(root1), uid=UID + 999983, min_age_min=120, now=NOW, proc_root=str(proc1))
reasons = {k["path"]: k["reason"] for k in res["kept"]}
ok(str(e1) not in res["removed"], "AC1: never removed for the wrong uid")
ok(str(e1) in reasons and "uid" in reasons[str(e1)], f"AC1: kept, reason names ownership: {reasons}")
print("AC1 ok")

# ── AC2 · too young is kept; the same fixture, aged past, is removed ─────────
root2, proc2 = root_and_proc("ac2")
e2 = root2 / "entry"
e2.mkdir()
(e2 / "f").write_text("x")
young_since = NOW - timedelta(minutes=10)
age_all(e2, young_since)
res = reap_tmp.run(root=str(root2), uid=UID, min_age_min=120, now=NOW, proc_root=str(proc2))
ok(str(e2) not in res["removed"], "AC2: too young, kept")
later = NOW + timedelta(minutes=130)
res2 = reap_tmp.run(root=str(root2), uid=UID, min_age_min=120, now=later, proc_root=str(proc2))
ok(str(e2) in res2["removed"], f"AC2: now advanced past min_age_min, removed: {res2}")
print("AC2 ok")

# ── AC3 · a live process cwd under the entry keeps it; dead, it is removed ───
root3, proc3 = root_and_proc("ac3")
e3 = root3 / "entry"
sub3 = e3 / "sub"
sub3.mkdir(parents=True)
age_all(e3, NOW - timedelta(days=1))
pid3 = kid()
add_pid(proc3, pid3, cwd=str(sub3))
res = reap_tmp.run(root=str(root3), uid=UID, min_age_min=120, now=NOW, proc_root=str(proc3))
ok(str(e3) not in res["removed"], "AC3: live proc cwd under the entry keeps it")
next(p for p in KIDS if p.pid == pid3).kill()
next(p for p in KIDS if p.pid == pid3).wait()
res2 = reap_tmp.run(root=str(root3), uid=UID, min_age_min=120, now=NOW, proc_root=str(proc3))
ok(str(e3) in res2["removed"],
   f"AC3: pid now dead, stale proc_root/<pid> left in place, removed anyway: {res2}")
print("AC3 ok")

# ── AC4 · a bound socket under the entry keeps it; gone, it is removed ───────
root4, proc4 = root_and_proc("ac4")
e4 = root4 / "entry"
e4.mkdir()
age_all(e4, NOW - timedelta(days=1))
add_socket(proc4, str(e4 / "sock"))
res = reap_tmp.run(root=str(root4), uid=UID, min_age_min=120, now=NOW, proc_root=str(proc4))
ok(str(e4) not in res["removed"], "AC4: bound socket under the entry keeps it")
(proc4 / "net" / "unix").write_text("Num RefCount Protocol Flags Type St Inode Path\n")
res2 = reap_tmp.run(root=str(root4), uid=UID, min_age_min=120, now=NOW, proc_root=str(proc4))
ok(str(e4) in res2["removed"], f"AC4: socket line gone, removed: {res2}")
print("AC4 ok")

# ── AC5 · a live pidfile keeps the entry; a known-dead pid does not ──────────
root5, proc5 = root_and_proc("ac5")
e5 = root5 / "entry"
e5.mkdir()
pid5 = kid()
(e5 / "server.pid").write_text(str(pid5))
age_all(e5, NOW - timedelta(days=1))  # after creation: writing the file bumps the dir's own mtime too
res = reap_tmp.run(root=str(root5), uid=UID, min_age_min=120, now=NOW, proc_root=str(proc5))
ok(str(e5) not in res["removed"], "AC5: live pidfile keeps the entry")
next(p for p in KIDS if p.pid == pid5).kill()
next(p for p in KIDS if p.pid == pid5).wait()
(e5 / "server.pid").write_text(str(dead_pid()))
age_all(e5, NOW - timedelta(days=1))  # re-age after the rewrite bumps mtimes again
res2 = reap_tmp.run(root=str(root5), uid=UID, min_age_min=120, now=NOW, proc_root=str(proc5))
ok(str(e5) in res2["removed"], f"AC5: pidfile names a known-dead pid, removed: {res2}")
print("AC5 ok")

# ── AC6 · one level of recursion; a grandchild is never independently listed ─
root6, proc6 = root_and_proc("ac6")
e6 = root6 / "entry"
A, B = e6 / "A", e6 / "B"
A.mkdir(parents=True)
(A / "f").write_text("x")
B.mkdir(parents=True)
(B / "f").write_text("x")
C = B / "C"
C.mkdir()
(C / "g").write_text("x")
age_all(e6, NOW - timedelta(days=1))
touch(A, NOW - timedelta(minutes=1))
touch(A / "f", NOW - timedelta(minutes=1))
res = reap_tmp.run(root=str(root6), uid=UID, min_age_min=120, now=NOW, proc_root=str(proc6))
ok(str(e6) not in res["removed"], "AC6: the top-level dir itself is never deleted as a whole")
kept_paths = {k["path"] for k in res["kept"]}
ok(str(A) in kept_paths, f"AC6: A kept for being young: {res}")
ok(str(B) in res["removed"], f"AC6: B removed: {res}")
ok(str(C) not in kept_paths and str(C) not in res["removed"],
   f"AC6: grandchild C never independently listed either way: {res}")
print("AC6 ok")

# ── AC7 · a top-level symlink is decided from its OWN lstat, target untouched ─
root7, proc7 = root_and_proc("ac7")
target7 = root7.parent / "ac7-target"
target7.mkdir(parents=True)
(target7 / "f").write_text("x")
touch(target7, NOW)
touch(target7 / "f", NOW)
link7 = root7 / "entry"
link7.symlink_to(target7)
touch(link7, NOW - timedelta(days=1), follow_symlinks=False)
res = reap_tmp.run(root=str(root7), uid=UID, min_age_min=120, now=NOW, proc_root=str(proc7))
ok(str(link7) in res["removed"], f"AC7: the link's own (old, owned) lstat removes it: {res}")
ok(target7.exists() and (target7 / "f").exists(), "AC7: the target directory itself is never walked")
print("AC7 ok")

# ── AC8 · unreadable is fail-safe: kept, reason exactly "undetermined" ───────
root8, proc8 = root_and_proc("ac8")
e8 = root8 / "entry"
blocked = e8 / "blocked"
blocked.mkdir(parents=True)
(blocked / "f").write_text("x")
age_all(e8, NOW - timedelta(days=1))
os.chmod(blocked, 0o000)
try:
    res = reap_tmp.run(root=str(root8), uid=UID, min_age_min=120, now=NOW, proc_root=str(proc8))
finally:
    os.chmod(blocked, 0o755)
reasons8 = {k["path"]: k["reason"] for k in res["kept"]}
ok(str(e8) not in res["removed"], "AC8: an unreadable descendant never removes the entry")
ok(reasons8.get(str(e8)) == "undetermined", f"AC8: reason is exactly 'undetermined': {reasons8}")
print("AC8 ok")

# ── AC9 · dry-run reports without deleting; a real run reports and deletes ──
root9, proc9 = root_and_proc("ac9")
e9 = root9 / "entry"
e9.mkdir()
(e9 / "f").write_text("x")
age_all(e9, NOW - timedelta(days=1))
res_dry = reap_tmp.run(root=str(root9), uid=UID, min_age_min=120, now=NOW, dry_run=True, proc_root=str(proc9))
ok(str(e9) in res_dry["removed"] and e9.exists(), f"AC9: dry-run leaves it on disk: {res_dry}")
res_real = reap_tmp.run(root=str(root9), uid=UID, min_age_min=120, now=NOW, dry_run=False, proc_root=str(proc9))
ok(str(e9) in res_real["removed"] and not e9.exists(), f"AC9: a real run deletes it: {res_real}")
print("AC9 ok")

# ── AC10 · the CLI's own log line, and the no-line case on a scan failure ────
doit_root_scratch = TMP / "ac10-doitroot"
(doit_root_scratch / "logs").mkdir(parents=True)
log_path = doit_root_scratch / "logs" / "tmp-reaper.log"
orig_root, orig_run = reap_tmp.ROOT, reap_tmp.run
reap_tmp.ROOT = doit_root_scratch
try:
    reap_tmp.run = lambda **kw: {"removed": [], "kept": [], "before_pct": 42, "after_pct": 10}
    rc = reap_tmp.main([])
    ok(rc == 0, "AC10: exits 0 on a normal run")
    lines = log_path.read_text().splitlines()
    ok(len(lines) == 1, f"AC10: exactly one line appended: {lines}")
    ok(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z /tmp \d+% -> \d+%$", lines[0]),
       f"AC10: matches the retired script's own log format: {lines[0]!r}")

    def _boom(**kw):
        raise FileNotFoundError("no such root")
    reap_tmp.run = _boom
    rc2 = reap_tmp.main([])
    ok(rc2 != 0, "AC10: a scan failure exits non-zero")
    ok(log_path.read_text().splitlines() == lines, "AC10: and appends no new line")
finally:
    reap_tmp.run = orig_run
print("AC10 ok")

# ── AC11 · the CLI's exact kwargs, flagged and default ───────────────────────
calls = []
reap_tmp.run = lambda **kw: calls.append(kw) or {"removed": [], "kept": [], "before_pct": 1, "after_pct": 1}
try:
    reap_tmp.main(["--dry-run", "--min-age", "30"])
    reap_tmp.main([])
finally:
    reap_tmp.run = orig_run
    reap_tmp.ROOT = orig_root
ok(calls[0] == {"root": "/tmp", "uid": os.getuid(), "min_age_min": 30, "dry_run": True, "proc_root": "/proc"},
   f"AC11: --dry-run --min-age 30 calls run with exactly these kwargs: {calls[0]}")
ok(calls[1] == {"root": "/tmp", "uid": os.getuid(), "min_age_min": 120, "dry_run": False, "proc_root": "/proc"},
   f"AC11: no flags calls run with the defaults: {calls[1]}")
print("AC11 ok")

# ── AC12 · (d)'s "under it" includes the referenced path's own identity ─────
root12, proc12 = root_and_proc("ac12")
e12 = root12 / "entry"
e12.mkdir()
child12 = e12 / "child"
child12.write_text("x")
age_all(e12, NOW - timedelta(days=1))
add_socket(proc12, str(child12))
res = reap_tmp.run(root=str(root12), uid=UID, min_age_min=120, now=NOW, proc_root=str(proc12))
ok(str(e12) not in res["removed"] and str(child12) not in res["removed"],
   f"AC12: neither the entry nor the child appears in removed: {res}")
kept12 = {k["path"] for k in res["kept"]}
ok(str(child12) in kept12, f"AC12: the child is independently kept, failing (d) on itself: {res}")
print("AC12 ok")

# ── AC13 · a reference AT the entry's own path protects the whole entry ─────
root13, proc13 = root_and_proc("ac13")
e13 = root13 / "entry"
sib13 = e13 / "sibling"
sib13.mkdir(parents=True)
(sib13 / "f").write_text("x")
age_all(e13, NOW - timedelta(days=1))
pid13 = kid()
add_pid(proc13, pid13, cwd=str(e13))
res = reap_tmp.run(root=str(root13), uid=UID, min_age_min=120, now=NOW, proc_root=str(proc13))
ok(str(e13) not in res["removed"], "AC13: the whole entry is kept, no recursion")
ok(str(sib13) not in res["removed"] and str(sib13) not in {k["path"] for k in res["kept"]},
   f"AC13: the otherwise-removable sibling is never independently evaluated: {res}")
print("AC13 ok")

# ── AC14 · the real `doit reap-tmp` wiring, falsified as a subprocess ───────
doit_path = pathlib.Path(__file__).resolve().parent.parent / "doit"
p14 = subprocess.run([str(doit_path), "reap-tmp", "--no-such-flag"], capture_output=True, text=True)
ok(p14.returncode == 2, f"AC14: exits 2 on a bad flag: {p14.returncode} / {p14.stderr!r}")
ok("usage:" in p14.stderr and "reap_tmp.py" in p14.stderr,
   f"AC14: the usage line names reap_tmp.py: {p14.stderr!r}")
print("AC14 ok")

for _p in KIDS:
    try:
        _p.kill()
    except ProcessLookupError:
        pass
    try:
        _p.wait(timeout=5)
    except Exception:
        pass

print(f"reap_tmp: {n} checks pass")
