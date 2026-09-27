#!/usr/bin/env python3
"""Checks on reap_tmp — the scratch reaper's owner/age/liveness decision (SD7),
plus L-spec-0440's per-user quota, pressure mode, worktree reap and orphan-
server surfaces (R9a-R9f).
Run: python3 test_reap_tmp.py

Hermetic (SD11): every `root` and `proc_root` here is a tempdir this file
builds and tears down; the real `/tmp`, the real `/proc`, the real crontab and
the real ledger are never touched. A live pid is this file's own child
(`/bin/sleep`, spawned and reaped here); a dead one is `pid_max + 1`, the
`test_panes.py` convention. AC10 exercises `reap_tmp.main()` in-process with
`reap_tmp.run` monkeypatched; AC14 is the one exception (SD11 permits it) — it
shells out to the real `./doit reap-tmp` to falsify the CLI's own `exec`
target, touching no real `/tmp` or `/proc` path. The L0440 sections that need
a real repo (AC8, AC16) build one under their own tempdir and monkeypatch
`fold.ROOT`/`fold.EVENTS` for the duration of one call only — never the real
ledger, never this process's own `DOIT_ROOT`.

L0440-AC23's guard is installed immediately below, before AC1: `scratch.root`/
`fold.read_events` are replaced with a function that raises if ever called,
proving — by the fact that every AC below still runs to completion — that
`run()`'s three opt-in surfaces never fall through to a live default anywhere
outside L0440-AC22's own bracket, which is the only place that installs (and
then removes) a real stand-in.
"""
import collections, os, pathlib, re, shutil, signal, subprocess, sys, tempfile, tomllib
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fold, look, panes, reap_tmp, scratch, tree_cleanup  # noqa: E402

TMP = pathlib.Path(tempfile.mkdtemp(prefix="reap-tmp-test-"))
NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
UID = os.getuid()
HOUR = 3600
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


# ══════════════════════════════════════════════════════════════════════════
# L0440-AC23 — the hermetic guard, installed BEFORE AC1 runs. Only AC22's own
# bracket may install a real stand-in; everything else must reach the bottom
# of this file with these raising patches still in place.
# ══════════════════════════════════════════════════════════════════════════
def _forbidden(*a, **kw):
    raise AssertionError("scratch.root()/fold.read_events() must never resolve inside a "
                          "hermetic test — only main() may, and only L0440-AC22 exercises that")


scratch.root, fold.read_events = _forbidden, _forbidden


# ── L0440 shared helpers: a real tiny repo+worktree, for AC8/AC16 ───────────
def _sh(cwd, *cmd):
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))
    assert p.returncode == 0, f"{cmd}: {p.stderr}"
    return p.stdout.strip()


def _git_repo(base, project):
    r = pathlib.Path(base) / "repos" / project
    r.mkdir(parents=True)
    _sh(r, "git", "init", "-q", "-b", "main")
    (r / "seed.txt").write_text("seed\n")
    _sh(r, "git", "add", "-A"); _sh(r, "git", "commit", "-qm", "seed")
    return r


def _git_branch(r, base, spec, content):
    b = spec.lower()
    wt = pathlib.Path(base) / "worktrees" / b
    _sh(r, "git", "worktree", "add", "-q", str(wt), "-b", b, "main")
    (wt / f"{b}.txt").write_text(content)
    _sh(wt, "git", "add", "-A"); _sh(wt, "git", "commit", "-qm", f"build {spec}")
    ready = _sh(wt, "git", "rev-parse", "HEAD")
    _sh(r, "git", "merge", "-q", "--no-ff", b, "-m", f"merge {spec}")
    return b, wt, ready


def _ev(actor, type_, subject, **kw):
    return {"v": 1, "ts": "2026-09-24T12:00:00+00:00", "actor": actor, "type": type_, "subject": subject, **kw}


def _killed_spec_events(charter, spec, branch, ready, project):
    """The shortest event trail that derives one spec to `killed` (terminal,
    R9c) with a real `build-done` `judge()` can prove dead — `spec_state()`'s
    OWN first check returns `killed` unconditionally, so no owed/verdict/
    review chain is needed the way `accepted` would require."""
    return [_ev("thinker", "charter-filed", charter, project=project),
            _ev("executor", "spec-written", spec, charter=charter),
            _ev("executor", "build-done", spec, branch=branch, ready_sha=ready),
            _ev("thinker", "spec-killed", spec)]


def _patch_fold_root(base):
    """`fold.append` writes through the module-level `EVENTS` global, never
    re-derived from `fold.ROOT` per call — both must be patched together, or
    a reaped-spec ledger write during an in-process AC would land in this
    process's own real `~/.do-it`. `fold.PROJECT` (also module-level, read
    once from `DOIT_PROJECT` at import time — long before this test process
    could set it) is patched to a truthy dummy too: `fold.append`'s own
    `PROJECT or subject_project(...)` would otherwise short-circuit to
    `subject_project()`, which calls the poisoned `fold.read_events()`."""
    orig = fold.ROOT, fold.EVENTS, fold.PROJECT
    fold.ROOT = pathlib.Path(base)
    fold.EVENTS = fold.ROOT / "events"
    fold.PROJECT = "l0440-test"
    return orig


def _restore_fold_root(orig):
    fold.ROOT, fold.EVENTS, fold.PROJECT = orig


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
    # L0440-AC23's guard poisons scratch.root/fold.read_events file-wide; this
    # pre-existing AC's own main() calls need harmless stand-ins too — `run`
    # is mocked right below and ignores every kwarg, so their VALUES here
    # are irrelevant, only that they resolve at all.
    scratch.root = lambda: TMP / "ac10-scratch"
    fold.read_events = lambda: []
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
    scratch.root, fold.read_events = _forbidden, _forbidden
print("AC10 ok")

# AC11 (the CLI's exact kwargs, flagged and default) is superseded by
# L0440-AC22 below, per L-spec-0440's own text: the old bare 5-key dict
# equality pre-dates `scratch_root`/`events`/`kill_fn` and would now fail
# spuriously on the three added keys every real `main()` call carries.

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

# ══════════════════════════════════════════════════════════════════════════
# L-spec-0440 — R9a: user_quota()
# ══════════════════════════════════════════════════════════════════════════
orig_quotactl = reap_tmp._quotactl_limit
orig_user_quota = reap_tmp.user_quota
orig_toml_path = look.DEFAULT_TOML_PATH

# ── L0440-AC1 · configured fallback: default 80%, then a fixture pct 55% ────
q1_root, _ = root_and_proc("l0440-q1")
reap_tmp._quotactl_limit = lambda mount, uid: None
look.DEFAULT_TOML_PATH = TMP / "l0440-no-such-dir" / "look.toml"
try:
    res = reap_tmp.user_quota(mount=str(q1_root), uid=UID)
    total1 = shutil.disk_usage(str(q1_root)).total
    ok(res["limit_source"] == "configured", f"L0440-AC1: configured when quotactl returns None: {res}")
    ok(res["limit_bytes"] == round(0.80 * total1), f"L0440-AC1: no look.toml -> default 80%: {res}")

    fixture_toml1 = TMP / "l0440-toml1" / "look.toml"
    fixture_toml1.parent.mkdir(parents=True)
    fixture_toml1.write_text("[thresholds]\ntmp_user_quota_pct = 55\n")
    look.DEFAULT_TOML_PATH = fixture_toml1
    res2 = reap_tmp.user_quota(mount=str(q1_root), uid=UID)
    ok(res2["limit_bytes"] == round(0.55 * total1), f"L0440-AC1: fixture pct 55 -> 55%: {res2}")
finally:
    reap_tmp._quotactl_limit = orig_quotactl
    look.DEFAULT_TOML_PATH = orig_toml_path
print("L0440-AC1 ok")

# ── L0440-AC2 · quotactl wins outright, ignoring look.toml's own pct ────────
q2_root, _ = root_and_proc("l0440-q2")
reap_tmp._quotactl_limit = lambda mount, uid: 123456
fixture_toml2 = TMP / "l0440-toml2" / "look.toml"
fixture_toml2.parent.mkdir(parents=True)
fixture_toml2.write_text("[thresholds]\ntmp_user_quota_pct = 10\n")
look.DEFAULT_TOML_PATH = fixture_toml2
try:
    res = reap_tmp.user_quota(mount=str(q2_root), uid=UID)
finally:
    reap_tmp._quotactl_limit = orig_quotactl
    look.DEFAULT_TOML_PATH = orig_toml_path
ok(res["limit_bytes"] == 123456 and res["limit_source"] == "quotactl",
   f"L0440-AC2: quotactl wins, look.toml's pct ignored: {res}")
print("L0440-AC2 ok")

# ── L0440-AC3 · used_bytes: owned counted, other-uid excluded, symlink's own inode only ──
q3_root, _ = root_and_proc("l0440-q3")
mine3 = q3_root / "mine.bin"
mine3.write_bytes(b"x" * 4096)
other3 = q3_root / "other.bin"
other3.write_bytes(b"x" * 4096)
big_target3 = q3_root.parent / "l0440-q3-target.bin"
big_target3.write_bytes(b"x" * (1024 * 1024))
link3 = q3_root / "link.bin"
link3.symlink_to(big_target3)

_FakeStat = collections.namedtuple("_FakeStat", "st_uid st_blocks")
_real_lstat = os.lstat
_other3_norm = os.path.normpath(str(other3))


def _fake_lstat(path, *a, **kw):
    real = _real_lstat(path, *a, **kw)
    if os.path.normpath(str(path)) == _other3_norm:
        return _FakeStat(st_uid=UID + 424243, st_blocks=real.st_blocks)
    return real


reap_tmp._quotactl_limit = lambda mount, uid: None
reap_tmp.os.lstat = _fake_lstat
try:
    mine_blocks3 = _real_lstat(mine3).st_blocks * 512
    link_blocks3 = _real_lstat(link3).st_blocks * 512
    res = reap_tmp.user_quota(mount=str(q3_root), uid=UID)
finally:
    reap_tmp._quotactl_limit = orig_quotactl
    reap_tmp.os.lstat = _real_lstat
ok(res["used_bytes"] == mine_blocks3 + link_blocks3,
   f"L0440-AC3: mine + the symlink's own tiny inode only, other-uid and the big target excluded: "
   f"{res} vs {mine_blocks3 + link_blocks3}")
print("L0440-AC3 ok")

# ══════════════════════════════════════════════════════════════════════════
# R9b — pressure mode, scratch_root reaped by the same rule, opt-in isolation
# ══════════════════════════════════════════════════════════════════════════

# ── L0440-AC4 · pressure boundary: 69.9% normal, 70.0% pressure (inclusive) ──
ac4_root, ac4_proc = root_and_proc("l0440-ac4")
reap_tmp.user_quota = lambda **kw: {"used_bytes": 699, "limit_bytes": 1000, "limit_source": "configured"}
try:
    res = reap_tmp.run(root=str(ac4_root), uid=UID, min_age_min=120, now=NOW, proc_root=str(ac4_proc))
    ok(res["mode"] == "normal", f"L0440-AC4: 69.9% is normal: {res['mode']}")
    reap_tmp.user_quota = lambda **kw: {"used_bytes": 700, "limit_bytes": 1000, "limit_source": "configured"}
    res2 = reap_tmp.run(root=str(ac4_root), uid=UID, min_age_min=120, now=NOW, proc_root=str(ac4_proc))
    ok(res2["mode"] == "pressure", f"L0440-AC4: 70.0% is pressure, boundary inclusive: {res2['mode']}")
finally:
    reap_tmp.user_quota = orig_user_quota
print("L0440-AC4 ok")

# ── L0440-AC5 · pressure's 30 wins over the passed 120, on root AND scratch_root ──
ac5_root, ac5_proc = root_and_proc("l0440-ac5")
ac5_scratch = TMP / "l0440-ac5-scratch"
ac5_scratch.mkdir()
e5r = ac5_root / "entry"; e5r.mkdir(); (e5r / "f").write_text("x")
e5s = ac5_scratch / "entry"; e5s.mkdir(); (e5s / "f").write_text("x")
age_all(e5r, NOW - timedelta(minutes=45))
age_all(e5s, NOW - timedelta(minutes=45))
reap_tmp.user_quota = lambda **kw: {"used_bytes": 700, "limit_bytes": 1000, "limit_source": "configured"}
try:
    res = reap_tmp.run(root=str(ac5_root), uid=UID, min_age_min=120, now=NOW, proc_root=str(ac5_proc),
                       scratch_root=str(ac5_scratch))
finally:
    reap_tmp.user_quota = orig_user_quota
ok(str(e5r) in res["removed"] and str(e5s) in res["removed"],
   f"L0440-AC5: pressure's effective 30 removes the 45-minute entry, both roots: {res}")

ac5n_root, ac5n_proc = root_and_proc("l0440-ac5n")
ac5n_scratch = TMP / "l0440-ac5n-scratch"
ac5n_scratch.mkdir()
e5nr = ac5n_root / "entry"; e5nr.mkdir(); (e5nr / "f").write_text("x")
e5ns = ac5n_scratch / "entry"; e5ns.mkdir(); (e5ns / "f").write_text("x")
age_all(e5nr, NOW - timedelta(minutes=45))
age_all(e5ns, NOW - timedelta(minutes=45))
res_n = reap_tmp.run(root=str(ac5n_root), uid=UID, min_age_min=120, now=NOW, proc_root=str(ac5n_proc),
                     scratch_root=str(ac5n_scratch))
ok(str(e5nr) not in res_n["removed"] and str(e5ns) not in res_n["removed"],
   f"L0440-AC5: normal mode's 120 keeps the identical 45-minute fixture: {res_n}")
print("L0440-AC5 ok")

# ── L0440-AC6 · scratch_root gets the identical SD7 scan, into the SAME lists ──
ac6_root, ac6_proc = root_and_proc("l0440-ac6")
ac6_scratch = TMP / "l0440-ac6-scratch"
ac6_scratch.mkdir()
young6 = ac6_scratch / "young"; young6.write_text("x")  # a plain file: decided at its own top level
old6 = ac6_scratch / "old"; old6.mkdir(); (old6 / "f").write_text("x")
age_all(young6, NOW - timedelta(minutes=10))
age_all(old6, NOW - timedelta(days=1))
res = reap_tmp.run(root=str(ac6_root), uid=UID, min_age_min=120, now=NOW, proc_root=str(ac6_proc),
                   scratch_root=str(ac6_scratch))
ok(any(k["path"] == str(young6) and "too young" in k["reason"] for k in res["kept"]),
   f"L0440-AC6: the young scratch entry is kept, reason names age: {res['kept']}")
ok(str(old6) in res["removed"], f"L0440-AC6: the old scratch entry is removed: {res}")
print("L0440-AC6 ok")

# ── L0440-AC7 · a bare call omitting all three opt-in surfaces stays hermetic ──
ac7_root, ac7_proc = root_and_proc("l0440-ac7")
res = reap_tmp.run(root=str(ac7_root), uid=UID, now=NOW, proc_root=str(ac7_proc))
ok(res["reaped_worktrees"] == [] and res["stopped_servers"] == [] and res["errors"] == {},
   f"L0440-AC7: every opt-in surface stays empty on a bare call: {res}")
print("L0440-AC7 ok")

# ── L0440-AC8 · one bad repo group isolated in errors["worktrees"]; the other reaped ──
with tempfile.TemporaryDirectory() as ac8_base:
    good_repo8 = _git_repo(ac8_base, "l0440-ac8-good")
    b8, wt8, ready8 = _git_branch(good_repo8, ac8_base, "L-spec-0888", "x")
    bad_repo8 = pathlib.Path(ac8_base) / "repos" / "l0440-ac8-bad"
    bad_repo8.mkdir(parents=True)
    (bad_repo8 / ".git").write_text("not a real git dir\n")  # every git call inside it errors
    ac8_events = (_killed_spec_events("L-charter-0888", "L-spec-0888", b8, ready8, "l0440-ac8-good")
                 + _killed_spec_events("L-charter-0889", "L-spec-0889", "l-spec-0889", "deadbeef",
                                       "l0440-ac8-bad"))
    ac8_root, ac8_proc = root_and_proc("l0440-ac8")
    saved_ledger_env8 = {k: os.environ.get(k) for k in ("DOIT_LEDGER_FILE", "DOIT_REAP_LEDGER_FILE")}
    orig_env8 = _patch_fold_root(ac8_base)
    try:
        res = reap_tmp.run(root=str(ac8_root), uid=UID, now=NOW, proc_root=str(ac8_proc), events=ac8_events)
    finally:
        _restore_fold_root(orig_env8)
        for k, v in saved_ledger_env8.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
ok(res["reaped_worktrees"] == [b8], f"L0440-AC8: the valid repo's branch is reaped: {res}")
ok("worktrees" in res["errors"], f"L0440-AC8: the invalid repo's failure is named: {res['errors']}")
ok(res["removed"] == [] and res["kept"] == [] and res["quota"] and res["mode"] in ("normal", "pressure"),
   f"L0440-AC8: root's own removed/kept/quota/mode are populated exactly as with no worktree surface: {res}")
print("L0440-AC8 ok")

# ══════════════════════════════════════════════════════════════════════════
# R9c (this half lives here — AC9-AC12 exercise `reap_merged_specs` directly,
# in test_tree_cleanup.py): AC13 proves `run()`'s OWN delegation.
# ══════════════════════════════════════════════════════════════════════════

# ── L0440-AC13 · run() delegates by identity, forwards dry_run, passes the return value through ──
ac13_root, ac13_proc = root_and_proc("l0440-ac13")
spy_calls13 = []


def _spy13(events, *, dry_run=False):
    spy_calls13.append((events, dry_run))
    return ["l-spec-9999"]


orig_reap_merged = tree_cleanup.reap_merged_specs
tree_cleanup.reap_merged_specs = _spy13
FIX13 = [{"type": "x"}]
try:
    res13 = reap_tmp.run(root=str(ac13_root), uid=UID, now=NOW, proc_root=str(ac13_proc),
                         events=FIX13, dry_run=True)
finally:
    tree_cleanup.reap_merged_specs = orig_reap_merged
ok(spy_calls13 and spy_calls13[0][0] is FIX13, f"L0440-AC13: events forwarded by identity: {spy_calls13}")
ok(spy_calls13[0][1] is True, f"L0440-AC13: dry_run kwarg forwarded: {spy_calls13}")
ok(res13["reaped_worktrees"] == ["l-spec-9999"], f"L0440-AC13: exactly the return value: {res13}")
print("L0440-AC13 ok")

# ══════════════════════════════════════════════════════════════════════════
# R9d — orphaned local dev servers
# ══════════════════════════════════════════════════════════════════════════

def _mk_orphan(proc_root, pid, ppid, uptime_val, age_s, cwd, cmdline_tokens, clk=None):
    clk = clk or os.sysconf("SC_CLK_TCK")
    d = proc_root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    tok = ["0"] * 20
    tok[0], tok[1] = "S", str(ppid)
    tok[19] = str(int((uptime_val - age_s) * clk))
    (d / "stat").write_text(f"{pid} (fake proc) " + " ".join(tok) + "\n")
    (d / "cwd").symlink_to(cwd)
    (d / "cmdline").write_bytes(("\0".join(cmdline_tokens) + "\0").encode())
    return d


# ── L0440-AC14 · all five conditions must hold; each fixture violates exactly one ──
ac14_root, ac14_proc = root_and_proc("l0440-ac14-root")
ac14_scratch = TMP / "l0440-ac14-scratch"
ac14_scratch.mkdir()
ac14_elsewhere = TMP / "l0440-ac14-elsewhere"
ac14_elsewhere.mkdir()
uptime14 = 10_000_000.0
(ac14_proc / "uptime").write_text(f"{uptime14} 0\n")
good_wt14 = ac14_scratch / "good"
good_wt14.mkdir()
_mk_orphan(ac14_proc, 66001, 2, uptime14, 3 * HOUR, str(good_wt14), ["vite"])              # (a) ppid != 1
_mk_orphan(ac14_proc, 66002, 1, uptime14, HOUR, str(good_wt14), ["vite"])                  # (b) too young
_mk_orphan(ac14_proc, 66003, 1, uptime14, 3 * HOUR, str(ac14_elsewhere), ["vite"])         # (c) cwd elsewhere
_mk_orphan(ac14_proc, 66004, 1, uptime14, 3 * HOUR, str(good_wt14),                        # (d) not SD17
          ["python3", "-m", "http.server"])
_mk_orphan(ac14_proc, 66005, 1, uptime14, 3 * HOUR, str(good_wt14), ["vite"])              # all five
res_real14 = reap_tmp.run(root=str(ac14_root), uid=UID, now=NOW, proc_root=str(ac14_proc),
                          scratch_root=str(ac14_scratch), kill_fn=lambda p, s: None)
stopped14 = {s["pid"] for s in res_real14["stopped_servers"]}
ok(stopped14 == {66005}, f"L0440-AC14: only the all-five process is stopped: {res_real14['stopped_servers']}")
res_wrong_owner14 = reap_tmp.run(root=str(ac14_root), uid=UID + 424244, now=NOW, proc_root=str(ac14_proc),
                                 scratch_root=str(ac14_scratch), kill_fn=lambda p, s: None)
ok(res_wrong_owner14["stopped_servers"] == [],
   f"L0440-AC14: a DIFFERENT uid= excludes even the all-five shape, by construction not chown: "
   f"{res_wrong_owner14['stopped_servers']}")
print("L0440-AC14 ok")

# ── L0440-AC15 · age from ticks+uptime only, never st_mtime ──────────────────
ac15_root, ac15_proc = root_and_proc("l0440-ac15")
ac15_scratch = TMP / "l0440-ac15-scratch"
ac15_scratch.mkdir()
wt15 = ac15_scratch / "wt"
wt15.mkdir()
uptime15 = 10_000_000.0
(ac15_proc / "uptime").write_text(f"{uptime15} 0\n")
d15a = _mk_orphan(ac15_proc, 67001, 1, uptime15, 30 * 60, str(wt15), ["vite"])   # 30 min by ticks
touch(d15a / "stat", NOW - timedelta(hours=3))                                  # mtime LIES: says 3h
d15b = _mk_orphan(ac15_proc, 67002, 1, uptime15, 3 * HOUR, str(wt15), ["vite"])  # 3h by ticks
touch(d15b / "stat", NOW - timedelta(minutes=30))                               # mtime LIES: says 30 min
res15 = reap_tmp.run(root=str(ac15_root), uid=UID, now=NOW, proc_root=str(ac15_proc),
                     scratch_root=str(ac15_scratch), kill_fn=lambda p, s: None)
stopped15 = {s["pid"] for s in res15["stopped_servers"]}
ok(67001 not in stopped15, f"L0440-AC15: 30-min-by-ticks is NOT stopped despite an old mtime: {res15}")
ok(67002 in stopped15, f"L0440-AC15: 3h-by-ticks IS stopped despite a fresh mtime: {res15}")
print("L0440-AC15 ok")

# ── L0440-AC16 · condition (c)'s second clause: terminal-owning spec, dirty or not ──
def _ac16_fixture(base, project, spec, killed, pid):
    r = _git_repo(base, project)
    b, wt, ready = _git_branch(r, base, spec, "x")
    (wt / "dirty.txt").write_text("uncommitted\n")  # retained: dirty, never actually reaped
    evs = [_ev("thinker", "charter-filed", f"L-charter-{spec[-4:]}", project=project),
           _ev("executor", "spec-written", spec, charter=f"L-charter-{spec[-4:]}"),
           _ev("executor", "build-done", spec, branch=b, ready_sha=ready)]
    if killed:
        evs.append(_ev("thinker", "spec-killed", spec))
    proc_root = pathlib.Path(base) / f"proc-{spec}"
    (proc_root / "net").mkdir(parents=True)
    (proc_root / "net" / "unix").write_text("Num RefCount Protocol Flags Type St Inode Path\n")
    up = 999999.0
    (proc_root / "uptime").write_text(f"{up} 0\n")
    _mk_orphan(proc_root, pid, 1, up, 3 * HOUR, str(wt), ["vite"])
    return evs, proc_root


with tempfile.TemporaryDirectory() as ac16_base:
    evs_k, proc_k = _ac16_fixture(ac16_base, "l0440-ac16-k", "L-spec-1616", True, 55001)
    evs_w, proc_w = _ac16_fixture(ac16_base, "l0440-ac16-w", "L-spec-1617", False, 55002)
    ac16_root, _ = root_and_proc("l0440-ac16-root")
    orig_env16 = _patch_fold_root(ac16_base)
    try:
        res_k = reap_tmp.run(root=str(ac16_root), uid=UID, now=NOW, proc_root=str(proc_k),
                             events=evs_k, kill_fn=lambda p, s: None, dry_run=True)
        res_w = reap_tmp.run(root=str(ac16_root), uid=UID, now=NOW, proc_root=str(proc_w),
                             events=evs_w, kill_fn=lambda p, s: None, dry_run=True)
    finally:
        _restore_fold_root(orig_env16)
ok(any(s["pid"] == 55001 for s in res_k["stopped_servers"]),
   f"L0440-AC16: a killed owner's DIRTY (retained) worktree still stops its orphan: {res_k['stopped_servers']}")
ok(all(s["pid"] != 55002 for s in res_w["stopped_servers"]),
   f"L0440-AC16: the identical fixture, owner still 'written', is NOT stopped: {res_w['stopped_servers']}")
print("L0440-AC16 ok")

# ── L0440-AC17 · postgres -D counts only when its OWN argument is under scratch_root ──
ac17_root, ac17_proc = root_and_proc("l0440-ac17")
ac17_scratch = TMP / "l0440-ac17-scratch"
ac17_scratch.mkdir()
wt17 = ac17_scratch / "wt"
wt17.mkdir()
outside17 = TMP / "l0440-ac17-outside-data"
outside17.mkdir()
inside17 = ac17_scratch / "pgdata"
inside17.mkdir()
uptime17 = 10_000_000.0
(ac17_proc / "uptime").write_text(f"{uptime17} 0\n")
_mk_orphan(ac17_proc, 68001, 1, uptime17, 3 * HOUR, str(wt17), ["postgres", "-D", str(outside17)])
_mk_orphan(ac17_proc, 68002, 1, uptime17, 3 * HOUR, str(wt17), ["postgres", "-D", str(inside17)])
res17 = reap_tmp.run(root=str(ac17_root), uid=UID, now=NOW, proc_root=str(ac17_proc),
                     scratch_root=str(ac17_scratch), kill_fn=lambda p, s: None)
stopped17 = {s["pid"] for s in res17["stopped_servers"]}
ok(68001 not in stopped17, f"L0440-AC17: -D outside scratch_root is never stopped: {res17}")
ok(68002 in stopped17, f"L0440-AC17: -D under scratch_root is stopped: {res17}")
print("L0440-AC17 ok")

# ── L0440-AC18 · exactly one SIGTERM; dry-run signals nothing, changes nothing ──
ac18_root, ac18_proc = root_and_proc("l0440-ac18")
ac18_scratch = TMP / "l0440-ac18-scratch"
ac18_scratch.mkdir()
wt18 = ac18_scratch / "wt"
wt18.mkdir()
uptime18 = 10_000_000.0
(ac18_proc / "uptime").write_text(f"{uptime18} 0\n")
_mk_orphan(ac18_proc, 69001, 1, uptime18, 3 * HOUR, str(wt18), ["vite"])
calls18 = []
res_real18 = reap_tmp.run(root=str(ac18_root), uid=UID, now=NOW, proc_root=str(ac18_proc),
                          scratch_root=str(ac18_scratch), kill_fn=lambda p, s: calls18.append((p, s)))
ok(calls18 == [(69001, signal.SIGTERM)], f"L0440-AC18: exactly one (pid, SIGTERM) call: {calls18}")
before18 = sorted(str(p) for p in ac18_scratch.rglob("*"))
res_dry18 = reap_tmp.run(root=str(ac18_root), uid=UID, now=NOW, proc_root=str(ac18_proc),
                         scratch_root=str(ac18_scratch), dry_run=True,
                         kill_fn=lambda p, s: calls18.append((p, s)))
ok(res_dry18["stopped_servers"] == res_real18["stopped_servers"],
   f"L0440-AC18: dry-run reports the identical stopped_servers: {res_dry18['stopped_servers']}")
ok(len(calls18) == 1, f"L0440-AC18: dry-run records zero new kill_fn calls: {calls18}")
ok(sorted(str(p) for p in ac18_scratch.rglob("*")) == before18, "L0440-AC18: dry-run leaves scratch untouched")
print("L0440-AC18 ok")

# ══════════════════════════════════════════════════════════════════════════
# R9e — the quota-high alarm
# ══════════════════════════════════════════════════════════════════════════

# ── L0440-AC19 · fires once at >=0.85, none below, fires again on a later call ──
ac19_root, ac19_proc = root_and_proc("l0440-ac19")
alarm_calls19 = []


class _LookStub19:
    def emit_once(self, condition, key, owner, reading, events, **kw):
        alarm_calls19.append((condition, key, owner, reading))


orig_look19 = reap_tmp.look
reap_tmp.look = _LookStub19()
try:
    reap_tmp.user_quota = lambda **kw: {"used_bytes": 850, "limit_bytes": 1000, "limit_source": "configured"}
    reap_tmp.run(root=str(ac19_root), uid=UID, now=NOW, proc_root=str(ac19_proc))
    ok(len(alarm_calls19) == 1, f"L0440-AC19: exactly 0.85 fires once: {alarm_calls19}")
    reap_tmp.user_quota = lambda **kw: {"used_bytes": 849999, "limit_bytes": 1000000, "limit_source": "configured"}
    reap_tmp.run(root=str(ac19_root), uid=UID, now=NOW, proc_root=str(ac19_proc))
    ok(len(alarm_calls19) == 1, f"L0440-AC19: 0.849999 fires none: {alarm_calls19}")
    reap_tmp.user_quota = lambda **kw: {"used_bytes": 850, "limit_bytes": 1000, "limit_source": "configured"}
    reap_tmp.run(root=str(ac19_root), uid=UID, now=NOW, proc_root=str(ac19_proc))
    ok(len(alarm_calls19) == 2, f"L0440-AC19: a second run() at >=0.85 calls it again: {alarm_calls19}")
finally:
    reap_tmp.look = orig_look19
    reap_tmp.user_quota = orig_user_quota
print("L0440-AC19 ok")

# ══════════════════════════════════════════════════════════════════════════
# R9f — 5-minute cadence, and look.toml's committed default
# ══════════════════════════════════════════════════════════════════════════
EXPECTED_CRONS_ROWS = [
    {"name": "tick", "where": "user", "schedule": "*/{tick_min} * * * *",
     "command": "DOIT_ROOT={root} {doit} tick >> {root}/logs/tick.log 2>&1",
     "path": "{doit}", "sig": r"\bdoit tick\b", "owner": "thinker", "project": "do-it-v2"},
    {"name": "look", "where": "user", "schedule": "*/10 * * * *",
     "command": "{doit} look >> {root}/logs/look.log 2>&1",
     "path": "{doit}", "sig": r"\bdoit look\b", "owner": "thinker", "project": "do-it-v2"},
    {"name": "tmp-reaper", "where": "user", "schedule": "*/5 * * * *",
     "command": "{doit} reap-tmp", "path": "{doit}", "sig": r"\bdoit reap-tmp\b",
     "owner": "thinker", "project": "do-it-v2"},
    {"name": "lessons-digest", "where": "user", "schedule": "7 * * * *",
     "command": "{repo}/scripts/lessons_digest.py >> {root}/lessons-digest.log 2>&1",
     "path": "{repo}/scripts/lessons_digest.py", "sig": r"lessons_digest\.py\b",
     "owner": "thinker", "project": "do-it-v2"},
    {"name": "backup-watch", "where": "user", "schedule": "* * * * *",
     "command": "{doit} backup watch --for 60 >> {root}/backup.log 2>&1",
     "path": "{doit}", "sig": r"\bdoit backup watch\b", "owner": "thinker", "project": "do-it-v2"},
    {"name": "ledger-snapshot", "where": "user", "schedule": "*/10 * * * *",
     "command": "/var/backups/do-it/snapshot.sh >/dev/null 2>&1",
     "path": "/var/backups/do-it/snapshot.sh", "sig": r"snapshot\.sh\b",
     "owner": "thinker", "project": "do-it-v2"},
    {"name": "sweep-owed", "where": "user", "schedule": "*/30 * * * *",
     "command": "{doit} sweep-owed >> {root}/logs/sweep-owed.log 2>&1",
     "path": "{doit}", "sig": r"\bdoit sweep-owed\b", "owner": "thinker", "project": "do-it-v2"},
    {"name": "auto-deploy", "where": "cron.d", "schedule": "*/5 * * * *",
     "command": "albert /opt/albert-scott/venv/bin/python /opt/albert-scott/scripts/ops/auto_deploy.py "
                "--once >> /var/log/albert-scott/auto-deploy-run.log 2>&1",
     "path": "/opt/albert-scott/venv/bin/python", "sig": r"auto_deploy\.py\b.*--once\b",
     "owner": "deployer", "project": "albert-scott"},
]

# ── L0440-AC20 · tmp-reaper is */5, every other row/field byte-identical ────
CRONS_PATH = pathlib.Path(__file__).resolve().parent.parent / "crons.toml"
with open(CRONS_PATH, "rb") as f:
    crons_doc = tomllib.load(f)
ok(crons_doc["row"] == EXPECTED_CRONS_ROWS,
   f"L0440-AC20: tmp-reaper is */5 * * * *, every other row/field unchanged: {crons_doc['row']}")
print("L0440-AC20 ok")

# ── L0440-AC21 · look.toml carries the default 80; missing key/table/file still yields 80 ──
LOOK_TOML_PATH = pathlib.Path(__file__).resolve().parent.parent / "look.toml"
with open(LOOK_TOML_PATH, "rb") as f:
    look_doc = tomllib.load(f)
ok(look_doc.get("thresholds", {}).get("tmp_user_quota_pct") == 80,
   f"L0440-AC21: the committed default is 80: {look_doc.get('thresholds')}")
missing_pct_toml = TMP / "l0440-ac21-missing" / "look.toml"
missing_pct_toml.parent.mkdir(parents=True)
missing_pct_toml.write_text("[thresholds]\ntmp_high = 85\n")  # no tmp_user_quota_pct key at all
look.DEFAULT_TOML_PATH = missing_pct_toml
reap_tmp._quotactl_limit = lambda mount, uid: None
q21_root, _ = root_and_proc("l0440-ac21")
try:
    res = reap_tmp.user_quota(mount=str(q21_root), uid=UID)
    total21 = shutil.disk_usage(str(q21_root)).total
finally:
    look.DEFAULT_TOML_PATH = orig_toml_path
    reap_tmp._quotactl_limit = orig_quotactl
ok(res["limit_bytes"] == round(0.80 * total21), f"L0440-AC21: missing key -> pct 80, never a raise: {res}")
print("L0440-AC21 ok")

# ══════════════════════════════════════════════════════════════════════════
# The hermetic guard — main() is the sole resolver
# ══════════════════════════════════════════════════════════════════════════

# ── L0440-AC22 · main() resolves scratch_root/events/kill_fn; supersedes the old AC11 ──
calls22 = []
reap_tmp.run = lambda **kw: calls22.append(kw) or {"removed": [], "kept": [], "before_pct": 1, "after_pct": 1}
fixture_scratch22 = TMP / "l0440-ac22-scratch"
fixture_scratch22.mkdir()
fixture_events22 = [{"type": "x"}]
try:
    scratch.root = lambda: fixture_scratch22
    fold.read_events = lambda: fixture_events22
    reap_tmp.main(["--dry-run", "--min-age", "30"])
    reap_tmp.main([])
finally:
    reap_tmp.run = orig_run
    reap_tmp.ROOT = orig_root
    scratch.root, fold.read_events = _forbidden, _forbidden
ok(calls22[0] == {"root": "/tmp", "uid": os.getuid(), "min_age_min": 30, "dry_run": True, "proc_root": "/proc",
                  "scratch_root": fixture_scratch22, "events": fixture_events22, "kill_fn": os.kill},
   f"L0440-AC22: --dry-run --min-age 30 carries all eight keys: {calls22[0]}")
ok(calls22[1] == {"root": "/tmp", "uid": os.getuid(), "min_age_min": 120, "dry_run": False, "proc_root": "/proc",
                  "scratch_root": fixture_scratch22, "events": fixture_events22, "kill_fn": os.kill},
   f"L0440-AC22: no flags carries the defaults across all eight keys: {calls22[1]}")
print("L0440-AC22 ok")

# L0440-AC23: every AC above ran to completion with the raising `scratch.root`/
# `fold.read_events` patches in place except inside AC22's own bracket — this
# literal last line is itself the mechanical proof.
print("L0440-AC-guard ok")
