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
del os.environ["DOIT_SEAT_CLAIM_SEC"]
MT.unlink()
N += 1

# ── AC6 · scripts/seat/claim.sh: O_EXCL semantics, all three sub-cases ─────────
claim_root = TMP / "claimtest"
(claim_root / "seat").mkdir(parents=True)
spawn_c = "L-research-claimtest1"
(claim_root / "seat" / f"{spawn_c}.packet.md").write_text("packet\n")
env_c = {**os.environ, "DOIT_ROOT": str(claim_root)}
r1 = subprocess.run(["bash", "scripts/seat/claim.sh", spawn_c], cwd=str(REPO_ROOT), env=env_c,
                    capture_output=True, text=True)
assert r1.returncode == 0, r1.stderr
claimed_p = claim_root / "seat" / f"{spawn_c}.claimed"
assert claimed_p.is_file(), "the first claim must create the file"
before_stat = claimed_p.stat()
r2 = subprocess.run(["bash", "scripts/seat/claim.sh", spawn_c], cwd=str(REPO_ROOT), env=env_c,
                    capture_output=True, text=True)
assert r2.returncode != 0, "a second claim on the same spawn must be refused"
after_stat = claimed_p.stat()
assert before_stat.st_mtime_ns == after_stat.st_mtime_ns and claimed_p.read_text() == "", \
    "the second call must not touch the first invocation's file"
spawn_nopkt = "L-research-claimtest-nopacket"
r3 = subprocess.run(["bash", "scripts/seat/claim.sh", spawn_nopkt], cwd=str(REPO_ROOT), env=env_c,
                    capture_output=True, text=True)
assert r3.returncode != 0
assert not (claim_root / "seat" / f"{spawn_nopkt}.claimed").is_file()
N += 1

# ── AC7 · all three serving-pattern texts name claim.sh as a first/before step ─
def _claim_first(text):
    lines = text.splitlines()
    low = [l.lower() for l in lines]
    for i, l in enumerate(low):
        if "claim.sh" in l:
            window = low[max(0, i - 1):i + 2]
            if any(("first" in w or "before" in w) for w in window):
                return True
    return False


for relpath in ("scripts/seat/README.md", "agents/planner.md", "agents/thinker.md"):
    fp = REPO_ROOT / relpath
    assert _claim_first(fp.read_text()), f"{relpath} must name claim.sh as its first/before serving step"
N += 1

# ── AC8 · the builder case, driven for real (not fabricated) ───────────────────
os.environ["DOIT_SEAT"] = "1"          # no models.toml now (AC5 unlinked it) — the flag forces the seat backend
os.environ["DOIT_SEAT_CLAIM_SEC"] = "1"
# L-spec-0269: `Unserved` now waits out `window` (the builder's own 90-min cap,
# at minimum), not DOIT_SEAT_CLAIM_SEC — shrunk to 0 so this pre-existing
# fixture (whose own point is the terminal shape, not window timing) still
# raises `Unserved` before its own claim-window log would ever fire, keeping
# the event list exactly `["build-started", "spawn-failed"]`, no `seat-stale`.
_real_window_min_ac8 = dispatch.window_min
dispatch.window_min = lambda *a_, **k_: 0
a = argparse.Namespace(role="builder", subject="L-spec-0001", packet=str(PK), path=None, cwd=str(REPO),
                       charter=None, project="t", mcp_config=None, timeout=60, max_usd=None, seat=False)
try:
    dispatch.main(a)
    code = 0
except SystemExit as e:
    code = e.code
finally:
    dispatch.window_min = _real_window_min_ac8
bev = [json.loads(l) for l in max((TMP / "events").glob("L-builder-*.jsonl")).read_text().splitlines()]
assert code == 1 and [e["type"] for e in bev] == ["build-started", "spawn-failed"], bev
assert bev[0]["backend"] == "seat", bev[0]
assert bev[-1]["reason"] == "unserved", bev[-1]
_rm_seat(bev[0]["spawn"])
del os.environ["DOIT_SEAT_CLAIM_SEC"]
del os.environ["DOIT_SEAT"]

import relay  # noqa: E402
assert relay.unserved(bev, TMP) == [], \
    "the real pair (build-started + spawn-failed{reason:unserved}) is already terminal, not pending — " \
    "and its own event timestamps postdate fold.NOW (a snapshot taken once, earlier in this run), so it " \
    "has not yet 'aged into' the failed-unserved window either — see AC10 for the synthetic in-window case"
N += 1

# ── L-spec-0269 (rewrite of the old AC12i block, §5) · a clock frozen forever
# past DOIT_SEAT_CLAIM_SEC no longer terminates the wait on its own (window
# defaults to the unreached timeout=6000) — a BOUNDED sequence advancing past
# an explicit small `window=330` is what must raise `Unserved`, in a bounded
# call count, never an unbounded loop against a frozen clock ─────────────────
os.environ["DOIT_SEAT_CLAIM_SEC"] = "1"
_ac12i_spawn = "L-research-r6ac12i"
_ac12i_seq = [0.0, 301.0, 301.0, 340.0]
_ac12i_idx = {"i": 0}


def _fake_time_301():
    i = min(_ac12i_idx["i"], len(_ac12i_seq) - 1)
    _ac12i_idx["i"] += 1
    return _ac12i_seq[i]


real_wall_t0 = time.perf_counter()
with _mock.patch("time.time", _fake_time_301), _mock.patch("time.sleep", lambda s: None):
    try:
        dispatch.run_seat(_ac12i_spawn, ["claude", "-p"], "packet", str(REPO), 6000, window=330)
        raise AssertionError("expected Unserved once the sequence passes the 330s window")
    except dispatch.Unserved:
        pass
real_wall_301 = time.perf_counter() - real_wall_t0
assert real_wall_301 < 5, f"the fake clock must have decided it, not real time: {real_wall_301}s"
assert _ac12i_idx["i"] <= len(_ac12i_seq) + 1, f"AC12i: bounded call count expected, got {_ac12i_idx['i']}"
_rm_seat(_ac12i_spawn)
del os.environ["DOIT_SEAT_CLAIM_SEC"]
N += 1

_ac12ii_spawn = "L-research-r6ac12ii"
_want2 = TMP / "seat" / f"{_ac12ii_spawn}.result.json"


def _write_result_fast():
    for _ in range(2000):
        if (TMP / "seat" / f"{_ac12ii_spawn}.packet.md").is_file():
            break
        _real_sleep(0.001)          # the genuine sleep — time.sleep is mocked to a no-op during this scenario
    _want2.write_text(json.dumps(
        {"is_error": False, "structured_output": {**research, "path": "content/L-research-r6ac12ii.md"},
         "num_turns": 1, "usage": {}, "total_cost_usd": None, "modelUsage": {"m": {}},
         "session_id": "fc2", "permission_denials": []}))


def _fake_time_299():
    _fake_time_299.n += 1
    return 0.0 if _fake_time_299.n == 1 else 299.0


_fake_time_299.n = 0
_th_fast = threading.Thread(target=_write_result_fast, daemon=True)
_th_fast.start()
real_wall_t0 = time.perf_counter()
with _mock.patch("time.time", _fake_time_299), _mock.patch("time.sleep", lambda s: None):
    r = dispatch.run_seat(_ac12ii_spawn, ["claude", "-p"], "packet", str(REPO), 6000)
real_wall_299 = time.perf_counter() - real_wall_t0
_th_fast.join(5)
assert real_wall_299 < 1.0, f"the boundary held at 299s the whole time: returned in {real_wall_299}s real time"
assert isinstance(r, dispatch.SeatResult), r
_rm_seat(_ac12ii_spawn)
N += 1

# the source-text supplement: DOIT_SEAT_CLAIM_SEC's default (300) appears in BOTH files
for relpath in ("src/dispatch.py", "src/relay.py"):
    text = (REPO_ROOT / relpath).read_text()
    assert 'os.environ.get("DOIT_SEAT_CLAIM_SEC", 300)' in text, \
        f"{relpath} is missing the DOIT_SEAT_CLAIM_SEC default reader, or its default diverged"
N += 1

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0269 · seat-stays-offered (L-charter-0033) — AC4, AC5, AC7
# ══════════════════════════════════════════════════════════════════════════════
import socket  # noqa: E402 — waiter_host comparisons below

# ── AC5 · dispatch.window_min: all five cases, pasted with their computed
# number (or, for (e), the no-exception result) ───────────────────────────────
(TMP / "content").mkdir(parents=True, exist_ok=True)
assert dispatch.window_min("builder", "L-spec-0269a5-nonseat", [], backend="claude-p") == 90, \
    "a non-seat backend always gets the flat, unbumped role cap"
assert dispatch.window_min("grader", "L-spec-0269a5a", [], backend="seat") == 30, \
    "(a) plain grader, no observed-data event -> the role's own cap"
ev_a5b = [{"type": "spec-written", "spec": "L-spec-0269a5b", "ac_types": ["observed-data"], "ts": dispatch.now()}]
assert dispatch.window_min("grader", "L-spec-0269a5b", ev_a5b, backend="seat") == 45, \
    "(b) observed-data grader -> 45"
(TMP / "content" / "L-spec-0269a5c.md").write_text("# fixture\nwindow_min: 500\n\n## 1. Goal\n")
ev_a5c = [{"type": "spec-written", "spec": "L-spec-0269a5c", "ac_types": ["observed-data"], "ts": dispatch.now()}]
assert dispatch.window_min("grader", "L-spec-0269a5c", ev_a5c, backend="seat") == 240, \
    "(c) window_min: 500, base 45 -> raised to 500, then capped at 240"
(TMP / "content" / "L-spec-0269a5d.md").write_text("# fixture\nwindow_min: 10\n\n## 1. Goal\n")
assert dispatch.window_min("builder", "L-spec-0269a5d", [], backend="seat") == 90, \
    "(d) window_min: 10 must never LOWER a 90-min builder window"
assert dispatch.window_min("reviewer", "L-spec-0269a5e-nonexistent", [], backend="seat") == 30, \
    "(e) no spec-written event and no readable content file -> the plain role cap, no exception"
N += 1

# ── AC4 · a spawn-started (grader, non-builder) carries window_min/waiter_pid/
# waiter_host/waiter_proc_start under BOTH backends, differing correctly on an
# observed-data subject ────────────────────────────────────────────────────────
ac4_subj = "L-spec-0269ac4"
# L-spec-0437: role=grader + backend=seat now needs a build-done with a ready_sha
# on the ledger before it builds a view (AC2) — a synthetic one, same convention
# as the spec-written line right below it, so this pre-existing window_min/waiter
# fixture keeps proving what it always proved, unaffected by the new gate.
(TMP / "events" / "L-fixture-0269ac4.jsonl").write_text(
    json.dumps({"v": 1, "ts": dispatch.now(), "type": "spec-written", "subject": ac4_subj, "spec": ac4_subj,
                "path": str(TMP / "content" / f"{ac4_subj}.md"), "ac_types": ["observed-data"], "ac_count": 1,
                "requirement_ids": ["R1"], "owed_ac_count": 0, "unknown_count": 0, "footprint": ["x.py"]}) + "\n" +
    json.dumps({"v": 1, "ts": dispatch.now(), "type": "build-done", "subject": ac4_subj,
                "base_sha": "ac4base", "ready_sha": "ac4ready"}) + "\n")

os.environ.pop("DOIT_SEAT", None)
PK.write_text("a packet for AC4 claude-p\n")
code, types, evs, _ = spawn("grader", out=grade([met]), subject=ac4_subj)
PK.write_text(PK_DEFAULT)
ss_cp = spawn.raw[0]
assert ss_cp["type"] == "spawn-started" and ss_cp["backend"] == "claude-p", ss_cp
assert ss_cp["window_min"] == 30, "AC4/AC6: claude-p never gets the 45-min seat bump (grader cap is 30 since 2026-10-04)"
assert ss_cp["waiter_pid"] == os.getpid() and ss_cp["waiter_host"] == socket.gethostname(), ss_cp
assert "waiter_proc_start" in ss_cp, ss_cp

os.environ["DOIT_SEAT"] = "1"
os.environ["DOIT_SEAT_CLAIM_SEC"] = "1"


def _ac4_serve():
    for _ in range(400):
        pk = [q for q in (list((TMP / "seat").glob("*.packet.md")) if (TMP / "seat").is_dir() else [])
              if not (TMP / "seat" / (q.name.split(".")[0] + ".result.json")).exists()
              and not (TMP / "seat" / (q.name.split(".")[0] + ".output.json")).exists()]
        if pk:
            sid = max(pk, key=lambda q: q.stat().st_mtime).name.split(".")[0]
            (TMP / "seat" / f"{sid}.result.json").write_text(json.dumps(
                {"is_error": False, "structured_output": grade([met]), "num_turns": 1, "usage": {},
                 "total_cost_usd": None, "modelUsage": {"m": {}}, "session_id": sid, "permission_denials": []}))
            return
        time.sleep(0.05)


threading.Thread(target=_ac4_serve, daemon=True).start()
PK.write_text("a packet for AC4 seat\n")
a4 = argparse.Namespace(role="grader", subject=ac4_subj, packet=str(PK), path=None, cwd=str(REPO),
                        charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=True)
# L-spec-0437: this fixture's own point is window_min/waiter fields, not the view
# build — stubbed to a bare temp dir so it never touches real content files.
_real_gv_build_ac4, grader_view.build = grader_view.build, lambda *a_, **k_: TMP / "grader-view-ac4"
try:
    dispatch.main(a4)
except SystemExit:
    pass
finally:
    grader_view.build = _real_gv_build_ac4
PK.write_text(PK_DEFAULT)
raw4 = [json.loads(l) for l in max((TMP / "events").glob("L-grader-*.jsonl")).read_text().splitlines()]
ss_seat = raw4[0]
assert ss_seat["type"] == "grader-view-built", ss_seat
ss_seat = raw4[1]
assert ss_seat["type"] == "spawn-started" and ss_seat["backend"] == "seat", ss_seat
assert ss_seat["window_min"] == 45, "AC4/AC5(b): the seat backend gets the observed-data bump"
assert ss_seat["waiter_pid"] == os.getpid() and ss_seat["waiter_host"] == socket.gethostname(), ss_seat
assert "waiter_proc_start" in ss_seat, ss_seat
_rm_seat(raw4[0]["spawn"])
del os.environ["DOIT_SEAT_CLAIM_SEC"]
del os.environ["DOIT_SEAT"]
N += 1

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0437 · grader-dispatch-view (L-charter-0042 R8) — AC1-AC12, AC14
# No models.toml exists on this root from here on (L-spec-0269 AC5 unlinked it
# above and nothing rewrites it before this section) — `a.seat=True` alone
# resolves role=grader onto the seat backend, exactly the combination the gate
# is scoped to.
# ══════════════════════════════════════════════════════════════════════════════


def _gv_build_done(subject, base_sha=None, ready_sha=None):
    kv = {}
    if base_sha is not None:
        kv["base_sha"] = base_sha
    if ready_sha is not None:
        kv["ready_sha"] = ready_sha
    (TMP / "events" / f"L-fixture-{subject}.jsonl").write_text(json.dumps(
        {"v": 1, "ts": dispatch.now(), "type": "build-done", "subject": subject, **kv}) + "\n")


def _gv_serve_with(out):
    def _serve():
        for _ in range(400):
            pk = [q for q in (list((TMP / "seat").glob("*.packet.md")) if (TMP / "seat").is_dir() else [])
                  if not (TMP / "seat" / (q.name.split(".")[0] + ".result.json")).exists()
                  and not (TMP / "seat" / (q.name.split(".")[0] + ".output.json")).exists()]
            if pk:
                sid = max(pk, key=lambda q: q.stat().st_mtime).name.split(".")[0]
                (TMP / "seat" / f"{sid}.result.json").write_text(json.dumps(
                    {"is_error": False, "structured_output": out, "num_turns": 1, "usage": {},
                     "total_cost_usd": None, "modelUsage": {"m": {}}, "session_id": sid, "permission_denials": []}))
                return
            time.sleep(0.05)
    return _serve


def _gv_drive(subject, project="t", view_build=None, serve_out=None, timeout=1):
    """Drives dispatch.main() with role=grader forced onto the seat backend.
    `view_build`, when given, replaces `grader_view.build` for this one call only.
    `serve_out`, when given, starts a background thread answering the seat packet
    — omitted for a fixture that must fail before any spend (nothing to serve)."""
    _real_build = grader_view.build
    if view_build is not None:
        grader_view.build = view_build
    if serve_out is not None:
        threading.Thread(target=_gv_serve_with(serve_out), daemon=True).start()
    a = argparse.Namespace(role="grader", subject=subject, packet=str(PK), path=None, cwd=str(REPO),
                           charter=None, project=project, mcp_config=None, timeout=timeout, max_usd=None,
                           seat=True)
    try:
        dispatch.main(a)
        code = 0
    except SystemExit as e:
        code = e.code
    finally:
        grader_view.build = _real_build
    files = sorted((TMP / "events").glob("L-grader-*.jsonl"), key=lambda p: p.stat().st_mtime)
    raw = [json.loads(l) for l in files[-1].read_text().splitlines()]
    return code, raw


def _boom_build(*_a, **_k):
    raise AssertionError("grader_view.build must not be called here")


# ── AC1 · a.project falsy refuses before any spend, before a spawn id spends
# anything under $R/seat ────────────────────────────────────────────────────
code, raw = _gv_drive("L-spec-0437ac1", project="")
assert code == 1 and [e["type"] for e in raw] == ["spawn-failed"], raw
assert "needs --project" in raw[0]["why"], raw[0]
sid1 = raw[0]["spawn"]
assert not (TMP / "seat" / f"{sid1}.packet.md").exists() and not (TMP / "seat" / f"{sid1}.cmd.json").exists()
N += 1

# ── AC2 · no build-done anywhere on the ledger for the subject ──────────────
code, raw = _gv_drive("L-spec-0437ac2")
assert code == 1 and [e["type"] for e in raw] == ["spawn-failed"], raw
assert "no build-done" in raw[0]["why"], raw[0]
sid2 = raw[0]["spawn"]
assert not (TMP / "seat" / f"{sid2}.packet.md").exists()
N += 1

# ── AC3 · the newest build-done carries no ready_sha ────────────────────────
_gv_build_done("L-spec-0437ac3", base_sha="B0")
code, raw = _gv_drive("L-spec-0437ac3")
assert code == 1 and [e["type"] for e in raw] == ["spawn-failed"], raw
assert "ready_sha" in raw[0]["why"], raw[0]
N += 1

# ── AC4/AC5/AC6/AC7 · grader_view.build called once with the right args, the
# packet copy lands byte-identical under view/seat/, cmd.json's cwd becomes
# view/tree, and the event order is grader-view-built, spawn-started, ...,
# spawn-done ─────────────────────────────────────────────────────────────────
_gv_build_done("L-spec-0437ac4", base_sha="B1", ready_sha="R1")
_ac4_calls = []
_ac4_view = pathlib.Path(tempfile.mkdtemp())


def _ac4_build(*args):
    _ac4_calls.append(args)
    return _ac4_view


code, raw = _gv_drive("L-spec-0437ac4", view_build=_ac4_build, serve_out=grade([met]))
assert code == 0, raw
assert [e["type"] for e in raw][:2] == ["grader-view-built", "spawn-started"] and raw[-1]["type"] == "spawn-done", raw
sid4 = raw[0]["spawn"]
assert len(_ac4_calls) == 1, _ac4_calls
assert _ac4_calls[0] == ("L-spec-0437ac4", dispatch.ROOT / "repos" / "t", "B1", "R1", sid4), _ac4_calls[0]
assert raw[0]["view"] == str(_ac4_view) and raw[0]["ready_sha"] == "R1", raw[0]
assert (_ac4_view / "seat" / f"{sid4}.packet.md").read_text() == (TMP / "seat" / f"{sid4}.packet.md").read_text(), \
    "AC5: the view copy must be byte-identical to the real seat packet"
cj4 = json.loads((TMP / "seat" / f"{sid4}.cmd.json").read_text())
assert cj4["cwd"] == str(_ac4_view / "tree") and cj4["cwd"] != str(REPO), cj4
_rm_seat(sid4)
N += 1

# ── AC8 · (a) a non-grader role on seat: grader_view.build is never invoked
# (patched to raise), no grader-view-built appears, cmd.json's cwd is the
# caller's --cwd unchanged. (b) grader resolving to claude-p, its verify text
# naming a real capability (`npm` -> node_modules): L-spec-0481 AC12(iii)
# governs over AC21 for this one fixture (Thinker ruling) — refused
# preflight:no-view before any spend, grader_view.build still never invoked.
# (c) grader resolving to codex, empty spec/verify text (nothing required):
# unaffected, grader_view.build still never invoked ───────────────────────────
_real_build_ac8 = grader_view.build
grader_view.build = _boom_build
try:
    threading.Thread(target=_gv_serve_with(card), daemon=True).start()
    a8a = argparse.Namespace(role="builder", subject="L-spec-0437ac8a", packet=str(PK), path=None, cwd=str(REPO),
                             charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=True)
    try:
        dispatch.main(a8a)
        code8a = 0
    except SystemExit as e:
        code8a = e.code
    raw8a = [json.loads(l) for l in
             max((TMP / "events").glob("L-builder-*.jsonl"), key=lambda p: p.stat().st_mtime).read_text().splitlines()]
    assert code8a == 0 and not any(e["type"] == "grader-view-built" for e in raw8a), raw8a
    sid8a = raw8a[0]["spawn"]
    cj8a = json.loads((TMP / "seat" / f"{sid8a}.cmd.json").read_text())
    assert cj8a["cwd"] == str(REPO), cj8a
    _rm_seat(sid8a)

    os.environ.pop("DOIT_SEAT", None)
    _gv_build_done("L-spec-0437ac8b", base_sha="B1", ready_sha="R1")
    dispatch.CONTENT.mkdir(parents=True, exist_ok=True)
    (dispatch.CONTENT / "verify-L-spec-0437ac8b-grader.sh").write_text("npm test\n")
    code, types8b, evs8b, _ = spawn("grader", out=grade([met]), subject="L-spec-0437ac8b")
    assert code == 1 and types8b == ["spawn-failed"] and not any(t == "grader-view-built" for t in types8b), types8b
    assert spawn.raw[0]["backend"] == "claude-p" and spawn.raw[0]["reason"] == "preflight:no-view", spawn.raw[0]

    MT.write_text('[contracts.grader]\nbackend = "codex"\nmodel = "gpt-6-astra"\n')
    _real_codex_exec_ac8 = dispatch.run_codex_exec

    def _codex_fake_grader(cmd, packet, cwd, timeout):
        pathlib.Path(cmd[cmd.index("-o") + 1]).write_text(json.dumps(grade([met])))
        return argparse.Namespace(returncode=0, stderr="", stdout=json.dumps(
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
    dispatch.run_codex_exec = _codex_fake_grader
    code, types8c, evs8c, _ = spawn("grader", out=grade([met]), subject="L-spec-0437ac8c")
    dispatch.run_codex_exec = _real_codex_exec_ac8
    MT.unlink()
    assert code == 0 and not any(t == "grader-view-built" for t in types8c), types8c
    assert spawn.raw[0]["backend"] == "codex", spawn.raw[0]
finally:
    grader_view.build = _real_build_ac8
N += 1

# ── AC9 · run_seat(view=) direct-call: the SAME polling file set under $R/seat
# in both runs, differing only in the view/seat/ copy and cmd.json's cwd ─────
os.environ["DOIT_SEAT_CLAIM_SEC"] = "1"


def _ac9_serve(spawn_id):
    def go():
        time.sleep(0.05)
        (TMP / "seat" / f"{spawn_id}.result.json").write_text(json.dumps(
            {"is_error": False, "structured_output": grade([met]), "num_turns": 1, "usage": {},
             "total_cost_usd": None, "modelUsage": {"m": {}}, "session_id": "s", "permission_denials": []}))
    threading.Thread(target=go, daemon=True).start()


ac9a, ac9b = "L-run-seat-ac9a", "L-run-seat-ac9b"
_ac9_serve(ac9a)
dispatch.run_seat(ac9a, ["claude", "-p"], "packet", str(REPO), 6)
suffixes_a = sorted(p.name.split(".", 1)[1] for p in (TMP / "seat").glob(f"{ac9a}.*"))
cjA = json.loads((TMP / "seat" / f"{ac9a}.cmd.json").read_text())
_rm_seat(ac9a)

ac9_view = pathlib.Path(tempfile.mkdtemp())
_ac9_serve(ac9b)
dispatch.run_seat(ac9b, ["claude", "-p"], "packet", str(REPO), 6, view=ac9_view)
suffixes_b = sorted(p.name.split(".", 1)[1] for p in (TMP / "seat").glob(f"{ac9b}.*"))
cjB = json.loads((TMP / "seat" / f"{ac9b}.cmd.json").read_text())
_rm_seat(ac9b)

assert suffixes_a == suffixes_b, (suffixes_a, suffixes_b)
assert cjA["cwd"] == str(REPO) and cjB["cwd"] == str(ac9_view / "tree"), (cjA, cjB)
assert (ac9_view / "seat" / f"{ac9b}.packet.md").is_file(), "the view copy exists only when view= is given"
del os.environ["DOIT_SEAT_CLAIM_SEC"]
N += 1

# ── AC11 · an exception inside grader_view.build is caught, never an unhandled
# traceback ───────────────────────────────────────────────────────────────────
_gv_build_done("L-spec-0437ac11", base_sha="B1", ready_sha="R1")


def _boom_raise(*_a, **_k):
    raise RuntimeError("boom")


code, raw = _gv_drive("L-spec-0437ac11", view_build=_boom_raise)
assert code == 1 and [e["type"] for e in raw] == ["spawn-failed"], raw
assert "boom" in raw[0]["why"], raw[0]
sid11 = raw[0]["spawn"]
assert not (TMP / "seat" / f"{sid11}.packet.md").exists() and not (TMP / "seat" / f"{sid11}.cmd.json").exists()
assert not any(e["type"] == "grader-view-built" for e in raw)
N += 1

# ── AC12 · a codex-initiated grader dispatch falling back to seat runs with
# view=None throughout — the gate only ever fires on the INITIAL backend ─────
MT.write_text('[contracts.grader]\nbackend = "codex"\nmodel = "gpt-6-astra"\n'
              'fallback = { backend = "seat", model = "claude-sonnet-5" }\n')
dispatch.run_codex_exec = codex_dead
grader_view.build = _boom_build
threading.Thread(target=_gv_serve_with(grade([met])), daemon=True).start()
a12 = argparse.Namespace(role="grader", subject="L-spec-0437ac12", packet=str(PK), path=None, cwd=str(REPO),
                         charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=False)
try:
    dispatch.main(a12)
    code12 = 0
except SystemExit as e:
    code12 = e.code
MT.unlink()
raw12 = [json.loads(l) for l in
         max((TMP / "events").glob("L-grader-*.jsonl"), key=lambda p: p.stat().st_mtime).read_text().splitlines()]
assert not any(e["type"] == "grader-view-built" for e in raw12), raw12
sid12 = next(e["spawn"] for e in raw12 if e["type"] == "spawn-started")
cj12 = json.loads((TMP / "seat" / f"{sid12}.cmd.json").read_text())
assert cj12["cwd"] == str(REPO), cj12
_rm_seat(sid12)
N += 1

# ── AC14 · relay.pending_packets (unmodified) already surfaces a stalled
# role=grader seat spawn — a spawn-started with no terminal event and a real
# packet on disk ─────────────────────────────────────────────────────────────
ac14_events = [{"type": "spawn-started", "role": "grader", "subject": "L-spec-0437ac14",
               "spawn": "L-grader-0437ac14", "ts": dispatch.now()}]
(TMP / "seat").mkdir(parents=True, exist_ok=True)
(TMP / "seat" / "L-grader-0437ac14.packet.md").write_text("a packet\n")
# served_by=None: since L-spec-8033 graders are served by "grader-pane", not the
# relay; this assertion is about the stalled spawn surfacing at all.
pend = relay.pending_packets(ac14_events, root=TMP, served_by=None)
assert any(p["spawn"] == "L-grader-0437ac14" for p in pend), pend
(TMP / "seat" / "L-grader-0437ac14.packet.md").unlink()
N += 1

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0481 · grading-capabilities (L-charter-0042 R12) — AC12-AC14, AC16, AC19
# Reuses `_gv_drive`/`_gv_build_done`/`_boom_build` above: role=grader forced
# onto the seat backend, no real Postgres/bwrap needed — `node_modules` (no
# `grading.toml` row for project "t") and `git` (never provisioned, R8) both
# fail preflight cheaply and deterministically.
# ══════════════════════════════════════════════════════════════════════════════
import grading_env  # noqa: E402


def _cap_content(subject, verify_text):
    (dispatch.CONTENT).mkdir(parents=True, exist_ok=True)
    (dispatch.CONTENT / f"{subject}.md").write_text(f"# {subject}\n")
    (dispatch.CONTENT / f"verify-{subject}-grader.sh").write_text(verify_text)


def _real_view_build(*_a, **_k):
    return pathlib.Path(tempfile.mkdtemp(prefix="l0481-view-"))


# ── AC12(ii)/AC16 · no hold yet, `node_modules` unprovable -> one
# grading-preflight-failed, one capability-hold (shared, capability:node_modules),
# one escalation-blocking(kind=capability-hold), reason preflight:node_modules,
# NO spawn-started; grading_budget is unmoved by any of it ─────────────────────
ac12ii_subj = "L-spec-0481ac12ii"
_cap_content(ac12ii_subj, "npm test\n")
_gv_build_done(ac12ii_subj, base_sha="B1", ready_sha="R1")
gb_before = dispatch.grading_budget(fold.read_events(), ac12ii_subj)
code, raw = _gv_drive(ac12ii_subj, view_build=_real_view_build)
assert code == 1, raw
types12ii = [e["type"] for e in raw]
assert types12ii == ["grader-view-built", "grading-preflight-failed", "capability-hold",
                     "escalation-blocking", "spawn-failed"], types12ii
assert not any(t == "spawn-started" for t in types12ii), "AC16: a refusal never appends spawn-started"
pf12 = next(e for e in raw if e["type"] == "grading-preflight-failed")
assert pf12["role"] == "grader" and pf12["capability"] == "node_modules", pf12
ch12 = next(e for e in raw if e["type"] == "capability-hold")
assert ch12["subject"] == "capability:node_modules" and ch12["capability"] == "node_modules" \
    and ch12["spec"] == ac12ii_subj, ch12
esc12 = next(e for e in raw if e["type"] == "escalation-blocking")
assert esc12["subject"] == "capability:node_modules" and esc12["kind"] == "capability-hold" \
    and esc12["owner"] == "thinker", esc12
assert esc12["default"] and esc12["deadline"] and esc12["revert"] == "doit append unblocked capability:node_modules", esc12
sf12 = raw[-1]
assert sf12["reason"] == "preflight:node_modules", sf12
gb_after = dispatch.grading_budget(fold.read_events(), ac12ii_subj)
assert gb_before == gb_after, "AC16: a preflight refusal never moves grading_budget"
N += 1

# ── AC13 · the hold really refuses the NEXT dispatch (real write-then-read
# through fold.read_events(), not the writer's in-memory list); no view built ──
ac13_subj = "L-spec-0481ac13"
_cap_content(ac13_subj, "npm test\n")
_gv_build_done(ac13_subj, base_sha="B1", ready_sha="R1")
code, raw13 = _gv_drive(ac13_subj, view_build=_boom_build)
assert code == 1 and [e["type"] for e in raw13] == ["spawn-failed"], raw13
assert raw13[0]["reason"] == "held:node_modules", raw13[0]
N += 1

# ── AC14(i) · a spec-local hold (on subject A) refuses only A; a clean
# subject B, unrelated to it, dispatches normally through to spawn-started ─────
# L-spec-8034/AC6: a bare `git` need (unattributed to any criterion) now
# ROUTES to owed instead of holding the dispatch — git is a GRADER_NEVER
# capability, excluded from `required()` before the held-capability/preflight
# checks ever run. `view-paths` (the one remaining SPEC_LOCAL capability
# routing never touches) preserves this fixture's original intent: a
# spec-local hold that isolates one subject without touching a sibling's.
ac14a_subj, ac14b_subj = "L-spec-0481ac14a", "L-spec-0481ac14b"
_cap_content(ac14a_subj, "/some/foreign/absolute/path\n")
_cap_content(ac14b_subj, "echo hi\n")
_gv_build_done(ac14a_subj, base_sha="B1", ready_sha="R1")
_gv_build_done(ac14b_subj, base_sha="B1", ready_sha="R1")


def _view_build_foreign_path(*_a, **_k):
    """`_real_view_build`'s stub carries no `verify.sh` of its own, so
    `_prove_view_paths` (which reads THIS view's `verify.sh`, not the
    content-dir's verify script) trivially passed with an empty file — this
    populates it with the same foreign path `required()` already saw, so the
    proof genuinely fails the way a real grader_view.build() would."""
    v = pathlib.Path(tempfile.mkdtemp(prefix="l0481-view-"))
    (v / "verify.sh").write_text("#!/bin/bash\n/some/foreign/absolute/path\n")
    return v


code, raw14a = _gv_drive(ac14a_subj, view_build=_view_build_foreign_path)
assert code == 1, raw14a
ch14a = next(e for e in raw14a if e["type"] == "capability-hold")
assert ch14a["subject"] == f"capability:view-paths:{ac14a_subj}", ch14a
code, raw14b = _gv_drive(ac14b_subj, view_build=_real_view_build, serve_out=grade([met]))
assert code == 0 and raw14b[-1]["type"] == "spawn-done", raw14b
assert not any(e["type"] in ("grading-preflight-failed", "capability-hold") for e in raw14b), \
    "AC14(i): subject B is not touched by A's spec-local hold"
_rm_seat(next(e["spawn"] for e in raw14b if e["type"] == "spawn-started"))
code, raw14a2 = _gv_drive(ac14a_subj, view_build=_boom_build)
assert code == 1 and raw14a2[0]["reason"] == "held:view-paths", raw14a2
N += 1

# ── AC14(ii) · reviewer preflight is read-only: porcelain(cwd) of a fixture
# git worktree is byte-identical before and after preflight(), and it writes
# no .env/grading.env/pg under it ───────────────────────────────────────────
rv_wt = harness.test_root("l0481-reviewer-wt")
subprocess.run(["git", "init", "-q"], cwd=rv_wt, check=True)
before_rv = dispatch.porcelain(rv_wt)
rv_failures = grading_env.preflight("reviewer", "L-spec-0481-nonexistent", rv_wt, "t", repo=dispatch.HERE.parent)
after_rv = dispatch.porcelain(rv_wt)
assert rv_failures == [], rv_failures    # gates-only, no [[prod]] row for "t" -> nothing required
assert before_rv == after_rv, (before_rv, after_rv)
assert not (rv_wt / ".env").exists() and not (rv_wt / "grading.env").exists() and not (rv_wt / "pg").exists()
harness.cleanup(rv_wt)
N += 1

# ── AC19 (dispatch-level) · a grader dispatch on the seat backend, `db` never
# required by its own verify text, gets `sandbox=`+dsn_role="none" on
# spawn-started, and grader-view-built precedes it (AC12(v) shape) ─────────────
ac19_subj = "L-spec-0481ac19"
_cap_content(ac19_subj, "echo hi\n")
_gv_build_done(ac19_subj, base_sha="B1", ready_sha="R1")
code, raw19 = _gv_drive(ac19_subj, view_build=_real_view_build, serve_out=grade([met]))
assert code == 0, raw19
assert [e["type"] for e in raw19][:2] == ["grader-view-built", "spawn-started"], raw19
ss19 = raw19[1]
assert ss19["dsn_role"] == "none" and ss19["sandbox"] in ("bwrap", "host"), ss19
_rm_seat(ss19["spawn"])
N += 1

# ── AC7 (dispatch half) · a REAL wall-clock timeout's `spawn-failed` now
# carries `reason: "timeout"` (mirroring the existing `reason: "unserved"`
# assertion above) — both of main()'s `except subprocess.TimeoutExpired` sites
# now stamp it; this fixture drives the first ─────────────────────────────────
def _timeout_boom(cmd, packet, cwd, timeout):
    raise subprocess.TimeoutExpired(cmd, timeout)


_real_run_claude_to = dispatch.run_claude
dispatch.run_claude = _timeout_boom
os.environ.pop("DOIT_SEAT", None)
PK.write_text("a packet for AC7 timeout\n")
a7 = argparse.Namespace(role="research", subject="L-spec-0001", packet=str(PK),
                        path=str(TMP / "content" / "L-research-0269to.md"), cwd=str(REPO),
                        charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=False)
try:
    dispatch.main(a7)
    code7 = 0
except SystemExit as e:
    code7 = e.code
PK.write_text(PK_DEFAULT)
dispatch.run_claude = _real_run_claude_to
raw7 = [json.loads(l) for l in max((TMP / "events").glob("L-research-*.jsonl")).read_text().splitlines()]
assert code7 == 1 and [e["type"] for e in raw7] == ["spawn-started", "spawn-failed"], raw7
assert raw7[-1]["reason"] == "timeout" and "timeout after" in raw7[-1]["why"], raw7[-1]
N += 1

# ── AC7 (doc-consistency half) · agents/executor.md's spawn-failed/spawn-stale
# row names the counted-attempt shape (the same one carry-failed's own row
# already uses) and the reviewer round-2-cap sentence, for reason in
# {unserved, timeout} — the same idiom `_claim_first` already uses for
# claim.sh, not a second grep step in Verification ────────────────────────────
_exec_text = (REPO_ROOT / "agents" / "executor.md").read_text()
_row7 = next(l for l in _exec_text.splitlines() if "spawn-failed" in l and "spawn-stale" in l and "no later" in l)
assert "{unserved, timeout}" in _row7, "AC7: the row must name reason in {unserved, timeout}"
assert "third" in _row7 and "escalation-blocking" in _row7, \
    "AC7: the counted-attempt (third-failure) wording must be present"
assert "round-2 cap" in _row7, "AC7: the reviewer round-cap sentence must be present"
N += 1


# An empty or non-file --packet is refused before a spawn id exists (pilot "Smaller"; charter 3).
before_files = sorted((TMP / "events").glob("L-research-*.jsonl"))
for bad_packet in ("", str(TMP / "nowhere.md")):
    try:
        dispatch.main(argparse.Namespace(role="research", subject="L-spec-0001", packet=bad_packet, path=None, cwd=str(REPO),
                                         charter=None, project="t", mcp_config=None, timeout=None, max_usd=None, seat=False))
        raise AssertionError("a non-file packet must be refused")
    except SystemExit as e:
        assert "not a file" in str(e.code) and "nothing allocated" in str(e.code), e.code
assert sorted((TMP / "events").glob("L-research-*.jsonl")) == before_files, "refused before allocation: no new spawn file"

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0262 · seat-write-path (L-charter-0031) — SWP1-SWP5
# ══════════════════════════════════════════════════════════════════════════════

# ── AC2 · research/reuse-scout with a.path=None: refused before any spend —
#    exactly one spawn-failed, no start event, no backend ever entered ────────
_ac2_hit = []
_real_run_claude, _real_run_seat, _real_run_codex = dispatch.run_claude, dispatch.run_seat, dispatch.run_codex


def _ac2_boom(name):
    def f(*a_, **k_):
        _ac2_hit.append(name)
        raise AssertionError(f"{name} must not be entered on a pre-spend refusal")
    return f


dispatch.run_claude, dispatch.run_seat, dispatch.run_codex = _ac2_boom("run_claude"), _ac2_boom("run_seat"), _ac2_boom("run_codex")
for _role in ("research", "reuse-scout"):
    # A fresh packet body — the bare default "a packet\n" already carries a
    # standing D120 dedup entry for research (research-0018's codex "weekly
    # limit" failure earlier in this file), which would refuse this dispatch
    # for THAT reason and mask the one this fixture means to prove.
    PK.write_text(f"a packet for AC2 ({_role})\n")
    code, raw, _ = _seat_driven(_role, "L-spec-0001", 1, path=None)
    PK.write_text(PK_DEFAULT)
    assert code == 1 and [e["type"] for e in raw] == ["spawn-failed"], (_role, raw)
    assert raw[0]["why"] == "a writing role needs --path", raw[0]
dispatch.run_claude, dispatch.run_seat, dispatch.run_codex = _real_run_claude, _real_run_seat, _real_run_codex
assert _ac2_hit == [], "AC2: no backend was ever entered on the pre-spend refusal"
N += 1


def _swp_serve(make_ready):
    """Background thread for the fixtures below: waits for exactly one still-open
    seat packet, calls `make_ready()` to create whatever file/dir the structured
    output it returns claims exists, then answers with a fixed completion envelope."""
    for _ in range(400):
        pk = [q for q in (list((TMP / "seat").glob("*.packet.md")) if (TMP / "seat").is_dir() else [])
              if not (TMP / "seat" / (q.name.split(".")[0] + ".result.json")).exists()
              and not (TMP / "seat" / (q.name.split(".")[0] + ".output.json")).exists()]
        if pk:
            sid = max(pk, key=lambda q: q.stat().st_mtime).name.split(".")[0]
            out = make_ready()
            (TMP / "seat" / f"{sid}.result.json").write_text(json.dumps(
                {"is_error": False, "structured_output": out, "num_turns": 1, "usage": {},
                 "total_cost_usd": None, "modelUsage": {"m": {}}, "session_id": sid, "permission_denials": []}))
            return
        time.sleep(0.05)


def _swp_dispatch(role, subject, path, make_ready, cwd=None, packet_text=None):
    """Drives dispatch.main() directly on the seat backend (seat=True — no
    DOIT_SEAT env needed); returns (code, events, spawn_id, cmd_json, packet_text).
    `packet_text`, when given, is written to PK for this call only and restored
    to the file-wide default after — a research dispatch earlier in this file
    already left a standing D120 dedup entry on the bare "a packet\\n" body."""
    if packet_text is not None:
        PK.write_text(packet_text)
    threading.Thread(target=_swp_serve, args=(make_ready,), daemon=True).start()
    a = argparse.Namespace(role=role, subject=subject, packet=str(PK), path=path, cwd=str(cwd or REPO),
                           charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=True)
    try:
        dispatch.main(a)
        code = 0
    except SystemExit as e:
        code = e.code
    if packet_text is not None:
        PK.write_text(PK_DEFAULT)
    raw = [json.loads(l) for l in max((TMP / "events").glob(f"L-{role}-*.jsonl")).read_text().splitlines()]
    sid = raw[0]["spawn"]
    cj = json.loads((TMP / "seat" / f"{sid}.cmd.json").read_text())
    return code, raw, sid, cj, (TMP / "seat" / f"{sid}.packet.md").read_text()


def _mk_spec(dest, subject):
    # spec-writer's own schema carries no "path" property (additionalProperties:
    # false) — its Output never echoes one back; `dispatch.main` reads the file at
    # `a.path` directly, so writing WELL_FORMED_SPEC there is the whole contract.
    def ready():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(WELL_FORMED_SPEC)
        return {**sw, "spec_id": subject, "escalations": []}
    return ready


def _mk_file(dest, base):
    def ready():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("dug")
        return {**base, "path": str(dest)}
    return ready


# ── AC1 (L-spec-0321/R5) · spec_path returns an absolute path even when
#    CONTENT itself is relative — proven directly, not through the
#    environment ─────────────────────────────────────────────────────────────
_content_real = dispatch.CONTENT
dispatch.CONTENT = pathlib.Path("content")
assert dispatch.spec_path("x").is_absolute(), "AC1: spec_path must be absolute even with a relative CONTENT"
dispatch.CONTENT = _content_real
N += 1

# ── AC1·SWP1 · spec-writer, no --path: gets the CONTENT default and reaches
#    spec-written/spawn-done — never "a writing role needs --path" ───────────
swp_sub1 = "L-spec-0261a"
swp_dest1 = dispatch.CONTENT / f"{swp_sub1}.md"
code, raw1, sid1, cj1, pkt1 = _swp_dispatch("spec-writer", swp_sub1, None, _mk_spec(swp_dest1, swp_sub1), cwd=TMP)
assert code == 0 and [e["type"] for e in raw1] == ["spawn-started", "spec-written", "spawn-done"], raw1
assert not any(e["type"] == "spawn-failed" for e in raw1), "AC1: no needs-path failure for a defaulted spec-writer"

# ── AC3·SWP2 · cmd.json["path"] for that same spawn: absolute, the CONTENT
#    default, and (this fixture's cwd IS the ledger root) the review_path's own
#    cwd-relative formula too ──────────────────────────────────────────────────
assert cj1["path"] == str(swp_dest1) == str(pathlib.Path(TMP) / "content" / f"{swp_sub1}.md"), cj1

# ── AC4·SWP2 · every kind-truthy role WITH an explicit --path: cmd.json["path"]
#    present, absolute, equal to the resolved value; a kind=None role (builder)
#    gets null ─────────────────────────────────────────────────────────────────
swp_r = TMP / "content" / "L-swp-ac4-research.md"
_, _, _, cj_r, _ = _swp_dispatch("research", "L-spec-0261b", str(swp_r), _mk_file(swp_r, research),
                                 packet_text="a packet for swp ac4 research\n")
assert cj_r["path"] == str(swp_r) and pathlib.Path(cj_r["path"]).is_absolute(), cj_r

swp_rs = TMP / "content" / "L-swp-ac4-reuse.md"
reuse_out = {"summary": "s", "candidates": [], "nothing_cleared": True, "complete": True,
            "contamination": False, "declarations": []}
_, _, _, cj_rs, _ = _swp_dispatch("reuse-scout", "L-spec-0261c", str(swp_rs), _mk_file(swp_rs, reuse_out))
assert cj_rs["path"] == str(swp_rs) and pathlib.Path(cj_rs["path"]).is_absolute(), cj_rs

swp_pd = TMP / "content" / "L-swp-ac4-probe"


def _swp_probe_ready():
    swp_pd.mkdir(parents=True, exist_ok=True)
    (swp_pd / "run.md").write_text("ran")
    return {**probe_out, "path": str(swp_pd) + "/"}


_, _, _, cj_p, _ = _swp_dispatch("probe", "L-spec-0261d", str(swp_pd), _swp_probe_ready)
assert cj_p["path"] == str(swp_pd) and pathlib.Path(cj_p["path"]).is_absolute(), cj_p

_, _, sid_b, cj_b, pkt_b = _swp_dispatch("builder", "L-spec-0261f", None, lambda: card)
assert cj_b["path"] is None, cj_b
N += 1

# ── AC3 (L-spec-0321/R5) · a RELATIVE --path (spec-writer), dispatched under a
#    cwd that is NOT $DOIT_ROOT (REPO, reproducing the 13:40Z incident's shape),
#    resolves against ROOT — never that cwd — to spec_path(subject) ───────────
swp_rel = "content/L-spec-0261g.md"
_, _, _, cj5, _ = _swp_dispatch("spec-writer", "L-spec-0261g", swp_rel,
                                _mk_spec(dispatch.spec_path("L-spec-0261g"), "L-spec-0261g"))
assert cj5["path"] == str(dispatch.spec_path("L-spec-0261g")), cj5
N += 1

# ── AC4 (L-spec-0321/R5) · spec-writer with a --path that is well-formed and
#    absolute under $DOIT_ROOT/content/, but names a DIFFERENT file than the
#    subject (the exact shape the old code silently honored, formerly
#    swp_sw/"L-spec-0261e") — refused before any seat file exists: exit 1, one
#    spawn-failed(reason=write-path-mismatch), no backend entered. Driven
#    DIRECTLY through dispatch.main — never through _swp_dispatch/_swp_serve,
#    whose background thread would otherwise wait for a seat packet this
#    refusal never writes, then serve the NEXT still-open packet
#    (L-spec-0261f/builder, above) with this call's own make_ready() output,
#    corrupting that already-completed fixture ─────────────────────────────────
swp_sw = TMP / "content" / "L-swp-ac4-spec.md"
_seat_before = sorted((TMP / "seat").glob("*")) if (TMP / "seat").is_dir() else []
dispatch.run_claude, dispatch.run_seat, dispatch.run_codex = _ac2_boom("run_claude"), _ac2_boom("run_seat"), _ac2_boom("run_codex")
a_sw = argparse.Namespace(role="spec-writer", subject="L-spec-0261e", packet=str(PK), path=str(swp_sw),
                          cwd=str(REPO), charter=None, project="t", mcp_config=None, timeout=1,
                          max_usd=None, seat=True)
try:
    dispatch.main(a_sw)
    raise AssertionError("a mismatched spec-writer --path must be refused")
except SystemExit as e:
    code_sw = e.code
dispatch.run_claude, dispatch.run_seat, dispatch.run_codex = _real_run_claude, _real_run_seat, _real_run_codex
raw_sw = [json.loads(l) for l in max((TMP / "events").glob("L-spec-writer-*.jsonl")).read_text().splitlines()]
assert code_sw == 1 and [e["type"] for e in raw_sw] == ["spawn-failed"], raw_sw
assert raw_sw[0]["reason"] == "write-path-mismatch", raw_sw[0]
_seat_after = sorted((TMP / "seat").glob("*")) if (TMP / "seat").is_dir() else []
assert _seat_after == _seat_before, "AC4: no seat/<spawn>.* file for the refused spawn"
assert _ac2_hit == [], "AC4: no backend was ever entered on the write-path-mismatch refusal"
N += 1

# ── AC5 (L-spec-0321/R5) · spec-writer with --path EXPLICITLY equal to
#    spec_path(subject) still succeeds — the mismatch check does not
#    over-refuse the legitimate case (e.g. carry.py's own explicit-absolute
#    call pattern) ───────────────────────────────────────────────────────────
swp_match_subj = "L-spec-0261h"
swp_match_path = str(dispatch.spec_path(swp_match_subj))
code_m, raw_m, _, cj_m, _ = _swp_dispatch("spec-writer", swp_match_subj, swp_match_path,
                                          _mk_spec(dispatch.spec_path(swp_match_subj), swp_match_subj))
assert code_m == 0 and [e["type"] for e in raw_m] == ["spawn-started", "spec-written", "spawn-done"], raw_m
assert cj_m["path"] == swp_match_path, cj_m
N += 1

# ── AC6·SWP3 · the packet text: "WRITE PATH: <path>\n\n" + original, for a
#    writing role; byte-identical (no prefix) for a non-writing role ──────────
assert pkt1.startswith(f"WRITE PATH: {cj1['path']}\n\n"), pkt1[:120]
assert pkt1[len(f"WRITE PATH: {cj1['path']}\n\n"):] == PK.read_text() + f"\n\nspawn_id: {sid1}\n", \
    "AC6: the remainder is the original packet, byte-for-byte"
assert not pkt_b.startswith("WRITE PATH:"), "AC6: a non-writing role's packet carries no WRITE PATH prefix"
assert pkt_b == PK.read_text() + f"\n\nspawn_id: {sid_b}\n", \
    "AC6: a non-writing role's packet is byte-identical to the one passed in"
N += 1

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0195 · spec-shape-checked-before-build (L-charter-0028) — AC2, AC4, AC6
#
# Placed BEFORE the L-spec-0192 section below on purpose: that section hand-writes
# spec-writer ledger files with non-numeric spawn-id suffixes ("...-stale9192",
# "...-open9192", ...), and this file's own `spawn()` helper finds "the latest"
# role file with a bare `max()` over a glob — lexicographically, not by alloc
# order — so a "spec-writer" spawn issued AFTER those exist would silently read
# one of THEM back instead of its own freshly allocated file.
# ══════════════════════════════════════════════════════════════════════════════

# ── AC6 · fold.EMITS["verify-waiver"] is executor/operator only ────────────────
assert fold.EMITS["verify-waiver"] == {"executor", "operator"}, fold.EMITS["verify-waiver"]

# ── AC2 · a malformed spec: spec-written, then spec-shape-failed, never
#          spawn-failed; fold state stays "written", never "void" ─────────────
BAD_SHAPE_SPEC = "# L-spec-0093\nno verification, no acceptance criteria, no writes grant\n"
sp93 = TMP / "content" / "L-spec-0093.md"
code, types, evs, cmd = spawn("spec-writer", out={**sw, "spec_id": "L-spec-0093"}, path=sp93,
                              side=lambda: sp93.write_text(BAD_SHAPE_SPEC), subject="L-spec-0093")
assert code == 0 and types == ["spec-written", "spec-shape-failed", "question", "spawn-done"], types
assert evs[1]["findings"], "spec-shape-failed carries what validate.spec_shape returned"
specs, *_ = fold.fold(fold.read_events())
assert specs["L-spec-0093"]["state"] == "written", \
    f"AC2: fold state stays 'written', never 'void': {specs['L-spec-0093']['state']}"

# ── AC4 · a builder dispatch against it is refused before any spend, and clears
#          the instant a fresh well-formed spec-written lands ─────────────────
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-0093")
assert code == 1 and types == ["spawn-failed"] and "spec-shape" in evs[0]["why"], (types, evs)
assert evs[0].get("reason") == "spec-shape", evs[0]
assert cmd is None, "run_claude must never be called on a spec-shape refusal"

code, types, evs, cmd = spawn("spec-writer", out={**sw, "spec_id": "L-spec-0093"}, path=sp93,
                              side=lambda: sp93.write_text(WELL_FORMED_SPEC), subject="L-spec-0093")
assert code == 0 and types == ["spec-written", "question", "spawn-done"], types
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-0093")
assert code == 0 and cmd is not None, (code, types, evs)

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0192 · fold-states-owed-due-and-killed (L-charter-0028) — R3
# ══════════════════════════════════════════════════════════════════════════════

# ── AC10 · role=builder is refused against a killed subject, before any spend ─
code, types, evs, cmd = spawn("spec-writer", out={**sw, "status": "killed", "killed_by_check": 2,
                                                   "escalations": []}, subject="L-spec-0099")
assert code == 0 and types == ["spec-killed", "spawn-done"], (types, evs)
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-0099")
assert code == 1 and types == ["spawn-failed"] and "killed" in evs[0]["why"], (types, evs)
assert cmd is None, "run_claude must never be called on a killed-subject refusal"

# ── AC12 · role=builder is refused while a spec-writer spawn is still open ────
OPEN_SW = TMP / "events" / "L-spec-writer-open9192.jsonl"
dispatch.emit(OPEN_SW, {}, "spawn-started", subject="L-spec-0097", role="spec-writer",
             spawn="L-spec-writer-open9192")
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-0097")
assert code == 1 and types == ["spawn-failed"] and "spec-writer spawn" in evs[0]["why"], (types, evs)
assert cmd is None, "run_claude must never be called on an open-spec-writer-spawn refusal"
# L-spec-0276/AC13: the open-spec-writer-spawn refusal now names its reason.
assert evs[0].get("reason") == "rework-open", evs[0]

# ── AC13a · a spawn-done for the EXACT spawn id lets the builder proceed ──────
DONE_SW = TMP / "events" / "L-spec-writer-done9192.jsonl"
dispatch.emit(DONE_SW, {}, "spawn-started", subject="L-spec-0096", role="spec-writer",
             spawn="L-spec-writer-done9192")
dispatch.emit(DONE_SW, {}, "spawn-done", subject="L-spec-0096", spawn="L-spec-writer-done9192")
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-0096")
assert code == 0 and cmd is not None, (code, types, evs)

# ── AC13b · a spawn-stale for the EXACT spawn id proceeds too ─────────────────
STALE_SW = TMP / "events" / "L-spec-writer-stale9192.jsonl"
dispatch.emit(STALE_SW, {}, "spawn-started", subject="L-spec-0095", role="spec-writer",
             spawn="L-spec-writer-stale9192")
dispatch.emit(STALE_SW, {}, "spawn-stale", subject="L-spec-0095", spawn="L-spec-writer-stale9192")
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-0095")
assert code == 0 and cmd is not None, (code, types, evs)

# ── AC13c · an unmatchable start (no spawn id) aged past 2x the spec-writer cap
# also proceeds — mirroring tick.in_flight's own handling of one.
_cap = dispatch.ROLES["spec-writer"][1]
_old_ts = (_dt.now(_timezone.utc) - _timedelta(minutes=2 * _cap + 5)).isoformat(timespec="seconds")
OLD_SW = TMP / "events" / "L-spec-writer-nospawn9192.jsonl"
OLD_SW.write_text(json.dumps({"v": 1, "ts": _old_ts, "type": "spawn-started",
                              "subject": "L-spec-0094", "role": "spec-writer"}) + "\n")
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-0094")
assert code == 0 and cmd is not None, (code, types, evs)

# ── AC14 · dispatch.emit() gates on the required-fields door ONLY ─────────────
EMIT_ESC = TMP / "events" / "L-emit-esc-test.jsonl"
before_lines = EMIT_ESC.read_text().splitlines() if EMIT_ESC.is_file() else []
reason = dispatch.emit(EMIT_ESC, {}, "escalation-blocking", subject="x", why="y")
after_lines = EMIT_ESC.read_text().splitlines() if EMIT_ESC.is_file() else []
assert reason is not None and "default" in reason and "deadline" in reason and "revert" in reason, reason
assert after_lines == before_lines, "a field-refused emit() writes nothing"

EMIT_VERDICT = TMP / "events" / "L-builder-notgrader-test.jsonl"   # actor "builder" is not in EMITS["verdict"]
r2 = dispatch.emit(EMIT_VERDICT, {}, "verdict", subject="x", confirmed=True, n=1,
                   matches_intent="yes", card_ok="yes", cannot_assess=[])
assert r2 is None, r2
assert any(json.loads(l)["type"] == "verdict" for l in EMIT_VERDICT.read_text().splitlines()), \
    "an actor/type mismatch alone is never refused at the emit() door — only recorded and ignored at fold time"

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0194 · worktree-readonly-dsn (L-charter-0028) — R3
# ══════════════════════════════════════════════════════════════════════════════
_H = []


def _root(name):
    """A fresh, private directory this file is responsible for cleaning up —
    never the real HOME/DOIT_ROOT (harness.py, L-spec-0183)."""
    p = harness.test_root(name)
    _H.append(p)
    return p


def _checkout(name, env_text=None):
    d = _root(f"checkout-{name}")
    if env_text is not None:
        (d / ".env").write_text(env_text)
    return d


def _worktree(name, git=False, gitignore=None, existing_env=None):
    d = _root(f"worktree-{name}")
    if git:
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    if gitignore is not None:
        (d / ".gitignore").write_text(gitignore)
    if existing_env is not None:
        (d / ".env").write_text(existing_env)
    return d


# ── AC1 · no .env at all -> None ──────────────────────────────────────────────
co1 = _checkout("ac1")
assert dispatch.readonly_dsn(co1) is None, "AC1"

# ── AC2 · .env with SUPABASE_DB_URL / SUPABASE_DB_URL_DIRECT only -> None ─────
co2 = _checkout("ac2", "SUPABASE_DB_URL=rw1\nSUPABASE_DB_URL_DIRECT=rw2\n")
assert dispatch.readonly_dsn(co2) is None, "AC2"

# ── AC3 · four .env shapes ─────────────────────────────────────────────────────
assert dispatch.readonly_dsn(_checkout("ac3-1", "SUPABASE_DB_URL_RO=plain-value\n")) == "plain-value", \
    "AC3.1: unquoted value verbatim"
assert dispatch.readonly_dsn(_checkout("ac3-2", 'SUPABASE_DB_URL_RO="quoted-value"\n')) == "quoted-value", \
    "AC3.2: double-quoted value, quotes stripped"
assert dispatch.readonly_dsn(_checkout("ac3-3", "# a comment\nSUPABASE_DB_URL_RO=real-value\n")) == "real-value", \
    "AC3.3: a comment line is not a declaration"
assert dispatch.readonly_dsn(_checkout("ac3-4", "SUPABASE_DB_URL_RO=first\nSUPABASE_DB_URL_RO=second\n")) == "second", \
    "AC3.4: the key declared twice -> the LAST value"

# ── AC4 · no RO key -> "absent", nothing written, worktree need not be a repo ──
wt4 = _worktree("ac4")
r = dispatch.provision_worktree_env(wt4, co2)
assert r == "absent" and not (wt4 / ".env").exists(), ("AC4", r)

# ── AC5 · RO byte-equals SUPABASE_DB_URL -> "refused" ─────────────────────────
co5 = _checkout("ac5", "SUPABASE_DB_URL=same-value\nSUPABASE_DB_URL_RO=same-value\n")
wt5 = _worktree("ac5")
r = dispatch.provision_worktree_env(wt5, co5)
assert r == "refused" and not (wt5 / ".env").exists(), ("AC5", r)

# ── AC6 · RO byte-equals SUPABASE_DB_URL_DIRECT (distinct from SUPABASE_DB_URL)
#         -> "refused" — the widened, not-just-SUPABASE_DB_URL match ──────────
co6 = _checkout("ac6", "SUPABASE_DB_URL=rw-value\nSUPABASE_DB_URL_DIRECT=direct-value\n"
                       "SUPABASE_DB_URL_RO=direct-value\n")
wt6 = _worktree("ac6")
r = dispatch.provision_worktree_env(wt6, co6)
assert r == "refused" and not (wt6 / ".env").exists(), ("AC6", r)

# A checkout with a valid RO DSN distinct from every OTHER DB_URL-named key,
# reused by AC7-AC10.
CO_VALID = _checkout("valid", "SUPABASE_DB_URL=rw-value\nSUPABASE_DB_URL_DIRECT=direct-value\n"
                              "PG_PARITY_DB_URL=parity-value\nSUPABASE_DB_URL_RO=ro-distinct-value\n")
RO_LINE = b"SUPABASE_DB_URL=ro-distinct-value\n"

# ── AC7 · worktree IS a git repo, .gitignore does not list .env -> "refused",
#         proving the ignore-gate fires independently of the equality gate ────
wt7 = _worktree("ac7", git=True)   # no .gitignore at all
r = dispatch.provision_worktree_env(wt7, CO_VALID)
assert r == "refused" and not (wt7 / ".env").exists(), ("AC7", r)

# ── AC8 · fresh git worktree, .gitignore lists .env, no pre-existing .env ─────
wt8 = _worktree("ac8", git=True, gitignore=".env\n")
r = dispatch.provision_worktree_env(wt8, CO_VALID)
env8 = wt8 / ".env"
assert r == "readonly" and env8.read_bytes() == RO_LINE, ("AC8", r, env8.read_bytes() if env8.exists() else None)
assert (env8.stat().st_mode & 0o777) == 0o600, oct(env8.stat().st_mode)

# ── AC9 · repeated on AC8's already-provisioned pair -> "readonly" again,
#         content unchanged (idempotent) ──────────────────────────────────────
r2 = dispatch.provision_worktree_env(wt8, CO_VALID)
assert r2 == "readonly" and env8.read_bytes() == RO_LINE, ("AC9", r2)

# ── AC10 · a pre-existing, unrelated .env -> "refused", bytes unchanged,
#          worktree need not be a git repo ────────────────────────────────────
wt10 = _worktree("ac10", existing_env="SOME_OTHER_VAR=x\n")
before10 = (wt10 / ".env").read_bytes()
r = dispatch.provision_worktree_env(wt10, CO_VALID)
assert r == "refused" and (wt10 / ".env").read_bytes() == before10, ("AC10", r)

for p in _H:
    harness.cleanup(p)

# ── AC11-AC15 · dispatch.main() wires dsn_role end to end ────────────────────
DSN_PROJECT = "dsnproj"
DSN_CHECKOUT = TMP / "repos" / DSN_PROJECT
DSN_CHECKOUT.mkdir(parents=True)
(DSN_CHECKOUT / ".env").write_text("SUPABASE_DB_URL=rw-value\nSUPABASE_DB_URL_DIRECT=direct-value\n"
                                   "SUPABASE_DB_URL_RO=e2e-ro-value\n")
E2E_LINE = b"SUPABASE_DB_URL=e2e-ro-value\n"
_H2 = []


def _dsn_worktree(name):
    d = harness.test_root(f"dsn-wt-{name}")
    _H2.append(d)
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    (d / ".gitignore").write_text(".env\n")
    return d


def drive(role, subject, cwd, project, out):
    """Like spawn() above, but drives dispatch.main() with a caller-chosen
    project/cwd — spawn() itself is pinned to REPO/project="t" for every
    other case in this file, which is exactly the case dsn_role=="absent"
    ends up exercising anyway (no repos/t/.env ever exists here)."""
    res = {"is_error": False, "terminal_reason": "completed", "structured_output": out, "num_turns": 1,
           "usage": {"input_tokens": 1, "output_tokens": 2}, "total_cost_usd": 0.01,
           "modelUsage": {"m": {}}, "permission_denials": []}
    seen = {}

    def fake(cmd, packet, cwd_, timeout):
        p = pathlib.Path(cwd_) / ".env"
        seen["env_exists_at_call"] = p.exists()
        seen["env_bytes_at_call"] = p.read_bytes() if p.exists() else None
        fake.cmd = cmd
        return argparse.Namespace(stdout=json.dumps(res), returncode=0, stderr="")
    dispatch.run_claude = fake
    a = argparse.Namespace(role=role, subject=subject, packet=str(PK), path=None,
                           cwd=str(cwd), charter=None, project=project, mcp_config=None,
                           timeout=None, max_usd=None)
    try:
        dispatch.main(a)
        code = 0
    except SystemExit as e:
        code = e.code
    # [0-9]*, not *: L-builder-notgrader-test.jsonl (EMIT_VERDICT, above) also
    # matches a bare "L-builder-*.jsonl" and, being non-numeric, sorts after
    # every real 4-digit spawn id — max() would silently pick IT instead.
    raw = [json.loads(l) for l in
           max((TMP / "events").glob(f"L-{role}-[0-9]*.jsonl")).read_text().splitlines()]
    return code, raw, seen


# AC11: build-started carries dsn_role=="readonly", and <cwd>/.env exists with
# the exact one line already at the moment the mocked spawn is invoked.
wt11 = _dsn_worktree("ac11")
code, raw11, seen11 = drive("builder", "L-spec-0111", wt11, DSN_PROJECT, card)
bs11 = next(e for e in raw11 if e["type"] == "build-started")
assert bs11["dsn_role"] == "readonly", bs11
assert seen11["env_exists_at_call"] and seen11["env_bytes_at_call"] == E2E_LINE, \
    ("AC11: not provisioned before the mocked spawn ran", seen11)
assert (wt11 / ".env").read_bytes() == E2E_LINE, "AC11: final state"

# AC12: the grader's own spawn-started carries role AND dsn_role together.
# L-spec-0481/AC19/AC21 (sanctioned edit): a grader receives NO production DSN
# of any kind (SD-R12-3b) — provision_worktree_env is never called for it, so
# this claude-p grader (no view, no `db` capability computed) is "none", never
# the L-spec-0194 "readonly" this used to assert.
wt12 = _dsn_worktree("ac12")
code, raw12, seen12 = drive("grader", "L-spec-0112", wt12, DSN_PROJECT, grade([met]))
ss12 = next(e for e in raw12 if e["type"] == "spawn-started")
assert ss12["role"] == "grader" and ss12["dsn_role"] == "none", ss12
assert not (wt12 / ".env").exists(), "AC19: no production DSN reaches a grader's cwd"

# AC13: every OTHER role's spawn-started carries no dsn_role key at all.
# A fresh packet body, not the file-wide "a packet\n": an identical
# (packet, contract) pair already failed as L-research-0018 (weekly limit)
# above, and D120 refuses to re-spend on that exact combination.
PK.write_text("a packet for AC13\n")
code, types, evs, _ = spawn("research", out=research, path=rp)
PK.write_text(PK_DEFAULT)
assert code == 0, (code, types, evs)
assert spawn.raw[0]["type"] == "spawn-started" and spawn.raw[0]["role"] == "research", spawn.raw[0]
assert "dsn_role" not in spawn.raw[0], spawn.raw[0]

# AC14: a.project falsy -> dsn_role=="absent", no .env written, and the
# git-check-ignore subprocess is never invoked at all.
wt14 = harness.test_root("dsn-wt-ac14")   # deliberately not even a git repo
_H2.append(wt14)
_ignore_calls = {"n": 0}
_real_subprocess_run = subprocess.run


def _counting_run(cmd, *a_, **kw):
    if len(cmd) > 1 and cmd[0] == "git" and "check-ignore" in cmd:
        _ignore_calls["n"] += 1
    return _real_subprocess_run(cmd, *a_, **kw)


subprocess.run = _counting_run
try:
    code, raw14, seen14 = drive("builder", "L-spec-0114", wt14, None, card)
finally:
    subprocess.run = _real_subprocess_run
bs14 = next(e for e in raw14 if e["type"] == "build-started")
assert bs14["dsn_role"] == "absent" and not (wt14 / ".env").exists() and _ignore_calls["n"] == 0, \
    (bs14, _ignore_calls)

# AC15: the RO DSN literal appears in neither captured stdout/stderr nor any
# field of any ledger event appended on the subject.
wt15 = _dsn_worktree("ac15")
_buf_out, _buf_err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(_buf_out), contextlib.redirect_stderr(_buf_err):
    code, raw15, seen15 = drive("builder", "L-spec-0115", wt15, DSN_PROJECT, card)
_captured = _buf_out.getvalue() + _buf_err.getvalue()
assert "e2e-ro-value" not in _captured, "AC15: the DSN literal leaked into stdout/stderr"
for e in raw15:
    assert "e2e-ro-value" not in json.dumps(e), ("AC15: the DSN literal leaked into a ledger event", e)

for p in _H2:
    harness.cleanup(p)

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0387 · owed-sweeper-role (L-charter-0038 R1) — AC6-9, AC12-16
# ══════════════════════════════════════════════════════════════════════════════
import jsonschema as _js387  # noqa: E402

OS_SCHEMA = json.loads((REPO_ROOT / "agents" / "owed-sweeper.schema.json").read_text())


def _sweep_manifest(sweep_id, rows, project="t", cwd=None):
    cwd = cwd or str(REPO)
    (TMP / "content" / f"{sweep_id}.md").write_text(
        f"# {sweep_id}\n\nproject: {project}\ncwd: {cwd}\n\n## Rows\n```json\n{json.dumps(rows)}\n```\n")


def _os_row(spec, criterion, declared_src="owed-ac", line="1"):
    return {"spec": spec, "criterion": criterion, "declared_src": declared_src, "line": line,
            "spec_path": str(dispatch.spec_path(spec))}


# ── AC6 · schema requires top-level batch/results; each result requires spec/
#         criterion/verdict; a met/failed row missing evidence, a failed row
#         missing kind, or a cannot-observe row missing capability/why raises ──
_os_good = {"batch": "L-owed-sweeper-schema", "contamination": False, "escalations": [], "declarations": [],
           "results": [{"spec": "L-spec-9001", "criterion": "AC1", "verdict": "met", "evidence": "e"},
                       {"spec": "L-spec-9002", "criterion": "AC2", "verdict": "failed", "evidence": "e",
                        "kind": "stale"},
                       {"spec": "L-spec-9003", "criterion": "AC3", "verdict": "cannot-observe",
                        "capability": "ro-dsn", "why": "w"}]}
_js387.validate(_os_good, OS_SCHEMA)      # every valid sample validates


def _os_bad_result(row):
    try:
        _js387.validate({**_os_good, "results": [row]}, OS_SCHEMA)
        raise AssertionError(f"AC6: schema accepted an invalid result row: {row!r}")
    except _js387.ValidationError:
        pass


_os_bad_result({"criterion": "AC1", "verdict": "met", "evidence": "e"})                          # no spec
_os_bad_result({"spec": "x", "verdict": "met", "evidence": "e"})                                 # no criterion
_os_bad_result({"spec": "x", "criterion": "AC1", "verdict": "met"})                               # met, no evidence
_os_bad_result({"spec": "x", "criterion": "AC1", "verdict": "failed", "evidence": "e"})            # failed, no kind
_os_bad_result({"spec": "x", "criterion": "AC1", "verdict": "cannot-observe", "why": "w"})         # no capability
_os_bad_result({"spec": "x", "criterion": "AC1", "verdict": "cannot-observe", "capability": "droplet"})  # no why
try:
    _js387.validate({k: v for k, v in _os_good.items() if k != "results"}, OS_SCHEMA)
    raise AssertionError("AC6: a document with no results must be refused")
except _js387.ValidationError:
    pass
try:
    _js387.validate({k: v for k, v in _os_good.items() if k != "batch"}, OS_SCHEMA)
    raise AssertionError("AC6: a document with no batch must be refused")
except _js387.ValidationError:
    pass
N += 1

# ── AC7 · a capability outside the enumerated six values fails schema validation
for cap in ("ro-dsn", "droplet", "browser", "operator-action", "elapsed", "other"):
    _js387.validate({**_os_good, "results": [{"spec": "x", "criterion": "AC1", "verdict": "cannot-observe",
                                              "capability": cap, "why": "w"}]}, OS_SCHEMA)
try:
    _js387.validate({**_os_good, "results": [{"spec": "x", "criterion": "AC1", "verdict": "cannot-observe",
                                              "capability": "nonsense", "why": "w"}]}, OS_SCHEMA)
    raise AssertionError("AC7: an out-of-enum capability must be refused")
except _js387.ValidationError:
    pass
N += 1

# ── AC8 · dispatch.ROLES["owed-sweeper"] == (None, 45, 5) ──────────────────────
assert dispatch.ROLES["owed-sweeper"] == (None, 45, 5), dispatch.ROLES["owed-sweeper"]
N += 1

# ── AC9 · both TOML templates carry [contracts.owed-sweeper] backend=seat,
#         model=claude-sonnet-5, and models.resolve agrees against each ───────
for _tmpl in ("models.example.toml", "models.claude-only.toml"):
    _mp = models.load(REPO_ROOT / _tmpl)
    assert _mp["owed-sweeper"]["backend"] == "seat" and _mp["owed-sweeper"]["model"] == "claude-sonnet-5", \
        (_tmpl, _mp["owed-sweeper"])
    _r = models.resolve("owed-sweeper", None, mp=_mp)
    assert _r["backend"] == "seat" and _r["model"] == "claude-sonnet-5", (_tmpl, _r)
N += 1

# ── AC12 · a 3-row manifest (A/AC1, B/AC2, C/AC3) drives exactly one owed-met,
#          one owed-failed, one owed-unobservable — each on its OWN row's spec,
#          never the batch subject; spawn-started/spawn-done keep the batch ───
SWEEP12 = "L-owed-sweeper-fx12"
ROWS12 = [_os_row("L-owsw-a12", "AC1"), _os_row("L-owsw-b12", "AC2"), _os_row("L-owsw-c12", "AC3")]
_sweep_manifest(SWEEP12, ROWS12)
OUT12 = {"batch": SWEEP12, "contamination": False, "escalations": [], "declarations": [],
         "results": [{"spec": "L-owsw-a12", "criterion": "AC1", "verdict": "met", "evidence": "e"},
                     {"spec": "L-owsw-b12", "criterion": "AC2", "verdict": "failed", "evidence": "e",
                      "kind": "unmet"},
                     {"spec": "L-owsw-c12", "criterion": "AC3", "verdict": "cannot-observe",
                      "capability": "droplet", "why": "w"}]}
# AC15 rides along on this same drive: provision_worktree_env must never be
# called for owed-sweeper, and no .env must appear under REPO (the manifest's cwd).
_real_pwe387 = dispatch.provision_worktree_env


def _pwe387_boom(*_a, **_k):
    raise AssertionError("AC15: provision_worktree_env must never be called for owed-sweeper")


dispatch.provision_worktree_env = _pwe387_boom
try:
    code12, types12, evs12, _ = spawn("owed-sweeper", out=OUT12, subject=SWEEP12)
finally:
    dispatch.provision_worktree_env = _real_pwe387
assert code12 == 0, evs12
assert not (REPO / ".env").exists(), "AC15: no .env under the manifest's cwd"
_om12 = [e for e in evs12 if e["type"] == "owed-met"]
_of12 = [e for e in evs12 if e["type"] == "owed-failed"]
_ou12 = [e for e in evs12 if e["type"] == "owed-unobservable"]
assert len(_om12) == 1 and _om12[0]["subject"] == "L-owsw-a12" and _om12[0]["criterion"] == "AC1" \
    and _om12[0]["batch"] == SWEEP12, _om12
assert len(_of12) == 1 and _of12[0]["subject"] == "L-owsw-b12" and _of12[0]["criterion"] == "AC2" \
    and _of12[0]["kind"] == "unmet" and _of12[0]["batch"] == SWEEP12, _of12
assert len(_ou12) == 1 and _ou12[0]["subject"] == "L-owsw-c12" and _ou12[0]["criterion"] == "AC3" \
    and _ou12[0]["capability"] == "droplet" and _ou12[0]["batch"] == SWEEP12, _ou12
assert spawn.raw[0]["type"] == "spawn-started" and spawn.raw[0]["subject"] == SWEEP12, spawn.raw[0]
_sd12 = next(e for e in spawn.raw if e["type"] == "spawn-done")
assert _sd12["subject"] == SWEEP12 and _sd12.get("off_manifest") == 0, _sd12
N += 1

# ── AC13 · a 4th, off-manifest result is dropped before events_for ever sees
#          it (no event for it), and counted on spawn-done; AC12's exact drive
#          re-run carries off_manifest == 0 ────────────────────────────────────
SWEEP13 = "L-owed-sweeper-fx13"
_sweep_manifest(SWEEP13, ROWS12)
OUT13 = {**OUT12, "batch": SWEEP13,
         "results": OUT12["results"] + [{"spec": "L-owsw-ghost13", "criterion": "AC9",
                                         "verdict": "met", "evidence": "e"}]}
code13, types13, evs13, _ = spawn("owed-sweeper", out=OUT13, subject=SWEEP13)
assert code13 == 0, evs13
assert not any(e.get("subject") == "L-owsw-ghost13" for e in evs13), \
    "AC13: an off-manifest row must never become an event"
_sd13 = next(e for e in spawn.raw if e["type"] == "spawn-done")
assert _sd13["off_manifest"] == 1, _sd13
# re-run AC12's exact (on-manifest-only) drive: off_manifest == 0
SWEEP13b = "L-owed-sweeper-fx13b"
_sweep_manifest(SWEEP13b, ROWS12)
code13b, types13b, evs13b, _ = spawn("owed-sweeper", out={**OUT12, "batch": SWEEP13b}, subject=SWEEP13b)
_sd13b = next(e for e in spawn.raw if e["type"] == "spawn-done")
assert _sd13b["off_manifest"] == 0, _sd13b
N += 1

# ── AC14 · role=owed-sweeper with no --path raises no "a writing role needs
#          --path" failure (kind is None); completes code == 0 given a valid stub
SWEEP14 = "L-owed-sweeper-fx14"
_sweep_manifest(SWEEP14, [_os_row("L-owsw-a14", "AC1")])
OUT14 = {"batch": SWEEP14, "contamination": False, "escalations": [], "declarations": [],
         "results": [{"spec": "L-owsw-a14", "criterion": "AC1", "verdict": "met", "evidence": "e"}]}
code14, types14, evs14, _ = spawn("owed-sweeper", out=OUT14, subject=SWEEP14, path=None)
assert code14 == 0, evs14
assert not any(e.get("why") == "a writing role needs --path" for e in evs14), evs14
N += 1

# ── AC16 · "owed-sweeper" carries no codex sandbox grant ───────────────────────
assert "owed-sweeper" not in dispatch.CODEX_WRITES, dispatch.CODEX_WRITES
N += 1

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-9004 · owed-sweeper-ro-credentials (L-charter-0042 OC1) — AC1-AC9
# ══════════════════════════════════════════════════════════════════════════════
_H9004 = []


def _root9004(name):
    d = harness.test_root(f"9004-{name}")
    _H9004.append(d)
    return d


def _checkout9004(name, env_text=None):
    d = _root9004(f"checkout-{name}")
    if env_text is not None:
        (d / ".env").write_text(env_text)
    return d


# ── AC1 · no SUPABASE_DB_URL_RO declared (incl. no .env at all) -> "absent",
#         no .env created ──────────────────────────────────────────────────────
sw_co_1a = _checkout9004("ac1-a")
sw_dir_1a = _root9004("ac1-dir-a")
r = dispatch.provision_sweep_env(sw_dir_1a, sw_co_1a)
assert r == "absent" and not (sw_dir_1a / ".env").exists(), ("9004 AC1a", r)
sw_co_1b = _checkout9004("ac1-b", "SUPABASE_DB_URL=rw1\nSUPABASE_DB_URL_DIRECT=rw2\n")
sw_dir_1b = _root9004("ac1-dir-b")
r = dispatch.provision_sweep_env(sw_dir_1b, sw_co_1b)
assert r == "absent" and not (sw_dir_1b / ".env").exists(), ("9004 AC1b", r)
N += 1

# ── AC2 · RO byte-equals SUPABASE_DB_URL, and separately SUPABASE_DB_URL_DIRECT
#         -> "refused", nothing written ────────────────────────────────────────
sw_co_2a = _checkout9004("ac2-a", "SUPABASE_DB_URL=same-value\nSUPABASE_DB_URL_RO=same-value\n")
sw_dir_2a = _root9004("ac2-dir-a")
r = dispatch.provision_sweep_env(sw_dir_2a, sw_co_2a)
assert r == "refused" and not (sw_dir_2a / ".env").exists(), ("9004 AC2a", r)
sw_co_2b = _checkout9004("ac2-b", "SUPABASE_DB_URL=rw-value\nSUPABASE_DB_URL_DIRECT=direct-value\n"
                                  "SUPABASE_DB_URL_RO=direct-value\n")
sw_dir_2b = _root9004("ac2-dir-b")
r = dispatch.provision_sweep_env(sw_dir_2b, sw_co_2b)
assert r == "refused" and not (sw_dir_2b / ".env").exists(), ("9004 AC2b", r)
N += 1

# A checkout with a valid RO DSN distinct from every OTHER DB_URL-named key,
# reused by AC3-AC5.
SW_CO_VALID = _checkout9004("valid", "SUPABASE_DB_URL=rw-value\nSUPABASE_DB_URL_DIRECT=direct-value\n"
                                     "PG_PARITY_DB_URL=parity-value\nSUPABASE_DB_URL_RO=ro-distinct-9004\n")
SW_RO_LINE = b"SUPABASE_DB_URL=ro-distinct-9004\n"

# ── AC3 · valid distinct RO, plain non-git sweep_dir -> "readonly", exactly one
#         line, mode 0o600; a repeat call is idempotent, bytes unchanged ──────
sw_dir_3 = _root9004("ac3-dir")
r = dispatch.provision_sweep_env(sw_dir_3, SW_CO_VALID)
env3 = sw_dir_3 / ".env"
assert r == "readonly" and env3.read_bytes() == SW_RO_LINE, ("9004 AC3", r, env3.read_bytes() if env3.exists() else None)
assert (env3.stat().st_mode & 0o777) == 0o600, oct(env3.stat().st_mode)
r2 = dispatch.provision_sweep_env(sw_dir_3, SW_CO_VALID)
assert r2 == "readonly" and env3.read_bytes() == SW_RO_LINE, ("9004 AC3 idempotent", r2)
N += 1

# ── AC4 · same valid RO, but sweep_dir sits at/under a .git entry -> "refused",
#         proving the destination-safety gate fires independently of DSN validity
sw_dir_4a = _root9004("ac4-dir-self")     # sweep_dir itself holds a .git entry
(sw_dir_4a / ".git").mkdir()
r = dispatch.provision_sweep_env(sw_dir_4a, SW_CO_VALID)
assert r == "refused" and not (sw_dir_4a / ".env").exists(), ("9004 AC4a", r)
sw_parent_4b = _root9004("ac4-parent")
(sw_parent_4b / ".git").mkdir()
sw_dir_4b = sw_parent_4b / "nested" / "sweep"
sw_dir_4b.mkdir(parents=True)
r = dispatch.provision_sweep_env(sw_dir_4b, SW_CO_VALID)
assert r == "refused" and not (sw_dir_4b / ".env").exists(), ("9004 AC4b", r)
N += 1

# ── AC5 · sweep_dir already holds an .env with DIFFERENT bytes -> "refused",
#         bytes unchanged ──────────────────────────────────────────────────────
sw_dir_5 = _root9004("ac5-dir")
(sw_dir_5 / ".env").write_text("SOME_OTHER_VAR=x\n")
before5 = (sw_dir_5 / ".env").read_bytes()
r = dispatch.provision_sweep_env(sw_dir_5, SW_CO_VALID)
assert r == "refused" and (sw_dir_5 / ".env").read_bytes() == before5, ("9004 AC5", r)
N += 1

# ── AC6 · dispatch.main() end-to-end for role=owed-sweeper: spawn-started
#         carries dsn_role=="readonly", and <cwd>/.env exists with the exact
#         one line already at the moment the mocked spawn is invoked ──────────
SWEEP9004_6 = "L-owed-sweeper-9004ac6"
_sweep_manifest(SWEEP9004_6, [_os_row("L-owsw-9004ac6", "AC1")])
cwd9004_6 = _root9004("ac6-cwd")
OUT9004_6 = {"batch": SWEEP9004_6, "contamination": False, "escalations": [], "declarations": [],
             "results": [{"spec": "L-owsw-9004ac6", "criterion": "AC1", "verdict": "met", "evidence": "e"}]}
code9004_6, raw9004_6, seen9004_6 = drive("owed-sweeper", SWEEP9004_6, cwd9004_6, DSN_PROJECT, OUT9004_6)
ss9004_6 = next(e for e in raw9004_6 if e["type"] == "spawn-started")
assert ss9004_6.get("dsn_role") == "readonly", ss9004_6
assert seen9004_6["env_exists_at_call"] and seen9004_6["env_bytes_at_call"] == E2E_LINE, \
    ("9004 AC6: not provisioned before the mocked spawn ran", seen9004_6)
assert (cwd9004_6 / ".env").read_bytes() == E2E_LINE, "9004 AC6: final state"
N += 1

# ── AC7 · same drive, but the checkout's RO value is byte-identical to its own
#         SUPABASE_DB_URL (mislabeled read-write): spawn-started carries
#         dsn_role=="refused", no .env exists under cwd afterward ─────────────
DSN_PROJECT_9004B = "dsnproj9004b"
DSN_CHECKOUT_9004B = TMP / "repos" / DSN_PROJECT_9004B
DSN_CHECKOUT_9004B.mkdir(parents=True)
(DSN_CHECKOUT_9004B / ".env").write_text("SUPABASE_DB_URL=same-rw-9004\nSUPABASE_DB_URL_RO=same-rw-9004\n")
SWEEP9004_7 = "L-owed-sweeper-9004ac7"
_sweep_manifest(SWEEP9004_7, [_os_row("L-owsw-9004ac7", "AC1")])
cwd9004_7 = _root9004("ac7-cwd")
OUT9004_7 = {"batch": SWEEP9004_7, "contamination": False, "escalations": [], "declarations": [],
             "results": [{"spec": "L-owsw-9004ac7", "criterion": "AC1", "verdict": "met", "evidence": "e"}]}
code9004_7, raw9004_7, seen9004_7 = drive("owed-sweeper", SWEEP9004_7, cwd9004_7, DSN_PROJECT_9004B, OUT9004_7)
ss9004_7 = next(e for e in raw9004_7 if e["type"] == "spawn-started")
assert ss9004_7.get("dsn_role") == "refused", ss9004_7
assert not (cwd9004_7 / ".env").exists(), "9004 AC7: no .env under cwd"
N += 1

# ── AC8 · five-row shape table: the checkout's own read-write value(s) never
#         appear in any written .env; exactly row 5 ever writes a non-empty one
RW_A, RW_B = "table-rw-a-9004", "table-rw-b-9004"
tbl_co_1 = _checkout9004("ac8-1", f"SUPABASE_DB_URL={RW_A}\nSUPABASE_DB_URL_DIRECT={RW_B}\n")
tbl_co_2 = _checkout9004("ac8-2", f"SUPABASE_DB_URL={RW_A}\nSUPABASE_DB_URL_RO={RW_A}\n")
tbl_co_3 = _checkout9004("ac8-3", f"SUPABASE_DB_URL={RW_A}\nSUPABASE_DB_URL_DIRECT={RW_B}\n"
                                  f"SUPABASE_DB_URL_RO={RW_B}\n")
tbl_co_45 = _checkout9004("ac8-45", f"SUPABASE_DB_URL={RW_A}\nSUPABASE_DB_URL_DIRECT={RW_B}\n"
                                    "SUPABASE_DB_URL_RO=table-ro-distinct-9004\n")
tbl_dir_4 = _root9004("ac8-dir-4")
(tbl_dir_4 / ".git").mkdir()
tbl_rows = [
    ("no-ro", tbl_co_1, _root9004("ac8-dir-1")),
    ("eq-rw", tbl_co_2, _root9004("ac8-dir-2")),
    ("eq-direct", tbl_co_3, _root9004("ac8-dir-3")),
    ("valid-git", tbl_co_45, tbl_dir_4),
    ("valid-plain", tbl_co_45, _root9004("ac8-dir-5")),
]
written_nonempty = []
for label, co, dest in tbl_rows:
    r = dispatch.provision_sweep_env(dest, co)
    env_p = dest / ".env"
    content = env_p.read_bytes() if env_p.exists() else b""
    assert RW_A.encode() not in content and RW_B.encode() not in content, ("9004 AC8", label, content)
    if content:
        written_nonempty.append(label)
assert written_nonempty == ["valid-plain"], ("9004 AC8", written_nonempty)
assert (tbl_rows[4][2] / ".env").read_bytes() == b"SUPABASE_DB_URL=table-ro-distinct-9004\n"
N += 1

# ── AC9 · provision_worktree_env is never called for owed-sweeper's DSN path ──
_real_pwe9004 = dispatch.provision_worktree_env


def _pwe9004_boom(*_a, **_k):
    raise AssertionError("9004 AC9: provision_worktree_env must never be called for owed-sweeper")


dispatch.provision_worktree_env = _pwe9004_boom
SWEEP9004_9 = "L-owed-sweeper-9004ac9"
_sweep_manifest(SWEEP9004_9, [_os_row("L-owsw-9004ac9", "AC1")])
cwd9004_9 = _root9004("ac9-cwd")
OUT9004_9 = {"batch": SWEEP9004_9, "contamination": False, "escalations": [], "declarations": [],
             "results": [{"spec": "L-owsw-9004ac9", "criterion": "AC1", "verdict": "met", "evidence": "e"}]}
try:
    code9004_9, raw9004_9, seen9004_9 = drive("owed-sweeper", SWEEP9004_9, cwd9004_9, DSN_PROJECT, OUT9004_9)
finally:
    dispatch.provision_worktree_env = _real_pwe9004
ss9004_9 = next(e for e in raw9004_9 if e["type"] == "spawn-started")
assert ss9004_9.get("dsn_role") == "readonly", ss9004_9
N += 1

for p in _H9004:
    harness.cleanup(p)

print(f"dispatch: +L-spec-9004 (owed-sweeper-ro-credentials: AC1-AC9)")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0276 · defaults-and-dispatch-order (L-charter-0033) — R3 Target 4, R5
# ══════════════════════════════════════════════════════════════════════════════

# ── AC10 · wave-order refuses under a bare id, a path, or no --charter at all,
# identically; a killed OR shipped earlier-wave sibling unblocks it ───────────
plan_ch276 = TMP / "content" / "plan-CH276.md"
plan_ch276.write_text(
    "# Plan — CH276\n\n"
    "## unit-a\nGoal: g.\nFootprint:\n- src/x276.py\nWave: 1\n\n"
    "## unit-b\nGoal: g.\nFootprint:\n- src/y276.py\nWave: 2\n")
SW276 = TMP / "events" / "L-spec-writer-fx276a.jsonl"
dispatch.emit(SW276, {}, "spec-written", subject="L-spec-2761", charter="CH276", footprint=["src/x276.py"])
dispatch.emit(SW276, {}, "spec-written", subject="L-spec-2762", charter="CH276", footprint=["src/y276.py"])

for charter_arg in ("CH276", "/any/path/CH276.md", None):
    code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-2762", charter=charter_arg)
    assert code == 1 and cmd is None and evs[-1]["type"] == "spawn-failed" \
        and evs[-1].get("reason") == "wave-order", (charter_arg, code, types, evs)

dispatch.emit(SW276, {}, "shipped", subject="L-spec-2761")
for charter_arg in ("CH276", "/any/path/CH276.md", None):
    code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-2762", charter=charter_arg)
    assert code == 0 and cmd is not None, (charter_arg, code, types, evs)

# ── AC11 · a killed wave-1 sibling never blocks wave 2 ─────────────────────────
dispatch.emit(SW276, {}, "spec-written", subject="L-spec-2765", charter="CH276", footprint=["src/x276.py"])
dispatch.emit(SW276, {}, "spec-killed", subject="L-spec-2765", check=1)
dispatch.emit(SW276, {}, "spec-written", subject="L-spec-2763", charter="CH276", footprint=["src/y276.py"])
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-2763", charter="CH276")
assert code == 0 and cmd is not None, (code, types, evs)

# ── AC12 · wrong-project is refused before any spend ───────────────────────────
# A FRESH subject: L-spec-2762 already carries a "project" field from its own
# earlier successful `build-started` above (AC10's second loop, project="t",
# `spawn()`'s own default) — `subject_project` reads a subject's FIRST-ever
# project, so this must be a subject nothing has dispatched before.
SW276c = TMP / "events" / "L-spec-writer-fx276c.jsonl"
dispatch.emit(SW276c, {}, "spec-written", subject="L-spec-2767", project="acme276")
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-2767", project="other-project")
assert code == 1 and cmd is None and evs[-1]["type"] == "spawn-failed" \
    and evs[-1].get("reason") == "wrong-project", (code, types, evs)

# ── AC14 · no resolvable charter, or no plan file, is undetermined — never a
# refusal — and `build-started` carries `wave_note="undetermined"` ────────────
SW276b = TMP / "events" / "L-spec-writer-fx276b.jsonl"
dispatch.emit(SW276b, {}, "spec-written", subject="L-spec-2764", footprint=["src/z276.py"])
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-2764")
assert code == 0 and cmd is not None, (code, types, evs)
bs276 = next(e for e in evs if e["type"] == "build-started")
assert bs276.get("wave_note") == "undetermined", bs276
# ...and a resolved charter/wave that DID determine carries no wave_note at all.
# (L-spec-2762's own recorded project is "t" — `spawn()`'s default, stamped by
# its own first successful build-started in AC10's second loop above.)
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-2762", charter="CH276")
bs_det = next(e for e in evs if e["type"] == "build-started")
assert "wave_note" not in bs_det, bs_det

# ── AC15 (dispatch half) · validate.spec_shape_warnings monkeypatched onto the
# imported module object; one deduplicated spec-lint-warning per finding ──────
# L-spec-0321/R5 (SWP2, pre-existing at this spec's own pinned base): a
# spec-writer dispatch's `--path` must resolve to exactly `spec_path(subject)`
# or `main` refuses it before any spend (`write-path-mismatch`) — this fixture
# predates that check and named two arbitrary sibling files under `content/`,
# which the check has refused ever since (confirmed failing identically at
# this spec's own pinned base, outside Target 1-6's footprint — fixed here
# because AC14 requires every pre-existing assertion in this file to pass).
# Both dispatches below share one subject, so both now write the ONE
# canonical destination that subject resolves to.
import validate as validate276  # noqa: E402
_orig_warn276 = getattr(validate276, "spec_shape_warnings", None)
validate276.spec_shape_warnings = lambda text: ["PL-002: test finding"]
try:
    # both writes must resolve to dispatch.spec_path("L-spec-2766") — a distinctly
    # named fixture path here would now be refused before spend (write-path-mismatch,
    # L-spec-0321), so both reuse the one canonical destination for this subject.
    sp276a = dispatch.spec_path("L-spec-2766")
    code, types, evs, cmd = spawn("spec-writer", out={**sw, "spec_id": "L-spec-2766"}, path=sp276a,
                                  side=lambda: sp276a.write_text(WELL_FORMED_SPEC), subject="L-spec-2766")
    assert types.count("spec-lint-warning") == 1, types
    lw276 = next(e for e in evs if e["type"] == "spec-lint-warning")
    assert lw276["subject"] == "L-spec-2766" and lw276["finding"] == "PL-002: test finding", lw276

    sp276b = dispatch.spec_path("L-spec-2766")
    code2, types2, evs2, cmd2 = spawn("spec-writer", out={**sw, "spec_id": "L-spec-2766"}, path=sp276b,
                                      side=lambda: sp276b.write_text(WELL_FORMED_SPEC), subject="L-spec-2766")
    assert "spec-lint-warning" not in types2, \
        "AC15: an identical (subject, finding) pair is not appended twice"
finally:
    if _orig_warn276 is None:
        delattr(validate276, "spec_shape_warnings")
    else:
        validate276.spec_shape_warnings = _orig_warn276

print(f"dispatch: {N} spawns mocked, every check fired · +L-spec-0276 "
      "(defaults-and-dispatch-order: AC10-AC15)")

# ── L-spec-0435 (R10(b)/(c)) · dispatch.grading_budget, pure, plus the
#    role=grader pre-spend branch it gates ─────────────────────────────────────
_GB_BASE = _dt(2026, 1, 1, tzinfo=_timezone.utc)


def GT(mins):
    """A fixture timestamp `mins` minutes off a fixed base — used only by the
    grading_budget() pure-function tests below, which never compare against a
    real dispatch.main()-emitted ts, so any monotonic base will do."""
    return (_GB_BASE + _timedelta(minutes=mins)).isoformat(timespec="seconds")


def FT(mins):
    """A fixture timestamp `mins` real-clock minutes from now — gives a
    hand-written ledger fixture a well-ordered position relative to the REAL
    now() a live dispatch.main() drive stamps its own events with."""
    return (_dt.now(_timezone.utc) + _timedelta(minutes=mins)).isoformat(timespec="seconds")


def gb(actor, type_, subject, ts, **kv):
    """One event dict, shaped exactly as fold.read_events() hands grading_budget
    one (`actor` present, `ts` an ISO string) — grading_budget takes a plain
    list, so no ledger file is needed for these."""
    return {"type": type_, "actor": actor, "subject": subject, "ts": ts, **kv}


def raw_ev(actor, tag, type_, subject, ts, **kv):
    """One raw ledger line, written directly (bypassing dispatch.emit, which
    always stamps ts=now()) — these tests need exact control over a fixture
    event's ts to prove the freshness comparisons the budget check makes.
    `actor` is read back from the filename by fold.read_events() (D90), same
    as every real event."""
    f = TMP / "events" / f"L-{actor}-{tag}.jsonl"
    with open(f, "a") as fh:
        fh.write(json.dumps({"v": 1, "ts": ts, "type": type_, "subject": subject, **kv}) + "\n")
    return f


# AC3: no rejects, at most 2 grader spawn-started rows -> None.
ac3_subj435 = "L-spec-9403"
ev3_435 = [gb("grader", "spawn-started", ac3_subj435, GT(0), role="grader"),
           gb("grader", "spawn-started", ac3_subj435, GT(1), role="grader")]
assert dispatch.grading_budget(ev3_435, ac3_subj435) is None, "AC3: <=2 grader spawns, no rejects -> None"
N += 1

# AC4: two rejected-criterion(AC2) rows, different why, no clearance, no reset
# newer than the 2nd -> repeat-rejection:AC2 regardless of the why differing.
ac4_subj435 = "L-spec-9404"
ev4_435 = [gb("grader", "rejected-criterion", ac4_subj435, GT(0), criterion="AC2",
              why="Evidence missing for the retry path"),
           gb("grader", "rejected-criterion", ac4_subj435, GT(10), criterion="AC2",
              why="the retry-path evidence isn't attached")]
assert dispatch.grading_budget(ev4_435, ac4_subj435) == "repeat-rejection:AC2", ev4_435
N += 1

# AC5: a build-done (a) strictly between, (b) strictly after -> unchanged in
# BOTH cases; a fresh spec-written, or ANY thinker decision (regrade or not),
# after the 2nd resets it; a builder's regrade=yes decision (wrong actor) does
# not.
ac5_subj435 = "L-spec-9405"
base5_435 = [gb("grader", "rejected-criterion", ac5_subj435, GT(0), criterion="AC2", why="w1"),
             gb("grader", "rejected-criterion", ac5_subj435, GT(10), criterion="AC2", why="w2")]
ev5a_435 = [base5_435[0], gb("builder", "build-done", ac5_subj435, GT(5), status="DONE"), base5_435[1]]
assert dispatch.grading_budget(ev5a_435, ac5_subj435) == "repeat-rejection:AC2", "AC5a: build-done between"
ev5b_435 = base5_435 + [gb("builder", "build-done", ac5_subj435, GT(20), status="DONE")]
assert dispatch.grading_budget(ev5b_435, ac5_subj435) == "repeat-rejection:AC2", "AC5b: build-done after"
ev5c_435 = base5_435 + [gb("spec-writer", "spec-written", ac5_subj435, GT(20))]
assert dispatch.grading_budget(ev5c_435, ac5_subj435) is None, "AC5: a fresh spec-written after the 2nd resets it"
ev5d_435 = base5_435 + [gb("thinker", "decision", ac5_subj435, GT(20))]
assert dispatch.grading_budget(ev5d_435, ac5_subj435) is None, "AC5: any thinker decision resets it, regrade or not"
ev5e_435 = base5_435 + [gb("builder", "decision", ac5_subj435, GT(20), regrade="yes")]
assert dispatch.grading_budget(ev5e_435, ac5_subj435) == "repeat-rejection:AC2", \
    "AC5: a builder's decision (wrong actor) never resets it"
N += 5

# AC6: L-spec-0173's real shape, replayed verbatim — three grader verdict
# rounds, each its own build-done, why distinct every round, no
# criterion-cleared ever -> repeat-rejection:AC1 after the third round (the
# pre-rewrite why-text-matching definition would return None here).
ac6_subj435 = "L-spec-9406"
ev6_435, t6_435 = [], 0
for why in ("round one's own reason", "a completely different wording", "yet another distinct reason"):
    ev6_435.append(gb("builder", "build-done", ac6_subj435, GT(t6_435), status="DONE")); t6_435 += 1
    ev6_435.append(gb("grader", "spawn-started", ac6_subj435, GT(t6_435), role="grader")); t6_435 += 1
    ev6_435.append(gb("grader", "verdict", ac6_subj435, GT(t6_435), confirmed=False)); t6_435 += 1
    ev6_435.append(gb("grader", "rejected-criterion", ac6_subj435, GT(t6_435), criterion="AC1", why=why))
    t6_435 += 1
assert dispatch.grading_budget(ev6_435, ac6_subj435) == "repeat-rejection:AC1", ev6_435
N += 1

# AC7: exactly 3 grader spawn-started rows, no qualifying regrade -> cap:3;
# operator regrade=yes newer than the 3rd -> None; builder regrade=yes (wrong
# actor), or a thinker decision with no regrade field, never lifts it.
ac7_subj435 = "L-spec-9407"
ev7_435 = [gb("grader", "spawn-started", ac7_subj435, GT(i), role="grader") for i in range(3)]
assert dispatch.grading_budget(ev7_435, ac7_subj435) == "cap:3", "AC7: 3 prior runs, no regrade -> cap:3"
assert dispatch.grading_budget(ev7_435 + [gb("operator", "decision", ac7_subj435, GT(10), regrade="yes")],
                               ac7_subj435) is None, "AC7: a qualifying operator regrade lifts it"
assert dispatch.grading_budget(ev7_435 + [gb("builder", "decision", ac7_subj435, GT(10), regrade="yes")],
                               ac7_subj435) == "cap:3", "AC7: wrong actor never lifts it"
assert dispatch.grading_budget(ev7_435 + [gb("thinker", "decision", ac7_subj435, GT(10))],
                               ac7_subj435) == "cap:3", "AC7: a decision with no regrade field never lifts it"
N += 4

# AC8: 4 grader spawn-started rows (a regrade already permitted the 4th), no
# regrade newer than the 4th -> cap:4 — the rule reapplies past the literal
# fourth run.
ac8_subj435 = "L-spec-9408"
ev8_435 = [gb("grader", "spawn-started", ac8_subj435, GT(i), role="grader") for i in range(3)]
ev8_435.append(gb("operator", "decision", ac8_subj435, GT(3), regrade="yes"))
ev8_435.append(gb("grader", "spawn-started", ac8_subj435, GT(4), role="grader"))
assert dispatch.grading_budget(ev8_435, ac8_subj435) == "cap:4", ev8_435
N += 1


def _boom435(*a, **k):
    raise AssertionError("a backend must not be entered for a refused grading-budget dispatch")


def drive_grader435(subject):
    """Drives dispatch.main() for role=grader with every backend monkeypatched
    to explode if entered (AC9) — the refusal this proves must land strictly
    before any of them is ever called. Returns (exit code, this drive's own new
    ledger file's parsed events, that file's path)."""
    real = dispatch.run_claude, dispatch.run_seat, dispatch.run_codex
    dispatch.run_claude = dispatch.run_seat = dispatch.run_codex = _boom435
    a = argparse.Namespace(role="grader", subject=subject, packet=str(PK), path=None,
                          cwd=str(REPO), charter=None, project="t", mcp_config=None,
                          timeout=None, max_usd=None)
    try:
        dispatch.main(a)
        code = 0
    except SystemExit as e:
        code = e.code
    finally:
        dispatch.run_claude, dispatch.run_seat, dispatch.run_codex = real
    f = max((TMP / "events").glob("L-grader-[0-9]*.jsonl"))
    return code, [json.loads(l) for l in f.read_text().splitlines()], f


# AC9: a subject with exactly 3 prior grader spawn-started rows, no qualifying
# regrade -> main() raises SystemExit before any backend, THIS spawn's own
# newly-allocated ledger file carries exactly [escalation-blocking,
# spawn-failed], reason=="cap:3", and no other file gains a new event.
AC9_SUBJ435 = "L-spec-9409"
_fx9a, _fx9b, _fx9c = (raw_ev("grader", f"fx9409{s}", "spawn-started", AC9_SUBJ435, FT(o), role="grader")
                       for s, o in (("a", -500), ("b", -499), ("c", -498)))
_pre9_435 = {f: f.read_text() for f in (_fx9a, _fx9b, _fx9c)}
code9_435, raw9_435, f9_435 = drive_grader435(AC9_SUBJ435)
assert code9_435 == 1, raw9_435
assert [e["type"] for e in raw9_435] == ["escalation-blocking", "spawn-failed"], raw9_435
assert raw9_435[1]["reason"] == "cap:3", raw9_435[1]
for f, content in _pre9_435.items():
    assert f.read_text() == content, f"AC9: {f} must gain no new event"
N += 1

# AC10: the escalation-blocking's own shape — kind, reason, default, revert, a
# deadline 23h55m-24h05m after its own ts, actor==grader (from its filename),
# and fold.escalation_ok() true.
_full9_435 = fold.read_events()
e10_435 = next(e for e in _full9_435 if e.get("subject") == AC9_SUBJ435 and e["type"] == "escalation-blocking")
assert e10_435["actor"] == "grader", e10_435
assert e10_435["kind"] == "grading-budget" and e10_435["reason"] == "cap:3", e10_435
assert e10_435["default"] == "no further grade; rework with the standing reasons, or the Thinker amends the spec"
assert e10_435["revert"] == "a decision regrade=yes"
_delta_h_435 = (fold.ts(e10_435["deadline"]) - fold.ts(e10_435["ts"])).total_seconds() / 3600
assert 23 + 55 / 60 <= _delta_h_435 <= 24 + 5 / 60, _delta_h_435
assert fold.escalation_ok(e10_435), e10_435
N += 1

# AC11: a SECOND drive on the SAME subject (still cap:3, no qualifying
# decision) — its own second ledger file appends a second spawn-failed but NO
# second escalation-blocking; reading the FULL event set (both files
# together), exactly one escalation-blocking{kind, reason} exists.
code11_435, raw11_435, f11_435 = drive_grader435(AC9_SUBJ435)
assert code11_435 == 1 and [e["type"] for e in raw11_435] == ["spawn-failed"], raw11_435
assert raw11_435[0]["reason"] == "cap:3", raw11_435[0]
_full11_435 = fold.read_events()
_esc11_435 = [e for e in _full11_435 if e.get("subject") == AC9_SUBJ435 and e["type"] == "escalation-blocking"
             and e.get("kind") == "grading-budget" and e.get("reason") == "cap:3"]
assert len(_esc11_435) == 1, "AC11: exactly one escalation-blocking across both drives"
N += 1

# AC12: the AC7 "lifted" fixture (a qualifying regrade already permits a 4th
# run) driven through main() with a normal successful grader response — the
# spawn completes (spawn-started/verdict/spawn-done), code 0, proving the
# check does not misfire once the condition is genuinely cleared.
AC12_SUBJ435 = "L-spec-9412"
for _s, _o in (("a", -500), ("b", -499), ("c", -498)):
    raw_ev("grader", f"fx9412{_s}", "spawn-started", AC12_SUBJ435, FT(_o), role="grader")
raw_ev("operator", "fx9412d", "decision", AC12_SUBJ435, FT(-100), regrade="yes")
code12_435, types12_435, evs12_435, _ = spawn("grader", out=grade([met]), subject=AC12_SUBJ435)
assert code12_435 == 0, evs12_435
assert "verdict" in types12_435 and "spawn-done" in types12_435, types12_435
assert "spawn-started" in [e["type"] for e in spawn.raw], spawn.raw
N += 1

# AC14: the AC9 standing escalation-blocking{cap:3}, resolved by an unrelated
# decision (regrade=no — never lifts the cap itself) newer than it — grading_budget
# still returns cap:3, AND this THIRD drive's own ledger file carries a SECOND,
# FRESH escalation-blocking{cap:3} (not suppressed by the first, now-resolved
# one) — a resolved-but-not-fixed block re-surfaces rather than wedging silent.
# L-spec-0482/R12.c superseded this block's own prior `fx9414b` build-done (it
# used to assert a build-done changes nothing here — R12.c's whole point is
# that it now does): removed, so this drive still sees only the 3 spawn-started
# rows plus the harmless `decision regrade=no`, and cap:3 still stands.
_dec_ts_435 = (fold.ts(e10_435["ts"]) + _timedelta(minutes=5)).isoformat(timespec="seconds")
raw_ev("operator", "fx9414a", "decision", AC9_SUBJ435, _dec_ts_435, regrade="no")
code14_435, raw14_435, f14_435 = drive_grader435(AC9_SUBJ435)
assert code14_435 == 1 and [e["type"] for e in raw14_435] == ["escalation-blocking", "spawn-failed"], raw14_435
assert raw14_435[0]["kind"] == "grading-budget" and raw14_435[0]["reason"] == "cap:3", raw14_435[0]
assert raw14_435[1]["reason"] == "cap:3", raw14_435[1]
N += 1

# L-spec-0482/R12.c (new): a build-done newer than every counted run resets the
# cap outright now — appended strictly after the third drive above, so it
# changes nothing about the assertions just made.
raw_ev("builder", "fx9414c482", "build-done", AC9_SUBJ435, FT(1), status="DONE")
assert dispatch.grading_budget(fold.read_events(), AC9_SUBJ435) is None, \
    "R12.c: a build-done newer than the newest counted run resets the cap"
N += 1

# AC13: agents/executor.md's spawn-failed/spawn-stale row states both reset
# paths, verbatim in substance, each naming its own — never the other's.
_exec_md_435 = (pathlib.Path(__file__).parent.parent / "agents" / "executor.md").read_text()
_row_435 = next(l for l in _exec_md_435.splitlines() if "spawn-stale" in l and "unserved, timeout" in l)
assert "repeat-rejection:" in _row_435 and "cap:" in _row_435, _row_435
assert "fresh `spec-written`" in _row_435 and "thinker`/`operator`" in _row_435 and \
    "regrade or not" in _row_435, "AC13: repeat-rejection's reset path, stated"
assert 'decision{regrade: "yes"}' in _row_435, "AC13: cap's own, narrower reset path, stated"
N += 1

print(f"dispatch: +L-spec-0435 (grading-spend: AC3-AC14)")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0482 (grading-breaker) — R12.b circuit breaker, R12.c cap reset,
# R12.e executor rule. AC1-AC9, AC15, AC16. `grading_env` already imported
# above (L-spec-0481 section); `_cap_content` reused for AC3's second spec.
# ══════════════════════════════════════════════════════════════════════════════

# AC1: schema enum equals grading_env.CAPABILITIES + "unknown"; grader.md states
# the rule; doit validate grader on four fixtures (met/no-field, tool-failed
# with a valid cap, tool-failed with the field absent, tool-failed with a
# bogus value — only the last is INVALID).
_schema482 = json.loads((pathlib.Path(__file__).parent.parent / "agents" / "grader.schema.json").read_text())
_cap_enum482 = _schema482["properties"]["verdicts"]["items"]["properties"]["missing_capability"]["enum"]
assert set(_cap_enum482) == set(grading_env.CAPABILITIES) | {"unknown"}, _cap_enum482
_grader_md482 = (pathlib.Path(__file__).parent.parent / "agents" / "grader.md").read_text()
assert "missing_capability" in _grader_md482, "AC1: grader.md must name the field"

import validate as validate482  # noqa: E402
_VDIR482 = TMP / "content" / "validate-fixtures-482"
_VDIR482.mkdir(parents=True, exist_ok=True)


def _grade_row482(**kw):
    return {"verdicts": [{"ac": "AC1", "verdict": "met", "reason": "r", **kw}],
            "matches_intent": "yes", "card_ok": "yes", "could_not_run": False,
            "contamination": False, "declarations": [], "checkers": []}


def _validate482(obj, name):
    p = _VDIR482 / f"{name}.json"
    p.write_text(json.dumps(obj))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            validate482.main(["grader", str(p)])
        return 0, buf.getvalue()
    except SystemExit as e:
        return (e.code if isinstance(e.code, int) else 1), str(e.code)


c1_482, o1_482 = _validate482(_grade_row482(), "met-no-field")
assert "VALID" in o1_482 and c1_482 == 0, (c1_482, o1_482)
c2_482, o2_482 = _validate482({**_grade_row482(), "verdicts": [
    {"ac": "AC1", "verdict": "cannot-assess", "reason": "r", "reason_code": "tool-failed",
     "missing_capability": "db"}]}, "tool-failed-valid")
assert "VALID" in o2_482 and c2_482 == 0, (c2_482, o2_482)
c3_482, o3_482 = _validate482({**_grade_row482(), "verdicts": [
    {"ac": "AC1", "verdict": "cannot-assess", "reason": "r", "reason_code": "tool-failed"}]}, "tool-failed-absent")
assert "VALID" in o3_482 and c3_482 == 0, (c3_482, o3_482)
c4_482, o4_482 = _validate482({**_grade_row482(), "verdicts": [
    {"ac": "AC1", "verdict": "cannot-assess", "reason": "r", "reason_code": "tool-failed",
     "missing_capability": "nonsense"}]}, "tool-failed-bogus")
assert c4_482 != 0 and "missing_capability" in o4_482, (c4_482, o4_482)
print("L-spec-0482 AC1 ok")

# Small fixture-row builders, reused across AC2-AC16 below.
_ca482 = lambda ac, cap=None, **kw: {"ac": ac, "verdict": "cannot-assess", "reason": "r",
                                     "reason_code": "tool-failed",
                                     **({"missing_capability": cap} if cap is not None else {}), **kw}

# AC2: a non-owed cannot-assess row (AC1, capability db) rides on the verdict
# event; an owed one (AC2, capability browser, declared owed) does not; a `met`
# row (AC3) is untouched. A second fixture: an executor-authored owed-ac with
# no prior declaration never suppresses.
# (uses "env-file"/"browser", never "db" — AC3/AC4 below open and reuse the
# shared capability:db hold across dispatches, and must start from it closed)
subj_ac2_482 = "L-spec-9450"
raw_ev("spec-writer", "fx482ac2", "owed-ac", subj_ac2_482, GT(0), criterion="AC2")
met_ac3_482 = {"ac": "AC3", "verdict": "met", "reason": "r"}
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC1", "env-file"), _ca482("AC2", "browser"),
                                                  met_ac3_482]), subject=subj_ac2_482)
vev2_482 = next(e for e in evs if e["type"] == "verdict")
assert vev2_482["missing_capability"] == [{"ac": "AC1", "capability": "env-file"}], vev2_482
assert set(vev2_482["cannot_assess"]) == {"AC1", "AC2"}, vev2_482

subj_ac2b_482 = "L-spec-9451"
# A subject's own admitted history must be non-empty before the sole event on
# it is one `fold()` ignores (a bare executor owed-ac with no prior
# declaration) — an all-ignored subject is a pre-existing `fold.fold()` crash
# (IndexError on `evs[-1]`) outside this spec's footprint; sidestepped here by
# giving the subject one harmless admitted event first.
raw_ev("spec-writer", "fx482ac2bpre", "spec-written", subj_ac2b_482, GT(-1))
raw_ev("executor", "fx482ac2b", "owed-ac", subj_ac2b_482, GT(0), criterion="AC4")
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC4", "env-file")]), subject=subj_ac2b_482)
vev2b_482 = next(e for e in evs if e["type"] == "verdict")
assert vev2b_482["missing_capability"] == [{"ac": "AC4", "capability": "env-file"}], \
    "AC2: an executor-authored owed-ac with no prior declaration never suppresses"
print("L-spec-0482 AC2 ok")

# AC3: the store round trip, through the production events_for/emit path — a
# fresh fold.read_events() shows both the hold and the escalation, and a
# SECOND spec whose content requires `db` is held by the shared shape.
ac3_second_482 = "L-spec-9452"
_cap_content(ac3_second_482, "pytest -k live_db\n")
subj_ac3_482 = "L-spec-9453"
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC1", "db")]), subject=subj_ac3_482)
assert "capability-hold" in types and "escalation-blocking" in types, types
hold3_482 = next(e for e in evs if e["type"] == "capability-hold")
# Operator ruling 2026-10-04: a verdict-sourced hold is spec-local for every
# capability — one grader's cannot-assess never holds another spec's grade.
L3 = f"capability:db:{subj_ac3_482}"
assert (hold3_482["subject"], hold3_482["capability"], hold3_482["spec"], hold3_482["source"]) == \
    (L3, "db", subj_ac3_482, "verdict"), hold3_482
esc3_482 = next(e for e in evs if e["type"] == "escalation-blocking")
assert esc3_482["subject"] == L3 and esc3_482["kind"] == "capability-hold" \
    and esc3_482["owner"] == "thinker" and esc3_482["revert"] == f"doit append unblocked {L3}", esc3_482
assert esc3_482.get("default") and esc3_482.get("deadline"), esc3_482
fresh3_482 = fold.read_events()
assert L3 in fold.capability_holds(fresh3_482), fold.capability_holds(fresh3_482)
assert "capability:db" not in fold.capability_holds(fresh3_482), fold.capability_holds(fresh3_482)
assert ac3_second_482 not in fold.held_specs(fresh3_482), \
    "a sibling spec needing db is NOT held by another spec's verdict"
print("L-spec-0482 AC3 ok (spec-local)")

# AC4: the SAME spec's second cannot-assess on an already-held capability opens
# nothing further; after `unblocked` the next one opens a fresh hold; two distinct
# capabilities in one verdict open two (spec-local) holds.
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC1", "db")]), subject=subj_ac3_482)
assert "capability-hold" not in types and "escalation-blocking" not in types, \
    "AC4: an already-open hold on this spec gets no second capability-hold/escalation"
raw_ev("operator", "fx482ac4unblock", "unblocked", L3, FT(1))
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC1", "db")]), subject=subj_ac3_482)
assert "capability-hold" in types and "escalation-blocking" in types, \
    "AC4: after an unblocked close, the next non-owed cannot-assess opens a fresh hold"
subj_ac4c_482 = "L-spec-9456"
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC1", "browser"), _ca482("AC2", "review-account")]),
                           subject=subj_ac4c_482)
hold_subjs_482 = {e["subject"] for e in evs if e["type"] == "capability-hold"}
assert hold_subjs_482 == {f"capability:browser:{subj_ac4c_482}", f"capability:review-account:{subj_ac4c_482}"}, \
    hold_subjs_482
print("L-spec-0482 AC4 ok (spec-local)")

# AC5: absent / literal "unknown" / an out-of-enum value all coerce to
# capability:unknown:<SPEC>, spec=<SPEC>; a bogus value fails the grader's own
# schema well before events_for, so it is exercised directly (same pattern the
# COMMIT-SHAPE AC13 block above uses for an unreachable-through-main() case).
subj_ac5a_482 = "L-spec-9460"
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC1")]), subject=subj_ac5a_482)  # field absent
hold5a_482 = next(e for e in evs if e["type"] == "capability-hold")
assert hold5a_482["subject"] == f"capability:unknown:{subj_ac5a_482}" and hold5a_482["spec"] == subj_ac5a_482, hold5a_482
assert fold.capability_holds(fold.read_events())[hold5a_482["subject"]]["specs"] == {subj_ac5a_482}
assert subj_ac5a_482 in fold.held_specs(fold.read_events())

subj_ac5b_482 = "L-spec-9461"
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC1", "unknown")]), subject=subj_ac5b_482)
hold5b_482 = next(e for e in evs if e["type"] == "capability-hold")
assert hold5b_482["subject"] == f"capability:unknown:{subj_ac5b_482}", hold5b_482

subj_ac5c_482 = "L-spec-9462"
a5c_482 = argparse.Namespace(subject=subj_ac5c_482)
base5c_482 = {"subject": subj_ac5c_482, "project": "t", "spawn": "L-grader-fake5c482"}
ev5c_482 = dispatch.events_for("grader", grade([_ca482("AC1", "nonsense-cap")]), a5c_482, base5c_482)
hold5c_482 = next(kv for t, kv in ev5c_482 if t == "capability-hold")
assert hold5c_482["subject"] == f"capability:unknown:{subj_ac5c_482}" and hold5c_482["spec"] == subj_ac5c_482, hold5c_482
print("L-spec-0482 AC5 ok")

# AC6: cannot-assess rows that are ALL owed open no hold, missing_capability is
# [], and verdict_confirmed is unchanged (True when matches_intent/card_ok yes).
subj_ac6_482 = "L-spec-9470"
raw_ev("spec-writer", "fx482ac6", "owed-ac", subj_ac6_482, GT(0), criterion="AC1")
code, types, evs, _ = spawn("grader", out=grade([{"ac": "AC1", "verdict": "cannot-assess", "reason": "r",
                                                  "reason_code": "criterion-unevaluable-from-packet"}]),
                          subject=subj_ac6_482)
assert "capability-hold" not in types and "escalation-blocking" not in types, types
vev6_482 = next(e for e in evs if e["type"] == "verdict")
assert vev6_482["missing_capability"] == [], vev6_482
assert fold.verdict_confirmed(vev6_482, {"AC1"}) is True, vev6_482
print("L-spec-0482 AC6 ok")

# AC7: grading_budget's build-done-gated window — 3 runs older than the
# newest build-done never count (None); 3 newer do (cap:3); no build-done at
# all still counts every run (cap:3, unchanged from before this unit).
ac7_subj482 = "L-spec-9480"
ev7a482 = [gb("grader", "spawn-started", ac7_subj482, GT(0), role="grader"),
          gb("grader", "verdict", ac7_subj482, GT(1), confirmed=False, cannot_assess=[]),
          gb("grader", "rejected-criterion", ac7_subj482, GT(1), criterion="AC1", why="w"),
          gb("grader", "spawn-started", ac7_subj482, GT(2), role="grader"),
          gb("grader", "verdict", ac7_subj482, GT(3), confirmed=False, cannot_assess=[]),
          gb("grader", "rejected-criterion", ac7_subj482, GT(3), criterion="AC2", why="w"),
          gb("grader", "spawn-started", ac7_subj482, GT(4), role="grader"),
          gb("grader", "verdict", ac7_subj482, GT(5), confirmed=False, cannot_assess=[]),
          gb("grader", "rejected-criterion", ac7_subj482, GT(5), criterion="AC3", why="w"),
          gb("builder", "build-done", ac7_subj482, GT(10), status="DONE")]
assert dispatch.grading_budget(ev7a482, ac7_subj482) is None, dispatch.grading_budget(ev7a482, ac7_subj482)

ev7b482 = [gb("builder", "build-done", ac7_subj482, GT(0), status="DONE"),
          gb("grader", "spawn-started", ac7_subj482, GT(1), role="grader"),
          gb("grader", "spawn-started", ac7_subj482, GT(2), role="grader"),
          gb("grader", "spawn-started", ac7_subj482, GT(3), role="grader")]
assert dispatch.grading_budget(ev7b482, ac7_subj482) == "cap:3", dispatch.grading_budget(ev7b482, ac7_subj482)

ev7c482 = [gb("grader", "spawn-started", ac7_subj482, GT(i), role="grader") for i in range(3)]
assert dispatch.grading_budget(ev7c482, ac7_subj482) == "cap:3", dispatch.grading_budget(ev7c482, ac7_subj482)
print("L-spec-0482 AC7 ok")

# AC8: 4 grader runs after the newest build-done, each carrying a non-owed
# cannot-assess -> None; all 4 owed (spec-writer-declared) -> cap:4; only an
# executor-authored owed-ac with no prior declaration -> still None.
ac8_subj482 = "L-spec-9481"


def _mk_run482(i, criterion):
    return [gb("grader", "spawn-started", ac8_subj482, GT(i * 2), role="grader", spawn=f"g482{i}"),
            gb("grader", "verdict", ac8_subj482, GT(i * 2 + 1), spawn=f"g482{i}", confirmed=False,
               cannot_assess=[criterion])]


_build8_482 = gb("builder", "build-done", ac8_subj482, GT(-1), status="DONE")
_runs8_482 = [ev for i, c in enumerate(("AC1", "AC2", "AC3", "AC4")) for ev in _mk_run482(i, c)]
ev8a_482 = [_build8_482] + _runs8_482
assert dispatch.grading_budget(ev8a_482, ac8_subj482) is None, dispatch.grading_budget(ev8a_482, ac8_subj482)

_owed8_482 = [gb("spec-writer", "owed-ac", ac8_subj482, GT(-2), criterion=c)
             for c in ("AC1", "AC2", "AC3", "AC4")]
ev8b_482 = _owed8_482 + ev8a_482
assert dispatch.grading_budget(ev8b_482, ac8_subj482) == "cap:4", dispatch.grading_budget(ev8b_482, ac8_subj482)

_owed8c_482 = [gb("executor", "owed-ac", ac8_subj482, GT(-2), criterion=c)
              for c in ("AC1", "AC2", "AC3", "AC4")]
ev8c_482 = _owed8c_482 + ev8a_482
assert dispatch.grading_budget(ev8c_482, ac8_subj482) is None, dispatch.grading_budget(ev8c_482, ac8_subj482)
print("L-spec-0482 AC8 ok")

# AC9: regression — the pre-existing L-spec-0435 suite above (AC3-AC12, AC14's
# rewritten block) is unmodified in behavior except the one block R12.c names;
# proved by this whole file exiting 0 (checked by the Verification command)
# and by `git diff` on this file showing no other pre-existing line touched.
print("L-spec-0482 AC9 ok (see: this file's own full run, plus git diff of the AC14 block)")

# AC15: agents/executor.md's cap: clause names the build-done reset AND the
# decision{regrade: "yes"} lift, while still carrying every substring the
# pre-existing 0435 `# AC13:` block already reads (`_row_435`, computed above).
assert "fresh `build-done` newer than the newest counted grader run" in _row_435, _row_435
assert "re-dispatches" in _row_435, _row_435
assert 'decision{regrade: "yes"}' in _row_435, _row_435
print("L-spec-0482 AC15 ok")

# AC16: the omitted-field path end to end through dispatch.main — passes
# schema validation, is not spawn-failed, opens capability:unknown:<SPEC>, and
# three such runs never count against the cap (grading_budget stays None).
subj_ac16_482 = "L-spec-9490"
row16_482 = {"ac": "AC1", "verdict": "cannot-assess", "reason": "r", "reason_code": "tool-failed"}
code16_482, types16_482, evs16_482, _ = spawn("grader", out=grade([row16_482]), subject=subj_ac16_482)
assert code16_482 == 0 and "spawn-failed" not in types16_482, (code16_482, types16_482)
vev16_482 = next(e for e in evs16_482 if e["type"] == "verdict")
assert vev16_482["missing_capability"] == [{"ac": "AC1", "capability": "unknown"}], vev16_482
hold16_482 = next(e for e in evs16_482 if e["type"] == "capability-hold")
assert hold16_482["subject"] == f"capability:unknown:{subj_ac16_482}", hold16_482
assert "escalation-blocking" in types16_482, types16_482
for _ in range(2):
    spawn("grader", out=grade([row16_482]), subject=subj_ac16_482)
assert dispatch.grading_budget(fold.read_events(), subj_ac16_482) is None, \
    "AC16: three environment-failure runs never count against the cap"
print("L-spec-0482 AC16 ok")

print("dispatch: +L-spec-0482 (grading-breaker: AC1-AC9 AC15 AC16)")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-8034 (capability-routing) — AC6 (dispatch records routing before spend,
# actor grader, non-seat grader route; never refused for a routed capability)
# and AC7 (events_for never derives criterion-routed from a grader Output)
# ══════════════════════════════════════════════════════════════════════════════
SUBJ_8034_6 = "L-spec-9472"
dispatch.CONTENT.mkdir(parents=True, exist_ok=True)
SPEC_8034_6 = dispatch.CONTENT / f"{SUBJ_8034_6}.md"
SPEC_8034_6.write_text(
    "## Acceptance criteria\n\nAC1 [ui]: browser criterion.\nreview_path: log in as x, go to /y\n\n"
    "AC2 [backend]: plain criterion.\nreview_path: nothing special here.\n")
met_8034 = {"ac": "AC2", "verdict": "met", "reason": "r"}

code6a, types6a, evs6a, _ = spawn("grader", out=grade([met_8034]), subject=SUBJ_8034_6)
assert code6a == 0 and "spawn-failed" not in types6a, (code6a, types6a, evs6a)
routed6a = [e for e in evs6a if e["type"] == "criterion-routed"]
assert len(routed6a) == 1 and routed6a[0]["criterion"] == "AC1" and routed6a[0]["capability"] == "browser" \
    and routed6a[0]["to"] == "owed", routed6a
raw6a_types = [e["type"] for e in spawn.raw]
assert raw6a_types.index("criterion-routed") < raw6a_types.index("spawn-started"), raw6a_types
N += 1

# a second, unchanged dispatch appends no new criterion-routed row
code6b, types6b, evs6b, _ = spawn("grader", out=grade([met_8034]), subject=SUBJ_8034_6)
assert code6b == 0 and not any(t == "criterion-routed" for t in types6b), types6b
N += 1

# rework: the browser criterion is dropped -> the next dispatch appends to=none
SPEC_8034_6.write_text(
    "## Acceptance criteria\n\nAC2 [backend]: plain criterion.\nreview_path: nothing special here.\n")
code6c, types6c, evs6c, _ = spawn("grader", out=grade([met_8034]), subject=SUBJ_8034_6)
routed6c = [e for e in evs6c if e["type"] == "criterion-routed"]
assert len(routed6c) == 1 and routed6c[0]["criterion"] == "AC1" and routed6c[0]["to"] == "none", routed6c
print("8034-AC6 ok (non-seat grader route)")

# AC6, seat route: the same routing computation runs before either grader
# refusal branch (dispatch.main's top `if a.role == "grader":` block, outside
# the grader_seat/non-seat split) — proven end to end via the seat backend too.
SUBJ_8034_6S = "L-spec-9471"
_gv_build_done(SUBJ_8034_6S, base_sha="B1", ready_sha="R1")
(dispatch.CONTENT / f"{SUBJ_8034_6S}.md").write_text(
    "## Acceptance criteria\n\nAC1 [ui]: browser criterion.\nreview_path: log in as x, go to /y\n\n"
    "AC2 [backend]: plain criterion.\nreview_path: nothing special here.\n")
_seat_view_6 = pathlib.Path(tempfile.mkdtemp())
code6s, raw6s = _gv_drive(SUBJ_8034_6S, view_build=lambda *a: _seat_view_6, serve_out=grade([met_8034]))
assert code6s == 0 and not any(e["type"] == "spawn-failed" for e in raw6s), raw6s
routed6s = [e for e in raw6s if e["type"] == "criterion-routed"]
assert len(routed6s) == 1 and routed6s[0]["criterion"] == "AC1" and routed6s[0]["to"] == "owed", raw6s
assert not any(e["type"] in ("grading-preflight-failed", "capability-hold") for e in raw6s), raw6s
print("8034-AC6 ok (seat grader route)")

# AC7 (events_for half): dispatch never derives a criterion-routed from a
# grader Output, whatever its fields look like.
ev_for_7 = dispatch.events_for("grader", grade([met_8034]), argparse.Namespace(subject=SUBJ_8034_6),
                               {"subject": SUBJ_8034_6})
assert not any(t == "criterion-routed" for t, _ in ev_for_7), ev_for_7
print("8034-AC7(events_for) ok")

# ══════════════════════════════════════════════════════════════════════════
# L-spec-0486 (R15d) · dispatch.ensure_room / _room_need_gb, plus the
# role=builder/grader pre-spend gate they back
# ══════════════════════════════════════════════════════════════════════════
import reap_tmp as _reap_tmp486  # noqa: E402

_orig_du486 = shutil.disk_usage
_orig_reap_now_486 = _reap_tmp486.reap_now


def _du_row486(total_gb, used_gb, free_gb):
    du = _orig_du486(str(TMP))
    return type(du)(total=int(total_gb * 2**30), used=int(used_gb * 2**30), free=int(free_gb * 2**30))


# AC12a: 0 bytes needed is always enough; the reaper never runs.
_reap_calls486 = []
_reap_tmp486.reap_now = lambda dry_run=False: (_reap_calls486.append(dry_run), {})[1]
try:
    assert _real_ensure_room_486(str(TMP), 0) is True, "AC12: 0 bytes needed is always enough"
    assert _reap_calls486 == [], f"AC12: the reaper is not called when there's already enough room: {_reap_calls486}"

    # too little, then enough right after the reaper "runs"
    _state486 = {"n": 0}

    def _du_low_then_high486(path):
        _state486["n"] += 1
        return _du_row486(100, 95, 0 if _state486["n"] == 1 else 10)

    shutil.disk_usage = _du_low_then_high486
    _reap_calls486.clear()
    assert _real_ensure_room_486(str(TMP), 5) is True, "AC12: too little then enough after the reaper -> True"
    assert len(_reap_calls486) == 1, f"AC12: the reaper ran exactly once: {_reap_calls486}"

    # stays too little
    shutil.disk_usage = lambda path: _du_row486(100, 99, 0)
    _reap_calls486.clear()
    assert _real_ensure_room_486(str(TMP), 5) is False, "AC12: stays too little -> False"
    assert len(_reap_calls486) == 1, f"AC12: exactly one reaper run even on failure: {_reap_calls486}"

    # a reaper that raises
    def _raising_reap_now486(dry_run=False):
        raise RuntimeError("L0486-AC12: forced")

    _reap_tmp486.reap_now = _raising_reap_now486
    assert _real_ensure_room_486(str(TMP), 5) is False, "AC12: a raising reaper yields False, never raises"
finally:
    shutil.disk_usage = _orig_du486
    _reap_tmp486.reap_now = _orig_reap_now_486
N += 1

# AC12b: DOIT_LEDGER_FILE is saved/restored around the reaper call — set to a
# distinct value, unset, and when the reaper itself raises after setting it.
shutil.disk_usage = lambda path: _du_row486(100, 99, 0)
try:
    for _prior486 in ("L-something-0001.jsonl", None):
        if _prior486 is None:
            os.environ.pop("DOIT_LEDGER_FILE", None)
        else:
            os.environ["DOIT_LEDGER_FILE"] = _prior486

        def _reap_now_sets_ledger486(dry_run=False):
            os.environ["DOIT_LEDGER_FILE"] = "L-executor-0001.jsonl"
            return {}

        _reap_tmp486.reap_now = _reap_now_sets_ledger486
        _real_ensure_room_486(str(TMP), 5)
        assert os.environ.get("DOIT_LEDGER_FILE") == _prior486, \
            f"AC12: DOIT_LEDGER_FILE restored to {_prior486!r}, got {os.environ.get('DOIT_LEDGER_FILE')!r}"

        if _prior486 is None:
            os.environ.pop("DOIT_LEDGER_FILE", None)
        else:
            os.environ["DOIT_LEDGER_FILE"] = _prior486

        def _reap_now_raises_after_set486(dry_run=False):
            os.environ["DOIT_LEDGER_FILE"] = "L-executor-0001.jsonl"
            raise RuntimeError("L0486-AC12: forced after set")

        _reap_tmp486.reap_now = _reap_now_raises_after_set486
        assert _real_ensure_room_486(str(TMP), 5) is False
        assert os.environ.get("DOIT_LEDGER_FILE") == _prior486, \
            f"AC12: restored even when the reaper raises after setting it: {os.environ.get('DOIT_LEDGER_FILE')!r}"
finally:
    shutil.disk_usage = _orig_du486
    _reap_tmp486.reap_now = _orig_reap_now_486
    os.environ.pop("DOIT_LEDGER_FILE", None)
N += 1

# AC12c: need_gb resolution — 5/2 defaults, look.toml's room_builder_gb/room_grader_gb override.
assert dispatch._room_need_gb("builder") == 5, "AC12: default builder need is 5GB"
assert dispatch._room_need_gb("grader") == 2, "AC12: default grader need is 2GB"
_fixture_toml_486 = TMP / "l0486-look-fixture.toml"
_fixture_toml_486.write_text("[thresholds]\nroom_builder_gb = 9\nroom_grader_gb = 4\n")
assert dispatch._room_need_gb("builder", toml_path=_fixture_toml_486) == 9, "AC12: room_builder_gb overrides to 9"
assert dispatch._room_need_gb("grader", toml_path=_fixture_toml_486) == 4, "AC12: room_grader_gb overrides to 4"
N += 1
print("L0486-AC12 ok")


def _boom13(*a, **kw):
    _boom13.calls.append(a)
    raise AssertionError("L0486-AC13: a backend must not be entered for a disk-refused dispatch")


_boom13.calls = []


def _drive_disk486(role, subject, force):
    """Drives dispatch.main() for role/subject with `ensure_room` forced to
    `force` and every backend stubbed to explode (AC13 must prove the refusal
    lands strictly before any of them). Returns (outcome, this drive's own
    ledger rows, that file's path) — outcome is the real exit code on a clean
    SystemExit, or the literal "reached-backend" when a stubbed backend fired
    (proof the dispatch got PAST the disk gate)."""
    dispatch.ensure_room = lambda path, need_gb: force
    real = dispatch.run_claude, dispatch.run_seat, dispatch.run_codex
    dispatch.run_claude = dispatch.run_seat = dispatch.run_codex = _boom13
    a = argparse.Namespace(role=role, subject=subject, packet=str(PK), path=None, cwd=str(REPO),
                           charter=None, project="t", mcp_config=None, timeout=None, max_usd=None)
    try:
        dispatch.main(a)
        outcome = 0
    except SystemExit as e:
        outcome = e.code
    except AssertionError:
        outcome = "reached-backend"
    finally:
        dispatch.run_claude, dispatch.run_seat, dispatch.run_codex = real
    f = max((TMP / "events").glob(f"L-{role}-[0-9]*.jsonl"))
    return outcome, [json.loads(l) for l in f.read_text().splitlines()], f


AC13_BUILDER_SUBJ, AC13_GRADER_SUBJ = "L-spec-9486e1", "L-spec-9486e2"
code13a, raw13a, _ = _drive_disk486("builder", AC13_BUILDER_SUBJ, False)
types13a = [e["type"] for e in raw13a]
assert code13a == 1, raw13a
assert "build-started" not in types13a and "spawn-started" not in types13a, \
    f"AC13: no build-started/spawn-started for a disk-refused builder: {types13a}"
assert any(e["type"] == "spawn-failed" and e.get("reason") == "disk" for e in raw13a), raw13a
assert _boom13.calls == [], f"AC13: no backend was ever entered: {_boom13.calls}"

code13b, raw13b, _ = _drive_disk486("grader", AC13_GRADER_SUBJ, False)
types13b = [e["type"] for e in raw13b]
assert code13b == 1, raw13b
assert "spawn-started" not in types13b and "grader-view-built" not in types13b, \
    f"AC13: no spawn-started/grader-view-built for a disk-refused grader: {types13b}"
assert any(e["type"] == "spawn-failed" and e.get("reason") == "disk" for e in raw13b), raw13b
assert _boom13.calls == [], f"AC13: no backend/grading call happened for the grader either: {_boom13.calls}"

_all13 = fold.read_events()
_briefs13 = [e for e in _all13 if e.get("type") == "brief" and e.get("condition") == "disk-room"]
assert _briefs13, "AC13: a brief for condition disk-room exists"
assert all(e.get("owner") == "thinker" for e in _briefs13), _briefs13
assert not any(e.get("type") in ("capability-hold", "escalation-blocking")
              and e.get("subject") in (AC13_BUILDER_SUBJ, AC13_GRADER_SUBJ) for e in _all13), \
    "AC13: a disk refusal is never a hold"
N += 1

# spec-writer/reviewer never call ensure_room at all
for _role13, _subj13 in (("spec-writer", "L-spec-9486e3"), ("reviewer", "L-spec-9486e4")):
    _calls13 = []
    dispatch.ensure_room = lambda path, need_gb: (_calls13.append(1), True)[1]
    real13 = dispatch.run_claude, dispatch.run_seat, dispatch.run_codex
    dispatch.run_claude = dispatch.run_seat = dispatch.run_codex = _boom13
    a13 = argparse.Namespace(role=_role13, subject=_subj13, packet=str(PK), path=None, cwd=str(REPO),
                             charter=None, project="t", mcp_config=None, timeout=None, max_usd=None)
    try:
        dispatch.main(a13)
    except (SystemExit, AssertionError):
        pass
    finally:
        dispatch.run_claude, dispatch.run_seat, dispatch.run_codex = real13
    assert _calls13 == [], f"L0486-AC13: {_role13} dispatch never calls ensure_room: {_calls13}"
N += 1

# a second dispatch, forced True, proceeds past the gate — the SAME subject's
# open disk-room brief is answered (why starting "cleared:").
_boom13.calls = []
code13c, raw13c, _ = _drive_disk486("builder", AC13_BUILDER_SUBJ, True)
assert code13c == "reached-backend", f"AC13: forced True proceeds to the (stubbed) backend: {code13c} {raw13c}"
assert "build-started" in [e["type"] for e in raw13c], \
    f"AC13: it got far enough to write build-started: {raw13c}"
assert not any(e.get("reason") == "disk" for e in raw13c), raw13c
_answered13 = [e for e in fold.read_events() if e.get("type") == "brief-answered"]
_cleared13 = [e for e in _answered13 if str(e.get("why", "")).startswith("cleared:")]
assert _cleared13, f"L0486-AC13: the disk-room brief is answered, why starting 'cleared:': {_answered13}"
N += 1

dispatch.ensure_room = lambda path, need_gb: True   # leave this file's own hermetic default in place
print("L0486-AC13 ok")
