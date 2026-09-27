#!/usr/bin/env python3
"""Checks on look_wallclock — the four wall-clock checks (L-charter-0038 R6,
L-spec-0389). Run: python3 test_look_wallclock.py

Every external read goes through a fake `Runner` (CI) or an explicit fixture
directory (quota/message transcripts, save/restore around `pane_resume.
PROJECTS`/`panes.SESSIONS`) — never the operator's real `~/.claude/{projects,
sessions}`, never a real `gh`/network call, never `claude -p` (SD13).
"""
import json, os, pathlib, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import dispatch, fold, look, look_wallclock, pane_resume, panes, relay  # noqa: E402

# Never read the operator's real ~/.claude/{projects,sessions} (SD13) — the
# quota/message checks glob `pane_resume.PROJECTS`/`panes.SESSIONS` by default,
# so every AC that does not explicitly patch them (CI/planner-stuck/ledger-only
# message ACs) must still see empty, guaranteed-inert directories.
pane_resume.PROJECTS = pathlib.Path(tempfile.mkdtemp())
panes.SESSIONS = pathlib.Path(tempfile.mkdtemp())

n = 0


def ok(cond, why):
    global n
    assert cond, why
    n += 1


NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
CI_ROW = {"project": "albert-scott", "repo": "/opt/albert-scott", "branch": "master", "workflows": []}


def newroot():
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "events").mkdir()
    return d


def iso(dt):
    return dt.isoformat(timespec="seconds")


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


def cfg(ci=None, classes=None):
    return {"ci": ci if ci is not None else [], "classes": classes or {}}


def wb(root):
    """`look._write_brief` — the SAME function `look.run()` injects as `write_brief`."""
    look._DRY_RUN = False
    return look._write_brief


def session(sessions_dir, name, session_id, pid):
    meta = {"name": name, "cwd": "/x", "status": "idle", "sessionId": session_id, "tmux": f"{name}:1.1"}
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{pid}.json").write_text(json.dumps(meta))


def transcript(projects_dir, subdir, session_id, entries, mtime=None):
    p = projects_dir / subdir
    p.mkdir(parents=True, exist_ok=True)
    f = p / f"{session_id}.jsonl"
    f.write_text("".join(json.dumps(e) + "\n" for e in entries))
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(f, (ts, ts))
    return f


def sys_hit(content, ts=None):
    e = {"type": "system", "subtype": "informational", "content": content}
    if ts is not None:
        e["timestamp"] = iso(ts)
    return e


class DummyRunner:
    """Quota/message-answered/planner-stuck never touch `runner` at all; only
    the CI check does, through `gh_run_list` alone (never real subprocess)."""
    def __init__(self, gh=None, clock_start=0.0):
        self._gh = gh or (lambda repo, branch, timeout: "[]")
        self._t = clock_start
        self.calls = []

    def gh_run_list(self, repo, branch, timeout):
        self.calls.append(("gh_run_list", repo, branch, timeout))
        return self._gh(repo, branch, timeout)

    def clock(self):
        return self._t

    def sleep(self, seconds):
        self._t += seconds


def patched_dirs(proj, sess):
    """A context manager-free save/restore pair for `pane_resume.PROJECTS`/
    `panes.SESSIONS` — every quota/message AC uses this so no test ever reads
    the operator's real `~/.claude` tree."""
    saved = pane_resume.PROJECTS, panes.SESSIONS
    pane_resume.PROJECTS, panes.SESSIONS = proj, sess
    return saved


def restore_dirs(saved):
    pane_resume.PROJECTS, panes.SESSIONS = saved


# ── AC1: quota pane-paused — evidence dedupe, resets_at, ref, fallback pane ──
def _quota_fixture(sid, entries, mtime, pane_meta=None):
    """A fully ISOLATED projects/sessions pair per sub-case — the quota check
    globs its whole `pane_resume.PROJECTS` tree, so sub-cases sharing one
    directory would cross-contaminate each other's results."""
    r = newroot()
    proj, sess = r / "projects", r / "sessions"
    if pane_meta:
        session(sess, *pane_meta)
    transcript(proj, "d", sid, entries, mtime=mtime)
    return r, proj, sess


root1, proj1, sess1 = _quota_fixture(
    "SID", [sys_hit("Usage limit reached · continuing automatically at 3:45pm · esc or type to cancel")],
    NOW - timedelta(hours=1), ("L-thinker-0004", "SID", os.getpid()))
saved1 = patched_dirs(proj1, sess1)
try:
    res1a = look_wallclock.run([], DummyRunner(), NOW, root1, cfg(), 10**9, wb(root1), False)
    ok(len(res1a["events"]) == 1 and res1a["events"][0]["type"] == "pane-paused", "AC1: exactly one pane-paused")
    ev1 = res1a["events"][0]
    ok(ev1["pane"] == "L-thinker-0004" and ev1["reason"] == "quota" and ev1["resets_at"] == "3:45pm"
       and "ref" not in ev1, "AC1: resolved pane, parsed resets_at, no ref (no escalation-blocking)")
    ok(ev1["evidence"] == "SID.jsonl:1", "AC1: evidence is <file-stem>.jsonl:<line-number>")

    evs1 = read_ledger(root1)
    res1b = look_wallclock.run(evs1, DummyRunner(), NOW, root1, cfg(), 10**9, wb(root1), False)
    ok(res1b["events"] == [], "AC1: a second pass over the SAME fixture appends nothing further")

    (root1 / "look" / "state.json").unlink()
    evs1b = read_ledger(root1)
    res1c = look_wallclock.run(evs1b, DummyRunner(), NOW, root1, cfg(), 10**9, wb(root1), False)
    ok(res1c["events"] == [], "AC1: a third pass after state.json is deleted still appends nothing")
finally:
    restore_dirs(saved1)

# backdated 3h — outside the 2h window — ignored entirely
root1d, proj1d, sess1d = _quota_fixture("SID-OLD", [sys_hit("Usage limit reached")], NOW - timedelta(hours=3))
saved1d = patched_dirs(proj1d, sess1d)
try:
    res1d = look_wallclock.run([], DummyRunner(), NOW, root1d, cfg(), 10**9, wb(root1d), False)
    ok(not any(e["type"] == "pane-paused" for e in res1d["events"]), "AC1: a file backdated 3h is ignored entirely")
finally:
    restore_dirs(saved1d)

# no "continuing automatically at" clause -> resets_at omitted
root1e, proj1e, sess1e = _quota_fixture("SID-NOCLAUSE", [sys_hit("Usage limit reached")], NOW - timedelta(minutes=30))
saved1e = patched_dirs(proj1e, sess1e)
try:
    res1e = look_wallclock.run([], DummyRunner(), NOW, root1e, cfg(), 10**9, wb(root1e), False)
    e1e = next(e for e in res1e["events"] if e["type"] == "pane-paused")
    ok("resets_at" not in e1e, "AC1: no clause -> resets_at omitted")
finally:
    restore_dirs(saved1e)

# a LATER structured quotaLimits.resetsAt overrides the parsed text
root1f, proj1f, sess1f = _quota_fixture(
    "SID-STRUCT", [sys_hit("Usage limit reached · continuing automatically at 3:45pm"),
                  {"type": "assistant", "quotaLimits": {"resetsAt": "2026-09-26T23:45:00Z"}}],
    NOW - timedelta(minutes=30))
saved1f = patched_dirs(proj1f, sess1f)
try:
    res1f = look_wallclock.run([], DummyRunner(), NOW, root1f, cfg(), 10**9, wb(root1f), False)
    e1f = next(e for e in res1f["events"] if e["type"] == "pane-paused")
    ok(e1f["resets_at"] == "2026-09-26T23:45:00Z", "AC1: a later structured resetsAt wins over the parsed text")
finally:
    restore_dirs(saved1f)

# unjoinable session id (no live pane at all) -> falls back to the bare sid
root1g, proj1g, sess1g = _quota_fixture("SID-UNJOINED", [sys_hit("Usage limit reached")], NOW - timedelta(minutes=30))
saved1g = patched_dirs(proj1g, sess1g)
try:
    res1g = look_wallclock.run([], DummyRunner(), NOW, root1g, cfg(), 10**9, wb(root1g), False)
    e1g = next(e for e in res1g["events"] if e["type"] == "pane-paused")
    ok(e1g["pane"] == "SID-UNJOINED", "AC1: an unresolvable session id falls back to the bare session id")
finally:
    restore_dirs(saved1g)

# ref: an existing escalation-blocking{reason_class:"quota"} for the resolved pane
root1h, proj1h, sess1h = _quota_fixture(
    "SID-REF", [sys_hit("Usage limit reached")], NOW - timedelta(minutes=30),
    ("L-thinker-0004", "SID-REF", os.getpid()))
saved1h = patched_dirs(proj1h, sess1h)
try:
    esc = {"type": "escalation-blocking", "subject": "L-thinker-0004", "reason_class": "quota",
          "_src": "L-tick-local.jsonl:1", "ts": iso(NOW - timedelta(minutes=40))}
    res1h = look_wallclock.run([esc], DummyRunner(), NOW, root1h, cfg(), 10**9, wb(root1h), False)
    e1h = next(e for e in res1h["events"] if e["type"] == "pane-paused")
    ok(e1h.get("ref") == "L-tick-local.jsonl:1", "AC1: an open escalation-blocking sets ref to its own _src")
finally:
    restore_dirs(saved1h)
print("AC1 ok")

# ── AC2: pane-pause-ended — synthetic rejected turn never resolves, a real one does
root2 = newroot()
proj2, sess2 = root2 / "projects", root2 / "sessions"
session(sess2, "L-thinker-0004", "SID2", os.getpid())
hit_ts2 = NOW - timedelta(minutes=30)
tp2 = transcript(proj2, "p", "SID2",
                 [sys_hit("Usage limit reached · continuing automatically at 4:00pm", ts=hit_ts2)],
                 mtime=NOW - timedelta(hours=1))
saved2 = patched_dirs(proj2, sess2)
try:
    res2a = look_wallclock.run([], DummyRunner(), NOW, root2, cfg(), 10**9, wb(root2), False)
    paused2 = next(e for e in res2a["events"] if e["type"] == "pane-paused")
    ref2 = paused2["_src"]

    entries2 = [json.loads(l) for l in tp2.read_text().splitlines()]
    entries2.append({"type": "assistant", "timestamp": iso(hit_ts2 + timedelta(minutes=5)),
                     "isApiErrorMessage": True, "quotaLimits": {"status": "rejected"}, "message": {"content": []}})
    tp2.write_text("".join(json.dumps(e) + "\n" for e in entries2))
    evs2 = read_ledger(root2)
    res2b = look_wallclock.run(evs2, DummyRunner(), NOW, root2, cfg(), 10**9, wb(root2), False)
    ok(not any(e["type"] == "pane-pause-ended" for e in res2b["events"]),
       "AC2: a synthetic rejected turn right after the hit never resolves the pause")

    entries2.append({"type": "assistant", "timestamp": iso(hit_ts2 + timedelta(minutes=10)),
                     "message": {"content": [{"type": "text", "text": "back"}]}})
    tp2.write_text("".join(json.dumps(e) + "\n" for e in entries2))
    evs2b = read_ledger(root2)
    res2c = look_wallclock.run(evs2b, DummyRunner(), NOW, root2, cfg(), 10**9, wb(root2), False)
    ended2 = next((e for e in res2c["events"] if e["type"] == "pane-pause-ended"), None)
    ok(ended2 is not None and ended2["ref"] == ref2 and ended2["pane"] == "L-thinker-0004",
       "AC2: a LATER real turn resolves the pause, ref names the pane-paused's own _src")

    evs2c = read_ledger(root2)
    res2d = look_wallclock.run(evs2c, DummyRunner(), NOW, root2, cfg(), 10**9, wb(root2), False)
    ok(not any(e["type"] == "pane-pause-ended" for e in res2d["events"]), "AC2: already-cleared, nothing further")
finally:
    restore_dirs(saved2)
print("AC2 ok")

# ── AC3: master CI — red/green transitions, cancelled dropped, workflows= ───
runs3 = [
    {"name": "Dashboard PR Quality", "status": "completed", "conclusion": "failure",
     "headSha": "sha1", "url": "url1", "updatedAt": iso(NOW - timedelta(minutes=30))},
    {"name": "CI", "status": "completed", "conclusion": "success",
     "headSha": "sha2", "url": "url2", "updatedAt": iso(NOW - timedelta(minutes=20))},
    {"name": "Dashboard PR Quality", "status": "completed", "conclusion": "cancelled",
     "headSha": "sha3", "url": "url3", "updatedAt": iso(NOW - timedelta(minutes=5))},
]
root3 = newroot()
runner3 = DummyRunner(gh=lambda repo, branch, timeout: json.dumps(runs3))
res3a = look_wallclock.run([], runner3, NOW, root3, cfg(ci=[CI_ROW]), 10**9, wb(root3), False)
red3 = [e for e in res3a["events"] if e["type"] == "ci-red"]
ok(len(red3) == 1 and red3[0]["workflow"] == "Dashboard PR Quality" and red3[0]["sha"] == "sha1"
   and red3[0]["since"] == NOW.isoformat(timespec="seconds"),
   "AC3: ci-red keyed off the failure run, never the newer cancelled one; since==now")
ok(not any(e["type"] == "ci-green" and e.get("workflow") == "CI" for e in res3a["events"]),
   "AC3: an already-green workflow with no prior state emits nothing")

evs3 = read_ledger(root3)
res3b = look_wallclock.run(evs3, runner3, NOW + timedelta(minutes=1), root3, cfg(ci=[CI_ROW]), 10**9, wb(root3), False)
ok(not any(e["type"] == "ci-red" for e in res3b["events"]), "AC3: an immediately following pass appends nothing further")

runs3b = [{"name": "Dashboard PR Quality", "status": "completed", "conclusion": "success",
          "headSha": "sha4", "url": "url4", "updatedAt": iso(NOW + timedelta(minutes=10))}]
runner3c = DummyRunner(gh=lambda repo, branch, timeout: json.dumps(runs3b))
evs3b = read_ledger(root3)
res3c = look_wallclock.run(evs3b, runner3c, NOW + timedelta(minutes=10), root3, cfg(ci=[CI_ROW]), 10**9, wb(root3), False)
green3 = [e for e in res3c["events"] if e["type"] == "ci-green"]
ok(len(green3) == 1 and green3[0]["workflow"] == "Dashboard PR Quality", "AC3: a later success clears the row with ci-green")

root3w = newroot()
res3w = look_wallclock.run([], DummyRunner(gh=lambda r, b, t: json.dumps(runs3)), NOW, root3w,
                           cfg(ci=[{**CI_ROW, "workflows": ["CI"]}]), 10**9, wb(root3w), False)
ok({e.get("workflow") for e in res3w["events"]} <= {"CI"}, "AC3: workflows=['CI'] tracks only CI")

captured_argv = []
real_run = subprocess.run
def _spy_run(argv, **kw):
    captured_argv.append(argv)
    class R:
        stdout = "[]"
    return R()
subprocess.run = _spy_run
try:
    look.Runner().gh_run_list("/opt/albert-scott", "master", 10)
finally:
    subprocess.run = real_run
ok(captured_argv and "--event" in captured_argv[0]
   and captured_argv[0][captured_argv[0].index("--event") + 1] == "push",
   "AC3: a spy on Runner.gh_run_list's real argument construction proves --event push is always passed")
print("AC3 ok")

# ── AC4: message answered — the ledger-reply path, and the same-actor exclusion
root4 = newroot()
append_raw(root4, "L-planner-0001.jsonl", iso(NOW - timedelta(minutes=10)), type="message-sent", to="someone", text="hello")
evs4 = read_ledger(root4)
src4 = evs4[0]["_src"]
append_raw(root4, "L-executor-0001.jsonl", iso(NOW - timedelta(minutes=3)), type="decision", ref=src4, subject="x")
evs4b = read_ledger(root4)
res4a = look_wallclock.run(evs4b, DummyRunner(), NOW, root4, cfg(), 10**9, wb(root4), False)
ans4 = [e for e in res4a["events"] if e["type"] == "message-answered"]
ok(len(ans4) == 1 and ans4[0]["ref"] == src4 and ans4[0]["by"] == "executor" and abs(ans4[0]["delay_min"] - 7) < 0.01,
   "AC4: a LATER different-actor decision answers the message; delay_min is the ts difference")
evs4c = read_ledger(root4)
res4b = look_wallclock.run(evs4c, DummyRunner(), NOW, root4, cfg(), 10**9, wb(root4), False)
ok(not any(e["type"] == "message-answered" for e in res4b["events"]), "AC4: a second pass appends nothing further")

root4d = newroot()
append_raw(root4d, "L-planner-0001.jsonl", iso(NOW - timedelta(minutes=10)), type="message-sent", to="someone", text="hello")
evs4d = read_ledger(root4d)
src4d = evs4d[0]["_src"]
append_raw(root4d, "L-planner-0001.jsonl", iso(NOW - timedelta(minutes=3)), type="correction", ref=src4d)
evs4e = read_ledger(root4d)
res4c = look_wallclock.run(evs4e, DummyRunner(), NOW, root4d, cfg(), 10**9, wb(root4d), False)
ok(not any(e["type"] == "message-answered" for e in res4c["events"]), "AC4: a same-actor correction never answers")
print("AC4 ok")

# ── AC5: message answered — the transcript path, comma-split, note-only skip ─
root5 = newroot()
proj5, sess5 = root5 / "projects", root5 / "sessions"
session(sess5, "L-thinker-0004", "SID5", os.getpid())
msg_text = "x" * 130
T0_5 = NOW - timedelta(minutes=20)
T1_5 = NOW - timedelta(minutes=5)
transcript(proj5, "p", "SID5", [
    {"type": "user", "message": {"content": [{"type": "text", "text": msg_text}]}},
    {"type": "assistant", "timestamp": iso(T1_5), "message": {"content": [{"type": "text", "text": "ok"}]}},
])
append_raw(root5, "L-planner-0001.jsonl", iso(T0_5), type="message-sent",
          to="L-thinker-0004,albert-scott-dc", text=msg_text)
saved5 = patched_dirs(proj5, sess5)
try:
    evs5 = read_ledger(root5)
    res5a = look_wallclock.run(evs5, DummyRunner(), NOW, root5, cfg(), 10**9, wb(root5), False)
    ans5 = [e for e in res5a["events"] if e["type"] == "message-answered"]
    ok(len(ans5) == 1 and ans5[0]["by"] == "L-thinker-0004",
       "AC5: comma-split tries the FIRST ledger pane name, never the tmux target after it")
finally:
    restore_dirs(saved5)

root5b = newroot()
proj5b, sess5b = root5b / "projects", root5b / "sessions"
session(sess5b, "L-thinker-0004", "SID5B", os.getpid())
transcript(proj5b, "p", "SID5B", [{"type": "user", "message": {"content": [{"type": "text", "text": "unrelated"}]}}])
append_raw(root5b, "L-planner-0001.jsonl", iso(T0_5), type="message-sent", to="L-thinker-0004", text=msg_text)
saved5b = patched_dirs(proj5b, sess5b)
try:
    evs5b = read_ledger(root5b)
    res5b = look_wallclock.run(evs5b, DummyRunner(), NOW, root5b, cfg(), 10**9, wb(root5b), False)
    ok(not any(e["type"] == "message-answered" for e in res5b["events"]), "AC5: a readable transcript with no match appends nothing")
finally:
    restore_dirs(saved5b)

root5c = newroot()
proj5c, sess5c = root5c / "projects", root5c / "sessions"
session(sess5c, "L-thinker-0004", "SID5C", os.getpid())
(proj5c / "p").mkdir(parents=True, exist_ok=True)
(proj5c / "p" / "SID5C.jsonl").mkdir()  # a DIRECTORY where a file is expected -> read_text() raises OSError
append_raw(root5c, "L-planner-0001.jsonl", iso(T0_5), type="message-sent", to="L-thinker-0004", text=msg_text)
saved5c = patched_dirs(proj5c, sess5c)
try:
    evs5c = read_ledger(root5c)
    res5c = look_wallclock.run(evs5c, DummyRunner(), NOW, root5c, cfg(), 10**9, wb(root5c), False)
    ok(not any(e["type"] == "message-answered" for e in res5c["events"]), "AC5: an unreadable transcript never falsely answers"
      ) and ok(any(b["key"] == "undetermined:message-answered" for b in res5c["briefs"]),
               "AC5: an unreadable transcript reads-undetermined instead")
finally:
    restore_dirs(saved5c)

root5d = newroot()
append_raw(root5d, "L-planner-0001.jsonl", iso(T0_5), type="message-sent", to="executor", note="only a summary")
evs5d = read_ledger(root5d)
res5d = look_wallclock.run(evs5d, DummyRunner(), NOW, root5d, cfg(), 10**9, wb(root5d), False)
ok(not any(e["type"] == "message-answered" for e in res5d["events"]) and
  not any(b["key"] == "undetermined:message-answered" for b in res5d["briefs"]),
  "AC5: a bare role word with only note (no text) appends nothing, never undetermined")
print("AC5 ok")

# ── AC6: planner attempts stuck — one restart exhausted + a later decision ──
def _planner_stuck_fixture(root, cid, decision_offset_min):
    append_raw(root, "L-thinker-0001.jsonl", iso(NOW - timedelta(days=2)), type="charter-filed", subject=cid)
    append_raw(root, "L-planner-0001.jsonl", iso(NOW - timedelta(hours=5)), type="planner-started", subject=cid)
    append_raw(root, "L-planner-0001.jsonl", iso(NOW - timedelta(hours=4, minutes=50)), type="planner-ended",
              subject=cid, reason="charter-gap")
    append_raw(root, "L-planner-0001.jsonl", iso(NOW - timedelta(hours=3)), type="planner-started", subject=cid)
    append_raw(root, "L-planner-0001.jsonl", iso(NOW - timedelta(hours=2, minutes=50)), type="planner-ended",
              subject=cid, reason="charter-gap")
    append_raw(root, "L-operator-0001.jsonl", iso(NOW - timedelta(minutes=decision_offset_min)),
              type="decision", subject=cid, text="proceed anyway")


root6 = newroot()
_planner_stuck_fixture(root6, "L-charter-0099", 60)
evs6 = read_ledger(root6)
res6a = look_wallclock.run(evs6, DummyRunner(), NOW, root6, cfg(), 10**9, wb(root6), False)
briefs6 = [b for b in res6a["briefs"] if b.get("condition") == "planner-attempts-stuck"]
ok(len(briefs6) == 1 and briefs6[0]["subject"] == "look:planner-attempts-stuck:L-charter-0099"
   and briefs6[0]["problem"] == "planner-attempts-stuck" and briefs6[0]["owner"] == "thinker",
   "AC6: one brief for the stuck charter, correct subject/problem/owner")
evs6b = read_ledger(root6)
res6b = look_wallclock.run(evs6b, DummyRunner(), NOW, root6, cfg(), 10**9, wb(root6), False)
ok(not any(b.get("condition") == "planner-attempts-stuck" for b in res6b["briefs"]),
   "AC6: a second pass appends nothing further (openkey dedupe)")

root6c = newroot()
_planner_stuck_fixture(root6c, "L-charter-0098", 6 * 60)   # decision 6h BEFORE the newest planner-ended
evs6c = read_ledger(root6c)
res6c = look_wallclock.run(evs6c, DummyRunner(), NOW, root6c, cfg(), 10**9, wb(root6c), False)
ok(not any(b.get("condition") == "planner-attempts-stuck" for b in res6c["briefs"]),
   "AC6: a decision BEFORE the newest planner-ended appends nothing")
print("AC6 ok")

# ── AC9: the CI deadline gate, and gh runs only through Runner.gh_run_list ──
root9 = newroot()
class AlwaysPastDeadline(DummyRunner):
    """Every `clock()` call reads past the deadline — `gh_run_list` must never
    fire (mirrors AC18b's `JumpingRunner` in test_look.py)."""
    def gh_run_list(self, repo, branch, timeout):
        raise AssertionError("gh_run_list called after the deadline had already passed")
    def clock(self):
        return 1000.0
res9 = look_wallclock.run([], AlwaysPastDeadline(), NOW, root9, cfg(ci=[CI_ROW]), 5.0, wb(root9), False)
und9 = [b for b in res9["briefs"] if str(b.get("key", "")).startswith("undetermined:ci:")]
ok(len(und9) == 1, "AC9: a clock already past deadline reads undetermined, never blocks the pass")
ok("subprocess" not in pathlib.Path(look_wallclock.__file__).read_text(),
   "AC9: look_wallclock.py never imports subprocess — gh runs ONLY through Runner.gh_run_list")
print("AC9 ok")

# ── AC10: dry-run — same shaped events, state.json and the ledger untouched ──
root10 = newroot()
proj10, sess10 = root10 / "projects", root10 / "sessions"
session(sess10, "L-thinker-0004", "SID10", os.getpid())
transcript(proj10, "p", "SID10",
          [sys_hit("Usage limit reached · continuing automatically at 5:00pm", ts=NOW - timedelta(minutes=30))],
          mtime=NOW - timedelta(hours=1))
append_raw(root10, "L-planner-0001.jsonl", iso(NOW - timedelta(minutes=10)), type="message-sent", to="someone", text="hi")
evs10 = read_ledger(root10)
src10 = evs10[0]["_src"]
append_raw(root10, "L-executor-0001.jsonl", iso(NOW - timedelta(minutes=3)), type="decision", ref=src10, subject="x")
evs10b = read_ledger(root10)

(root10 / "look").mkdir(exist_ok=True)
statep10 = root10 / "look" / "state.json"
statep10.write_text(json.dumps({"hello": "world"}))
ledgerp10 = root10 / "events" / look_wallclock.LEDGER_NAME
ledgerp10.parent.mkdir(parents=True, exist_ok=True)
ledgerp10.write_text("")
pre_state10, pre_ledger10 = statep10.read_bytes(), ledgerp10.read_bytes()

runs10 = [{"name": "Dashboard PR Quality", "status": "completed", "conclusion": "failure",
          "headSha": "sha1", "url": "url1", "updatedAt": iso(NOW - timedelta(minutes=30))}]
runner10 = DummyRunner(gh=lambda r, b, t: json.dumps(runs10))
saved10 = patched_dirs(proj10, sess10)
try:
    res10 = look_wallclock.run(evs10b, runner10, NOW, root10, cfg(ci=[CI_ROW]), 10**9, wb(root10), True)
finally:
    restore_dirs(saved10)

ok(any(e["type"] == "pane-paused" and e["_src"] == "dry-run" for e in res10["events"]), "AC10: dry-run pane-paused tagged dry-run")
ok(any(e["type"] == "ci-red" and e["_src"] == "dry-run" for e in res10["events"]), "AC10: dry-run ci-red tagged dry-run")
ok(any(e["type"] == "message-answered" and e["_src"] == "dry-run" for e in res10["events"]),
  "AC10: dry-run message-answered tagged dry-run")
ok(statep10.read_bytes() == pre_state10, "AC10: state.json byte-identical after a dry-run pass")
ok(ledgerp10.read_bytes() == pre_ledger10, "AC10: L-look-local.jsonl byte-identical after a dry-run pass")
print("AC10 ok")

print(f"look_wallclock: {n} checks passed")
