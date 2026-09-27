#!/usr/bin/env python3
"""One runnable check on grader_serve.py's AC1-AC5, AC13, AC14. Run:
python3 test_grader_serve.py

Before importing `grader_serve`, this sets DOIT_ROOT, DOIT_SCRATCH and HOME to
fresh temp-directory fixtures — never the live `$R/seat`, never the real
`~/.claude/.credentials.json`, and never the live shared `grader-claude`
config dir (mirrors test_grader_view.py's own isolation). Every
tmux/claim.sh/stamp.sh call goes through an injected `runner` that records
its own calls and returns a fake `CompletedProcess` — no real tmux, bwrap or
login anywhere in this file.
"""
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
import fold, grader_serve, relay, scratch  # noqa: E402

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
relay.pending_packets = pending_stub([
    {"spawn": "L-grader-1001", "age_min": 5.0},
    {"spawn": "L-grader-1002", "age_min": 50.0},   # oldest
    {"spawn": "L-grader-1003", "age_min": 20.0},
])
r1 = Runner()
grader_serve.run([], runner=r1, now=fold.NOW)
check(len(r1.claim_calls()) == 2, f"AC1: exactly 2 claim.sh calls, got {len(r1.claim_calls())}")
check(len(r1.pane_start_calls()) == 2, f"AC1: exactly 2 pane-start calls, got {len(r1.pane_start_calls())}")
claimed_spawns = [c[0][1] for c in r1.claim_calls()]
check(set(claimed_spawns) == {"L-grader-1002", "L-grader-1003"},
      f"AC1: the 2 OLDEST spawns are claimed, got {claimed_spawns}")
# claim before start, each: for every claim call, the very next matching-spawn
# pane-start call must come strictly after it in the recorded call order.
idx = {id(c): i for i, c in enumerate(r1.calls)}
for spawn in claimed_spawns:
    claim_i = next(i for i, c in enumerate(r1.calls) if c[0] and str(c[0][0]).endswith("claim.sh") and c[0][1] == spawn)
    start_i = next(i for i, c in enumerate(r1.calls) if "claude" in c[0] and spawn in c[0])
    check(claim_i < start_i, f"AC1: claim must precede start for {spawn}: claim@{claim_i} start@{start_i}")

# ═══════════════════════════ AC2 — capacity accounts for running panes ═══════════════════════════
touch_claimed("L-grader-2000")
relay.pending_packets = pending_stub([
    {"spawn": "L-grader-2000", "age_min": 999.0},   # already claimed-and-running
    {"spawn": "L-grader-2001", "age_min": 10.0},
    {"spawn": "L-grader-2002", "age_min": 30.0},    # older of the two unclaimed
])
r2 = Runner()
grader_serve.run([], runner=r2, now=fold.NOW)
check(len(r2.pane_start_calls()) == 1, f"AC2: exactly 1 new pane starts, got {len(r2.pane_start_calls())}")
check(any("L-grader-2002" in c[0] for c in r2.pane_start_calls()),
      "AC2: the older unclaimed spawn (2002) is the one that starts")

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

print(f"grader_serve: {N} checks pass")
