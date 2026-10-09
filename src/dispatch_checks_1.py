#!/usr/bin/env python3
"""One runnable check on the dispatch wrapper. Run: python3 test_dispatch.py
The spawn is mocked; every after-the-fact check is exercised against the
failure it was written for (D116, D120)."""
import argparse, json, os, pathlib, shutil, subprocess, sys, tempfile
from datetime import datetime as _dt, timedelta as _timedelta, timezone as _timezone

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"], os.environ["DOIT_NO_POKE"] = str(TMP), "1"
# Hermetic w.r.t. the pane: env.sh exports DOIT_SEAT=1 in every pane on this box,
# which routes every mocked spawn into run_seat to wait for a <spawn>.result.json
# no mock ever writes — the file then hangs to the role timeout instead of failing.
# The seat-route blocks below set and del it around themselves on purpose.
os.environ.pop("DOIT_SEAT", None)
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import dispatch, fold, grader_view, harness  # noqa: E402
import scratch as _scratch486  # noqa: E402
# L-spec-0486/R15d: `dispatch.ensure_room` now runs for EVERY role=builder/
# grader dispatch, before any backend. Stubbed True here — this file's own
# hermetic default, mirroring test_tick.py's `intake.ghlimit`/`pane_resume.run`
# pattern — so the ~120 pre-existing builder/grader drives below never touch
# the real scratch root or real disk; L-spec-0486's own AC12/AC13 section
# restores the real function (or installs its own forced stub) around itself
# only, and always leaves this same True-stub behind when it is done.
_SCRATCH_FIXTURE_486 = TMP / "scratch"
_SCRATCH_FIXTURE_486.mkdir(parents=True, exist_ok=True)
_real_scratch_root_486 = _scratch486.root
_scratch486.root = lambda: _SCRATCH_FIXTURE_486
_real_ensure_room_486 = dispatch.ensure_room
dispatch.ensure_room = lambda path, need_gb: True

REPO = TMP / "repo"
REPO.mkdir()
subprocess.run(["git", "init", "-q"], cwd=REPO, check=True)
PK = TMP / "packet.md"
# R7/AC14: the file-wide default packet body now carries a valid PINNED_BASE_SHA
# line — every plain `PK.write_text("a packet\n")` reset in this file becomes
# `PK.write_text(PK_DEFAULT)` instead, so no existing `spawn("builder", ...)`
# call trips AC4's new no-pinned-base refusal. The value itself is inert: no
# builder fixture in this file reads it back except where a test names
# PINNED_BASE_SHA explicitly.
PK_DEFAULT = "a packet\nPINNED_BASE_SHA: X0000000\n"
PK.write_text(PK_DEFAULT)
N = 0

# Rework-round note (re-dispatch correction, this same subject): a prior
# grading pass named this file's AC11 dsn assertion (the `build-started`
# lookup a few hundred lines below) as failing at a line near here and asked
# that it be recorded as a pre-existing/main failure rather than claimed as
# passing. Re-run three consecutive times against this exact commit, this
# file exits 0 clean every time — that specific assertion does not reproduce
# as failing against this pinned base. The card for this round records that
# finding plus the two failures that DO reproduce identically on main outside
# this file's footprint (test_look.py, test_think.py) — see the card, not
# this comment, for the reproduction evidence.


def spawn(role, out=None, result=None, side=None, path=None, subject="L-spec-0001",
          charter=None, project="t"):
    res = {"is_error": False, "terminal_reason": "completed", "structured_output": out, "num_turns": 1,
           "usage": {"input_tokens": 1, "output_tokens": 2}, "total_cost_usd": 0.01,
           "modelUsage": {"m": {}}, "permission_denials": [], **(result or {})}

    def fake(cmd, packet, cwd, timeout):
        fake.cmd = cmd
        side and side()
        return argparse.Namespace(stdout=json.dumps(res), returncode=0, stderr="")
    dispatch.run_claude = fake
    a = argparse.Namespace(role=role, subject=subject, packet=str(PK), path=path and str(path),
                           cwd=str(REPO), charter=charter, project=project, mcp_config=None,
                           timeout=None, max_usd=None)
    try:
        dispatch.main(a)
        code = 0
    except SystemExit as e:
        code = e.code
    # L-spec-0276: numeric suffixes only (mirrors alloc()'s own glob) — a later
    # fixture named e.g. `L-builder-notgrader-test.jsonl` (AC14 above) sorts
    # ABOVE every real `L-builder-0014.jsonl`-style alloc()'d file under plain
    # `max()`, and this helper must always read the spawn IT JUST MADE.
    raw = [json.loads(l) for l in
           max((TMP / "events").glob(f"L-{role}-[0-9]*.jsonl")).read_text().splitlines()]
    spawn.raw = raw
    evs = [e for e in raw if e["type"] != "spawn-started"]      # asserted once below, hidden elsewhere
    global N
    N += 1
    return code, [e["type"] for e in evs], evs, fake.cmd if hasattr(fake, "cmd") else None


research = {"path": "content/L-research-0001.md", "summary": "x", "answered": "yes", "contamination": False}
rp = TMP / "content" / "L-research-0001.md"

code, types, evs, cmd = spawn("research", out=None, path=rp)
assert code == 1 and types == ["spawn-failed"] and "null structured_output" in evs[0]["why"], (types, evs)
assert "--agent" in cmd and "research" in cmd and "--strict-mcp-config" in cmd and "ANTHROPIC_API_KEY" not in " ".join(cmd)
assert cmd[cmd.index("--allowedTools") + 1] == "Read,Glob,Grep,Write,Bash", "the allow list is the contract's tools: line"

code, types, evs, _ = spawn("research", result={"is_error": True, "terminal_reason": "api_error",
                                                "api_error_status": 401, "result": "Not logged in"}, path=rp)
assert code == 1 and types == ["escalation-blocking", "spawn-failed"], types
assert "/login" in evs[0]["why"], "an unreachable seat is the operator's, never retried"
# AC23/L-spec-0192: this call now carries what satisfies escalation_ok, or emit()'s
# new required-fields door (R3) would have refused it outright and dropped the
# type from `types` above entirely — the assertion just above is the regression.
assert fold.escalation_ok(evs[0]) and "irreversible" in evs[0], evs[0]

code, types, evs, _ = spawn("research", out=research, path=rp)
assert code == 1 and "nothing at" in evs[0]["why"], "D120 W3: a success claim with no file on disk fails"

PK.write_text("a packet carrying the hypothesis\n")     # its own bytes: a contaminated packet is never re-sent
code, types, evs, _ = spawn("research", out={**research, "contamination": True}, path=rp, side=lambda: rp.write_text("d"))
assert code == 1 and "contamination" in evs[0]["why"]
PK.write_text(PK_DEFAULT)

code, types, evs, _ = spawn("research", out={**research, "path": "content/L-research-0009.md"}, path=rp)
assert code == 1 and "path mismatch" in evs[0]["why"]

stray = REPO / "stray.txt"
# Concurrent work is not a failed spawn: a file appearing under --cwd mid-run is
# another charter's build landing in the same shared checkout, and the spawn that
# ran correctly beside it records the movement instead of being voided by it.
code, types, evs, _ = spawn("research", out=research, path=rp, side=lambda: stray.write_text("x"))
assert code == 0 and types == ["research-filed", "spawn-done"], (code, types, evs)
assert evs[-1]["repo_moved"] == {"lines": ["?? stray.txt"], "paths": ["stray.txt"], "by_this_spawn": False}, evs[-1]
stray.unlink()

# The after-snapshot going undetermined is a broken observation, not concurrency.
real_porcelain = dispatch.porcelain


def nth(seq):
    """porcelain() answering seq[0] on the before-snapshot and seq[1] on the after."""
    calls = []

    def fake_porcelain(cwd):
        calls.append(cwd)
        return seq[min(len(calls) - 1, len(seq) - 1)](cwd)
    return fake_porcelain


dispatch.porcelain = nth([real_porcelain, lambda cwd: None])
code, types, evs, _ = spawn("research", out=research, path=rp)
dispatch.porcelain = real_porcelain
assert code == 1 and types == ["spawn-failed"], (code, types, evs)
assert "undetermined" in evs[0]["why"] and "after" in evs[0]["why"], evs[0]["why"]
assert evs[0]["why"] != f"repo status undetermined in {REPO} before spawn — not spent", \
    "the after-spawn message is its own, never the before-spawn one"

# A NOT_A_REPO <-> real flip is the repository itself coming or going under --cwd:
# still a hard failure, and never routed into repo_moved.
for seq in ([real_porcelain, lambda cwd: dispatch.NOT_A_REPO], [lambda cwd: dispatch.NOT_A_REPO, real_porcelain]):
    dispatch.porcelain = nth(seq)
    code, types, evs, _ = spawn("research", out=research, path=rp)
    dispatch.porcelain = real_porcelain
    assert code == 1 and types == ["spawn-failed"] and "repo identity changed" in evs[0]["why"], (code, types, evs)
    assert not any("repo_moved" in e for e in spawn.raw), "a sentinel flip is never a movement"

code, types, evs, _ = spawn("research", out=research, path=rp)
assert code == 0 and types == ["research-filed", "spawn-done"], types
assert "repo_moved" not in evs[-1], "nothing moved: the observation is absent, not an empty noise field"
assert spawn.raw[0]["type"] == "spawn-started" and spawn.raw[0]["role"] == "research", "every role starts loudly"
assert evs[1]["answered"] == "yes" and evs[1]["cost_usd"] == 0.01 and evs[1]["packet_sha256"], evs[1]
assert "actor" not in evs[1], "D90: never an actor field"

# L-spec-0195: a well-formed spec fixture — a real `## Verification` `&&` chain,
# an `## Acceptance Criteria` section, a path-shaped `**Writes:**` line — so the
# `spec-writer` wrapper's `validate.spec_shape` gate passes and every assertion
# below that expects no `spec-shape-failed` keeps passing unchanged (Assumptions).
WELL_FORMED_SPEC = ("# fixture spec\n## Verification\n```\ntrue\n```\n"
                    "## Acceptance Criteria\nAC1 [backend]: x.\n  review_path: y\n"
                    "Writes: a.py\n"
                    "In plain English: fixture carries a plain line.\n")

sw = {"status": "written", "spec_id": "L-spec-0001", "ac_count": 2, "ac_types": ["backend"], "footprint": ["a.py"],
      "requirement_ids": ["R1"], "owed": 0, "unknowns": 1, "split": [], "weak_dimensions": [],
      "escalations": [{"asks": "q?", "blocks": ["AC1"], "default": "d", "deadline": "2026-09-09"}], "declarations": []}
sp = TMP / "content" / "L-spec-0001.md"
code, types, evs, _ = spawn("spec-writer", out=sw, path=sp, side=lambda: sp.write_text(WELL_FORMED_SPEC))
assert code == 0 and types == ["spec-written", "question", "spawn-done"], types
assert evs[0]["unknown_count"] == 1 and evs[0]["footprint"] == ["a.py"]
# AC11/L-spec-0192: its own dedicated subject, never "L-spec-0001" — R3's new
# refusal (AC10) reads `spec-killed` as terminal, and the card/builder-dispatch
# fixture further down still targets the harness-default "L-spec-0001"; a
# spec-killed written there would collide with it and break a passing path.
code, types, evs, _ = spawn("spec-writer", out={**sw, "status": "killed", "killed_by_check": 2, "escalations": []},
                            subject="L-spec-0002")
assert code == 0 and types == ["spec-killed", "spawn-done"] and evs[0]["check"] == 2, "killed needs no file"
# S15: an owed criterion carries the instant that proves it, or the schema refuses it.
owed = {**sw, "escalations": [], "declarations": [{"term": "owed-ac", "criterion": "AC7", "wake_at": "2026-09-16T11:04:00Z",
                                                   "line": "the lock is reaped on the first run after it passes the threshold"}]}
code, types, evs, _ = spawn("spec-writer", out=owed, path=sp, side=lambda: sp.write_text(WELL_FORMED_SPEC))
assert code == 0 and "owed-ac" in types and next(e for e in evs if e["type"] == "owed-ac")["wake_at"] == "2026-09-16T11:04:00Z", evs
assert fold.ts("2026-09-16T11:04:00Z").tzinfo is not None, "the fold parses the Z form"
PK.write_text("a packet, owed without wake_at\n")
code, types, evs, _ = spawn("spec-writer", out={**owed, "declarations": [{"term": "owed-ac", "line": "no instant"}]}, path=sp)
assert code == 1 and "violates spec-writer.schema.json" in evs[-1]["why"] and "wake_at" in evs[-1]["why"], evs[-1]
PK.write_text(PK_DEFAULT)

card = {"status": "DONE", "identity": {"spec_id": "L-spec-0001", "built_by": "L-builder-0001", "branch": "l-spec-0001",
                                       "base_sha": "abc1234", "ready_sha": "def5678"},
        "acs": [{"id": f"AC{i}", "criterion_type": "backend", "evidence": "e", "evidence_type": "command-output",
                 "check": "python3 t.py", "disposition": "done"} for i in range(1, 14)],
        "verify": {"command": "python3 t.py", "exit_code": 0, "result": "ok"}, "stubs": [{"path": "s.py", "why": "w"}],
        "deviations": [{"type": "significant", "what": "forked X", "why": "twin was unusable"}],
        "tests": {"added": True}, "not_built": [{"item": "n", "reason": "out-of-scope-per-spec"}], "unknowns": [],
        "built_against": [], "escalations": [],
        "declarations": [{"term": "spec-ambiguity", "root_cause": "false-premise", "line": "l"}, {"term": "worked", "line": "y"}]}
code, types, evs, cmd = spawn("builder", out=card, side=lambda: stray.write_text("builder may write"))
assert code == 0, evs
assert types == ["build-started", "build-done", "adr-filed", "build-deviation", "build-stub", "spec-ambiguity", "worked", "spawn-done"], types
assert evs[0]["ts"] <= evs[-1]["ts"] and "--disallowedTools" in cmd and "Bash(*--no-verify*)" in cmd[cmd.index("--disallowedTools") + 1]
c = TMP / "content" / "L-card-0001.md"
assert c.exists() and len(c.read_text().splitlines()) <= 15 and "not built: n" in c.read_text(), c.read_text()
cj = json.loads(c.with_suffix(".json").read_text())
assert len(cj["acs"]) == 13 and "why" not in cj["deviations"][0], "every row survives beside the render; never the builder's why"
assert (TMP / "content" / "L-adr-0001.md").exists() and evs[2]["adr"] == "L-adr-0001"
assert evs[3]["deviation"] == "significant" and evs[3]["type"] == "build-deviation", "the deviation's kind survives the event type"
assert evs[5]["root_cause"] == "false-premise", "a declaration is an event typed by its term"
stray.unlink()

grade = lambda vs, **kw: {"verdicts": vs, "matches_intent": "yes", "card_ok": "yes", "could_not_run": False,
                          "contamination": False, "declarations": [],
                          "checkers": [{"id": "gate", "version": "1", "coverage_note": "n1", "result": "pass"}], **kw}
met, unmet = {"ac": "AC1", "verdict": "met", "reason": "r"}, {"ac": "AC2", "verdict": "unmet", "reason": "no evidence"}
rejects = lambda: fold.fold(fold.read_events())[0]["L-spec-0001"]["rejects"]


def _lift_cap435(subject="L-spec-0001", _n=[0]):
    """L-spec-0435: this narrative re-dispatches role=grader on the same
    subject far past the new cap:3 — exactly what a real operator's regrade
    decision is for, so the fixture supplies one before every dispatch from
    the 4th on, rather than being quietly exempted from a rule it never tests.
    Stamped strictly after the subject's own newest event (never bare `now()`,
    whose 1-second resolution a fast-running suite can tie against the
    just-written spawn-started it must outrank)."""
    _n[0] += 1
    newest = max((fold.ts(e.get("ts")) for e in fold.read_events() if e.get("subject") == subject),
                default=_dt.now(_timezone.utc))
    ts = (newest + _timedelta(seconds=5)).isoformat(timespec="seconds")
    # L-<actor>-<token>.jsonl, no embedded hyphen in <token>: fold.read_events()
    # derives the actor by splitting the stem on "-" and joining every middle
    # part — an extra hyphen here reads back as actor "operator-cap435lift0N",
    # never "operator", and the decision's actor-scoped check silently misses it.
    f = TMP / "events" / f"L-operator-cap435lift{_n[0]}.jsonl"
    f.write_text(json.dumps({"v": 1, "ts": ts, "type": "decision", "subject": subject, "regrade": "yes"}) + "\n")


code, types, evs, _ = spawn("grader", out=grade([met, unmet]))
assert types == ["verdict", "rejected-criterion", "checker-coverage-change", "spawn-done"], types
assert evs[0]["confirmed"] is False and evs[1]["criterion"] == "AC2" and rejects() == 1
code, types, evs, _ = spawn("grader", out=grade([met], card_ok="cannot-assess", could_not_run=True))
assert evs[0]["confirmed"] is False and "gate-infra" in types and "criterion-cleared" not in types
assert "checker-coverage-change" not in types, "same coverage note: no change event"
code, types, evs, _ = spawn("grader", out=grade([met, {"ac": "AC2", "verdict": "met", "reason": "now evidenced"}]))
assert "criterion-cleared" in types and evs[0]["confirmed"] is True and rejects() == 0, "a re-grade that names it met clears it"
_lift_cap435()
code, types, evs, _ = spawn("grader", out=grade([met, {"ac": "DONE-COND", "verdict": "unmet", "reason": "residue"}]))
assert rejects() == 1
_lift_cap435()
code, types, evs, _ = spawn("grader", out=grade([met], card_ok="no"))
assert "criterion-cleared" not in types and rejects() == 1, "an unconfirmed verdict clears nothing it did not test"
_lift_cap435()
code, types, evs, _ = spawn("grader", out=grade([met]))
assert [e["criterion"] for e in evs if e["type"] == "criterion-cleared"] == ["DONE-COND"] and rejects() == 0, \
    "a confirmed verdict clears a standing rejection the packet no longer names (first real chain)"

# a-6 (S15/S33): the verdict event carries which rows were cannot-assess, not just
# the roll-up — the fold needs the row-level data to widen `confirmed` over
# EVALUABLE rows when a cannot-assess row's criterion was declared owed. This
# wrapper still writes the literal, strict `confirmed` (unaware of `owed-ac` —
# that's the fold's job); it only stops hiding which rows those were.
cannot_assess = {"ac": "AC7", "verdict": "cannot-assess", "reason": "post-merge check-run does not exist yet",
                 "reason_code": "criterion-unevaluable-from-packet"}
_lift_cap435()
code, types, evs, _ = spawn("grader", out=grade([met, cannot_assess]))
assert types[0] == "verdict" and evs[0]["confirmed"] is False and evs[0]["cannot_assess"] == ["AC7"], evs[0]
assert "rejected-criterion" not in types, "cannot-assess is not unmet — no rejected-criterion for it"
_lift_cap435()
code, types, evs, _ = spawn("grader", out=grade([met]))
assert evs[0]["cannot_assess"] == [], "an all-met grade carries an empty list, not an absent field"

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0434 · rework-base-pinned (L-charter-0042) — R7, AC4-AC13. Each test
# below gets its own real git repo (COMMIT-SHAPE needs real `git rev-list
# --count` answers, never the fake shas the `card` fixture above uses) and its
# own fresh subject, so nothing here can collide with a fixture above or below.
# The synthetic "prior build-done" ledger files below are named "L-builder-
# fakecsN.jsonl" — no digit immediately after the role, so `cs_spawn`'s own
# `[0-9]*`-restricted glob never confuses one for the spawn it just made
# (same convention as this file's "open9192"-style fixtures elsewhere) — but
# SEVERAL OTHER helpers later in this file (`drive`, `_seat_driven`, the raw
# AC8/AC4-seat blocks) glob bare `L-builder-*.jsonl`/`L-grader-*.jsonl` and
# take `max()`, which sorts any letter-suffixed name after every real
# digit-suffixed one FOREVER — so these files are deleted again right after
# this section uses them, before any later test in this file can trip on them.
_H_CS, _FAKE_LEDGERS = [], []


def _fake_ledger(name, **kv):
    p = TMP / "events" / name
    _FAKE_LEDGERS.append(p)
    dispatch.emit(p, {}, "build-done", **kv)
    return p


def _cs_repo(name):
    d = harness.test_root(f"cs-{name}")
    _H_CS.append(d)
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
    return d


def _cs_commit(d, msg):
    subprocess.run(["git", "-C", str(d), "commit", "-q", "--allow-empty", "-m", msg], check=True)
    return subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def cs_spawn(role, subject, cwd, out, packet_text):
    """Like spawn() above, but with a caller-chosen cwd (a real git repo) and a
    caller-chosen packet body (never PK's file-wide default) — restored after,
    so nothing here leaks into a fixture elsewhere in this file."""
    saved = PK.read_text()
    PK.write_text(packet_text)
    res = {"is_error": False, "terminal_reason": "completed", "structured_output": out, "num_turns": 1,
           "usage": {"input_tokens": 1, "output_tokens": 2}, "total_cost_usd": 0.01,
           "modelUsage": {"m": {}}, "permission_denials": []}

    def fake(cmd, packet, cwd_, timeout):
        fake.cmd = cmd
        return argparse.Namespace(stdout=json.dumps(res), returncode=0, stderr="")
    dispatch.run_claude = fake
    a = argparse.Namespace(role=role, subject=subject, packet=str(PK), path=None, cwd=str(cwd),
                           charter=None, project="cs434", mcp_config=None, timeout=None, max_usd=None)
    try:
        dispatch.main(a)
        code = 0
    except SystemExit as e:
        code = e.code
    PK.write_text(saved)
    raw = [json.loads(l) for l in
           max((TMP / "events").glob(f"L-{role}-[0-9]*.jsonl")).read_text().splitlines()]
    evs = [e for e in raw if e["type"] != "spawn-started"]
    return code, [e["type"] for e in evs], evs, (fake.cmd if hasattr(fake, "cmd") else None)


# ── AC3 · dispatch.pinned_base / pinned_base_explicit read straight off a
#          packet FILE's own lines — trailing whitespace stripped, None/False
#          on a missing line or an unreadable path ───────────────────────────
PIN_F = TMP / "content" / "pin-fixture-434.md"
PIN_F.write_text("1. hi\nPINNED_BASE_SHA: abc123f \n2. bye\n")
assert dispatch.pinned_base(PIN_F) == "abc123f", dispatch.pinned_base(PIN_F)
assert dispatch.pinned_base_explicit(PIN_F) is False
PIN_F.write_text("PINNED_BASE_SHA: abc123f\nPINNED_BASE_SHA_EXPLICIT: true\n")
assert dispatch.pinned_base(PIN_F) == "abc123f"
assert dispatch.pinned_base_explicit(PIN_F) is True
PIN_F.write_text("PINNED_BASE_SHA: abc123f\nPINNED_BASE_SHA_EXPLICIT: nope\n")
assert dispatch.pinned_base_explicit(PIN_F) is False, "any other value than the literal true reads False"
PIN_F.write_text("no such line here\n")
assert dispatch.pinned_base(PIN_F) is None
assert dispatch.pinned_base_explicit(PIN_F) is False
_missing434 = TMP / "content" / "does-not-exist-434.md"
assert dispatch.pinned_base(_missing434) is None, "an unreadable path is None, not a crash"
assert dispatch.pinned_base_explicit(_missing434) is False

# ── AC4 · no PINNED_BASE_SHA line at all: refused before any spend ───────────
d4 = _cs_repo("ac4")
c4 = _cs_commit(d4, "base")
card4 = {**card, "identity": {**card["identity"], "base_sha": c4, "ready_sha": c4}}
code, types, evs, cmd = cs_spawn("builder", "L-spec-cs7001", d4, card4, "a packet with no pin at all\n")
assert code == 1 and types == ["spawn-failed"] and cmd is None, (code, types, evs)
assert evs[0].get("reason") == "no-pinned-base", evs[0]

# ── AC5 · the line IS present: build-started.base_sha equals it exactly ──────
d5 = _cs_repo("ac5")
c5 = _cs_commit(d5, "base")
card5 = {**card, "identity": {**card["identity"], "base_sha": c5, "ready_sha": c5}}
code, types, evs, cmd = cs_spawn("builder", "L-spec-cs7002", d5, card5, f"a packet\nPINNED_BASE_SHA: {c5}\n")
assert code == 0 and cmd is not None, (code, types, evs)
bs5 = next(e for e in evs if e["type"] == "build-started")
assert bs5["base_sha"] == c5, bs5

# ── AC6 · build-done.base_sha is the PINNED value, never the card's own —
#          card_base_sha rides only when they differ; base_sha_explicit only
#          when the packet carried PINNED_BASE_SHA_EXPLICIT: true ────────────
d6 = _cs_repo("ac6")
c6 = _cs_commit(d6, "base")
card6a = {**card, "identity": {**card["identity"], "base_sha": "deadbeef01", "ready_sha": c6}}
code, types, evs, cmd = cs_spawn("builder", "L-spec-cs7003", d6, card6a, f"a packet\nPINNED_BASE_SHA: {c6}\n")
bd6a = next(e for e in evs if e["type"] == "build-done")
assert bd6a["base_sha"] == c6 and bd6a.get("card_base_sha") == "deadbeef01", bd6a
assert "base_sha_explicit" not in bd6a, bd6a

card6b = {**card, "identity": {**card["identity"], "base_sha": c6, "ready_sha": c6}}
code, types, evs, cmd = cs_spawn("builder", "L-spec-cs7004", d6, card6b, f"a packet\nPINNED_BASE_SHA: {c6}\n")
bd6b = next(e for e in evs if e["type"] == "build-done")
assert "card_base_sha" not in bd6b, "AC6: the card's value agrees with the pinned one — no card_base_sha field"

card6c = {**card, "identity": {**card["identity"], "base_sha": c6, "ready_sha": c6}}
code, types, evs, cmd = cs_spawn("builder", "L-spec-cs7005", d6, card6c,
                                 f"a packet\nPINNED_BASE_SHA: {c6}\nPINNED_BASE_SHA_EXPLICIT: true\n")
bd6c = next(e for e in evs if e["type"] == "build-done")
assert bd6c.get("base_sha_explicit") is True, bd6c

# ── AC7 · round one (no prior build-done) is never checked, whatever the real
#          count and whatever the card's status: 2 commits above base would
#          reject if this were a rework round, and it is not one ───────────
d7 = _cs_repo("ac7")
c7base = _cs_commit(d7, "base")
_cs_commit(d7, "c1")
c7ready = _cs_commit(d7, "c2")
card7 = {**card, "status": "DONE", "identity": {**card["identity"], "base_sha": c7base, "ready_sha": c7ready}}
code, types, evs, cmd = cs_spawn("builder", "L-spec-cs7006", d7, card7, f"a packet\nPINNED_BASE_SHA: {c7base}\n")
assert not any(e["type"] in ("rejected-criterion", "criterion-cleared") and e.get("criterion") == "COMMIT-SHAPE"
              for e in evs), evs

# ── AC8 · a rework round (a prior build-done on record) whose card is DONE:
#          exactly 1 commit above the pinned base clears; 0, 2+, or an
#          unreadable ready_sha rejects, naming the count and the base ───────
d8c = _cs_repo("ac8-clear")
base8c = _cs_commit(d8c, "base")
ready8c = _cs_commit(d8c, "c1")
_fake_ledger("L-builder-fakecs8c.jsonl", subject="L-spec-cs7010",
             status="DONE", card="x", branch="b", base_sha=base8c, ready_sha="priorready0", verify_exit=0,
             tests_added=True)
card8c = {**card, "status": "DONE", "identity": {**card["identity"], "base_sha": base8c, "ready_sha": ready8c}}
code, types, evs, cmd = cs_spawn("builder", "L-spec-cs7010", d8c, card8c, f"a packet\nPINNED_BASE_SHA: {base8c}\n")
cc8 = next(e for e in evs if e["type"] == "criterion-cleared" and e.get("criterion") == "COMMIT-SHAPE")
assert cc8, evs
assert not any(e["type"] == "rejected-criterion" and e.get("criterion") == "COMMIT-SHAPE" for e in evs), evs

d8z = _cs_repo("ac8-zero")
base8z = _cs_commit(d8z, "base")
_fake_ledger("L-builder-fakecs8z.jsonl", subject="L-spec-cs7011",
             status="DONE", card="x", branch="b", base_sha=base8z, ready_sha="priorready1", verify_exit=0,
             tests_added=True)
card8z = {**card, "status": "DONE", "identity": {**card["identity"], "base_sha": base8z, "ready_sha": base8z}}
code, types, evs, cmd = cs_spawn("builder", "L-spec-cs7011", d8z, card8z, f"a packet\nPINNED_BASE_SHA: {base8z}\n")
rc8z = next(e for e in evs if e["type"] == "rejected-criterion" and e.get("criterion") == "COMMIT-SHAPE")
assert "0 commits" in rc8z["why"] and base8z in rc8z["why"], rc8z

d8t = _cs_repo("ac8-two")
base8t = _cs_commit(d8t, "base")
_cs_commit(d8t, "c1")
ready8t = _cs_commit(d8t, "c2")
_fake_ledger("L-builder-fakecs8t.jsonl", subject="L-spec-cs7012",
             status="DONE", card="x", branch="b", base_sha=base8t, ready_sha="priorready2", verify_exit=0,
             tests_added=True)
card8t = {**card, "status": "DONE", "identity": {**card["identity"], "base_sha": base8t, "ready_sha": ready8t}}
code, types, evs, cmd = cs_spawn("builder", "L-spec-cs7012", d8t, card8t, f"a packet\nPINNED_BASE_SHA: {base8t}\n")
rc8t = next(e for e in evs if e["type"] == "rejected-criterion" and e.get("criterion") == "COMMIT-SHAPE")
assert "2 commits" in rc8t["why"], rc8t

d8u = _cs_repo("ac8-unreadable")
base8u = _cs_commit(d8u, "base")
_fake_ledger("L-builder-fakecs8u.jsonl", subject="L-spec-cs7013",
             status="DONE", card="x", branch="b", base_sha=base8u, ready_sha="priorready3", verify_exit=0,
             tests_added=True)
card8u = {**card, "status": "DONE",
         "identity": {**card["identity"], "base_sha": base8u, "ready_sha": "deadbeef02"}}
code, types, evs, cmd = cs_spawn("builder", "L-spec-cs7013", d8u, card8u, f"a packet\nPINNED_BASE_SHA: {base8u}\n")
rc8u = next(e for e in evs if e["type"] == "rejected-criterion" and e.get("criterion") == "COMMIT-SHAPE")
assert "unreadable" in rc8u["why"], rc8u

# ── AC9 · a rework round whose card is BLOCKED: neither event, whatever the
#          count (including 0, the legitimate no-commit-made case) ───────────
d9 = _cs_repo("ac9")
base9 = _cs_commit(d9, "base")
_fake_ledger("L-builder-fakecs9.jsonl", subject="L-spec-cs7014",
             status="BLOCKED", card="x", branch="b", base_sha=base9, ready_sha="priorready4", verify_exit=1,
             tests_added=False)
card9 = {**card, "status": "BLOCKED", "identity": {**card["identity"], "base_sha": base9, "ready_sha": base9}}
code, types, evs, cmd = cs_spawn("builder", "L-spec-cs7015", d9, card9, f"a packet\nPINNED_BASE_SHA: {base9}\n")
assert not any(e["type"] in ("rejected-criterion", "criterion-cleared") and e.get("criterion") == "COMMIT-SHAPE"
              for e in evs), evs

# ── AC12 · a standing COMMIT-SHAPE rejection refuses a grader dispatch before
#           any spend, reason exactly "commit-shape" ─────────────────────────
d12 = _cs_repo("ac12")
base12 = _cs_commit(d12, "base")
ready12 = _cs_commit(d12, "c1")
ready12b = _cs_commit(d12, "c2")     # 2 commits above base: the rework round rejects
_fake_ledger("L-builder-fakecs12.jsonl", subject="L-spec-cs7020",
             status="DONE", card="x", branch="b", base_sha=base12, ready_sha=ready12, verify_exit=0,
             tests_added=True)
card12 = {**card, "status": "DONE", "identity": {**card["identity"], "base_sha": base12, "ready_sha": ready12b}}
code, types, evs, cmd = cs_spawn("builder", "L-spec-cs7020", d12, card12, f"a packet\nPINNED_BASE_SHA: {base12}\n")
assert any(e["type"] == "rejected-criterion" and e.get("criterion") == "COMMIT-SHAPE" for e in evs), evs
code12g, types12g, evs12g, cmd12g = cs_spawn("grader", "L-spec-cs7020", d12, grade([met]), PK_DEFAULT)
assert code12g == 1 and types12g == ["spawn-failed"] and cmd12g is None, (code12g, types12g, evs12g)
assert evs12g[0].get("reason") == "commit-shape", evs12g[0]

# ── AC13 · a grader's own confirmed verdict never clears a standing
#           COMMIT-SHAPE, even naming no COMMIT-SHAPE criterion at all. AC12
#           above already proves `dispatch.main` refuses this dispatch
#           outright while COMMIT-SHAPE stands — so the ONLY way to reach the
#           "confirmed clears every standing reject" loop this deep is to call
#           `events_for` directly, exactly as `main()`'s own grader branch
#           does, real ledger and all ─────────────────────────────────────
d13 = _cs_repo("ac13")
base13 = _cs_commit(d13, "base")
ready13a = _cs_commit(d13, "c1")
ready13b = _cs_commit(d13, "c2")
_fake_ledger("L-builder-fakecs13.jsonl", subject="L-spec-cs7021",
             status="DONE", card="x", branch="b", base_sha=base13, ready_sha=ready13a, verify_exit=0,
             tests_added=True)
card13 = {**card, "status": "DONE", "identity": {**card["identity"], "base_sha": base13, "ready_sha": ready13b}}
code, types, evs, cmd = cs_spawn("builder", "L-spec-cs7021", d13, card13, f"a packet\nPINNED_BASE_SHA: {base13}\n")
assert any(e["type"] == "rejected-criterion" and e.get("criterion") == "COMMIT-SHAPE" for e in evs), evs
a13g = argparse.Namespace(subject="L-spec-cs7021")
base13g = {"subject": "L-spec-cs7021", "project": "cs434", "spawn": "L-grader-fakecs13"}
ev13g = dispatch.events_for("grader", grade([met]), a13g, base13g)
assert any(t == "verdict" and kv["confirmed"] for t, kv in ev13g), ev13g
assert not any(t == "criterion-cleared" and kv.get("criterion") == "COMMIT-SHAPE" for t, kv in ev13g), ev13g

for p in _H_CS:
    harness.cleanup(p)
for p in _FAKE_LEDGERS:
    p.unlink(missing_ok=True)

# ★ `probe` runs OUTSIDE every repo on purpose (§4.6·10, §9.5). Read as
# undetermined, that refuses the one contract that spends at planning time
# before it spends anything at all.
OUTSIDE = TMP / "run-dir"
OUTSIDE.mkdir()
assert dispatch.porcelain(OUTSIDE) == dispatch.NOT_A_REPO, "no repo is a definite answer, not an undetermined one"
assert dispatch.porcelain(REPO) is not None and dispatch.porcelain(REPO) != dispatch.NOT_A_REPO

# ★ repo_moved proven directly, not only through a mocked spawn: the whole-line
# symmetric difference, the paths those lines name, and the role fact.
rm = dispatch.repo_moved
assert rm("", "?? a.txt\n", "research") == {"lines": ["?? a.txt"], "paths": ["a.txt"], "by_this_spawn": False}
assert rm("?? a.txt\n", "", "research") == {"lines": ["?? a.txt"], "paths": ["a.txt"], "by_this_spawn": False}, \
    "a path that went away moved too"
assert rm("?? a.txt\n", "?? a.txt\n", "research") == {"lines": [], "paths": [], "by_this_spawn": False}, \
    "identical snapshots moved nothing"
staged = rm(" M a.txt\n", "M  a.txt\n", "research")
assert staged["lines"] == [" M a.txt", "M  a.txt"] and staged["paths"] == ["a.txt"], staged
assert staged["lines"], "the operator staging a file mid-spawn is a real movement whose path diff is empty"
ren = rm("", "R  a.txt -> b.txt\n", "research")
assert ren["paths"] == ["a.txt", "b.txt"] and not any(" -> " in p for p in ren["paths"]), ren
assert rm("", "?? a.txt\n", "builder")["by_this_spawn"] is True
assert rm("", "?? a.txt\n", "grader")["by_this_spawn"] is False, \
    "by_this_spawn is which role was dispatched, never a proof of authorship"
probe_out = {"path": "content/L-probe-0001/", "summary": "s", "externals": [{"name": "x", "came_back": "y"}],
             "n_inputs": 3, "spend_usd": 0.0, "broke": [], "complete": True, "contamination": False,
             "declarations": []}
pd = TMP / "content" / "L-probe-0001"
pd.mkdir(parents=True)
(pd / "run.md").write_text("ran")
a = argparse.Namespace(role="probe", subject="L-charter-0001", packet=str(PK), path=str(pd),
                       cwd=str(OUTSIDE), charter=None, project="t", mcp_config=None, timeout=None, max_usd=None)


def fake(cmd, packet, cwd, timeout):
    return argparse.Namespace(returncode=0, stderr="", stdout=json.dumps(
        {"is_error": False, "terminal_reason": "completed", "structured_output": probe_out, "num_turns": 1,
         "usage": {"input_tokens": 1, "output_tokens": 2}, "total_cost_usd": 0.01, "modelUsage": {"m": {}},
         "permission_denials": []}))


dispatch.run_claude = fake
try:
    dispatch.main(a)
    code = 0
except SystemExit as e:
    code = e.code
N += 1
pev = [json.loads(l) for l in max((TMP / "events").glob("L-probe-*.jsonl")).read_text().splitlines()]
assert code == 0, [e.get("why") for e in pev]
assert [e["type"] for e in pev] == ["spawn-started", "probe-run", "spawn-done"], [e["type"] for e in pev]
assert pev[1]["externals"] == ["x"] and pev[1]["n_inputs"] == 3, pev[1]

ev = fold.read_events()
specs, *_ = fold.fold(ev)
assert {e["actor"] for e in ev} >= {"research", "spec-writer", "builder", "grader"}, "D90: the actor is the filename"
assert specs["L-spec-0001"]["state"] == "reviewing", specs["L-spec-0001"]["state"]
assert dispatch.alloc(TMP / "events", "L-x-", ".jsonl") != dispatch.alloc(TMP / "events", "L-x-", ".jsonl")

# §2.8: max+1 over the ids that EXIST. The Planner hit this on the first real
# charter — three spec subjects lived in the ledger with no content file, so
# alloc handed back an id already carrying `spec-closed`. A spec-writer
# dispatched there is a silent loss in an append-only ledger.
(TMP / "content").mkdir(exist_ok=True)
(TMP / "content" / "L-spec-0001.md").write_text("the only file\n")
dispatch.emit(TMP / "events" / "L-planner-0001.jsonl", {}, "spec-written", subject="L-spec-0007")
dispatch.emit(TMP / "events" / "L-planner-0001.jsonl", {}, "brief", subject="L-brief-0042")
got = dispatch.alloc(TMP / "content", "L-spec-", ".md")
assert got.stem == "L-spec-0008", got            # the ledger's 0007 raised the floor
assert dispatch.subject_ids("L-spec-") and 42 not in dispatch.subject_ids("L-spec-"), "another kind never counts"
N += 1
# ── the seat path: no `claude -p`; the result comes back from a file. Where the
# headless CLI is banned as metered (Albert Scott, spec 572) the spawn is an
# interactive session's sub-agent, and the wrapper only writes the packet and
# waits. Every after-the-fact check above runs unchanged on it, and the terminal
# event says which route ran, so the two are distinguishable forever.
import threading, time
os.environ["DOIT_SEAT"] = "1"


def never(*_):
    raise AssertionError("the seat path must not exec claude -p")


dispatch.run_claude = never
rp2 = TMP / "content" / "L-research-0002.md"


def seat_writer():
    for _ in range(400):
        pk = list((TMP / "seat").glob("*.packet.md")) if (TMP / "seat").is_dir() else []
        pk = [q for q in pk if not (TMP / "seat" / (q.name.split(".")[0] + ".result.json")).exists()]
        if pk:
            sid = pk[0].name.split(".")[0]
            assert f"spawn_id: {sid}" in pk[0].read_text(), "the packet carries the spawn id"
            assert json.loads((TMP / "seat" / f"{sid}.cmd.json").read_text())["cmd"][:2] == ["claude", "-p"], \
                "the line that WOULD have run is recorded beside the packet"
            rp2.write_text("dug")
            (TMP / "seat" / f"{sid}.result.json").write_text(json.dumps(
                {"is_error": False, "structured_output": {**research, "path": "content/L-research-0002.md"},
                 "num_turns": 3, "usage": {"input_tokens": 5, "output_tokens": 6}, "total_cost_usd": None,
                 "modelUsage": {"seat-model": {}}, "session_id": "seat-1", "permission_denials": []}))
            return
        time.sleep(0.05)


threading.Thread(target=seat_writer, daemon=True).start()
a = argparse.Namespace(role="research", subject="L-spec-0001", packet=str(PK), path=str(rp2), cwd=str(REPO),
                       charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=False)
try:
    dispatch.main(a)
    code = 0
except SystemExit as e:
    code = e.code
sev = [json.loads(l) for l in max((TMP / "events").glob("L-research-*.jsonl")).read_text().splitlines()]
assert code == 0, [e.get("why") for e in sev]
assert [e["type"] for e in sev] == ["spawn-started", "research-filed", "spawn-done"], [e["type"] for e in sev]
assert sev[-1]["spawn_path"] == "seat" and sev[-1]["session"] == "seat-1" and sev[-1]["model"] == "seat-model", sev[-1]
assert sev[-1]["cost_usd"] is None, "a seat spawn has no list-price figure and must not read as free"
N += 1

# The schema is the wrapper's to enforce on the seat route — the CLI is not there to.
rp3 = TMP / "content" / "L-research-0003.md"


def seat_writer_bad():
    for _ in range(400):
        pk = list((TMP / "seat").glob("*.packet.md")) if (TMP / "seat").is_dir() else []
        pk = [q for q in pk if not (TMP / "seat" / (q.name.split(".")[0] + ".result.json")).exists()]
        if pk:
            sid = pk[0].name.split(".")[0]
            rp3.write_text("dug")
            (TMP / "seat" / f"{sid}.result.json").write_text(json.dumps(
                {"is_error": False, "structured_output": {**research, "path": "content/L-research-0003.md",
                                                          "answered": "maybe"},
                 "num_turns": 1, "usage": {}, "total_cost_usd": None, "modelUsage": {}, "session_id": "seat-2",
                 "permission_denials": []}))
            return
        time.sleep(0.05)


threading.Thread(target=seat_writer_bad, daemon=True).start()
a = argparse.Namespace(role="research", subject="L-spec-0001", packet=str(PK), path=str(rp3), cwd=str(REPO),
                       charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=True)
try:
    dispatch.main(a)
    code = 0
except SystemExit as e:
    code = e.code
sev = [json.loads(l) for l in max((TMP / "events").glob("L-research-*.jsonl")).read_text().splitlines()]
assert code == 1 and sev[-1]["type"] == "spawn-failed" and "violates research.schema.json" in sev[-1]["why"], sev[-1]
del os.environ["DOIT_SEAT"]
N += 1
# A bare, validated Output plus a meta sidecar is accepted as-is: the envelope is the wrapper's.
os.environ["DOIT_SEAT"] = "1"
rp5 = TMP / "content" / "L-research-0005.md"


def seat_writer_bare():
    for _ in range(400):
        pk = list((TMP / "seat").glob("*.packet.md")) if (TMP / "seat").is_dir() else []
        pk = [q for q in pk if not (TMP / "seat" / (q.name.split(".")[0] + ".output.json")).exists()
              and not (TMP / "seat" / (q.name.split(".")[0] + ".result.json")).exists()]
        if pk:
            sid = pk[0].name.split(".")[0]
            rp5.write_text("dug")
            # an invalid draft first, as a contract iterating with `doit validate` writes
            (TMP / "seat" / f"{sid}.output.json").write_text(json.dumps({**research, "answered": "maybe"}))
            time.sleep(3.5)             # longer than the wrapper's poll + settle
            (TMP / "seat" / f"{sid}.output.json").write_text(json.dumps({**research, "path": "content/L-research-0005.md"}))
            (TMP / "seat" / f"{sid}.meta.json").write_text(json.dumps({"model": "claude-opus-5", "session": "seat-3", "turns": 4}))
            return
        time.sleep(0.05)


threading.Thread(target=seat_writer_bare, daemon=True).start()
a = argparse.Namespace(role="research", subject="L-spec-0001", packet=str(PK), path=str(rp5), cwd=str(REPO),
                       charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=True)
try:
    dispatch.main(a)
    code = 0
except SystemExit as e:
    code = e.code
sev = [json.loads(l) for l in max((TMP / "events").glob("L-research-*.jsonl")).read_text().splitlines()]
assert code == 0 and sev[-1]["type"] == "spawn-done" and sev[-1]["session"] == "seat-3" \
    and sev[-1]["model"] == "claude-opus-5" and sev[-1]["turns"] == 4, sev[-1]
del os.environ["DOIT_SEAT"]
N += 1

# retro step 9: a meta sidecar carrying the four-way split (stamp.sh/usage.py's
# shape) plus model_observed flows onto the terminal event UNCHANGED by main()
# — model_used prefers the OBSERVED model (real transcript evidence) over the
# pane-typed `model` field, and model_observed is true only because that
# evidence exists; subagent_tokens (the older blended figure) rides beside the
# split, not instead of it.
os.environ["DOIT_SEAT"] = "1"
rp5b = TMP / "content" / "L-research-0005b.md"


def seat_writer_split():
    for _ in range(400):
        pk = list((TMP / "seat").glob("*.packet.md")) if (TMP / "seat").is_dir() else []
        pk = [q for q in pk if not (TMP / "seat" / (q.name.split(".")[0] + ".output.json")).exists()
              and not (TMP / "seat" / (q.name.split(".")[0] + ".result.json")).exists()]
        if pk:
            sid = pk[0].name.split(".")[0]
            rp5b.write_text("dug")
            (TMP / "seat" / f"{sid}.output.json").write_text(json.dumps({**research, "path": "content/L-research-0005b.md"}))
            (TMP / "seat" / f"{sid}.meta.json").write_text(json.dumps({
                "model": "claude-opus-5", "session": "seat-split", "turns": 7, "duration_ms": 42000,
                "model_observed": "claude-sonnet-5",     # the transcript's OWN model, not the typed claim above
                "usage": {"subagent_tokens": 5000, "input_tokens": 10, "output_tokens": 20,
                          "cache_read_input_tokens": 300, "cache_creation_input_tokens": 40}}))
            return
        time.sleep(0.05)


threading.Thread(target=seat_writer_split, daemon=True).start()
a = argparse.Namespace(role="research", subject="L-spec-0001", packet=str(PK), path=str(rp5b), cwd=str(REPO),
                       charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=True)
try:
    dispatch.main(a)
    code = 0
except SystemExit as e:
    code = e.code
sev = [json.loads(l) for l in max((TMP / "events").glob("L-research-*.jsonl")).read_text().splitlines()]
d = sev[-1]
assert code == 0 and d["type"] == "spawn-done", d
assert d["model_used"] == "claude-sonnet-5", "model_used prefers the transcript-observed model over the typed one"
assert d["model_observed"] is True, "real transcript evidence -> observed, not assumed"
assert d["input_tokens"] == 10 and d["output_tokens"] == 20 and d["cache_read"] == 300 \
    and d["cache_creation"] == 40, d
assert d["subagent_tokens"] == 5000, "the older blended figure rides beside the split, not replaced by it"
del os.environ["DOIT_SEAT"]
N += 1

# DOIT_REPO_VOLATILE: a declared path that moves under a spawn does not void it; an undeclared one still does.
os.environ["DOIT_REPO_VOLATILE"] = "docs/sessions/*"
import importlib
importlib.reload(dispatch)
# L-spec-0486/R15d: a reload re-executes dispatch.py's own top-level code,
# which wipes this file's file-top `dispatch.ensure_room` hermetic stub (a
# fresh function object replaces it) — re-applied here, immediately, so every
# builder/grader spawn() drive for the REST of this file still never touches
# this shared box's real, genuinely tight disk (measured 2026-09-30: ~32G
# avail on the orchestration box, with sibling builders concurrently
# consuming it) instead of the fixture scratch root this file set up.
dispatch.ensure_room = lambda path, need_gb: True
dispatch.run_claude = never
(REPO / "docs" / "sessions").mkdir(parents=True)
vol = REPO / "docs" / "sessions" / "health.md"
rp4 = TMP / "content" / "L-research-0004.md"
assert dispatch.porcelain(REPO) == "", dispatch.porcelain(REPO)
vol.write_text("cron wrote this")
assert dispatch.porcelain(REPO) == "", "a declared volatile path is invisible to the check"
stray.write_text("x")
assert "stray.txt" in dispatch.porcelain(REPO), "an undeclared path is still seen"
stray.unlink(), vol.unlink()
del os.environ["DOIT_REPO_VOLATILE"]
importlib.reload(dispatch)
dispatch.ensure_room = lambda path, need_gb: True   # re-applied post-reload, see the comment above

# `doit validate` is the seat route's StructuredOutput: exit 1 names the violation, exit 0 says VALID.
good, bad = TMP / "good.json", TMP / "bad.json"
good.write_text(json.dumps(research)), bad.write_text(json.dumps({**research, "answered": "maybe"}))
v = lambda f: subprocess.run([sys.executable, str(pathlib.Path(__file__).parent / "validate.py"), "research", str(f)],
                             capture_output=True, text=True)
assert v(good).returncode == 0 and "VALID" in v(good).stdout, v(good)
assert v(bad).returncode == 1 and "INVALID at answered" in v(bad).stderr, v(bad)

# ── the model map (models.toml): decided once per root; requested vs used stamped on
# every terminal event; the codex backend; a flag that disagrees is refused unspent.
import models
MT = TMP / "models.toml"
MT.write_text('[defaults]\nbackend = "seat"\n'
              '[contracts.research]\nbackend = "codex"\nmodel = "gpt-6-astra"\n'
              '[contracts.grader]\nbackend = "claude-p"\nmodel = "claude-sonnet-5"\n')
rp6 = TMP / "content" / "L-research-0006.md"


def fake_codex(cmd, packet, cwd, timeout):
    fake_codex.cmd = cmd
    assert packet.startswith("a packet") and "spawn_id: L-research-" in packet
    pathlib.Path(cmd[cmd.index("-o") + 1]).write_text(json.dumps({**research, "path": "content/L-research-0006.md"}))
    rp6.write_text("dug")
    return argparse.Namespace(returncode=0, stderr="", stdout="\n".join(json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": "codex-1"}, {"type": "turn.started"},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 7}},
        {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 3}}]))


dispatch.run_codex_exec, dispatch.run_claude = fake_codex, never


def run(role, path, seat=False):
    a = argparse.Namespace(role=role, subject="L-spec-0001", packet=str(PK), path=str(path), cwd=str(REPO),
                           charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=seat)
    try:
        dispatch.main(a)
        code = 0
    except SystemExit as e:
        code = e.code
    global N
    N += 1
    return code, [json.loads(l) for l in max((TMP / "events").glob(f"L-{role}-*.jsonl")).read_text().splitlines()]


code, ev = run("research", rp6)
assert code == 0 and ev[-1]["type"] == "spawn-done", [e.get("why") for e in ev]
d = ev[-1]
assert d["backend"] == d["spawn_path"] == "codex" and d["model_requested"] == "gpt-6-astra" and d["model_map"] == "models.toml", d
assert d["model_used"] == "gpt-6-astra" and d["model_observed"] is False and d["model_match"] is True, "codex does not echo its model: used = the flag, and the event says unobserved"
assert d["session"] == "codex-1" and d["turns"] == 2 and d["input_tokens"] == 150 and d["cache_read"] == 10 and d["cost_usd"] is None, d
assert d["first_on_model"] is True, "the first spawn-done of a (contract, model) pair is the D120 trust run"
c = fake_codex.cmd
assert c[:2] == ["codex", "exec"] and c[c.index("-m") + 1] == "gpt-6-astra" and c[c.index("--sandbox") + 1] == "workspace-write" \
    and "--output-schema" in c and c[-1] == "-", c
assert (TMP / "seat" / f"{d['spawn']}.codex.jsonl").is_file() and (TMP / "seat" / f"{d['spawn']}.cmd.json").is_file()
code, ev = run("research", rp6)
assert code == 0 and ev[-1]["first_on_model"] is False, "the second run on the same (contract, model) is not the trust run"
code, ev = run("research", rp6, seat=True)
assert code == 1 and [e["type"] for e in ev] == ["spawn-failed"] and "contradicts" in ev[-1]["why"], \
    "a flag that disagrees with the root's map is refused before a start event or a spend"
_lift_cap435()
code, types, evs, cmd = spawn("grader", out=grade([met]))
assert code == 0 and cmd[cmd.index("--model") + 1] == "claude-sonnet-5", "the map's model rides the -p line"
assert evs[-1]["backend"] == "claude-p" and evs[-1]["model_requested"] == "claude-sonnet-5" and evs[-1]["model_used"] == "m" \
    and evs[-1]["model_match"] is False and evs[-1]["model_observed"] is True, evs[-1]
# codex that returned nothing usable is a failed spawn, not a null read as clean
def codex_dead(cmd, packet, cwd, timeout):
    return argparse.Namespace(returncode=1, stderr="quota", stdout=json.dumps({"type": "error", "message": "weekly limit"}))
dispatch.run_codex_exec = codex_dead
code, ev = run("research", rp6)
assert code == 1 and ev[-1]["type"] == "spawn-failed" and "weekly limit" in ev[-1]["why"], ev[-1]
# the loader refuses a map that lies
for bad, word in [('[contracts.grader]\nbackend = "seat"\nmodel = "claude-fable-5-1"\n', "pane-only"),
                  ('[contracts.thinker]\nbackend = "seat"\nmodel = "claude-sonnet-5"\n', "pane role"),
                  ('[contracts.executor]\nbackend = "codex"\nmodel = "gpt-6-astra"\n', "executor"),
                  ('[contracts.builder]\nbackend = "cloud"\nmodel = "x"\n', "not one of")]:
    MT.write_text(bad)
    try:
        models.load()
        raise AssertionError(f"accepted: {bad}")
    except ValueError as e:
        assert word in str(e), (word, str(e))
# the poke obeys the map: an Executor that is a pane is never poked into claude -p (S32)
MT.write_text('[contracts.executor]\nbackend = "pane"\nmodel = "claude-sonnet-5"\n')
del os.environ["DOIT_NO_POKE"]
def boom(*a, **k):
    raise AssertionError("poke spawned tick.py on a root whose Executor is a pane")
real_popen, dispatch.subprocess.Popen = dispatch.subprocess.Popen, boom
dispatch.poke()
dispatch.subprocess.Popen = real_popen
os.environ["DOIT_NO_POKE"] = "1"
MT.unlink()

# `fallback`: a codex refusal that is the backend's own re-dispatches ONCE on the fallback
# backend, and the event says so. Here: codex dead -> seat, served by the seat writer.
MT.write_text('[contracts.research]\nbackend = "codex"\nmodel = "gpt-6-astra"\n'
              'fallback = { backend = "seat", model = "claude-sonnet-5" }\n')
rp7 = TMP / "content" / "L-research-0007.md"


def seat_writer_fb():
    for _ in range(600):
        pk = [q for q in (list((TMP / "seat").glob("*.packet.md")) if (TMP / "seat").is_dir() else [])
              if not (TMP / "seat" / (q.name.split(".")[0] + ".result.json")).exists()
              and not (TMP / "seat" / (q.name.split(".")[0] + ".output.json")).exists()
              and (TMP / "seat" / (q.name.split(".")[0] + ".cmd.json")).exists()
              and json.loads((TMP / "seat" / (q.name.split(".")[0] + ".cmd.json")).read_text())["cmd"][:2] == ["claude", "-p"]]
        if pk:
            sid = pk[0].name.split(".")[0]
            rp7.write_text("dug")
            (TMP / "seat" / f"{sid}.result.json").write_text(json.dumps(
                {"is_error": False, "structured_output": {**research, "path": "content/L-research-0007.md"},
                 "num_turns": 2, "usage": {}, "total_cost_usd": None, "modelUsage": {"claude-sonnet-5": {}},
                 "session_id": "seat-fb", "permission_denials": []}))
            return
        time.sleep(0.05)


dispatch.run_codex_exec = codex_dead
PK.write_text("a packet for the fallback run\n")   # D120 refuses an identical packet that already failed on codex
threading.Thread(target=seat_writer_fb, daemon=True).start()
code, ev = run("research", rp7)
PK.write_text(PK_DEFAULT)
assert code == 0 and [e["type"] for e in ev] == ["spawn-started", "backend-fallback", "research-filed", "spawn-done"], \
    [(e["type"], e.get("why")) for e in ev]
assert ev[1]["from_backend"] == "codex" and ev[1]["to_backend"] == "seat" and "weekly limit" in ev[1]["why"]
d = ev[-1]
assert d["backend"] == "seat" and d["backend_fallback"] is True and d["fallback_from"] == "codex" \
    and d["model_requested"] == "claude-sonnet-5" and d["model_used"] == "claude-sonnet-5" and d["session"] == "seat-fb", d
MT.write_text('[contracts.research]\nbackend = "codex"\nmodel = "gpt-6-astra"\n'
              'fallback = { backend = "codex", model = "gpt-6-astra" }\n')
try:
    models.load()
    raise AssertionError("a fallback on the same backend must be refused")
except ValueError as e:
    assert "different" in str(e)
MT.unlink()

# `doit models use <profile>` installs a validated template and records the change.
os.environ["DOIT_LEDGER_FILE"] = "L-operator-test.jsonl"
import io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    assert models.main(["use", "claude-only"]) == 0
assert MT.is_file() and models.load()["spec-auditor"]["model"] == "claude-opus-5" and models.backend_of("executor", models.load()) == "pane"
chg = [json.loads(l) for l in (TMP / "events" / "L-operator-test.jsonl").read_text().splitlines()]
assert chg[-1]["type"] == "models-changed" and chg[-1]["profile"] == "claude-only" and chg[-1]["sha256"], chg[-1]
with contextlib.redirect_stdout(io.StringIO()) as buf:
    assert models.main(["show"]) == 0
assert "spec-auditor      seat      claude-opus-5" in buf.getvalue(), buf.getvalue()
del os.environ["DOIT_LEDGER_FILE"]
MT.unlink()

# [weights] — retro step 9: model -> price ratios relative to input=1.0. No
# models.toml, or a map with no [weights] table, or a model missing from it,
# all render unweighted ({} / absent) rather than fabricating a ratio; a model
# that IS named must carry all four keys and they must be numbers, or the map
# is refused like any other bad map.
assert models.load_weights(TMP / "no-such-models.toml") == {}, "no file -> {}"
MT.write_text('[defaults]\nbackend = "seat"\n')
assert models.load_weights() == {}, "a map with no [weights] table -> {}"
MT.write_text('[defaults]\nbackend = "seat"\n'
              '[weights.claude-sonnet-5]\ninput = 1.0\noutput = 5.0\ncache_read = 0.1\ncache_creation = 1.25\n')
w = models.load_weights()
assert w == {"claude-sonnet-5": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_creation": 1.25}}, w
assert models.load()["_weights"] == w, "load() carries the same table under _weights"
MT.write_text('[defaults]\nbackend = "seat"\n'
              '[weights.claude-sonnet-5]\ninput = 1.0\noutput = 5.0\n')          # missing two required keys
try:
    models.load_weights()
    raise AssertionError("a named model missing a required ratio must be refused")
except ValueError as e:
    assert "missing" in str(e), str(e)
MT.write_text('[defaults]\nbackend = "seat"\n'
              '[weights.claude-sonnet-5]\ninput = 1.0\noutput = "five"\ncache_read = 0.1\ncache_creation = 1.25\n')
try:
    models.load_weights()
    raise AssertionError("a non-numeric ratio must be refused")
except ValueError as e:
    assert "non-numeric" in str(e), str(e)
MT.unlink()
# both shipped templates carry a valid [weights] table for every model they name
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for tmpl in ("models.example.toml", "models.claude-only.toml"):
    tw = models.load(REPO_ROOT / tmpl)["_weights"]
    assert tw["claude-sonnet-5"] == {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_creation": 1.25}, (tmpl, tw)
assert "gpt-6-astra" in models.load(REPO_ROOT / "models.example.toml")["_weights"], \
    "every model models.example.toml names in a contract gets a weights row"

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0189 · unserved-seat-fails-loudly (L-charter-0028) — R6
# ══════════════════════════════════════════════════════════════════════════════
import unittest.mock as _mock

DISPATCH_TIME = time  # the real `time` module dispatch.run_seat's local `import time` shares
_real_sleep = time.sleep  # captured BEFORE any patch — a direct object ref survives mock.patch("time.sleep", ...)


def _rm_seat(spawn_id):
    for suf in (".packet.md", ".cmd.json"):
        p = TMP / "seat" / f"{spawn_id}{suf}"
        p.exists() and p.unlink()


# Sweep orphaned packets from earlier in this file (e.g. L-research-0018's codex
# `is_error` run, which writes a `.packet.md` that never resolves to `.output.json`
# or `.result.json`) — this block's own glob-based "find the open packet" helpers
# below must not pick up a leftover from a wholly unrelated, already-answered test.
if (TMP / "seat").is_dir():
    for _p in list((TMP / "seat").glob("*.packet.md")):
        _sid = _p.name.split(".")[0]
        if not (TMP / "seat" / f"{_sid}.result.json").exists() and \
                not (TMP / "seat" / f"{_sid}.output.json").exists():
            _rm_seat(_sid)


# ── L-spec-0269 AC1 (rewrite of the old AC1 block, §5) · run_seat given
# ledger=/base=/role=: nobody ever claims -> exactly one non-terminal
# `seat-stale` between roughly 1s and 3s of wall time, and `Unserved` does NOT
# fire there any more (the 60s timeout/window is untouched) ──────────────────
os.environ["DOIT_SEAT_CLAIM_SEC"] = "1"
ac1_spawn = "L-research-r6ac1"
ac1_ledger = TMP / "events" / "L-r6ac1-ledger.jsonl"
ac1_ledger.write_text("")
ac1_base = {"subject": "L-spec-0269ac1", "project": "t", "spawn": ac1_spawn}
ac1_holder = {}


def _ac1_bg():
    try:
        dispatch.run_seat(ac1_spawn, ["claude", "-p"], "packet", str(REPO), 60,
                          ledger=ac1_ledger, base=ac1_base, role="grader")
    except Exception as exc:
        ac1_holder["exc"] = exc


ac1_th = threading.Thread(target=_ac1_bg, daemon=True)
ac1_th.start()
ac1_th.join(3.5)
assert "exc" not in ac1_holder, f"AC1: run_seat must not raise within the claim window's own ping: {ac1_holder.get('exc')}"
ac1_lines = [json.loads(l) for l in ac1_ledger.read_text().splitlines() if l.strip()]
assert len(ac1_lines) == 1 and ac1_lines[0]["type"] == "seat-stale", ac1_lines
assert ac1_lines[0]["spawn"] == ac1_spawn and ac1_lines[0]["role"] == "grader" and \
    ac1_lines[0]["subject"] == "L-spec-0269ac1" and ac1_lines[0]["age_s"] >= 1, ac1_lines[0]
_rm_seat(ac1_spawn)
N += 1

# ── L-spec-0269 AC2 (direct half) · run_seat given `window=3` (seconds) raises
# `Unserved` once wall time passes 3s — NOT at DOIT_SEAT_CLAIM_SEC=1 ──────────
ac2b_spawn = "L-research-0269ac2"
ac2b_t0 = DISPATCH_TIME.time()
try:
    dispatch.run_seat(ac2b_spawn, ["claude", "-p"], "packet", str(REPO), 60, window=3)
    raise AssertionError("run_seat must raise Unserved once window=3 elapses")
except dispatch.Unserved:
    ac2b_wall = DISPATCH_TIME.time() - ac2b_t0
assert 3 <= ac2b_wall < 8, f"AC2: must fire at ~3s (the window), not ~1s (the claim ping): {ac2b_wall}"
_rm_seat(ac2b_spawn)
del os.environ["DOIT_SEAT_CLAIM_SEC"]
N += 1

# ── L-spec-0269 AC3 · claimed at (simulated) 2.5s, cap 4s: the wait-for-result
# clock restarts at the CLAIM, not the call's start — a result landing at
# (simulated) 6s — 3.5s after the claim — is picked up normally, never treated
# as a timeout (a single, start-measured clock would have raised at 6s) ──────
ac3_spawn = "L-research-0269ac3"
(TMP / "seat").mkdir(parents=True, exist_ok=True)
(TMP / "seat" / f"{ac3_spawn}.claimed").write_text("")     # pre-claimed: observed on the very first check


def _ac3_finish():
    _real_sleep(0.05)
    (TMP / "seat" / f"{ac3_spawn}.result.json").write_text(json.dumps(
        {"is_error": False, "structured_output": {**research, "path": "content/L-research-0269ac3.md"},
         "num_turns": 1, "usage": {}, "total_cost_usd": None, "modelUsage": {"m": {}},
         "session_id": "ac3", "permission_denials": []}))


def _fake_time_ac3():
    _fake_time_ac3.n += 1
    return {1: 0.0, 2: 2.5}.get(_fake_time_ac3.n, 6.0)


_fake_time_ac3.n = 0
threading.Thread(target=_ac3_finish, daemon=True).start()
real_t0_ac3 = time.perf_counter()
with _mock.patch("time.time", _fake_time_ac3), _mock.patch("time.sleep", lambda s: None):
    r_ac3 = dispatch.run_seat(ac3_spawn, ["claude", "-p"], "packet", str(REPO), 4, window=3)
real_wall_ac3 = time.perf_counter() - real_t0_ac3
assert isinstance(r_ac3, dispatch.SeatResult), r_ac3
assert real_wall_ac3 < 5, f"the fake clock must have decided it, not real time: {real_wall_ac3}s"
_rm_seat(ac3_spawn)
N += 1

# ── AC2 (updated for L-spec-0269) · the 300s DOIT_SEAT_CLAIM_SEC default does
# not fire prematurely within a short window — but the exception an omitted
# `window` produces for a wholly-unclaimed seat is now `Unserved`, not
# `TimeoutExpired`: back-compat means the SAME timing (window defaults to
# `timeout`), never the SAME exception type, since which of the two now fires
# turns on claimed state, not on which of claim_sec/timeout is smaller ────────
ac2_spawn = "L-research-r6ac2"
ac2_holder = {}


def ac2_bg():
    try:
        dispatch.run_seat(ac2_spawn, ["claude", "-p"], "packet", str(REPO), 6)
    except Exception as exc:
        ac2_holder["exc"] = exc


ac2_th = threading.Thread(target=ac2_bg, daemon=True)
ac2_th.start()
ac2_th.join(3)
assert "exc" not in ac2_holder, f"the default (unset) claim window fired prematurely: {ac2_holder.get('exc')}"
ac2_th.join(6)
assert isinstance(ac2_holder.get("exc"), dispatch.Unserved), \
    "L-spec-0269: an omitted window defaults to timeout, so a wholly-unclaimed seat now ends in " \
    "Unserved once that shared threshold elapses, not TimeoutExpired"
_rm_seat(ac2_spawn)
N += 1


def _seat_driven(role, subject, timeout_min, path=None):
    a = argparse.Namespace(role=role, subject=subject, packet=str(PK), path=path, cwd=str(REPO),
                           charter=None, project="t", mcp_config=None, timeout=timeout_min, max_usd=None,
                           seat=False)
    t0 = DISPATCH_TIME.time()
    try:
        dispatch.main(a)
        code = 0
    except SystemExit as ex:
        code = ex.code
    wall = DISPATCH_TIME.time() - t0
    raw = [json.loads(l) for l in max((TMP / "events").glob(f"L-{role}-*.jsonl")).read_text().splitlines()]
    return code, raw, wall


# ── L-spec-0269 (rewrite of the old AC3 main() block, §5) · DOIT_SEAT_CLAIM_SEC
# alone no longer ends the wait — a `window_min: 1` on the dispatched subject's
# spec text is read (and, being smaller than research's own 5-min cap, has no
# effect — proving the "raised, never lowered" rule holds even here); what
# actually keeps this fast is a mocked clock jumping straight past whatever
# window is computed (no role's cap is under 5 real minutes). `main()`'s
# existing `except Unserved` path still turns this into a terminal
# `spawn-failed{reason: "unserved"}`, byte-for-byte, well under 15s of REAL
# wall time, with one non-terminal `seat-stale` recorded first ────────────────
os.environ["DOIT_SEAT"] = "1"
dispatch.run_claude = never
os.environ["DOIT_SEAT_CLAIM_SEC"] = "1"
ac3main_subject = "L-spec-0269ac3main"
(TMP / "content").mkdir(parents=True, exist_ok=True)
(TMP / "content" / f"{ac3main_subject}.md").write_text(
    "# L-spec-0269ac3main\nwindow_min: 1\n\n## 1. Goal\n\ntext\n")
# a fresh packet body — L-research-0018 earlier in this file already recorded an
# is_error failure for the default "a packet\n" body on this same contract, and
# D120's dedup (packet_sha256 + contract_sha256) would refuse this dispatch before
# it ever reached run_seat, on a packet whose CONTENT happens to match, not a re-run.
PK.write_text("a packet for r6ac3\n")


def _fake_time_ac3main():
    _fake_time_ac3main.n += 1
    return 0.0 if _fake_time_ac3main.n == 1 else 100000.0


_fake_time_ac3main.n = 0
# L-spec-0262/SWP1: research is a writing role with no system-wide default, so it
# needs an explicit --path to ever reach run_seat at all (the case this fixture
# means to exercise) — a bare TMP/content path, never opened, is enough.
a3main = argparse.Namespace(role="research", subject=ac3main_subject, packet=str(PK),
                            path=str(TMP / "content" / "L-research-r6ac3.md"), cwd=str(REPO),
                            charter=None, project="t", mcp_config=None, timeout=60, max_usd=None, seat=False)
real_t0_ac3main = time.perf_counter()
with _mock.patch("time.time", _fake_time_ac3main), _mock.patch("time.sleep", lambda s: None):
    try:
        dispatch.main(a3main)
        code = 0
    except SystemExit as ex:
        code = ex.code
real_wall_ac3main = time.perf_counter() - real_t0_ac3main
PK.write_text(PK_DEFAULT)
raw = [json.loads(l) for l in max((TMP / "events").glob("L-research-*.jsonl")).read_text().splitlines()]
assert code == 1 and [e["type"] for e in raw] == ["spawn-started", "seat-stale", "spawn-failed"], raw
assert raw[-1]["reason"] == "unserved" and raw[-1]["spawn_path"] == "seat", raw[-1]
assert real_wall_ac3main < 15, real_wall_ac3main
_rm_seat(raw[0]["spawn"])
N += 1

# ── AC4 · a CLAIMED seat is wholly unaffected — reaches spawn-done, no
# spawn-failed anywhere in its (unfiltered) event list ─────────────────────────
PK.write_text("a packet for r6ac4\n")           # same D120 reason as AC3 above
rp_ac4 = TMP / "content" / "L-research-r6ac4.md"


def claim_then_finish():
    for _ in range(400):
        # neither `.result.json` NOR `.output.json`: earlier fixtures in this file
        # (seat_writer_bare, seat_writer_split) complete via output.json+meta.json
        # and never write a result.json, so a filter on result.json alone would
        # still see their packet.md as "open" and grab the wrong one; the NEWEST
        # by mtime is this test's own, freshly written.
        pk = [q for q in (list((TMP / "seat").glob("*.packet.md")) if (TMP / "seat").is_dir() else [])
              if not (TMP / "seat" / (q.name.split(".")[0] + ".result.json")).exists()
              and not (TMP / "seat" / (q.name.split(".")[0] + ".output.json")).exists()]
        if pk:
            sid = max(pk, key=lambda q: q.stat().st_mtime).name.split(".")[0]
            (TMP / "seat" / f"{sid}.claimed").write_text("")
            time.sleep(0.2)
            rp_ac4.write_text("dug")
            (TMP / "seat" / f"{sid}.result.json").write_text(json.dumps(
                {"is_error": False, "structured_output": {**research, "path": "content/L-research-r6ac4.md"},
                 "num_turns": 1, "usage": {}, "total_cost_usd": None, "modelUsage": {"m": {}},
                 "session_id": "r6ac4", "permission_denials": []}))
            return
        time.sleep(0.05)


threading.Thread(target=claim_then_finish, daemon=True).start()
a = argparse.Namespace(role="research", subject="L-spec-0001", packet=str(PK), path=str(rp_ac4), cwd=str(REPO),
                       charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=True)
try:
    dispatch.main(a)
    code = 0
except SystemExit as e:
    code = e.code
sev = [json.loads(l) for l in max((TMP / "events").glob("L-research-*.jsonl")).read_text().splitlines()]
assert code == 0 and [e["type"] for e in sev] == ["spawn-started", "research-filed", "spawn-done"], sev
assert not any(e["type"] == "spawn-failed" for e in sev), "a claimed seat must never fail as unserved"
PK.write_text(PK_DEFAULT)
del os.environ["DOIT_SEAT_CLAIM_SEC"]
N += 1

# ── AC5 · codex-fallback-to-seat: unserved on the FALLBACK's own seat dispatch ─
# DOIT_SEAT is still "1" from AC3/AC4 (a.seat=False there too) — but here the map
# itself names backend="codex" for research, and a DOIT_SEAT that disagrees with
# the map is refused on principle (S32), so it must be unset: the fallback's own
# seat dispatch gets its backend from the map's `fallback=` clause, not the flag.
del os.environ["DOIT_SEAT"]
os.environ["DOIT_SEAT_CLAIM_SEC"] = "1"
MT.write_text('[contracts.research]\nbackend = "codex"\nmodel = "gpt-6-astra"\n'
              'fallback = { backend = "seat", model = "claude-sonnet-5" }\n')
dispatch.run_codex_exec = codex_dead
PK.write_text("a packet for the r6ac5 fallback run\n")
rp_ac5 = TMP / "content" / "L-research-r6ac5.md"
a = argparse.Namespace(role="research", subject="L-spec-0001", packet=str(PK), path=str(rp_ac5), cwd=str(REPO),
                       charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=False)
# L-spec-0269: `Unserved` on the fallback's own seat dispatch now waits out
# `window` (a role's own minutes-cap, at minimum) rather than
# DOIT_SEAT_CLAIM_SEC — shrunk here to 0 so this pre-existing fixture (whose
# own point is the fallback's `reason` shape, not window timing) stays fast.
_real_window_min_ac5 = dispatch.window_min
dispatch.window_min = lambda *a_, **k_: 0
try:
    dispatch.main(a)
    code = 0
except SystemExit as e:
    code = e.code
finally:
    dispatch.window_min = _real_window_min_ac5
PK.write_text(PK_DEFAULT)
sev = [json.loads(l) for l in max((TMP / "events").glob("L-research-*.jsonl")).read_text().splitlines()]
assert code == 1 and sev[-1]["type"] == "spawn-failed" and sev[-1]["reason"] == "unserved", sev[-1]
assert not any(e["type"] == "spawn-done" for e in sev), "the second run_seat call site (the fallback) must be covered too"
assert any(e["type"] == "backend-fallback" for e in sev), sev
_rm_seat(sev[0]["spawn"])
