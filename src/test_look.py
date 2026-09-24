#!/usr/bin/env python3
"""Checks on look — the babysitter's ten-minute check, as a script
(L-charter-0036 R1/R3/R4/R5, L-spec-0322). Run: python3 test_look.py

Every external read goes through a fake `Runner`; nothing here opens a real
ssh/git/tmux/crontab/df call. `panes`/`pane_resume` reads (local session and
transcript files) are pointed at tempdirs explicitly, never the operator's
real `~/.claude/sessions`. Clocks used for the 90s-budget mechanism (AC18)
are the fake Runner's own scripted `clock()`/`sleep()` — never a real wait.
"""
import json, os, pathlib, socket, subprocess, sys, tempfile, time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import dispatch, fold, look, panes  # noqa: E402

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
                 disk_usage=None, clock_start=0.0):
        self._ssh = ssh or (lambda target, cmd, timeout: "")
        self._git = git or (lambda args, cwd, timeout: "")
        self._tmux = tmux_capture or (lambda target, timeout: "")
        self._crontab = crontab_text or (lambda timeout: "")
        self._disk = disk_usage or (lambda path, timeout: {"total": 100, "used": 10, "free": 90})
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


# ── AC1: single injectable Runner; a real-subprocess sentinel proves no leak ─
root1 = newroot()
raising = lambda *a, **k: (_ for _ in ()).throw(AssertionError("real subprocess/shutil call leaked past Runner"))
real_run, real_popen, real_du = subprocess.run, os.popen, __import__("shutil").disk_usage
subprocess.run, os.popen = raising, raising
import shutil as _shutil
_shutil.disk_usage = raising
class _CronsAllPresent:
    @staticmethod
    def check(crontab_text=None):
        return []


sys.modules["crons"] = _CronsAllPresent()
try:
    fake1 = FakeRunner(ssh=lambda t, c, to: json.dumps({"sha": "a" * 7}) if "-w" not in c else "200",
                       git=lambda a, cwd, to: "a" * 7,
                       tmux_capture=lambda t, to: "some codex text\n",
                       crontab_text=lambda to: "")
    toml1 = write_toml(root1, prod=[prod_row()], pane_at_menu={"codex_patterns": ["nomatch"], "codex_targets": ["t1"]})
    res1 = look.run([], now=NOW, runner=fake1, root=root1, toml_path=toml1)
    methods1 = {c[0] for c in fake1.calls}
    ok(methods1 == {"ssh", "git", "tmux_capture", "crontab_text", "disk_usage"}, "AC1: every reading used the fixture")
    ok(sum(1 for c in fake1.calls if c[0] == "ssh" and "-w" in c[2]) == 1, "AC1: one health read (early 200)")
finally:
    subprocess.run, os.popen, _shutil.disk_usage = real_run, real_popen, real_du
    del sys.modules["crons"]
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
    def check(crontab_text=None):
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
    del sys.modules["crons"]
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
    del sys.modules["crons"]
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

# ── AC15: cron-missing, per row; crons.Undetermined; unimportable crons ─────
root15 = newroot()


class _Undetermined(Exception):
    pass


class _CronsMissing:
    @staticmethod
    def check(crontab_text=None):
        return [{"name": "tmp-reaper"}, {"name": "lessons-digest"}]


sys.modules["crons"] = _CronsMissing()
try:
    res15 = look.run([], now=NOW, runner=FakeRunner(), root=root15, toml_path=clean_toml(root15))
finally:
    del sys.modules["crons"]
ok(len(briefs_of(res15, "cron-missing")) == 2, "AC15: two missing rows -> two separate briefs")
ok({b["owner"] for b in briefs_of(res15, "cron-missing")} == {"thinker"}, "AC15: owner thinker each")

root15b = newroot()


class _CronsRaises:
    Undetermined = _Undetermined

    @staticmethod
    def check(crontab_text=None):
        raise _Undetermined("cron state unknown")


sys.modules["crons"] = _CronsRaises()
try:
    res15b = look.run([], now=NOW, runner=FakeRunner(), root=root15b, toml_path=clean_toml(root15b))
finally:
    del sys.modules["crons"]
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

print(f"look: {n} checks passed")
