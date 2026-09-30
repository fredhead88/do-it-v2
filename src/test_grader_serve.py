#!/usr/bin/env python3
"""One runnable check on grader_serve.py's AC1-AC5, AC13, AC14, plus
L-spec-8033's own R8.2-R8.3 cases (AC4-AC9). Run: python3 test_grader_serve.py

Before importing `grader_serve`, this sets DOIT_ROOT, DOIT_SCRATCH and HOME to
fresh temp-directory fixtures — never the live `$R/seat`, never the real
`~/.claude/.credentials.json`, and never the live shared `grader-claude`
config dir (mirrors test_grader_view.py's own isolation). Every
tmux/claim.sh/stamp.sh call goes through an injected `runner` that records
its own calls and returns a fake `CompletedProcess` — no real tmux, bwrap or
login anywhere in this file.
"""
import datetime
import json
import os
import pathlib
import sys
import tempfile

FIXTURE_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="grader-serve-root-"))
FIXTURE_SCRATCH = pathlib.Path(tempfile.mkdtemp(prefix="grader-serve-scratch-"))
FIXTURE_HOME = pathlib.Path(tempfile.mkdtemp(prefix="grader-serve-home-"))

os.environ["DOIT_ROOT"] = str(FIXTURE_ROOT)
os.environ["DOIT_SCRATCH"] = str(FIXTURE_SCRATCH)
os.environ["HOME"] = str(FIXTURE_HOME)
os.environ.pop("DOIT_PROJECT", None)
(FIXTURE_ROOT / "events").mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold, grader_serve, grader_view, grading_env, look, relay, scratch  # noqa: E402

# L-spec-8033/AC5-AC9: the REAL `relay.pending_packets`, captured before the
# first `pending_stub` monkeypatch below overwrites the module attribute —
# those cases restore this rather than exercising the stub (Assumptions).
REAL_PENDING = relay.pending_packets

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


class FakeCP:
    def __init__(self, returncode=0):
        self.returncode = returncode


class Runner:
    """Records every call `(argv, kwargs)`; `claim.sh` and `tmux has-session`
    calls are answered per the constructor flags, everything else `0`."""
    def __init__(self, claim_rc=0, session_present=True):
        self.calls = []
        self.claim_rc = claim_rc
        self.session_present = session_present

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if argv and str(argv[0]).endswith("claim.sh"):
            return FakeCP(self.claim_rc)
        if len(argv) >= 2 and argv[0] == "tmux" and argv[1] == "has-session":
            return FakeCP(0 if self.session_present else 1)
        return FakeCP(0)

    def calls_matching(self, pred):
        return [c for c in self.calls if pred(c[0])]

    def claim_calls(self):
        return self.calls_matching(lambda a: a and str(a[0]).endswith("claim.sh"))

    def pane_start_calls(self):
        return self.calls_matching(lambda a: "claude" in a and "--agent" in a and "grader" in a)

    def kill_calls(self):
        return self.calls_matching(lambda a: a[:2] == ["tmux", "kill-window"])

    def stamp_calls(self):
        return self.calls_matching(lambda a: a and str(a[0]).endswith("stamp.sh"))


def pending_stub(items):
    def _f(events, root=None, served_by=None):
        return [dict(i) for i in items]
    return _f


def touch_claimed(spawn):
    seat = FIXTURE_ROOT / "seat"
    seat.mkdir(parents=True, exist_ok=True)
    (seat / f"{spawn}.claimed").touch()


def valid_output_obj():
    return {
        "verdicts": [{"ac": "AC1", "verdict": "met", "reason": "ok"}],
        "matches_intent": "yes",
        "card_ok": "yes",
        "checkers": [],
        "could_not_run": False,
        "contamination": False,
        "declarations": [{"term": "worked", "line": "ok"}],
    }


def write_view_output(spawn, obj_or_text):
    d = scratch.sub("grade") / spawn / "seat"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{spawn}.output.json"
    if isinstance(obj_or_text, str):
        p.write_text(obj_or_text)
    else:
        p.write_text(json.dumps(obj_or_text))
    return p


def ev(type_, ts, **kw):
    return {"type": type_, "ts": ts, **kw}


# ═══════════════════════════ AC1 — start, oldest first, capped ═══════════════════════════
# 5 candidates against a cap of 4 (L-spec-8033/R8.2 raised max_panes 2 -> 4,
# read straight off THIS repo's own launch.toml — the same read AC4 below
# checks explicitly) — one excluded (youngest) proves the cap still bites.
relay.pending_packets = pending_stub([
    {"spawn": "L-grader-1001", "age_min": 5.0},    # youngest: excluded once capacity fills
    {"spawn": "L-grader-1002", "age_min": 50.0},   # oldest
    {"spawn": "L-grader-1003", "age_min": 20.0},
    {"spawn": "L-grader-1004", "age_min": 40.0},
    {"spawn": "L-grader-1005", "age_min": 30.0},
])
r1 = Runner()
grader_serve.run([], runner=r1, now=fold.NOW)
check(len(r1.claim_calls()) == 4, f"AC1: exactly 4 claim.sh calls (cap), got {len(r1.claim_calls())}")
check(len(r1.pane_start_calls()) == 4, f"AC1: exactly 4 pane-start calls, got {len(r1.pane_start_calls())}")
claimed_spawns = [c[0][1] for c in r1.claim_calls()]
check(set(claimed_spawns) == {"L-grader-1002", "L-grader-1003", "L-grader-1004", "L-grader-1005"},
      f"AC1: the 4 OLDEST of 5 spawns are claimed, youngest (1001) is not: {claimed_spawns}")
# claim before start, each: for every claim call, the very next matching-spawn
# pane-start call must come strictly after it in the recorded call order.
idx = {id(c): i for i, c in enumerate(r1.calls)}
for spawn in claimed_spawns:
    claim_i = next(i for i, c in enumerate(r1.calls) if c[0] and str(c[0][0]).endswith("claim.sh") and c[0][1] == spawn)
    start_i = next(i for i, c in enumerate(r1.calls) if "claude" in c[0] and spawn in c[0])
    check(claim_i < start_i, f"AC1: claim must precede start for {spawn}: claim@{claim_i} start@{start_i}")

# ═══════════════════════════ L-spec-8033/R8.2 · AC4 — max_panes() reads launch.toml's 4 ═══════════════════════════
check(grader_serve.max_panes() == 4,
      f"AC4: max_panes() (default root, this repo's own launch.toml) == 4, got {grader_serve.max_panes()}")
check(grader_serve.max_panes(root=grader_serve.HERE.parent) == 4,
      f"AC4: and explicitly against the repo root's launch.toml: "
      f"{grader_serve.max_panes(root=grader_serve.HERE.parent)}")
print("L-spec-8033 AC4 ok")

# ═══════════════════════════ AC2 — capacity accounts for running panes ═══════════════════════════
# 1 already-running + 4 unclaimed against a cap of 4 leaves room for exactly 3.
touch_claimed("L-grader-2000")
relay.pending_packets = pending_stub([
    {"spawn": "L-grader-2000", "age_min": 999.0},   # already claimed-and-running
    {"spawn": "L-grader-2001", "age_min": 10.0},    # youngest unclaimed: excluded once capacity fills
    {"spawn": "L-grader-2002", "age_min": 30.0},
    {"spawn": "L-grader-2003", "age_min": 20.0},
    {"spawn": "L-grader-2004", "age_min": 40.0},    # oldest unclaimed
])
r2 = Runner()
grader_serve.run([], runner=r2, now=fold.NOW)
check(len(r2.pane_start_calls()) == 3,
      f"AC2: 1 already running + cap 4 leaves exactly 3 new starts, got {len(r2.pane_start_calls())}")
check(all(any(sp in c[0] for c in r2.pane_start_calls()) for sp in ("L-grader-2002", "L-grader-2003", "L-grader-2004")),
      "AC2: the 3 oldest unclaimed spawns start")
check(not any("L-grader-2001" in c[0] for c in r2.pane_start_calls()),
      "AC2: the youngest unclaimed (2001) does not, once capacity fills")

# ═══════════════════════════ AC3 — mirror + stamp, byte-identical, duration_ms ═══════════════════════════
SPAWN3 = "L-grader-3001"
touch_claimed(SPAWN3)
out3 = write_view_output(SPAWN3, valid_output_obj())
relay.pending_packets = pending_stub([{"spawn": SPAWN3, "age_min": 1.0}])
START_TS = "2026-09-27T00:00:00+00:00"
import datetime
NOW3 = fold.ts(START_TS) + datetime.timedelta(seconds=43)
events3 = [ev("grader-pane-started", START_TS, pane=f"flow:{SPAWN3}", spawn_ids=SPAWN3,
              subject="grader-panes-1")]
r3 = Runner()
grader_serve.run(events3, runner=r3, now=NOW3)
copy3 = FIXTURE_ROOT / "seat" / f"{SPAWN3}.output.json"
check(copy3.is_file(), "AC3: the output.json is copied to $R/seat")
check(copy3.read_bytes() == out3.read_bytes(), "AC3: the copy is byte-identical")
stamps3 = r3.stamp_calls()
check(len(stamps3) == 1, f"AC3: exactly one stamp.sh call, got {len(stamps3)}")
argv3, kw3 = stamps3[0]
check(argv3[1] == SPAWN3, f"AC3: stamp.sh names the spawn: {argv3}")
check(str(kw3.get("env", {}).get("DOIT_CLAUDE_PROJECTS", "")).endswith("/projects"),
      f"AC3: DOIT_CLAUDE_PROJECTS ends in /projects: {kw3.get('env')}")
check(argv3[-2] == "43000", f"AC3: duration_ms == 43000, got {argv3}")

# ═══════════════════════════ AC4 — never copied/stamped on invalid output ═══════════════════════════
for label, spawn, content in [
    ("absent", "L-grader-4001", None),
    ("malformed", "L-grader-4002", "{not json"),
    ("schema-invalid", "L-grader-4003", {"verdicts": []}),   # missing required fields
]:
    touch_claimed(spawn)
    if content is not None:
        write_view_output(spawn, content)
    relay.pending_packets = pending_stub([{"spawn": spawn, "age_min": 1.0}])
    r4 = Runner()
    grader_serve.run([], runner=r4, now=fold.NOW)
    check(not (FIXTURE_ROOT / "seat" / f"{spawn}.output.json").exists(),
          f"AC4 ({label}): no copy is made")
    check(len(r4.stamp_calls()) == 0, f"AC4 ({label}): no stamp.sh call")

# ═══════════════════════════ AC5 — grader-pane-started, exactly once, never on 0 ═══════════════════════════
relay.pending_packets = pending_stub([
    {"spawn": "L-grader-5001", "age_min": 5.0},
    {"spawn": "L-grader-5002", "age_min": 8.0},
])
tick_before = (FIXTURE_ROOT / "events" / "L-tick-local.jsonl")
before_lines = tick_before.read_text().splitlines() if tick_before.exists() else []
r5a = Runner()
grader_serve.run([], runner=r5a, now=fold.NOW)
after_lines = tick_before.read_text().splitlines()
new_lines = [json.loads(l) for l in after_lines[len(before_lines):]]
started5 = [e for e in new_lines if e["type"] == "grader-pane-started"]
check(len(started5) == 1, f"AC5: exactly one grader-pane-started after the starting call, got {len(started5)}")
check(set(started5[0]["spawn_ids"].split(",")) == {"L-grader-5001", "L-grader-5002"},
      f"AC5: spawn_ids matches the 2 started spawns: {started5[0]}")

# second call: both spawns now claimed-and-running, nothing left to start -> 0 capacity used.
touch_claimed("L-grader-5001")
touch_claimed("L-grader-5002")
before_lines2 = tick_before.read_text().splitlines()
r5b = Runner()
grader_serve.run([], runner=r5b, now=fold.NOW)
after_lines2 = tick_before.read_text().splitlines()
check(after_lines2 == before_lines2, "AC5: a call starting 0 panes appends no grader-pane-started")

# ═══════════════════════════ AC13 — credential isolation in the env -i prefix ═══════════════════════════
STRIP = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
saved_env = {k: os.environ.get(k) for k in STRIP}
os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "tok-oauth"
os.environ["ANTHROPIC_API_KEY"] = "tok-anthropic"
os.environ["OPENAI_API_KEY"] = "tok-openai"
try:
    relay.pending_packets = pending_stub([{"spawn": "L-grader-1301", "age_min": 1.0}])
    r13 = Runner()
    grader_serve.run([], runner=r13, now=fold.NOW)
finally:
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
starts13 = r13.pane_start_calls()
check(len(starts13) == 1, f"AC13: one pane-start call, got {len(starts13)}")
argv13 = starts13[0][0]
ei = argv13.index("env")
check(argv13[ei + 1] == "-i", f"AC13: env -i prefix present: {argv13}")
tail = argv13[ei + 2:argv13.index("claude", ei)]
check(any(seg.startswith("CLAUDE_CONFIG_DIR=") for seg in tail),
      f"AC13: CLAUDE_CONFIG_DIR= present in the env -i segment: {tail}")
check(not any(seg.split("=", 1)[0] in STRIP for seg in tail),
      f"AC13: no stripped credential name appears as a KEY= entry: {tail}")

# ═══════════════════════════ AC14 — reap kills exactly the mapped, terminal, unmirrored spawn ═══════════════════════════
relay.pending_packets = pending_stub([])   # AC14 exercises step (0) only

# (a) S later spawn-stale, no output.json -> kill exactly once, return list names it.
PANE_A, SPAWN_A = "flow:L-grader-6001", "L-grader-6001"
events_a = [
    ev("grader-pane-started", "2026-09-27T00:00:00+00:00", pane=PANE_A, spawn_ids=SPAWN_A, subject="p"),
    ev("spawn-stale", "2026-09-27T00:10:00+00:00", spawn=SPAWN_A),
]
r14a = Runner()
out14a = grader_serve.run(events_a, runner=r14a, now=fold.NOW)
kills_a = r14a.kill_calls()
check(len(kills_a) == 1 and kills_a[0][0] == ["tmux", "kill-window", "-t", PANE_A],
      f"AC14(a): exactly one matching kill-window call, got {kills_a}")
check(any(PANE_A in l and SPAWN_A in l for l in out14a),
      f"AC14(a): the return list names the pane/spawn: {out14a}")

# (b) same, but $R/seat/S.output.json already exists -> no kill call.
(FIXTURE_ROOT / "seat").mkdir(parents=True, exist_ok=True)
(FIXTURE_ROOT / "seat" / f"{SPAWN_A}.output.json").write_text(json.dumps(valid_output_obj()))
r14b = Runner()
grader_serve.run(events_a, runner=r14b, now=fold.NOW)
check(len(r14b.kill_calls()) == 0, "AC14(b): already mirrored -> no kill call")

# (c) S with no terminal event at all -> no kill call.
SPAWN_C, PANE_C = "L-grader-6002", "flow:L-grader-6002"
events_c = [ev("grader-pane-started", "2026-09-27T00:00:00+00:00", pane=PANE_C, spawn_ids=SPAWN_C, subject="p")]
r14c = Runner()
grader_serve.run(events_c, runner=r14c, now=fold.NOW)
check(len(r14c.kill_calls()) == 0, "AC14(c): no terminal event -> no kill call")

# ═══════════════════════════ L-spec-0481 AC8 — one environment for proof and grade ═══════════════════════════
relay.pending_packets = pending_stub([{"spawn": "L-grader-8001", "age_min": 1.0}])
view8 = scratch.sub("grade") / "L-grader-8001"
view8.mkdir(parents=True, exist_ok=True)
(view8 / ".grading_state.json").write_text(json.dumps(
    {"venv": "", "db_env_names": ["SUPABASE_DB_URL"], "dsn": "postgresql://albert@/scratch?host=/x&port=1"}))
events8 = [ev("spawn-started", "2026-09-27T00:00:00+00:00", spawn="L-grader-8001",
              role="grader", project="albert-scott")]
r8 = Runner()
grader_serve.run(events8, runner=r8, now=fold.NOW)
starts8 = r8.pane_start_calls()
check(len(starts8) == 1, f"AC8: one pane-start call, got {starts8}")
argv8 = starts8[0][0]
expect_bwrap = grader_view.bwrap_argv(view8, doit_src=grader_serve.HERE.parent,
                                       binds=grading_env.sandbox_binds(view8, "albert-scott"))
new_window_end = argv8.index("--") + 1
bwrap_segment = argv8[new_window_end:new_window_end + len(expect_bwrap)]
check(bwrap_segment == expect_bwrap, f"AC8: bwrap segment matches grading_env.sandbox_binds: {bwrap_segment}")
ei8 = argv8.index("env")
tail8 = argv8[ei8 + 2:argv8.index("claude", ei8)]
tail_names = {seg.split("=", 1)[0] for seg in tail8}
for name in grading_env.pane_env(view8):
    check(name in tail_names, f"AC8: pane_env name {name!r} present in env -i segment: {tail8}")
print("L-spec-0481 AC8 ok")


# ═══════════════════════════ L-spec-8027 AC10 — pane env carries HOME=<view>, ═══
# ═══════════════════════════ seat is read/mirrored from the view, never HOME ════
SPAWN10 = "L-grader-10001"
VIEW10 = scratch.sub("grade") / SPAWN10
(VIEW10 / "seat").mkdir(parents=True, exist_ok=True)
(VIEW10 / "seat" / f"{SPAWN10}.packet.md").write_text(f"packet body\nspawn_id: {SPAWN10}\n")

# the invoker's own HOME (this test process) must differ from <view>, so a
# pane env HOME that leaked the invoker's HOME instead of <view> would be caught
check(os.environ["HOME"] == str(FIXTURE_HOME) and str(VIEW10) != str(FIXTURE_HOME),
      "L-spec-8027 AC10 setup: the invoker's own HOME differs from the fixture view")

relay.pending_packets = pending_stub([{"spawn": SPAWN10, "age_min": 1.0}])
r10 = Runner()
grader_serve.run([], runner=r10, now=fold.NOW)
starts10 = r10.pane_start_calls()
check(len(starts10) == 1, f"L-spec-8027 AC10: one pane-start call, got {starts10}")
argv10 = starts10[0][0]
ei10 = argv10.index("env")
tail10 = argv10[ei10 + 2:argv10.index("claude", ei10)]
home_entries10 = [seg for seg in tail10 if seg.startswith("HOME=")]
check(len(home_entries10) == 1, f"L-spec-8027 AC10: exactly one HOME= entry in env -i: {tail10}")
check(home_entries10[0] == f"HOME={VIEW10}",
      f"L-spec-8027 AC10: the pane's env HOME equals <view> exactly — not the invoker's "
      f"HOME and not <view>/home (R8.5's own new dir is for verify.sh checks only): {home_entries10[0]}")
check((VIEW10 / "seat" / f"{SPAWN10}.packet.md").is_file(),
      "L-spec-8027 AC10: the packet is present at <view>/seat/<spawn>.packet.md")

# the mirror half: claim it (as the started pane would be), write its own
# output at <view>/seat/<spawn>.output.json, and confirm it mirrors out to
# $R/seat — read from the VIEW's own seat dir, never from HOME
touch_claimed(SPAWN10)
out10 = write_view_output(SPAWN10, valid_output_obj())
check(out10 == VIEW10 / "seat" / f"{SPAWN10}.output.json",
      "L-spec-8027 AC10 setup: the fixture output lands at <view>/seat, matching grader_serve's own read path")
relay.pending_packets = pending_stub([{"spawn": SPAWN10, "age_min": 1.0}])
r10b = Runner()
grader_serve.run([], runner=r10b, now=fold.NOW)
mirrored10 = FIXTURE_ROOT / "seat" / f"{SPAWN10}.output.json"
check(mirrored10.is_file() and mirrored10.read_bytes() == out10.read_bytes(),
      "L-spec-8027 AC10: the view's own output.json is mirrored to $R/seat, byte-identical")
check(not (pathlib.Path(FIXTURE_HOME) / "seat").exists(),
      "L-spec-8027 AC10: nothing is ever read from or written to the invoker's own HOME/seat")
print("L-spec-8027 AC10 ok")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-8033 · grader-pane-route (L-charter-0042 R8.3) — AC5-AC9
#
# These run against the REAL `relay.pending_packets` (restored from
# `REAL_PENDING`, captured before the first `pending_stub` above) over the
# real `FIXTURE_ROOT` ledger on disk — a "terminal" spawn under the stub is
# simply absent from it, which would make the terminal-clears-the-alarm case
# vacuous (Assumptions). Each case appends real `spawn-started`/`.packet.md`
# fixtures and reads the ledger back with `fold.read_events()`, the same
# round trip `tick._record` performs between calls.
# ══════════════════════════════════════════════════════════════════════════════
relay.pending_packets = REAL_PENDING


def iso(delta_min):
    return (fold.NOW - datetime.timedelta(minutes=delta_min)).isoformat(timespec="seconds")


def write_ev(sid, type_, ts, **kw):
    """One REAL ledger line under FIXTURE_ROOT/events — unlike `ev()` above (an
    in-memory dict fed through `pending_stub`), this lands on disk so the REAL
    `relay.pending_packets` and `fold.read_events()` see it."""
    p = FIXTURE_ROOT / "events" / f"{sid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as fh:
        fh.write(json.dumps({"v": 1, "ts": ts, "type": type_, "spawn": sid, **kw}, sort_keys=True) + "\n")


def write_packet(spawn):
    (FIXTURE_ROOT / "seat").mkdir(parents=True, exist_ok=True)
    (FIXTURE_ROOT / "seat" / f"{spawn}.packet.md").write_text("packet")


def is_open(spawn):
    """Whether a `grader-pane-unserved` alarm for `spawn` is still open —
    `look`'s own notion (`_open_ref`), not a hand-rolled brief/brief-answered
    count."""
    return look._open_ref(FIXTURE_ROOT, f"grader-pane-unserved|{spawn}",
                          f"look:grader-pane-unserved:{spawn}", fold.read_events()) is not None


# ── AC5: GRADER_PANE_UNSERVED_MIN == 10, and the alarm fires past it ──────────
check(relay.GRADER_PANE_UNSERVED_MIN == 10,
      f"AC5: relay.GRADER_PANE_UNSERVED_MIN == 10, got {relay.GRADER_PANE_UNSERVED_MIN}")
SPAWN5 = "L-grader-9001"
write_packet(SPAWN5)
write_ev(SPAWN5, "spawn-started", iso(11), role="grader")
grader_serve.run(fold.read_events(), runner=Runner(), now=fold.NOW)
briefs5 = [e for e in fold.read_events()
          if e.get("type") == "brief" and e.get("condition") == "grader-pane-unserved" and e.get("key") == SPAWN5]
check(len(briefs5) == 1, f"AC5: exactly one grader-pane-unserved brief for {SPAWN5}: {briefs5}")
check(briefs5[0].get("owner") == "thinker", f"AC5: owner is thinker: {briefs5[0]}")
check(str(briefs5[0].get("reading", {}).get("spawn")) == SPAWN5 and "age_min" in briefs5[0].get("reading", {}),
      f"AC5: the reading names the spawn and carries its age_min: {briefs5[0]}")
print("L-spec-8033 AC5 ok")

# ── AC6: no alarm — too young, claimed, or already terminal ───────────────────
SPAWN6A = "L-grader-9002"   # unclaimed, age 9 < GRADER_PANE_UNSERVED_MIN
write_packet(SPAWN6A)
write_ev(SPAWN6A, "spawn-started", iso(9), role="grader")

SPAWN6B = "L-grader-9003"   # claimed, age 60
write_packet(SPAWN6B)
write_ev(SPAWN6B, "spawn-started", iso(60), role="grader")
(FIXTURE_ROOT / "seat" / f"{SPAWN6B}.claimed").write_text("")

SPAWN6C = "L-grader-9004"   # terminal (spawn-done), age 60
write_packet(SPAWN6C)
write_ev(SPAWN6C, "spawn-started", iso(60), role="grader")
write_ev(SPAWN6C, "spawn-done", iso(1))

grader_serve.run(fold.read_events(), runner=Runner(), now=fold.NOW)
for spawn, why in ((SPAWN6A, "too young"), (SPAWN6B, "claimed"), (SPAWN6C, "terminal")):
    check(not is_open(spawn), f"AC6: no alarm for {spawn} ({why})")
print("L-spec-8033 AC6 ok")

# ── AC7: clear on claim, clear on terminal, no duplicate while still open ─────
SPAWN7A = "L-grader-9010"
write_packet(SPAWN7A)
write_ev(SPAWN7A, "spawn-started", iso(11), role="grader")
grader_serve.run(fold.read_events(), runner=Runner(), now=fold.NOW)
check(is_open(SPAWN7A), f"AC7: alarm opens for unclaimed {SPAWN7A}")
(FIXTURE_ROOT / "seat" / f"{SPAWN7A}.claimed").write_text("")
grader_serve.run(fold.read_events(), runner=Runner(), now=fold.NOW)
check(not is_open(SPAWN7A), f"AC7: claiming {SPAWN7A} clears its alarm on the next run")

SPAWN7B = "L-grader-9011"
write_packet(SPAWN7B)
write_ev(SPAWN7B, "spawn-started", iso(11), role="grader")
grader_serve.run(fold.read_events(), runner=Runner(), now=fold.NOW)
check(is_open(SPAWN7B), f"AC7: alarm opens for unclaimed {SPAWN7B}")
write_ev(SPAWN7B, "spawn-done", iso(1))
grader_serve.run(fold.read_events(), runner=Runner(), now=fold.NOW)
check(not is_open(SPAWN7B), f"AC7: a spawn-done for {SPAWN7B} clears its alarm on the next run")

SPAWN7C = "L-grader-9012"
write_packet(SPAWN7C)
write_ev(SPAWN7C, "spawn-started", iso(11), role="grader")
grader_serve.run(fold.read_events(), runner=Runner(), now=fold.NOW)
check(is_open(SPAWN7C), f"AC7: alarm opens for unclaimed {SPAWN7C}")
grader_serve.run(fold.read_events(), runner=Runner(), now=fold.NOW)   # still unclaimed, non-terminal
briefs7c = [e for e in fold.read_events()
           if e.get("type") == "brief" and e.get("condition") == "grader-pane-unserved" and e.get("key") == SPAWN7C]
check(len(briefs7c) == 1, f"AC7: a still-open, still-unserved alarm is never duplicated: {briefs7c}")
check(is_open(SPAWN7C), f"AC7: and stays open: {SPAWN7C}")
print("L-spec-8033 AC7 ok")

# ── AC8: the alarm fires even when the start step raises; the exception still
# propagates to the caller ────────────────────────────────────────────────────
SPAWN8A = "L-grader-9020"
write_packet(SPAWN8A)
write_ev(SPAWN8A, "spawn-started", iso(11), role="grader")
orig_pane_env = grading_env.pane_env
grading_env.pane_env = lambda view: (_ for _ in ()).throw(RuntimeError("boom-pane-env"))
raised8a = False
try:
    grader_serve.run(fold.read_events(), runner=Runner(), now=fold.NOW)
except RuntimeError as exc:
    raised8a = "boom-pane-env" in str(exc)
finally:
    grading_env.pane_env = orig_pane_env
check(raised8a, "AC8(a): the original exception (from a raising start step) propagates to the caller")
check(is_open(SPAWN8A), f"AC8(a): the alarm still fires for {SPAWN8A} despite the raise")

SPAWN8B = "L-grader-9021"
write_packet(SPAWN8B)
write_ev(SPAWN8B, "spawn-started", iso(11), role="grader")


class RaisingRunner(Runner):
    def __call__(self, argv, **kw):
        if len(argv) >= 2 and argv[0] == "tmux" and argv[1] == "new-window":
            raise RuntimeError("boom-tmux")
        return super().__call__(argv, **kw)


raised8b = False
try:
    grader_serve.run(fold.read_events(), runner=RaisingRunner(), now=fold.NOW)
except RuntimeError as exc:
    raised8b = "boom-tmux" in str(exc)
check(raised8b, "AC8(b): the original exception (from the injected runner) propagates to the caller")
check(is_open(SPAWN8B), f"AC8(b): the alarm still fires for {SPAWN8B} despite the raise")
print("L-spec-8033 AC8 ok")

# ── AC9: the alarm's write really round-trips through disk ───────────────────
SPAWN9 = "L-grader-9030"
write_packet(SPAWN9)
write_ev(SPAWN9, "spawn-started", iso(11), role="grader")
grader_serve.run(fold.read_events(), runner=Runner(), now=fold.NOW)
reread9 = fold.read_events()   # a fresh, separate open of events/L-look-local.jsonl
briefs9 = [e for e in reread9
          if e.get("type") == "brief" and e.get("condition") == "grader-pane-unserved" and e.get("key") == SPAWN9]
check(len(briefs9) == 1, f"AC9: the brief is found on a fresh disk re-read, not only in memory: {briefs9}")
check((FIXTURE_ROOT / "events" / "L-look-local.jsonl").is_file(),
      "AC9: the write landed through look.emit_once's own production path (L-look-local.jsonl)")
print("L-spec-8033 AC9 ok")

print(f"grader_serve: {N} checks pass")
