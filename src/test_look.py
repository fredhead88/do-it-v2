#!/usr/bin/env python3
"""Checks on look — the babysitter's ten-minute check, as a script
(L-charter-0036 R1/R3/R4/R5, L-spec-0322). Run: python3 test_look.py

Every external read goes through a fake `Runner`; nothing here opens a real
ssh/git/tmux/crontab/df/gh call. `panes`/`pane_resume` reads (local session and
transcript files) are pointed at tempdirs explicitly, never the operator's
real `~/.claude/sessions`. Clocks used for the 90s-budget mechanism (AC18)
are the fake Runner's own scripted `clock()`/`sleep()` — never a real wait.

`look.run()` now unconditionally runs `look_wallclock`'s own quota check too
(L-spec-0389), which globs `pane_resume.PROJECTS` by default — module-level
`pane_resume.PROJECTS`/`panes.SESSIONS` are therefore repointed at fresh empty
tempdirs immediately below, for every AC in this file, so a `look.run()` call
that never mentions either directory still never reads the operator's real one.
"""
import json, os, pathlib, socket, subprocess, sys, tempfile, time
from datetime import datetime, timedelta, timezone

# Never the real ledger: without this, cases that reach tick's stale-spawn sweep
# appended `spawn-stale L-grader-9001` to the live L-tick-local.jsonl on every run
# (780 rows by 2026-10-04).
_FIXTURE_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="test-look-root-"))
(_FIXTURE_ROOT / "events").mkdir(parents=True, exist_ok=True)
os.environ["DOIT_ROOT"] = str(_FIXTURE_ROOT)

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import crons, dispatch, fold, look, look_wallclock, pane_resume, panes  # noqa: E402

pane_resume.PROJECTS = pathlib.Path(tempfile.mkdtemp())
panes.SESSIONS = pathlib.Path(tempfile.mkdtemp())
# R14a/L-spec-0485: `_check_crons`/`_check_cron_unwrapped` default `crond_dir`
# to `crons.CROND_DEFAULT` whenever a test doesn't pass one explicitly (most
# of this file doesn't, since it isn't testing the cron.d row at all) — left
# at its real "/etc/cron.d", that default would make dozens of unrelated
# `look.run()` calls below genuinely read the operator's real cron.d
# directory. Repointed once, here, to an empty real tempdir so every such
# call degrades to "absent" harmlessly instead. The five `sys.modules
# ["crons"] = ...` fakes below restore this SAME (already-repointed) real
# module object in their own `finally`, never `del`, so the patch survives
# every fake/restore cycle in this file.
crons.CROND_DEFAULT = str(pathlib.Path(tempfile.mkdtemp()))

n = 0


def ok(cond, why):
    global n
    assert cond, why
    n += 1


NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def newroot():
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "events").mkdir()
    return d


def read_ledger(root):
    saved_root, saved_events = fold.ROOT, fold.EVENTS
    fold.ROOT, fold.EVENTS = root, root / "events"
    try:
        return fold.read_events()
    finally:
        fold.ROOT, fold.EVENTS = saved_root, saved_events


def append_raw(root, filename, ts, **kv):
    p = root / "events" / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps({"v": 1, "ts": ts, **kv}) + "\n")


def iso(dt):
    return dt.isoformat(timespec="seconds")


def write_toml(root, **overrides):
    """A minimal look.toml-shaped file the test points `toml_path` at."""
    lines = []
    # Bare top-level keys MUST precede every table header below, or TOML nests
    # them inside whichever header came last — an explicit `ci=[]` reads as
    # "no rows", never DEFAULTS' fallback row (look.load_toml's own "in doc" check).
    if "ci" in overrides and not (overrides["ci"] or []):
        lines.append("ci = []")
    th = overrides.get("thresholds") or {}
    if th:
        lines.append("[thresholds]")
        for k, v in th.items():
            lines.append(f"{k} = {v}")
    for row in overrides.get("prod") or []:
        lines.append("[[prod]]")
        for k, v in row.items():
            lines.append(f'{k} = "{v}"')
    pam = overrides.get("pane_at_menu")
    if pam:
        lines.append("[pane_at_menu]")
        for k, v in pam.items():
            lines.append(f"{k} = {json.dumps(v)}")
    for row in overrides.get("ci") or []:
        lines.append("[[ci]]")
        for k, v in row.items():
            lines.append(f"{k} = {json.dumps(v)}")
    cls = overrides.get("classes")
    if cls:
        lines.append("[classes]")
        for k, v in cls.items():
            lines.append(f"{k} = {json.dumps(v)}")
    p = root / "look.toml"
    p.write_text("\n".join(lines) + "\n")
    return p


def prod_row(project="p1", repo="/r", ssh_target="root@x", base_url="http://x",
             version_path="/version", health_path="/health"):
    return {"project": project, "repo": repo, "ssh_target": ssh_target,
            "base_url": base_url, "version_path": version_path, "health_path": health_path}


class FakeRunner:
    """Every call recorded; each behavior swappable per-test. `sleep()` advances
    a SCRIPTED virtual clock — never a real wait (AC18)."""
    def __init__(self, ssh=None, git=None, tmux_capture=None, crontab_text=None,
                 disk_usage=None, gh_run_list=None, clock_start=0.0):
        self._ssh = ssh or (lambda target, cmd, timeout: "")
        self._git = git or (lambda args, cwd, timeout: "")
        self._tmux = tmux_capture or (lambda target, timeout: "")
        self._crontab = crontab_text or (lambda timeout: "")
        self._disk = disk_usage or (lambda path, timeout: {"total": 100, "used": 10, "free": 90})
        self._gh = gh_run_list or (lambda repo, branch, timeout: "[]")
        self._t = clock_start
        self.calls = []

    def ssh(self, target, cmd, timeout):
        self.calls.append(("ssh", target, cmd, timeout))
        return self._ssh(target, cmd, timeout)

    def git(self, args, cwd, timeout):
        self.calls.append(("git", args, cwd, timeout))
        return self._git(args, cwd, timeout)

    def tmux_capture(self, target, timeout):
        self.calls.append(("tmux_capture", target, timeout))
        return self._tmux(target, timeout)

    def crontab_text(self, timeout):
        self.calls.append(("crontab_text", timeout))
        return self._crontab(timeout)

    def disk_usage(self, path, timeout):
        self.calls.append(("disk_usage", path, timeout))
        return self._disk(path, timeout)

    def gh_run_list(self, repo, branch, timeout):
        self.calls.append(("gh_run_list", repo, branch, timeout))
        return self._gh(repo, branch, timeout)

    def clock(self):
        return self._t

    def sleep(self, seconds):
        self._t += seconds


def briefs_of(res, condition):
    return [b for b in res["briefs"] if b["condition"] == condition]


def clean_toml(root):
    """No [[prod]] rows and no codex targets — every check with an external
    dependency degrades to nothing but disk/tmp/packet/pane-dead/spec-misrouted,
    each of which is itself inert against an empty events list and empty dirs."""
    return write_toml(root, prod=[], pane_at_menu={"codex_patterns": [], "codex_targets": []})


def _write_cron_manifest(root, rows_, filename="crons-fixture.toml"):
    """A synthetic `crons.toml`-shaped manifest (R14a/b/c fixtures): `rows_` is
    a list of the 8-field row dicts plus an optional `max_runtime_s`. `sig` is
    written as a single-quoted TOML literal string (no escape processing,
    matching `crons.toml`/`test_crons.py`'s own convention) so a backslash
    regex round-trips untouched."""
    lines = []
    for r in rows_:
        lines.append("[[row]]")
        for k in ("name", "where", "schedule", "command", "sig", "path", "owner", "project"):
            v = r[k]
            lines.append(f"{k} = '{v}'" if k == "sig" else f"{k} = {v!r}")
        if r.get("max_runtime_s") is not None:
            lines.append(f"max_runtime_s = {int(r['max_runtime_s'])}")
        lines.append("")
    p = root / filename
    p.write_text("\n".join(lines))
    return p


def _write_cron_store(root, name, rows_):
    """Hand-written `$R/state/cron-runs/<name>.jsonl` rows — the exact shape
    `cron_run.py` itself writes (tested separately in test_cron_run.py); here
    only to feed `look._check_cron_runs` a scripted history."""
    p = root / "state" / "cron-runs" / f"{name}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in rows_) + "\n")
    return p



# ══════════════════════════════════════════════════════════════════════════════
# L-charter-0042/L-spec-0427 — R1/SD4: spec-dispatchable-stale
# ══════════════════════════════════════════════════════════════════════════════
import dispatch as _dispatch  # noqa: E402

CLEAN_SPEC_AD = """# Goal

Fixture spec used only by test_look.py's autodispatch-0427 fixtures.

# Acceptance criteria

AC1 [backend]: trivially true. review_path: n/a.

Writes:
- fixture/file.py

# Verification

```
true && echo VERIFIED
```
"""


def _ad_root_setup(root, project="proj"):
    """`autodispatch.candidates` reads `fold.ROOT` (repo symlink, capacity) and
    `dispatch.CONTENT` (spec text) as module globals, never `look.run`'s own
    `root` argument — production runs both off the SAME DOIT_ROOT; these
    fixtures line them up by hand, and restore them afterward."""
    saved = (fold.ROOT, _dispatch.ROOT, _dispatch.EVENTS, _dispatch.CONTENT)
    fold.ROOT = root
    _dispatch.ROOT, _dispatch.EVENTS, _dispatch.CONTENT = root, root / "events", root / "content"
    (root / "content").mkdir(parents=True, exist_ok=True)
    (root / "repos").mkdir(parents=True, exist_ok=True)
    bare = root / "bare-repo"
    bare.mkdir(exist_ok=True)
    link = root / "repos" / project
    if not link.exists():
        link.symlink_to(bare)
    return saved


def _ad_root_restore(saved):
    fold.ROOT, _dispatch.ROOT, _dispatch.EVENTS, _dispatch.CONTENT = saved


# AC20 — a dispatchable spec 40 minutes stale briefs once; 10 minutes briefs nothing.
root20 = newroot()
saved20 = _ad_root_setup(root20, project="proj20")
(root20 / "content" / "L-spec-20001.md").write_text(CLEAN_SPEC_AD)
append_raw(root20, "L-planner-0001.jsonl", iso(NOW - timedelta(minutes=40)),
           type="spec-written", subject="L-spec-20001", project="proj20")
ev20 = read_ledger(root20)
res20 = look.run(ev20, now=NOW, runner=FakeRunner(), root=root20, toml_path=clean_toml(root20))
b20 = briefs_of(res20, "spec-dispatchable-stale")
ok(len(b20) == 1 and b20[0]["key"] == "L-spec-20001" and b20[0]["owner"] == "executor",
   f"AC20: one brief for a 40-minute-stale dispatchable spec: {b20}")
_ad_root_restore(saved20)

root20b = newroot()
saved20b = _ad_root_setup(root20b, project="proj20b")
(root20b / "content" / "L-spec-20002.md").write_text(CLEAN_SPEC_AD)
append_raw(root20b, "L-planner-0001.jsonl", iso(NOW - timedelta(minutes=10)),
           type="spec-written", subject="L-spec-20002", project="proj20b")
ev20b = read_ledger(root20b)
res20b = look.run(ev20b, now=NOW, runner=FakeRunner(), root=root20b, toml_path=clean_toml(root20b))
ok(briefs_of(res20b, "spec-dispatchable-stale") == [], "AC20: a fresh (10-minute) dispatchable spec briefs nothing")
_ad_root_restore(saved20b)
print("autodispatch-0427 AC20 ok")

# AC21 — a seat-wait spec (full capacity, otherwise eligible) never alarms, even at 40 minutes.
root21 = newroot()
saved21 = _ad_root_setup(root21, project="proj21")
(root21 / "models.toml").write_text("[seats]\nbuilder = 1\n")
(root21 / "content" / "L-spec-21001.md").write_text(CLEAN_SPEC_AD)
(root21 / "content" / "L-spec-21002.md").write_text(CLEAN_SPEC_AD)
append_raw(root21, "L-planner-0001.jsonl", iso(NOW - timedelta(hours=2)),
           type="spec-written", subject="L-spec-21001", project="proj21")
append_raw(root21, "L-planner-0002.jsonl", iso(NOW - timedelta(minutes=40)),
           type="spec-written", subject="L-spec-21002", project="proj21")
ev21 = read_ledger(root21)
res21 = look.run(ev21, now=NOW, runner=FakeRunner(), root=root21, toml_path=clean_toml(root21))
ok(not any(b["key"] == "L-spec-21002" for b in briefs_of(res21, "spec-dispatchable-stale")),
   f"AC21: a seat-wait spec never alarms: {res21['briefs']}")
_ad_root_restore(saved21)
print("autodispatch-0427 AC21 ok")

# AC22 — reading is dispatch-failed:<reason> for a standing failure, tick-not-running otherwise.
root22 = newroot()
saved22 = _ad_root_setup(root22, project="proj22")
(root22 / "content" / "L-spec-22001.md").write_text(CLEAN_SPEC_AD)
append_raw(root22, "L-planner-0001.jsonl", iso(NOW - timedelta(minutes=40)),
           type="spec-written", subject="L-spec-22001", project="proj22")
append_raw(root22, "L-tick-0001.jsonl", iso(NOW - timedelta(minutes=35)),
           type="autodispatch-failed", subject="L-spec-22001", reason="boom-22")
ev22 = read_ledger(root22)
res22 = look.run(ev22, now=NOW, runner=FakeRunner(), root=root22, toml_path=clean_toml(root22))
b22 = briefs_of(res22, "spec-dispatchable-stale")
ok(len(b22) == 1 and b22[0]["reading"] == "dispatch-failed:boom-22", f"AC22a: {b22}")
_ad_root_restore(saved22)

root22b = newroot()
saved22b = _ad_root_setup(root22b, project="proj22b")
(root22b / "content" / "L-spec-22002.md").write_text(CLEAN_SPEC_AD)
append_raw(root22b, "L-planner-0001.jsonl", iso(NOW - timedelta(minutes=40)),
           type="spec-written", subject="L-spec-22002", project="proj22b")
ev22b = read_ledger(root22b)
res22b = look.run(ev22b, now=NOW, runner=FakeRunner(), root=root22b, toml_path=clean_toml(root22b))
b22b = briefs_of(res22b, "spec-dispatchable-stale")
ok(len(b22b) == 1 and b22b[0]["reading"] == "tick-not-running", f"AC22b: {b22b}")
_ad_root_restore(saved22b)
print("autodispatch-0427 AC22 ok")

# AC23 — once the spec leaves `written` (a build-started lands), a second pass
# briefs nothing further and clears the standing one via emit_once/_clear.
root23 = newroot()
saved23 = _ad_root_setup(root23, project="proj23")
(root23 / "content" / "L-spec-23001.md").write_text(CLEAN_SPEC_AD)
append_raw(root23, "L-planner-0001.jsonl", iso(NOW - timedelta(minutes=40)),
           type="spec-written", subject="L-spec-23001", project="proj23")
ev23a = read_ledger(root23)
res23a = look.run(ev23a, now=NOW, runner=FakeRunner(), root=root23, toml_path=clean_toml(root23))
ok(len(briefs_of(res23a, "spec-dispatchable-stale")) == 1, f"AC23: seeding a standing brief first: {res23a['briefs']}")
append_raw(root23, "L-builder-2301.jsonl", iso(NOW - timedelta(minutes=5)),
           type="build-started", subject="L-spec-23001")
ev23b = read_ledger(root23)
res23b = look.run(ev23b, now=NOW, runner=FakeRunner(), root=root23, toml_path=clean_toml(root23))
ok(briefs_of(res23b, "spec-dispatchable-stale") == [], "AC23: no further brief once off written")
ev23c = read_ledger(root23)
ok(any(e.get("type") == "brief-answered" for e in ev23c), "AC23: the standing brief is cleared on the ledger")
_ad_root_restore(saved23)
print("autodispatch-0427 AC23 ok")


# ── AC1: single injectable Runner; a real-subprocess sentinel proves no leak ─
root1 = newroot()
raising = lambda *a, **k: (_ for _ in ()).throw(AssertionError("real subprocess/shutil call leaked past Runner"))
real_run, real_popen, real_du = subprocess.run, os.popen, __import__("shutil").disk_usage
subprocess.run, os.popen = raising, raising
import shutil as _shutil
_shutil.disk_usage = raising
class _CronsAllPresent:
    @staticmethod
    def check(crontab_text=None, manifest=None, crond_dir=None):
        return []

    @staticmethod
    def unwrapped(crontab_text=None, manifest=None, crond_dir=None):
        return []

    @staticmethod
    def rows(manifest=None):
        return []


sys.modules["crons"] = _CronsAllPresent()
try:
    fake1 = FakeRunner(ssh=lambda t, c, to: json.dumps({"sha": "a" * 7}) if "-w" not in c else "200",
                       git=lambda a, cwd, to: "a" * 7,
                       tmux_capture=lambda t, to: "some codex text\n",
                       crontab_text=lambda to: "")
    toml1 = write_toml(root1, prod=[prod_row()], ci=[],
                       pane_at_menu={"codex_patterns": ["nomatch"], "codex_targets": ["t1"]})
    res1 = look.run([], now=NOW, runner=fake1, root=root1, toml_path=toml1)
    methods1 = {c[0] for c in fake1.calls}
    ok(methods1 == {"ssh", "git", "tmux_capture", "crontab_text", "disk_usage"}, "AC1: every reading used the fixture")
    ok(sum(1 for c in fake1.calls if c[0] == "ssh" and "-w" in c[2]) == 1, "AC1: one health read (early 200)")
finally:
    subprocess.run, os.popen, _shutil.disk_usage = real_run, real_popen, real_du
    sys.modules["crons"] = crons  # restore the same, already-repointed, real module (never delete — see top-of-file note)
print("AC1 ok")

# ── AC2: overlapping fire under the same held flock appends nothing ─────────
root2 = newroot()
toml2 = clean_toml(root2)
lockp2 = root2 / "look" / "look.lock"
lockp2.parent.mkdir(parents=True, exist_ok=True)
heldfd = open(lockp2, "w")
import fcntl
fcntl.flock(heldfd, fcntl.LOCK_EX)
res2a = look.run([], now=NOW, runner=FakeRunner(), root=root2, toml_path=toml2)
ok(res2a == {"briefs": [], "answered": []}, "AC2: a concurrent held lock returns empty, appends nothing")
ok(not (root2 / "events" / "L-look-local.jsonl").exists(), "AC2: nothing appended while locked")
fcntl.flock(heldfd, fcntl.LOCK_UN)
heldfd.close()
res2b = look.run([], now=NOW, runner=FakeRunner(), root=root2, toml_path=toml2)
ok((root2 / "look" / "state.json").exists(), "AC2: releasing the lock lets the next pass run normally")
print("AC2 ok")

# ── AC3: merge-undeployed ────────────────────────────────────────────────────
root3 = newroot()
toml3 = write_toml(root3, prod=[prod_row(project="albert-scott")])
old_ts = int((NOW - timedelta(minutes=20)).timestamp())


def git3(args, cwd, timeout):
    return "tip1234" if args[0] == "rev-parse" else str(old_ts)


def ssh3(target, cmd, timeout):
    return "200" if "-w" in cmd else json.dumps({"sha": "dep5678"})


res3 = look.run([], now=NOW, runner=FakeRunner(ssh=ssh3, git=git3), root=root3, toml_path=toml3)
ok(len(briefs_of(res3, "merge-undeployed")) == 1, "AC3: >=15min behind briefs merge-undeployed")
ev3 = read_ledger(root3)
res3b = look.run(ev3, now=NOW, runner=FakeRunner(ssh=ssh3, git=git3), root=root3, toml_path=toml3)
ok(not briefs_of(res3b, "merge-undeployed"), "AC3: a second identical pass appends nothing (SD3)")

root3c = newroot()
recent_ts = int((NOW - timedelta(minutes=5)).timestamp())
git3c = lambda a, cwd, to: "tip1234" if a[0] == "rev-parse" else str(recent_ts)
res3c = look.run([], now=NOW, runner=FakeRunner(ssh=ssh3, git=git3c), root=root3c, toml_path=write_toml(root3c, prod=[prod_row()]))
ok(not briefs_of(res3c, "merge-undeployed"), "AC3: <15min behind briefs nothing")

root3d = newroot()
ssh3d = lambda t, c, to: "200" if "-w" in c else "not json"
res3d = look.run([], now=NOW, runner=FakeRunner(ssh=ssh3d, git=git3), root=root3d, toml_path=write_toml(root3d, prod=[prod_row()]))
ok(any(b["key"] == "undetermined:merge-undeployed" for b in briefs_of(res3d, "reading-undetermined")),
   "AC3: a non-JSON version body reads undetermined")
print("AC3 ok")

# ── AC4: prod-unhealthy, 3 reads 10s apart within one pass ──────────────────
root4 = newroot()
toml4 = write_toml(root4, prod=[prod_row()])
ssh4_calls = []


def ssh4_allbad(target, cmd, timeout):
    if "-w" in cmd:
        ssh4_calls.append(1)
        return "500"
    return json.dumps({"sha": "same111"})


fake4 = FakeRunner(ssh=ssh4_allbad, git=lambda a, cwd, to: "same111")
res4 = look.run([], now=NOW, runner=fake4, root=root4, toml_path=toml4)
ok(len(ssh4_calls) == 3, "AC4: 3 health reads within one pass")
health_calls = [c for c in fake4.calls if c[0] == "ssh" and "-w" in c[2]]
ok(len(health_calls) == 3, "AC4: 3 reads went through the runner")
ok(len(briefs_of(res4, "prod-unhealthy")) == 1, "AC4: all-3-bad briefs prod-unhealthy")

root4b = newroot()
ssh4_one200 = lambda t, c, to: ("200" if "-w" in c else json.dumps({"sha": "same111"}))
res4b = look.run([], now=NOW, runner=FakeRunner(ssh=ssh4_one200, git=lambda a, cwd, to: "same111"),
                 root=root4b, toml_path=write_toml(root4b, prod=[prod_row()]))
ok(not briefs_of(res4b, "prod-unhealthy"), "AC4: an early 200 appends nothing")

ev4 = read_ledger(root4)
fake4c = FakeRunner(ssh=ssh4_one200, git=lambda a, cwd, to: "same111")
res4c = look.run(ev4, now=NOW, runner=fake4c, root=root4, toml_path=toml4)
ok(len(res4c["answered"]) >= 1 and any(a for a in res4c["answered"]), "AC4: a later fully-200 pass clears it (SD19)")
print("AC4 ok")

# ── AC5: packet-unserved, both shapes in one fixture ledger ─────────────────
root5 = newroot()
os.environ["DOIT_SEAT_CLAIM_SEC"] = "300"
seat5 = root5 / "seat"
seat5.mkdir()
old5 = iso(NOW - timedelta(minutes=10))
append_raw(root5, "L-executor-0001.jsonl", old5, type="spawn-started", spawn="L-grader-9001", role="grader", subject="L-spec-9001")
(seat5 / "L-grader-9001.packet.md").write_text("x")
(seat5 / "L-builder-9002.packet.md").write_text("x")
mt = time.time() - 600
os.utime(seat5 / "L-builder-9002.packet.md", (mt, mt))
(seat5 / "L-builder-9003.packet.md").write_text("x")  # claimed — never briefs
(seat5 / "L-builder-9003.claimed").write_text("x")
os.utime(seat5 / "L-builder-9003.packet.md", (mt, mt))
ev5 = read_ledger(root5)
res5 = look.run(ev5, now=NOW, runner=FakeRunner(), root=root5, toml_path=clean_toml(root5))
keys5 = {b["key"] for b in briefs_of(res5, "packet-unserved")}
ok("L-grader-9001" in keys5, "AC5a: an old pending spawn with no claim briefs, keyed by spawn id")
ok("L-builder-9002" in keys5, "AC5b: an orphan packet.md past the claim window briefs, owner executor")
ok("L-builder-9003" not in keys5, "AC5: a claimed spawn never briefs")
b9002 = next(b for b in briefs_of(res5, "packet-unserved") if b["key"] == "L-builder-9002")
ok(b9002["owner"] == "executor" and b9002["reading"].get("started") is False, "AC5b: reading.started=false, owner executor")
b9001 = next(b for b in briefs_of(res5, "packet-unserved") if b["key"] == "L-grader-9001")
ok(b9001["owner"] == "relay", "AC5a: owner relay")

root5b = newroot()
seat5b = root5b / "seat"
seat5b.mkdir()
(seat5b / "L-builder-9004.packet.md").write_text("x")
old_mt = time.time() - 25 * 3600
os.utime(seat5b / "L-builder-9004.packet.md", (old_mt, old_mt))
res5b = look.run([], now=NOW, runner=FakeRunner(), root=root5b, toml_path=clean_toml(root5b))
ok(not briefs_of(res5b, "packet-unserved"), "AC5b: older than 24h excludes")
print("AC5 ok")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0431 · install-and-serve (L-charter-0042) — R5c AC12: `served_by=None`
# keeps every role on `_check_packet_unserved`'s alerting path, whatever a
# LATER spec remaps that role's own `relay.SERVERS` entry away from "relay" to.
# ══════════════════════════════════════════════════════════════════════════════
root5c = newroot()
seat5c = root5c / "seat"
seat5c.mkdir()
old5c = iso(NOW - timedelta(minutes=10))
append_raw(root5c, "L-planner-9101.jsonl", old5c, type="spawn-started", spawn="L-planner-9101",
           role="planner", subject="L-charter-9101")
(seat5c / "L-planner-9101.packet.md").write_text("x")
ev5c = read_ledger(root5c)
res5c = look.run(ev5c, now=NOW, runner=FakeRunner(), root=root5c, toml_path=clean_toml(root5c))
keys5c = {b["key"] for b in briefs_of(res5c, "packet-unserved")}
ok("L-planner-9101" in keys5c,
   f"installsync-0431 AC12: served_by=None still surfaces a role=planner unserved packet: {keys5c}")
print("installsync-0431 AC12 ok")

# ── AC6a: pane-at-menu, Claude — quota/auth and an unanswered AskUserQuestion
root6 = newroot()
sess6, proj6 = root6 / "sessions", root6 / "projects"
sess6.mkdir(), proj6.mkdir()


def session(d, name, session_id, pid):
    meta = {"name": name, "cwd": "/x", "status": "idle", "sessionId": session_id, "tmux": f"{name}:1.1"}
    (d / "sessions" / f"{pid}.json").write_text(json.dumps(meta))


def transcript(d, session_id, *entries):
    p = d / "projects" / "-x"
    p.mkdir(parents=True, exist_ok=True)
    (p / f"{session_id}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))


def assistant_error(uuid, error="authentication_failed"):
    return {"type": "assistant", "uuid": uuid, "timestamp": iso(NOW),
            "isApiErrorMessage": True, "error": error, "message": {"content": []}}


def assistant_ask(uuid, tool_id, answered=False):
    entries = [{"type": "assistant", "uuid": uuid, "timestamp": iso(NOW),
                "message": {"content": [{"type": "tool_use", "id": tool_id, "name": "AskUserQuestion"}]}}]
    if answered:
        entries.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id}]}})
    return entries


PID = os.getpid()
session(root6, "L-thinker-0001", "s1", PID)
transcript(root6, "s1", assistant_error("u1", "authentication_failed"))
briefs6 = []
look._check_pane_at_menu_claude([], root6, briefs6, sessions_dir=sess6, projects_dir=proj6)
ok(len(briefs6) == 1 and briefs6[0]["owner"] == "operator" and briefs6[0]["reading"]["class"] == "auth",
   "AC6a: an auth-classified last turn briefs, owner operator")

root6b = newroot()
sess6b, proj6b = root6b / "sessions", root6b / "projects"
sess6b.mkdir(), proj6b.mkdir()
session(root6b, "L-thinker-0002", "s2", PID)
transcript(root6b, "s2", *assistant_ask("u2", "tool2", answered=False))
briefs6b = []
look._check_pane_at_menu_claude([], root6b, briefs6b, sessions_dir=sess6b, projects_dir=proj6b)
ok(len(briefs6b) == 1 and briefs6b[0]["owner"] == "thinker", "AC6a: an unanswered AskUserQuestion briefs, owner thinker")

root6c = newroot()
sess6c, proj6c = root6c / "sessions", root6c / "projects"
sess6c.mkdir(), proj6c.mkdir()
session(root6c, "L-thinker-0003", "s3", PID)
transcript(root6c, "s3", *assistant_ask("u3", "tool3", answered=True))
briefs6c = []
look._check_pane_at_menu_claude([], root6c, briefs6c, sessions_dir=sess6c, projects_dir=proj6c)
ok(not briefs6c, "AC6a: an answered AskUserQuestion briefs nothing")
print("AC6 ok (a)")

# ── AC6b: pane-at-menu, Codex — a tmux tail matching a configured pattern ───
root6d = newroot()
toml6d = write_toml(root6d, pane_at_menu={"codex_patterns": ["Press \\d to continue"], "codex_targets": ["codex:1"]})
fake6d = FakeRunner(tmux_capture=lambda t, to: "some output\nPress 1 to continue\n")
res6d = look.run([], now=NOW, runner=fake6d, root=root6d, toml_path=toml6d)
ok(len(briefs_of(res6d, "pane-at-menu")) == 1, "AC6b: a matching codex tail briefs, owner thinker")
ok(briefs_of(res6d, "pane-at-menu")[0]["owner"] == "thinker", "AC6b: owner thinker")

root6e = newroot()
fake6e = FakeRunner(tmux_capture=lambda t, to: "nothing interesting\n")
toml6e = write_toml(root6e, pane_at_menu={"codex_patterns": ["Press \\d to continue"], "codex_targets": ["codex:1"]})
res6e = look.run([], now=NOW, runner=fake6e, root=root6e, toml_path=toml6e)
ok(not briefs_of(res6e, "pane-at-menu"), "AC6b: no match briefs nothing")
print("AC6 ok (b)")

# ── AC7: pane-dead ────────────────────────────────────────────────────────
def dead_pid():
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


root7 = newroot()
old_launch = iso(NOW - timedelta(minutes=5))
dpid = dead_pid()
ev7 = [{"type": "role-launched", "pane": "codex-1", "pid": dpid, "host": "h", "proc_start": None, "ts": old_launch, "_src": "x:1"}]
briefs7 = []
look._check_pane_dead(ev7, NOW, root7, briefs7)
ok(len(briefs7) == 1 and briefs7[0]["key"] == "codex-1", "AC7: a dead pid, no end event, >60s old briefs")

ev7b = ev7 + [{"type": "role-ended", "pane": "codex-1", "ts": iso(NOW), "_src": "x:2"}]
briefs7b = []
look._check_pane_dead(ev7b, NOW, root7, briefs7b)
ok(not briefs7b, "AC7: an end event excludes it")

fresh_launch = iso(NOW - timedelta(seconds=10))
ev7c = [{"type": "role-launched", "pane": "codex-2", "pid": dpid, "host": "h", "proc_start": None, "ts": fresh_launch, "_src": "x:3"}]
briefs7c = []
look._check_pane_dead(ev7c, NOW, root7, briefs7c)
ok(not briefs7c, "AC7: a dead pid <60s old excludes it")

live_ev = [{"type": "role-launched", "pane": "codex-3", "pid": os.getpid(), "host": "h",
            "proc_start": panes.proc_start(os.getpid()), "ts": old_launch, "_src": "x:4"}]
briefs7d = []
look._check_pane_dead(live_ev, NOW, root7, briefs7d)
ok(not briefs7d, "AC7: a live pid excludes it")
print("AC7 ok")

# ── AC8: reading-undetermined — 2 failing attempts vs fail-then-succeed ─────
class _CronsStub:
    @staticmethod
    def check(crontab_text=None, manifest=None, crond_dir=None):
        return []

    @staticmethod
    def unwrapped(crontab_text=None, manifest=None, crond_dir=None):
        return []

    @staticmethod
    def rows(manifest=None):
        return []


root8 = newroot()
calls8 = []


def crontab8_fail(timeout):
    calls8.append(1)
    raise TimeoutError("no crontab")


fake8 = FakeRunner(crontab_text=crontab8_fail)
sys.modules["crons"] = _CronsStub()
try:
    res8 = look.run([], now=NOW, runner=fake8, root=root8, toml_path=clean_toml(root8))
finally:
    sys.modules["crons"] = crons  # restore the same, already-repointed, real module (never delete — see top-of-file note)
ok(len(calls8) == 2, "AC8: 2 attempts before giving up")
ok(any(b["key"] == "undetermined:crons" for b in briefs_of(res8, "reading-undetermined")), "AC8: both fail -> undetermined")

root8b = newroot()
calls8b = []


def crontab8_recover(timeout):
    calls8b.append(1)
    if len(calls8b) == 1:
        raise TimeoutError("first fails")
    return ""


fake8b = FakeRunner(crontab_text=crontab8_recover)
sys.modules["crons"] = _CronsStub()
try:
    res8b = look.run([], now=NOW, runner=fake8b, root=root8b, toml_path=clean_toml(root8b))
finally:
    sys.modules["crons"] = crons  # restore the same, already-repointed, real module (never delete — see top-of-file note)
ok(not any(b["key"] == "undetermined:crons" for b in briefs_of(res8b, "reading-undetermined")),
   "AC8: a second-attempt success appends nothing")
print("AC8 ok")

# ── AC9: SD3 exactly-once, both memory paths, plus the 30-minute cooldown ──
root9 = newroot()
ev9 = []
r9a = look.emit_once("test-cond", "k1", "operator", {"x": 1}, ev9, root=root9)
ok(r9a is not None, "AC9a: a fresh crossing briefs")
ev9 = read_ledger(root9)
r9b = look.emit_once("test-cond", "k1", "operator", {"x": 1}, ev9, root=root9)
ok(r9b is None, "AC9a: a live state.json open entry suppresses a repeat")
(root9 / "look" / "state.json").unlink()
r9c = look.emit_once("test-cond", "k1", "operator", {"x": 1}, ev9, root=root9)
ok(r9c is None, "AC9b: with state.json gone, an unanswered ledger brief still suppresses it, no raise")
cleared9 = look._clear("test-cond", "k1", "done", ev9, root=root9)
ok(cleared9 is not None, "AC9: clears the open brief")
ev9 = read_ledger(root9)
r9d = look.emit_once("test-cond", "k1", "operator", {"x": 1}, ev9, root=root9)
ok(r9d is None, "AC9: a re-crossing within the 30-minute cooldown does not brief")

# backdate the brief-answered event past the cooldown and re-arm
lines9 = (root9 / "events" / look.LEDGER_NAME).read_text().splitlines()
patched = []
for ln in lines9:
    e = json.loads(ln)
    if e.get("type") == "brief-answered":
        e["ts"] = iso(fold.NOW - timedelta(minutes=35))
    patched.append(json.dumps(e))
(root9 / "events" / look.LEDGER_NAME).write_text("\n".join(patched) + "\n")
ev9 = read_ledger(root9)
r9e = look.emit_once("test-cond", "k1", "operator", {"x": 1}, ev9, root=root9)
ok(r9e is not None, "AC9: re-arms once the cooldown has passed")
print("AC9 ok")

# ── AC10: brief-answered wiring, actor authorization, TRIAGE rendering ──────
root10 = newroot()
ev10 = []
b10 = look.emit_once("merge-undeployed", "prod sha", "deployer", {"tip": "x"}, ev10, root=root10)
ok(b10 is not None and "subject" in b10 and "why" in b10, "AC10: the brief carries subject+why alongside condition/owner/key")
ev10 = read_ledger(root10)
specs10, charters10, ignored10, by_subject10 = fold.fold(ev10)
rendered10 = fold.render(ev10, specs10, charters10, ignored10, by_subject10)
ok("TRIAGE" in rendered10 and b10["subject"] in rendered10, "AC10: the real fold.render() shows a readable TRIAGE row")
ok(fold.check_append({"type": "brief-answered", "ref": "x:1", "why": "y"}, actor="look") is None,
   "AC10: fold.EMITS['brief-answered'] admits look")
ok(fold.check_append({"type": "brief-answered", "ref": "x:1", "why": "y"}, actor="builder") is not None,
   "AC10: builder is refused")
cleared10 = look._clear("merge-undeployed", "prod sha", "cleared", ev10, root=root10)
ok(cleared10 is not None and cleared10["ref"] == b10["_src"], "AC10: brief-answered names the open brief's own _src")
ev10 = read_ledger(root10)
ok(not fold.triage_briefs(ev10), "AC10: a cleared condition no longer lists in fold.triage_briefs()")
print("AC10 ok")

# ── AC13/AC14: tmp-high/disk-high hysteresis, tmp-climbing + slope floor ────
root13 = newroot()
disk13 = lambda pct: (lambda path, timeout: {"total": 100, "used": pct, "free": 100 - pct})
fake13 = FakeRunner(disk_usage=disk13(90))
res13 = look.run([], now=NOW, runner=fake13, root=root13, toml_path=clean_toml(root13))
ok(any(b["condition"] == "tmp-high" for b in res13["briefs"]), "AC13: tmp>=85 fires tmp-high")
ok(any(b["condition"] == "disk-high" for b in res13["briefs"]), "AC13: disk>=90 fires disk-high")
ev13 = read_ledger(root13)
fake13b = FakeRunner(disk_usage=disk13(80))
res13b = look.run(ev13, now=NOW + timedelta(minutes=10), runner=fake13b, root=root13, toml_path=clean_toml(root13))
ok(not any(a for a in res13b["answered"] if "tmp" in str(a)), "AC13: 80% (below high, above clear) does not clear tmp-high")
ev13b = read_ledger(root13)
fake13c = FakeRunner(disk_usage=disk13(70))
res13c = look.run(ev13b, now=NOW + timedelta(minutes=20), runner=fake13c, root=root13, toml_path=clean_toml(root13))
ok(any(a.get("ref") for a in res13c["answered"]), "AC13: <75% clears tmp-high")

# tmp-climbing: two points 10 min apart projecting a fill within 2h
root13g = newroot()
fake13g = FakeRunner(disk_usage=disk13(50))
res13g = look.run([], now=NOW, runner=fake13g, root=root13g, toml_path=clean_toml(root13g))
ok(not any(b["condition"] == "tmp-climbing" for b in res13g["briefs"]), "AC14: one reading -> no climbing brief, no false read")
und13g = briefs_of(res13g, "reading-undetermined")
ok(any(b["key"] == "undetermined:tmp-climbing" for b in und13g), "AC14: one prior reading reads undetermined")

ev13g = read_ledger(root13g)
fake13h = FakeRunner(disk_usage=disk13(90))  # +40 pts in 10 min -> 240 pts/h -> fills in <1h
res13h = look.run(ev13g, now=NOW + timedelta(minutes=10), runner=fake13h, root=root13g, toml_path=clean_toml(root13g))
ok(any(b["condition"] == "tmp-climbing" for b in res13h["briefs"]), "AC13: a steep 2-point slope fires tmp-climbing")

ev13h = read_ledger(root13g)
fake13i = FakeRunner(disk_usage=disk13(89))  # one flat-ish pass — must NOT clear alone
res13i = look.run(ev13h, now=NOW + timedelta(minutes=20), runner=fake13i, root=root13g, toml_path=clean_toml(root13g))
ok(not any(a.get("ref") for a in res13i["answered"]), "AC13: one non-positive-slope pass alone does not clear tmp-climbing")

ev13i = read_ledger(root13g)
fake13j = FakeRunner(disk_usage=disk13(88))  # second consecutive non-positive slope
res13j = look.run(ev13i, now=NOW + timedelta(minutes=30), runner=fake13j, root=root13g, toml_path=clean_toml(root13g))
ok(any(a.get("ref") for a in res13j["answered"]), "AC13: 2 consecutive non-positive slopes clear tmp-climbing")
print("AC13 ok")

# AC14: two readings only 5 minutes apart -> undetermined, never a computed slope.
# Seed state.json with the FIRST reading directly (bypassing an actual pass) so
# the SECOND reading, 5 minutes later, is this root's own FIRST real `look.run()`
# pass — SD3's exactly-once memory would otherwise mask a repeat undetermined.
root14 = newroot()
look._write_state(root14, {"readings": [{"ts": iso(NOW), "tmp_pct": 50.0}]})
fake14b = FakeRunner(disk_usage=disk13(60))
res14b = look.run([], now=NOW + timedelta(minutes=5), runner=fake14b, root=root14, toml_path=clean_toml(root14))
ok(not any(b["condition"] == "tmp-climbing" for b in res14b["briefs"]), "AC14: a <8min gap never computes a slope")
ok(any(b["key"] == "undetermined:tmp-climbing" for b in briefs_of(res14b, "reading-undetermined")),
   "AC14: <8min gap reads undetermined, not a false flat/rising slope")
print("AC14 ok")

# ── L-spec-0389 AC7: fix_for — a configured class + an open charter's own
# `corrects:` header sets `fix` on a look brief; no class or no naming charter
# carries `owner` only. Placed BEFORE AC15 on purpose: AC15c's own pre-existing
# failure (unrelated, confirmed identical at base_sha) halts this script, and
# this spec's own markers must print regardless.
root_ac7 = newroot()
(root_ac7 / "content").mkdir()
(root_ac7 / "content" / "L-charter-0099.md").write_text("corrects: ops, other\n\nSome charter body.\n")
append_raw(root_ac7, "L-thinker-0001.jsonl", iso(NOW - timedelta(days=1)), type="charter-filed", subject="L-charter-0099")
ev_ac7 = read_ledger(root_ac7)


class _CronsOneMissing:
    @staticmethod
    def check(crontab_text=None, manifest=None, crond_dir=None):
        return [{"name": "tmp-reaper"}]

    @staticmethod
    def unwrapped(crontab_text=None, manifest=None, crond_dir=None):
        return []

    @staticmethod
    def rows(manifest=None):
        return []


toml_ac7 = write_toml(root_ac7, classes={"cron-missing": "ops"})
sys.modules["crons"] = _CronsOneMissing()
try:
    res_ac7a = look.run(ev_ac7, now=NOW, runner=FakeRunner(), root=root_ac7, toml_path=toml_ac7)
finally:
    sys.modules["crons"] = crons  # restore the same, already-repointed, real module (never delete — see top-of-file note)
b_ac7a = next(b for b in briefs_of(res_ac7a, "cron-missing"))
ok(b_ac7a.get("fix") == "L-charter-0099", "L-spec-0389 AC7: a configured class + a naming open charter sets fix")

root_ac7b = newroot()
ev_ac7b = read_ledger(root_ac7b)
toml_ac7b = write_toml(root_ac7b, classes={})   # the shipped default: empty
sys.modules["crons"] = _CronsOneMissing()
try:
    res_ac7b = look.run(ev_ac7b, now=NOW, runner=FakeRunner(), root=root_ac7b, toml_path=toml_ac7b)
finally:
    sys.modules["crons"] = crons  # restore the same, already-repointed, real module (never delete — see top-of-file note)
b_ac7b = next(b for b in briefs_of(res_ac7b, "cron-missing"))
ok("fix" not in b_ac7b and b_ac7b.get("owner") == "thinker",
   "L-spec-0389 AC7: empty [classes] (or no open charter naming the class) -> owner only, never fix:null/fix:''")
print("AC7 ok")

# ── L-spec-0389 AC8: look.run() calls look_wallclock.run() exactly once, BEFORE
# the look-stale clear, wires "wallclock"/"briefs" apart, and degrades a raise
# to one reading-undetermined rather than crashing the pass.
calls_ac8, order_ac8 = [], []
fake_events_ac8 = [{"type": "ci-red", "repo": "x", "workflow": "y", "sha": "s", "run_url": "u", "since": "t"}]
fake_briefs_ac8 = [{"type": "brief", "condition": "planner-attempts-stuck", "key": "k", "owner": "thinker",
                   "problem": "planner-attempts-stuck", "reading": {}, "measured_at": "m", "subject": "s", "why": "w"}]


def fake_wallclock_run(events, runner, now, root, cfg, deadline, write_brief, dry_run):
    calls_ac8.append((events, runner, now, root, cfg, deadline, write_brief, dry_run))
    order_ac8.append("wallclock")
    return {"events": list(fake_events_ac8), "briefs": list(fake_briefs_ac8)}


real_wallclock_run, real_clear = look_wallclock.run, look._clear


def spy_clear(*a, **k):
    order_ac8.append("clear")
    return real_clear(*a, **k)


look_wallclock.run, look._clear = fake_wallclock_run, spy_clear
root_ac8 = newroot()
try:
    res_ac8a = look.run([], now=NOW, runner=FakeRunner(), root=root_ac8, toml_path=clean_toml(root_ac8), dry_run=True)
finally:
    look_wallclock.run, look._clear = real_wallclock_run, real_clear
ok(len(calls_ac8) == 1, "L-spec-0389 AC8: look_wallclock.run called exactly once per pass")
ok(calls_ac8[0][6] is look._write_brief, "L-spec-0389 AC8: look.run() passes its OWN _write_brief as write_brief")
ok(calls_ac8[0][7] is True, "L-spec-0389 AC8: look.run() passes its own dry_run flag through unchanged")
last_clear_idx = len(order_ac8) - 1 - order_ac8[::-1].index("clear")
ok(order_ac8.index("wallclock") < last_clear_idx, "L-spec-0389 AC8: look_wallclock.run runs BEFORE the look-stale clear")
ok(res_ac8a["wallclock"] == fake_events_ac8, "L-spec-0389 AC8: the ci-red dict lands unmodified in wallclock")
ok(not any(b == fake_events_ac8[0] for b in res_ac8a["briefs"]), "L-spec-0389 AC8: the ci-red dict never lands in briefs")
ok(any(b == fake_briefs_ac8[0] for b in res_ac8a["briefs"]), "L-spec-0389 AC8: the brief dict lands unmodified in briefs")
ok(not any(e == fake_briefs_ac8[0] for e in res_ac8a["wallclock"]), "L-spec-0389 AC8: the brief dict never lands in wallclock")


def raising_wallclock_run(*a, **k):
    raise RuntimeError("boom")


root_ac8b = newroot()
look_wallclock.run = raising_wallclock_run
try:
    res_ac8b = look.run([], now=NOW, runner=FakeRunner(), root=root_ac8b, toml_path=clean_toml(root_ac8b))
finally:
    look_wallclock.run = real_wallclock_run
ok(res_ac8b["wallclock"] == [], "L-spec-0389 AC8: a raising look_wallclock.run returns wallclock:[] for that pass")
ok(any(b["key"] == "undetermined:wallclock" for b in briefs_of(res_ac8b, "reading-undetermined")),
   "L-spec-0389 AC8: a raising look_wallclock.run degrades to one reading-undetermined, never a crashed pass")
print("AC8 ok")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0428 · supervisor-current-code (L-charter-0042 R2) — AC7-AC12:
# `doit look`'s new `supervisor-stale-code` check. Placed BEFORE AC15 on
# purpose (the file's own established pattern): AC15c's own pre-existing
# failure (unrelated, confirmed identical at base_sha) halts this script, and
# this spec's own markers must print regardless.
# ══════════════════════════════════════════════════════════════════════════════


def _boot_row(root, kind, sha, age_min, pid=None):
    append_raw(root, f"L-up-sc-{kind}.jsonl", iso(NOW - timedelta(minutes=age_min)),
              type="supervisor-code", subject=kind, kind=kind, sha=sha,
              pid=pid if pid is not None else os.getpid(), reason="start")


def _start_row(root, kind, sha, age_min):
    if kind == "planner":
        append_raw(root, "L-planner-sc.jsonl", iso(NOW - timedelta(minutes=age_min)),
                  type="planner-started", subject="L-charter-0001", planner="L-planner-sc",
                  attempt=1, mode="charter", code_sha=sha)
    else:
        append_raw(root, f"L-{kind}-sc.jsonl", iso(NOW - timedelta(minutes=age_min)),
                  type="spawn-started", subject=f"L-{kind}-sc", spawn=f"L-{kind}-sc",
                  role=kind, code_sha=sha)


# ── R2 AC7: a stale sha (per the FLAGGED --first-parent call, not a bare
# reconstruction) briefs supervisor-stale-code ────────────────────────────────
root_r2ac7 = newroot()
_boot_row(root_r2ac7, "planner", "OLD", 60)
_start_row(root_r2ac7, "planner", "OLD", 30)


def git_r2ac7(args, cwd, timeout):
    if "--first-parent" in args:
        return str(int((NOW - timedelta(minutes=45)).timestamp()))   # <= the start row's own ts
    return str(int((NOW - timedelta(minutes=1)).timestamp()))        # a later, WRONG reconstruction


res_r2ac7 = look.run(read_ledger(root_r2ac7), now=NOW, runner=FakeRunner(git=git_r2ac7),
                     root=root_r2ac7, toml_path=clean_toml(root_r2ac7))
b_r2ac7 = briefs_of(res_r2ac7, "supervisor-stale-code")
ok(len(b_r2ac7) == 1 and b_r2ac7[0]["key"] == "planner" and b_r2ac7[0]["owner"] == "thinker",
   f"R2 AC7: a cycled sha (per the flagged --first-parent call) briefs supervisor-stale-code: {b_r2ac7}")
print("R2 AC7 ok")

# ── R2 AC8: a sha current at write-time is not flagged only because HEAD has
# since moved further ─────────────────────────────────────────────────────────
root_r2ac8 = newroot()
_boot_row(root_r2ac8, "planner", "OLD", 60)
_start_row(root_r2ac8, "planner", "OLD", 30)
git_r2ac8 = lambda args, cwd, to: str(int((NOW - timedelta(minutes=5)).timestamp()))   # strictly AFTER the start row
res_r2ac8 = look.run(read_ledger(root_r2ac8), now=NOW, runner=FakeRunner(git=git_r2ac8),
                     root=root_r2ac8, toml_path=clean_toml(root_r2ac8))
ok(not briefs_of(res_r2ac8, "supervisor-stale-code"),
   f"R2 AC8: a sha current at write-time is not flagged for a later HEAD move: {res_r2ac8['briefs']}")
print("R2 AC8 ok")

# ── R2 AC9: wedged is decided by the DIVERGENCE's age, never the row's own ────
root_r2ac9a = newroot()
_boot_row(root_r2ac9a, "planner", "SHA_A", 300)
res_r2ac9a = look.run(read_ledger(root_r2ac9a), now=NOW, runner=FakeRunner(git=lambda a, c, t: ""),
                      root=root_r2ac9a, toml_path=clean_toml(root_r2ac9a))
ok(not briefs_of(res_r2ac9a, "supervisor-stale-code"),
   f"R2 AC9a: sha==HEAD (empty times) never wedges, however old the row: {res_r2ac9a['briefs']}")

root_r2ac9b = newroot()
_boot_row(root_r2ac9b, "planner", "SHA_B", 300)
git_r2ac9b = lambda a, c, t: str(int((NOW - timedelta(minutes=2)).timestamp()))
res_r2ac9b = look.run(read_ledger(root_r2ac9b), now=NOW, runner=FakeRunner(git=git_r2ac9b),
                      root=root_r2ac9b, toml_path=clean_toml(root_r2ac9b))
ok(not briefs_of(res_r2ac9b, "supervisor-stale-code"),
   f"R2 AC9b: a divergence within 5 minutes of now never wedges, however old the row: {res_r2ac9b['briefs']}")

root_r2ac9c = newroot()
_boot_row(root_r2ac9c, "planner", "SHA_C", 300)
git_r2ac9c = lambda a, c, t: str(int((NOW - timedelta(hours=4)).timestamp()))
res_r2ac9c = look.run(read_ledger(root_r2ac9c), now=NOW, runner=FakeRunner(git=git_r2ac9c),
                      root=root_r2ac9c, toml_path=clean_toml(root_r2ac9c))
b_r2ac9c = briefs_of(res_r2ac9c, "supervisor-stale-code")
ok(len(b_r2ac9c) == 1 and b_r2ac9c[0]["key"] == "planner", f"R2 AC9c: a >=3h-old divergence wedges: {b_r2ac9c}")
print("R2 AC9 ok")

# ── R2 AC10: a dead pid never briefs, however stale its sha/old its ts ────────
root_r2ac10 = newroot()
dead_pid_r2 = dead_pid()
_boot_row(root_r2ac10, "planner", "DEADSHA", 600, pid=dead_pid_r2)
git_r2ac10 = lambda a, c, t: str(int((NOW - timedelta(hours=5)).timestamp()))
res_r2ac10 = look.run(read_ledger(root_r2ac10), now=NOW, runner=FakeRunner(git=git_r2ac10),
                      root=root_r2ac10, toml_path=clean_toml(root_r2ac10))
ok(not briefs_of(res_r2ac10, "supervisor-stale-code"),
   f"R2 AC10: a dead pid never briefs, however stale: {res_r2ac10['briefs']}")
print("R2 AC10 ok")

# ── R2 AC11: a raising --first-parent call degrades to one reading-undetermined,
# never a supervisor-stale-code brief for any kind ────────────────────────────
root_r2ac11 = newroot()
_boot_row(root_r2ac11, "planner", "ANYSHA", 300)


def git_r2ac11_raise(a, c, t):
    raise TimeoutError("git hung")


res_r2ac11 = look.run(read_ledger(root_r2ac11), now=NOW, runner=FakeRunner(git=git_r2ac11_raise),
                      root=root_r2ac11, toml_path=clean_toml(root_r2ac11))
ok(any(b["key"] == "undetermined:supervisor-stale-code" for b in briefs_of(res_r2ac11, "reading-undetermined")),
   f"R2 AC11: a raising --first-parent call fires reading-undetermined: {res_r2ac11['briefs']}")
ok(not briefs_of(res_r2ac11, "supervisor-stale-code"), "R2 AC11: and no supervisor-stale-code brief for any kind")
print("R2 AC11 ok")

# ── R2 AC12: a stale planner + a current relay, same pass -> exactly one
# brief, keyed planner ────────────────────────────────────────────────────────
root_r2ac12 = newroot()
_boot_row(root_r2ac12, "planner", "OLDP", 60)
_start_row(root_r2ac12, "planner", "OLDP", 30)
_boot_row(root_r2ac12, "relay", "OLDR", 60)
_start_row(root_r2ac12, "relay", "OLDR", 30)


def git_r2ac12(args, cwd, timeout):
    sha_range = args[-1]
    if sha_range.startswith("OLDP.."):
        return str(int((NOW - timedelta(minutes=45)).timestamp()))   # <= planner's start ts -> cycled
    if sha_range.startswith("OLDR.."):
        return str(int((NOW - timedelta(minutes=5)).timestamp()))    # AFTER relay's start ts -> current
    return ""


res_r2ac12 = look.run(read_ledger(root_r2ac12), now=NOW, runner=FakeRunner(git=git_r2ac12),
                      root=root_r2ac12, toml_path=clean_toml(root_r2ac12))
b_r2ac12 = briefs_of(res_r2ac12, "supervisor-stale-code")
ok(len(b_r2ac12) == 1 and b_r2ac12[0]["key"] == "planner",
   f"R2 AC12: exactly one supervisor-stale-code brief, keyed planner: {b_r2ac12}")
print("R2 AC12 ok")


# ══════════════════════════════════════════════════════════════════════════════
# L-charter-0042/L-spec-0485 — R14: repeat-failure-alarm
# ══════════════════════════════════════════════════════════════════════════════

ROW_JOB7 = {"name": "job7", "where": "user", "schedule": "*/5 * * * *", "command": "doit job7",
           "sig": r"\bdoit job7\b", "path": "/bin/true", "owner": "o", "project": "p"}

# ── R14-AC7: three identical failures raise one repeat-failure brief ────────
root_r14ac7 = newroot()
manifest_r14ac7 = _write_cron_manifest(root_r14ac7, [ROW_JOB7])
_write_cron_store(root_r14ac7, "job7", [
    {"ev": "start", "ts": iso(NOW - timedelta(minutes=15)), "pid": 1},
    {"ev": "end", "ts": iso(NOW - timedelta(minutes=14)), "rc": 1, "duration_s": 1.0,
     "error_class": "rc:1:boom", "error_tail": "boom", "start_ts": iso(NOW - timedelta(minutes=15))},
    {"ev": "start", "ts": iso(NOW - timedelta(minutes=10)), "pid": 2},
    {"ev": "end", "ts": iso(NOW - timedelta(minutes=9)), "rc": 1, "duration_s": 1.0,
     "error_class": "rc:1:boom", "error_tail": "boom", "start_ts": iso(NOW - timedelta(minutes=10))},
    {"ev": "start", "ts": iso(NOW - timedelta(minutes=5)), "pid": 3},
    {"ev": "end", "ts": iso(NOW - timedelta(minutes=4)), "rc": 1, "duration_s": 1.0,
     "error_class": "rc:1:boom", "error_tail": "boom", "start_ts": iso(NOW - timedelta(minutes=5))},
])
res_r14ac7a = look.run([], now=NOW, runner=FakeRunner(), root=root_r14ac7, toml_path=clean_toml(root_r14ac7),
                       crons_manifest=str(manifest_r14ac7))
b_r14ac7a = briefs_of(res_r14ac7a, "repeat-failure")
ok(len(b_r14ac7a) == 1 and b_r14ac7a[0]["key"] == "job7" and "boom" in str(b_r14ac7a[0].get("why")),
   f"R14-AC7: three identical failures raise exactly one repeat-failure brief: {b_r14ac7a}")

root_r14ac7b = newroot()
manifest_r14ac7b = _write_cron_manifest(root_r14ac7b, [ROW_JOB7])
_write_cron_store(root_r14ac7b, "job7", [
    {"ev": "end", "ts": iso(NOW - timedelta(minutes=9)), "rc": 1, "duration_s": 1.0,
     "error_class": "rc:1:boom", "error_tail": "boom"},
    {"ev": "end", "ts": iso(NOW - timedelta(minutes=4)), "rc": 1, "duration_s": 1.0,
     "error_class": "rc:1:boom", "error_tail": "boom"},
])
res_r14ac7b = look.run([], now=NOW, runner=FakeRunner(), root=root_r14ac7b, toml_path=clean_toml(root_r14ac7b),
                       crons_manifest=str(manifest_r14ac7b))
ok(not briefs_of(res_r14ac7b, "repeat-failure"), "R14-AC7: two identical failures raise none")

root_r14ac7c = newroot()
manifest_r14ac7c = _write_cron_manifest(root_r14ac7c, [ROW_JOB7])
_write_cron_store(root_r14ac7c, "job7", [
    {"ev": "end", "ts": iso(NOW - timedelta(minutes=15)), "rc": 1, "duration_s": 1.0,
     "error_class": "rc:1:boom", "error_tail": "boom"},
    {"ev": "end", "ts": iso(NOW - timedelta(minutes=9)), "rc": 2, "duration_s": 1.0,
     "error_class": "rc:2:bang", "error_tail": "bang"},
    {"ev": "end", "ts": iso(NOW - timedelta(minutes=4)), "rc": 1, "duration_s": 1.0,
     "error_class": "rc:1:boom", "error_tail": "boom"},
])
res_r14ac7c = look.run([], now=NOW, runner=FakeRunner(), root=root_r14ac7c, toml_path=clean_toml(root_r14ac7c),
                       crons_manifest=str(manifest_r14ac7c))
ok(not briefs_of(res_r14ac7c, "repeat-failure"), "R14-AC7: three failures with differing classes raise none")

# a second and third pass over the unchanged store still leave exactly one open brief
ev_r14ac7_2 = read_ledger(root_r14ac7)
res_r14ac7_2 = look.run(ev_r14ac7_2, now=NOW, runner=FakeRunner(), root=root_r14ac7, toml_path=clean_toml(root_r14ac7),
                        crons_manifest=str(manifest_r14ac7))
ok(not briefs_of(res_r14ac7_2, "repeat-failure"), "R14-AC7: second pass over the unchanged store emits no NEW brief")
ev_r14ac7_3 = read_ledger(root_r14ac7)
res_r14ac7_3 = look.run(ev_r14ac7_3, now=NOW, runner=FakeRunner(), root=root_r14ac7, toml_path=clean_toml(root_r14ac7),
                        crons_manifest=str(manifest_r14ac7))
ok(not briefs_of(res_r14ac7_3, "repeat-failure"), "R14-AC7: third pass still emits no new brief")
open_count_r14ac7 = sum(1 for e in read_ledger(root_r14ac7)
                        if e.get("type") == "brief" and e.get("condition") == "repeat-failure")
ok(open_count_r14ac7 == 1, f"R14-AC7: exactly one open brief across three passes: {open_count_r14ac7}")
print("R14-AC7 ok")

# ── R14-AC8: a clean run answers; a further failure starts a fresh streak of one ─
store_r14ac7_path = root_r14ac7 / "state" / "cron-runs" / "job7.jsonl"
rows_r14ac8 = [json.loads(l) for l in store_r14ac7_path.read_text().splitlines() if l.strip()]
rows_r14ac8.append({"ev": "end", "ts": iso(NOW), "rc": 0, "duration_s": 1.0, "error_class": "", "error_tail": ""})
store_r14ac7_path.write_text("\n".join(json.dumps(r) for r in rows_r14ac8) + "\n")
ev_r14ac8a = read_ledger(root_r14ac7)
res_r14ac8a = look.run(ev_r14ac8a, now=NOW, runner=FakeRunner(), root=root_r14ac7, toml_path=clean_toml(root_r14ac7),
                       crons_manifest=str(manifest_r14ac7))
ok(len(res_r14ac8a["answered"]) >= 1, "R14-AC8: a clean run answers the open brief")

rows_r14ac8.append({"ev": "end", "ts": iso(NOW), "rc": 1, "duration_s": 1.0,
                    "error_class": "rc:1:boom", "error_tail": "boom"})
store_r14ac7_path.write_text("\n".join(json.dumps(r) for r in rows_r14ac8) + "\n")
ev_r14ac8b = read_ledger(root_r14ac7)
res_r14ac8b = look.run(ev_r14ac8b, now=NOW, runner=FakeRunner(), root=root_r14ac7, toml_path=clean_toml(root_r14ac7),
                       crons_manifest=str(manifest_r14ac7))
ok(not briefs_of(res_r14ac8b, "repeat-failure"),
   "R14-AC8: a single new failure after recovery raises nothing (streak of one)")
print("R14-AC8 ok")

# ── R14-AC9: cron-overrun — finished run above/at limit, undetermined schedule ─
ROW_BW9 = {"name": "bw9", "where": "user", "schedule": "* * * * *", "command": "doit bw9",
          "sig": r"\bdoit bw9\b", "path": "/bin/true", "owner": "o", "project": "p", "max_runtime_s": 90}
root_r14ac9a = newroot()
manifest_r14ac9a = _write_cron_manifest(root_r14ac9a, [ROW_BW9])
_write_cron_store(root_r14ac9a, "bw9", [{"ev": "end", "ts": iso(NOW), "rc": 0, "duration_s": 120,
                                        "error_class": "", "error_tail": ""}])
res_r14ac9a = look.run([], now=NOW, runner=FakeRunner(), root=root_r14ac9a, toml_path=clean_toml(root_r14ac9a),
                       crons_manifest=str(manifest_r14ac9a))
ok(len(briefs_of(res_r14ac9a, "cron-overrun")) == 1, "R14-AC9: max_runtime_s-based overrun raises one brief")

ROW_TICK9 = {"name": "tick9", "where": "user", "schedule": "*/5 * * * *", "command": "doit tick9",
            "sig": r"\bdoit tick9\b", "path": "/bin/true", "owner": "o", "project": "p"}
root_r14ac9b = newroot()
manifest_r14ac9b = _write_cron_manifest(root_r14ac9b, [ROW_TICK9])
_write_cron_store(root_r14ac9b, "tick9", [{"ev": "end", "ts": iso(NOW), "rc": 0, "duration_s": 480,
                                          "error_class": "", "error_tail": ""}])
res_r14ac9b = look.run([], now=NOW, runner=FakeRunner(), root=root_r14ac9b, toml_path=clean_toml(root_r14ac9b),
                       crons_manifest=str(manifest_r14ac9b))
ok(len(briefs_of(res_r14ac9b, "cron-overrun")) == 1,
   "R14-AC9: interval_s-based overrun (*/5 row, 480s run) raises one brief")

root_r14ac9c = newroot()
manifest_r14ac9c = _write_cron_manifest(root_r14ac9c, [ROW_BW9])
_write_cron_store(root_r14ac9c, "bw9", [{"ev": "end", "ts": iso(NOW), "rc": 0, "duration_s": 90,
                                        "error_class": "", "error_tail": ""}])
res_r14ac9c = look.run([], now=NOW, runner=FakeRunner(), root=root_r14ac9c, toml_path=clean_toml(root_r14ac9c),
                       crons_manifest=str(manifest_r14ac9c))
ok(not briefs_of(res_r14ac9c, "cron-overrun"), "R14-AC9: a run AT the limit raises none")

ROW_WAT9 = {"name": "wat9", "where": "user", "schedule": "@daily", "command": "doit wat9",
           "sig": r"\bdoit wat9\b", "path": "/bin/true", "owner": "o", "project": "p"}
root_r14ac9d = newroot()
manifest_r14ac9d = _write_cron_manifest(root_r14ac9d, [ROW_WAT9])
_write_cron_store(root_r14ac9d, "wat9", [{"ev": "end", "ts": iso(NOW), "rc": 0, "duration_s": 5,
                                         "error_class": "", "error_tail": ""}])
res_r14ac9d = look.run([], now=NOW, runner=FakeRunner(), root=root_r14ac9d, toml_path=clean_toml(root_r14ac9d),
                       crons_manifest=str(manifest_r14ac9d))
b_r14ac9d = briefs_of(res_r14ac9d, "cron-overrun")
ok(len(b_r14ac9d) == 1 and "undetermined" in str(b_r14ac9d[0].get("reading")),
   f"R14-AC9: unreadable schedule + no max_runtime_s -> one cron-overrun, reading says undetermined: {b_r14ac9d}")
ev_r14ac9d2 = read_ledger(root_r14ac9d)
res_r14ac9d2 = look.run(ev_r14ac9d2, now=NOW, runner=FakeRunner(), root=root_r14ac9d, toml_path=clean_toml(root_r14ac9d),
                        crons_manifest=str(manifest_r14ac9d))
ok(not res_r14ac9d2["answered"], "R14-AC9: the undetermined overrun never clears while both stay so")
print("R14-AC9 ok")

# ── R14-AC10: cron-overrun — an open run, above/at limit; only the newest counts ─
ROW_BW10 = {"name": "bw10", "where": "user", "schedule": "* * * * *", "command": "doit bw10",
           "sig": r"\bdoit bw10\b", "path": "/bin/true", "owner": "o", "project": "p", "max_runtime_s": 90}
root_r14ac10a = newroot()
manifest_r14ac10a = _write_cron_manifest(root_r14ac10a, [ROW_BW10])
_write_cron_store(root_r14ac10a, "bw10", [{"ev": "start", "ts": iso(NOW - timedelta(seconds=200)), "pid": 1}])
res_r14ac10a = look.run([], now=NOW, runner=FakeRunner(), root=root_r14ac10a, toml_path=clean_toml(root_r14ac10a),
                        crons_manifest=str(manifest_r14ac10a))
ok(len(briefs_of(res_r14ac10a, "cron-overrun")) == 1, "R14-AC10: an open run past its limit raises one brief")

root_r14ac10b = newroot()
manifest_r14ac10b = _write_cron_manifest(root_r14ac10b, [ROW_BW10])
_write_cron_store(root_r14ac10b, "bw10", [{"ev": "start", "ts": iso(NOW - timedelta(seconds=30)), "pid": 1}])
res_r14ac10b = look.run([], now=NOW, runner=FakeRunner(), root=root_r14ac10b, toml_path=clean_toml(root_r14ac10b),
                        crons_manifest=str(manifest_r14ac10b))
ok(not briefs_of(res_r14ac10b, "cron-overrun"), "R14-AC10: an open run within its limit raises none")

root_r14ac10c = newroot()
manifest_r14ac10c = _write_cron_manifest(root_r14ac10c, [ROW_BW10])
_write_cron_store(root_r14ac10c, "bw10", [
    {"ev": "start", "ts": iso(NOW - timedelta(seconds=500)), "pid": 1},   # abandoned
    {"ev": "start", "ts": iso(NOW - timedelta(seconds=30)), "pid": 2},    # the real open run
])
res_r14ac10c = look.run([], now=NOW, runner=FakeRunner(), root=root_r14ac10c, toml_path=clean_toml(root_r14ac10c),
                        crons_manifest=str(manifest_r14ac10c))
ok(not briefs_of(res_r14ac10c, "cron-overrun"),
   "R14-AC10: only the newest start row counts as open (an earlier abandoned one is ignored)")
print("R14-AC10 ok")

# ── R14-AC11b: look — cron-unwrapped brief; the wrapped line clears it ──────
ROW_U11B = {"name": "u11b", "where": "user", "schedule": "* * * * *", "command": "doit u11b",
           "sig": r"\bdoit u11b\b", "path": "/bin/true", "owner": "o", "project": "p"}
root_r14ac11 = newroot()
manifest_r14ac11 = _write_cron_manifest(root_r14ac11, [ROW_U11B])
crond_r14ac11 = root_r14ac11 / "crond-empty"
crond_r14ac11.mkdir()
res_r14ac11a = look.run([], now=NOW, runner=FakeRunner(crontab_text=lambda to: "* * * * * doit u11b\n"),
                        root=root_r14ac11, toml_path=clean_toml(root_r14ac11),
                        crons_manifest=str(manifest_r14ac11), crond_dir=str(crond_r14ac11))
ok(len(briefs_of(res_r14ac11a, "cron-unwrapped")) == 1,
   "R14-AC11b: a bare sig-matched row raises one cron-unwrapped brief")

ev_r14ac11b = read_ledger(root_r14ac11)
res_r14ac11b = look.run(ev_r14ac11b, now=NOW,
                        runner=FakeRunner(crontab_text=lambda to: "* * * * * /x/doit cron-run u11b -- doit u11b\n"),
                        root=root_r14ac11, toml_path=clean_toml(root_r14ac11),
                        crons_manifest=str(manifest_r14ac11), crond_dir=str(crond_r14ac11))
ok(len(res_r14ac11b["answered"]) >= 1, "R14-AC11b: the wrapped line answers the open brief")
print("R14-AC11b ok")

# ── R14-AC12: dispatch repeat-failure — builder/grader, refusals skipped ────
root_r14ac12 = newroot()
for i in range(1, 4):
    spawn = f"L-builder-000{i}"
    append_raw(root_r14ac12, "L-executor-0001.jsonl", iso(NOW - timedelta(minutes=30 - i)),
              type="build-started", spawn=spawn, subject="L-spec-9")
    append_raw(root_r14ac12, "L-builder-fail.jsonl", iso(NOW - timedelta(minutes=29 - i)),
              type="spawn-failed", spawn=spawn, reason="x", why="x: boom")
ev_r14ac12a = read_ledger(root_r14ac12)
res_r14ac12a = look.run(ev_r14ac12a, now=NOW, runner=FakeRunner(), root=root_r14ac12, toml_path=clean_toml(root_r14ac12))
b_r14ac12a = briefs_of(res_r14ac12a, "repeat-failure")
ok(len(b_r14ac12a) == 1 and b_r14ac12a[0]["key"] == "builder" and "boom" in str(b_r14ac12a[0].get("why")),
   f"R14-AC12: three build-started/spawn-failed(reason=x) raise one repeat-failure keyed builder: {b_r14ac12a}")

root_r14ac12b = newroot()
for i in range(1, 4):
    spawn = f"L-grader-000{i}"
    append_raw(root_r14ac12b, "L-executor-0001.jsonl", iso(NOW - timedelta(minutes=30 - i)),
              type="spawn-started", spawn=spawn, role="grader", subject="L-spec-9")
    append_raw(root_r14ac12b, "L-grader-fail.jsonl", iso(NOW - timedelta(minutes=29 - i)),
              type="spawn-failed", spawn=spawn, reason="y", why="y: bang")
ev_r14ac12b = read_ledger(root_r14ac12b)
res_r14ac12b = look.run(ev_r14ac12b, now=NOW, runner=FakeRunner(), root=root_r14ac12b, toml_path=clean_toml(root_r14ac12b))
b_r14ac12b = briefs_of(res_r14ac12b, "repeat-failure")
ok(len(b_r14ac12b) == 1 and b_r14ac12b[0]["key"] == "grader", f"R14-AC12: grader case: {b_r14ac12b}")

root_r14ac12c = newroot()
for i in range(1, 4):
    append_raw(root_r14ac12c, "L-ghost-fail.jsonl", iso(NOW - timedelta(minutes=10 - i)),
              type="spawn-failed", spawn=f"L-ghost-000{i}", why="refused: no seat")
ev_r14ac12c = read_ledger(root_r14ac12c)
res_r14ac12c = look.run(ev_r14ac12c, now=NOW, runner=FakeRunner(), root=root_r14ac12c, toml_path=clean_toml(root_r14ac12c))
ok(not briefs_of(res_r14ac12c, "repeat-failure"), "R14-AC12: three refusals alone (no start of their own spawn) raise none")

# a newer spawn-done (with its own start) answers the open builder brief
append_raw(root_r14ac12, "L-executor-0001.jsonl", iso(NOW - timedelta(minutes=3)),
          type="build-started", spawn="L-builder-0004", subject="L-spec-9")
append_raw(root_r14ac12, "L-builder-done.jsonl", iso(NOW - timedelta(minutes=2)),
          type="spawn-done", spawn="L-builder-0004")
ev_r14ac12d = read_ledger(root_r14ac12)
res_r14ac12d = look.run(ev_r14ac12d, now=NOW, runner=FakeRunner(), root=root_r14ac12, toml_path=clean_toml(root_r14ac12))
ok(not briefs_of(res_r14ac12d, "repeat-failure"), "R14-AC12: a newer spawn-done breaks the builder streak")
ok(any(e.get("type") == "brief-answered" for e in read_ledger(root_r14ac12)),
   "R14-AC12: the open builder brief is answered")

# a later single failure starts a fresh streak of one — raises nothing
append_raw(root_r14ac12, "L-executor-0001.jsonl", iso(NOW - timedelta(minutes=1)),
          type="build-started", spawn="L-builder-0005", subject="L-spec-9")
append_raw(root_r14ac12, "L-builder-fail.jsonl", iso(NOW),
          type="spawn-failed", spawn="L-builder-0005", reason="x", why="x: boom2")
ev_r14ac12e = read_ledger(root_r14ac12)
res_r14ac12e = look.run(ev_r14ac12e, now=NOW, runner=FakeRunner(), root=root_r14ac12, toml_path=clean_toml(root_r14ac12))
ok(not briefs_of(res_r14ac12e, "repeat-failure"), "R14-AC12: a later single failure starts a fresh streak of one")
print("R14-AC12 ok")

# ── AC15: cron-missing, per row; crons.Undetermined; unimportable crons ─────
root15 = newroot()


class _Undetermined(Exception):
    pass


class _CronsMissing:
    @staticmethod
    def check(crontab_text=None, manifest=None, crond_dir=None):
        return [{"name": "tmp-reaper"}, {"name": "lessons-digest"}]

    @staticmethod
    def unwrapped(crontab_text=None, manifest=None, crond_dir=None):
        return []

    @staticmethod
    def rows(manifest=None):
        return []


sys.modules["crons"] = _CronsMissing()
try:
    res15 = look.run([], now=NOW, runner=FakeRunner(), root=root15, toml_path=clean_toml(root15))
finally:
    sys.modules["crons"] = crons  # restore the same, already-repointed, real module (never delete — see top-of-file note)
ok(len(briefs_of(res15, "cron-missing")) == 2, "AC15: two missing rows -> two separate briefs")
ok({b["owner"] for b in briefs_of(res15, "cron-missing")} == {"thinker"}, "AC15: owner thinker each")

root15b = newroot()


class _CronsRaises:
    Undetermined = _Undetermined

    @staticmethod
    def check(crontab_text=None, manifest=None, crond_dir=None):
        raise _Undetermined("cron state unknown")

    @staticmethod
    def unwrapped(crontab_text=None, manifest=None, crond_dir=None):
        return []

    @staticmethod
    def rows(manifest=None):
        return []


sys.modules["crons"] = _CronsRaises()
try:
    res15b = look.run([], now=NOW, runner=FakeRunner(), root=root15b, toml_path=clean_toml(root15b))
finally:
    sys.modules["crons"] = crons  # restore the same, already-repointed, real module (never delete — see top-of-file note)
ok(not briefs_of(res15b, "cron-missing"), "AC15: Undetermined -> no cron-missing brief")
ok(any(b["key"] == "undetermined:crons" for b in briefs_of(res15b, "reading-undetermined")),
   "AC15: Undetermined -> exactly one reading-undetermined")

root15c = newroot()
res15c = look.run([], now=NOW, runner=FakeRunner(), root=root15c, toml_path=clean_toml(root15c))
ok(any(b["key"] == "undetermined:crons" for b in briefs_of(res15c, "reading-undetermined")),
   "AC15: crons unimportable -> the same reading lands")
ok(not briefs_of(res15c, "cron-missing"), "AC15: crons unimportable -> no cron-missing brief")
print("AC15 ok")

# ── AC16: spec-misrouted, both source shapes, plus the no-spec_path degrade ─
root16 = newroot()
seat16 = root16 / "seat"
seat16.mkdir()
(seat16 / "L-spec-writer-0099.cmd.json").write_text(json.dumps({"cmd": ["x"], "cwd": "/x", "path": "/wrong/path.md"}))
append_raw(root16, "L-executor-0001.jsonl", iso(NOW - timedelta(hours=1)),
          type="spawn-started", spawn="L-spec-writer-0099", subject="L-spec-0400", role="spec-writer")
dispatch.spec_path = lambda subject: pathlib.Path("/content") / f"{subject}.md"
try:
    ev16 = read_ledger(root16)
    res16 = look.run(ev16, now=NOW, runner=FakeRunner(), root=root16, toml_path=clean_toml(root16))
    ok(any(b["key"] == "L-spec-0400" for b in briefs_of(res16, "spec-misrouted")),
       "AC16: a cmd.json path mismatch briefs, keyed by the joined subject")

    root16b = newroot()
    seat16b = root16b / "seat"
    seat16b.mkdir()
    (seat16b / "L-spec-writer-0100.cmd.json").write_text(json.dumps({"cmd": ["x"], "cwd": "/x", "path": "/content/L-spec-0401.md"}))
    append_raw(root16b, "L-executor-0001.jsonl", iso(NOW - timedelta(hours=1)),
              type="spawn-started", spawn="L-spec-writer-0100", subject="L-spec-0401", role="spec-writer")
    ev16b = read_ledger(root16b)
    res16b = look.run(ev16b, now=NOW, runner=FakeRunner(), root=root16b, toml_path=clean_toml(root16b))
    ok(not briefs_of(res16b, "spec-misrouted"), "AC16: a matching path briefs nothing")

    root16c = newroot()
    seat16c = root16c / "seat"
    seat16c.mkdir()
    (seat16c / "L-spec-writer-0101.cmd.json").write_text(json.dumps({"cmd": ["x"], "cwd": "/x", "path": "/wrong.md"}))
    ev16c = read_ledger(root16c)
    res16c = look.run(ev16c, now=NOW, runner=FakeRunner(), root=root16c, toml_path=clean_toml(root16c))
    ok(any(b["key"] == "L-spec-writer-0101" for b in briefs_of(res16c, "spec-misrouted")),
       "AC16: no joinable spawn-started -> keyed by the bare spawn id")

    root16d = newroot()
    seat16d = root16d / "seat"
    seat16d.mkdir()
    (seat16d / "L-spec-writer-0102.cmd.json").write_text(json.dumps({"cmd": ["x"], "cwd": "/x", "path": "/wrong.md"}))
    old_mtime = time.time() - 25 * 3600
    os.utime(seat16d / "L-spec-writer-0102.cmd.json", (old_mtime, old_mtime))
    ev16d = read_ledger(root16d)
    res16d = look.run(ev16d, now=NOW, runner=FakeRunner(), root=root16d, toml_path=clean_toml(root16d))
    ok(not briefs_of(res16d, "spec-misrouted"), "AC16: older than 24h with a genuine mismatch appends nothing")

    root16e = newroot()
    append_raw(root16e, "L-planner-0001.jsonl", iso(NOW - timedelta(hours=1)),
              type="spec-written", spec="L-spec-0402", path="/bad/path.md")
    ev16e = read_ledger(root16e)
    res16e = look.run(ev16e, now=NOW, runner=FakeRunner(), root=root16e, toml_path=clean_toml(root16e))
    ok(any(b["key"] == "L-spec-0402" for b in briefs_of(res16e, "spec-misrouted")),
       "AC16: a spec-written path mismatch briefs, keyed by spec")

    root16f = newroot()
    append_raw(root16f, "L-planner-0001.jsonl", iso(NOW - timedelta(hours=1)),
              type="spec-written", spec="L-spec-0403")
    ev16f = read_ledger(root16f)
    res16f = look.run(ev16f, now=NOW, runner=FakeRunner(), root=root16f, toml_path=clean_toml(root16f))
    ok(any(b["key"] == "L-spec-0403" for b in briefs_of(res16f, "spec-misrouted")),
       "AC16: a missing path, within the window, also briefs")
finally:
    del dispatch.spec_path

ok(getattr(dispatch, "spec_path", None) is None, "sanity: dispatch.spec_path restored to absent")
root16g = newroot()
seat16g = root16g / "seat"
seat16g.mkdir()
(seat16g / "L-spec-writer-0199.cmd.json").write_text(json.dumps({"cmd": ["x"], "cwd": "/x", "path": "/wrong.md"}))
ev16g = read_ledger(root16g)
res16g = look.run(ev16g, now=NOW, runner=FakeRunner(), root=root16g, toml_path=clean_toml(root16g))
ok(not briefs_of(res16g, "spec-misrouted"), "AC16: with dispatch.spec_path absent, no spec-misrouted brief fires")
ok(any(b["key"] == "undetermined:spec-misrouted" for b in briefs_of(res16g, "reading-undetermined")),
   "AC16: absent spec_path -> exactly one reading-undetermined instead")
print("AC16 ok")

# ── AC18: the 90s pass-budget deadline, scripted clock, no real sleep ───────
root18 = newroot()
calls18 = []


def ssh18_slow(target, cmd, timeout):
    calls18.append(fake18.clock())
    return "200" if "-w" in cmd else json.dumps({"sha": "same111"})


fake18 = FakeRunner(ssh=ssh18_slow, git=lambda a, cwd, to: "same111", clock_start=0.0)
res18 = look.run([], now=NOW, runner=fake18, root=root18, toml_path=write_toml(root18, prod=[prod_row()]))
ok(all(t < 90 for t in calls18), "AC18a: an ordinary pass issues every call before the deadline")
ok(not any(b["condition"] == "reading-undetermined" and "prod" in str(b.get("reading")) for b in res18["briefs"]),
   "AC18a: no spurious undetermined when comfortably inside budget")

root18b = newroot()
calls18b = []


class JumpingRunner(FakeRunner):
    """The FIRST `clock()` call (computing the deadline) reads 0; every call
    after that jumps straight past it — no external call may fire."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._n = 0

    def clock(self):
        self._n += 1
        return 0.0 if self._n == 1 else 200.0


fake18b = JumpingRunner(ssh=lambda t, c, to: "200" if "-w" in c else json.dumps({"sha": "x"}),
                        git=lambda a, cwd, to: "x")
res18b = look.run([], now=NOW, runner=fake18b, root=root18b, toml_path=write_toml(root18b, prod=[prod_row()]))
ok(fake18b.calls == [], "AC18b: a clock already past deadline fires no external call at all")
ok(len(res18b["briefs"]) >= 1 and all(b["condition"] == "reading-undetermined" for b in res18b["briefs"]),
   "AC18b: every reading after the jump comes back reading-undetermined")
print("AC18 ok")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0482/R12.d — the `grades-per-shipped-high` alarm ────────────────────
# ══════════════════════════════════════════════════════════════════════════════
ok(look.DEFAULTS["thresholds"]["grades_per_shipped_high"] == 1.5, "AC12: DEFAULTS carries 1.5")
_committed_toml482 = look.load_toml(look.DEFAULT_TOML_PATH)
ok(_committed_toml482["thresholds"]["grades_per_shipped_high"] == 1.5, "AC12: the committed look.toml carries 1.5 too")


def _gps_root482(root, n_specs, extra_runs_on_first=0):
    """`n_specs` shipped specs (recent, within the window), one grader
    `spawn-started` each, plus `extra_runs_on_first` more on the first one —
    ratio = (n_specs + extra_runs_on_first) / n_specs."""
    for i in range(n_specs):
        sid = f"L-spec-9482{i}"
        append_raw(root, "L-executor-0001.jsonl", iso(NOW), type="shipped", subject=sid)
        for j in range(1 + (extra_runs_on_first if i == 0 else 0)):
            append_raw(root, f"L-grader-9482{i}{j}.jsonl", iso(NOW), type="spawn-started",
                      subject=sid, role="grader", spawn=f"g482{i}{j}")
    return read_ledger(root)


# ratio 1.6 (8 runs / 5 specs) fires.
root12_482 = newroot()
ev12a_482 = _gps_root482(root12_482, n_specs=5, extra_runs_on_first=3)
res12a_482 = look.run(ev12a_482, now=NOW, runner=FakeRunner(), root=root12_482, toml_path=clean_toml(root12_482))
b12a_482 = briefs_of(res12a_482, "grades-per-shipped-high")
ok(len(b12a_482) == 1 and b12a_482[0]["owner"] == "thinker", f"AC12: ratio 1.6 fires: {b12a_482}")

# ratio exactly 1.5 (6/4) stays quiet — a fresh root, no prior state.
root12b_482 = newroot()
ev12b_482 = _gps_root482(root12b_482, n_specs=4, extra_runs_on_first=2)
res12b_482 = look.run(ev12b_482, now=NOW, runner=FakeRunner(), root=root12b_482, toml_path=clean_toml(root12b_482))
ok(briefs_of(res12b_482, "grades-per-shipped-high") == [], "AC12: ratio exactly 1.5 stays quiet")

# nothing shipped (ratio None) stays quiet.
root12c_482 = newroot()
res12c_482 = look.run([], now=NOW, runner=FakeRunner(), root=root12c_482, toml_path=clean_toml(root12c_482))
ok(briefs_of(res12c_482, "grades-per-shipped-high") == [], "AC12: ratio None stays quiet")

# a later pass observing ratio 1.0, against the SAME standing state as the
# 1.6 fire above, clears it (the "ref" ties the clear to that exact brief).
root12d_events_482 = newroot()
ev12d_482 = _gps_root482(root12d_events_482, n_specs=3, extra_runs_on_first=0)   # 3/3 = 1.0
res12d_482 = look.run(ev12d_482, now=NOW, runner=FakeRunner(), root=root12_482, toml_path=clean_toml(root12_482))
ok(any(a.get("ref") == b12a_482[0]["_src"] for a in res12d_482["answered"]),
   f"AC12: a later ratio-1.0 pass clears the standing 1.6 brief: {res12d_482['answered']}")

# a raised toml threshold (2.0) keeps a 1.6 fixture quiet.
root12e_482 = newroot()
ev12e_482 = _gps_root482(root12e_482, n_specs=5, extra_runs_on_first=3)
res12e_482 = look.run(ev12e_482, now=NOW, runner=FakeRunner(), root=root12e_482,
                      toml_path=write_toml(root12e_482, prod=[], thresholds={"grades_per_shipped_high": 2.0}))
ok(briefs_of(res12e_482, "grades-per-shipped-high") == [], "AC12: a raised toml threshold (2.0) keeps 1.6 quiet")
print("L-spec-0482 AC12 ok")

print(f"look: {n} checks passed")
