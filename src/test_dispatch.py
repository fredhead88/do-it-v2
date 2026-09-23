#!/usr/bin/env python3
"""One runnable check on the dispatch wrapper. Run: python3 test_dispatch.py
The spawn is mocked; every after-the-fact check is exercised against the
failure it was written for (D116, D120)."""
import argparse, json, os, pathlib, subprocess, sys, tempfile
from datetime import datetime as _dt, timedelta as _timedelta, timezone as _timezone

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"], os.environ["DOIT_NO_POKE"] = str(TMP), "1"
# Hermetic w.r.t. the pane: env.sh exports DOIT_SEAT=1 in every pane on this box,
# which routes every mocked spawn into run_seat to wait for a <spawn>.result.json
# no mock ever writes — the file then hangs to the role timeout instead of failing.
# The seat-route blocks below set and del it around themselves on purpose.
os.environ.pop("DOIT_SEAT", None)
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import dispatch, fold, harness  # noqa: E402

REPO = TMP / "repo"
REPO.mkdir()
subprocess.run(["git", "init", "-q"], cwd=REPO, check=True)
PK = TMP / "packet.md"
PK.write_text("a packet\n")
N = 0


def spawn(role, out=None, result=None, side=None, path=None, subject="L-spec-0001"):
    res = {"is_error": False, "terminal_reason": "completed", "structured_output": out, "num_turns": 1,
           "usage": {"input_tokens": 1, "output_tokens": 2}, "total_cost_usd": 0.01,
           "modelUsage": {"m": {}}, "permission_denials": [], **(result or {})}

    def fake(cmd, packet, cwd, timeout):
        fake.cmd = cmd
        side and side()
        return argparse.Namespace(stdout=json.dumps(res), returncode=0, stderr="")
    dispatch.run_claude = fake
    a = argparse.Namespace(role=role, subject=subject, packet=str(PK), path=path and str(path),
                           cwd=str(REPO), charter=None, project="t", mcp_config=None, timeout=None, max_usd=None)
    try:
        dispatch.main(a)
        code = 0
    except SystemExit as e:
        code = e.code
    raw = [json.loads(l) for l in max((TMP / "events").glob(f"L-{role}-*.jsonl")).read_text().splitlines()]
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
PK.write_text("a packet\n")

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

sw = {"status": "written", "spec_id": "L-spec-0001", "ac_count": 2, "ac_types": ["backend"], "footprint": ["a.py"],
      "requirement_ids": ["R1"], "owed": 0, "unknowns": 1, "split": [], "weak_dimensions": [],
      "escalations": [{"asks": "q?", "blocks": ["AC1"], "default": "d", "deadline": "2026-09-09"}], "declarations": []}
sp = TMP / "content" / "L-spec-0001.md"
code, types, evs, _ = spawn("spec-writer", out=sw, path=sp, side=lambda: sp.write_text("spec"))
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
code, types, evs, _ = spawn("spec-writer", out=owed, path=sp, side=lambda: sp.write_text("spec"))
assert code == 0 and "owed-ac" in types and next(e for e in evs if e["type"] == "owed-ac")["wake_at"] == "2026-09-16T11:04:00Z", evs
assert fold.ts("2026-09-16T11:04:00Z").tzinfo is not None, "the fold parses the Z form"
PK.write_text("a packet, owed without wake_at\n")
code, types, evs, _ = spawn("spec-writer", out={**owed, "declarations": [{"term": "owed-ac", "line": "no instant"}]}, path=sp)
assert code == 1 and "violates spec-writer.schema.json" in evs[-1]["why"] and "wake_at" in evs[-1]["why"], evs[-1]
PK.write_text("a packet\n")

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
code, types, evs, _ = spawn("grader", out=grade([met, unmet]))
assert types == ["verdict", "rejected-criterion", "checker-coverage-change", "spawn-done"], types
assert evs[0]["confirmed"] is False and evs[1]["criterion"] == "AC2" and rejects() == 1
code, types, evs, _ = spawn("grader", out=grade([met], card_ok="cannot-assess", could_not_run=True))
assert evs[0]["confirmed"] is False and "gate-infra" in types and "criterion-cleared" not in types
assert "checker-coverage-change" not in types, "same coverage note: no change event"
code, types, evs, _ = spawn("grader", out=grade([met, {"ac": "AC2", "verdict": "met", "reason": "now evidenced"}]))
assert "criterion-cleared" in types and evs[0]["confirmed"] is True and rejects() == 0, "a re-grade that names it met clears it"
code, types, evs, _ = spawn("grader", out=grade([met, {"ac": "DONE-COND", "verdict": "unmet", "reason": "residue"}]))
assert rejects() == 1
code, types, evs, _ = spawn("grader", out=grade([met], card_ok="no"))
assert "criterion-cleared" not in types and rejects() == 1, "an unconfirmed verdict clears nothing it did not test"
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
code, types, evs, _ = spawn("grader", out=grade([met, cannot_assess]))
assert types[0] == "verdict" and evs[0]["confirmed"] is False and evs[0]["cannot_assess"] == ["AC7"], evs[0]
assert "rejected-criterion" not in types, "cannot-assess is not unmet — no rejected-criterion for it"
code, types, evs, _ = spawn("grader", out=grade([met]))
assert evs[0]["cannot_assess"] == [], "an all-met grade carries an empty list, not an absent field"

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
PK.write_text("a packet\n")
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


# ── AC1 · run_seat called directly: nobody ever claims -> Unserved, well under
# the 60s `timeout` argument, naming the spawn id ─────────────────────────────
os.environ["DOIT_SEAT_CLAIM_SEC"] = "1"
ac1_spawn = "L-research-r6ac1"
t0 = DISPATCH_TIME.time()
try:
    dispatch.run_seat(ac1_spawn, ["claude", "-p"], "packet", str(REPO), 60)
    raise AssertionError("run_seat must raise Unserved when nobody claims the seat")
except dispatch.Unserved as e:
    ac1_wall = DISPATCH_TIME.time() - t0
    ac1_msg = str(e)
assert ac1_spawn in ac1_msg, ac1_msg
assert ac1_wall < 15, f"run_seat waited {ac1_wall}s for a 1s claim window — the 60s timeout, not the claim window, must not be what fired"
_rm_seat(ac1_spawn)
del os.environ["DOIT_SEAT_CLAIM_SEC"]
N += 1

# ── AC2 · the 300s default does not fire prematurely within a short window ────
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
assert isinstance(ac2_holder.get("exc"), subprocess.TimeoutExpired), \
    "the probe's own short `timeout` fired normally afterward — never dispatch.Unserved — proving the default window did not fire early"
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


# ── AC3 · main(): a fully-unserved seat dispatch fails loudly, well under 15s ──
os.environ["DOIT_SEAT"] = "1"
dispatch.run_claude = never
os.environ["DOIT_SEAT_CLAIM_SEC"] = "1"
# a fresh packet body — L-research-0018 earlier in this file already recorded an
# is_error failure for the default "a packet\n" body on this same contract, and
# D120's dedup (packet_sha256 + contract_sha256) would refuse this dispatch before
# it ever reached run_seat, on a packet whose CONTENT happens to match, not a re-run.
PK.write_text("a packet for r6ac3\n")
code, raw, wall = _seat_driven("research", "L-spec-0001", 60)
PK.write_text("a packet\n")
assert code == 1 and [e["type"] for e in raw] == ["spawn-started", "spawn-failed"], raw
assert raw[-1]["reason"] == "unserved" and raw[-1]["spawn_path"] == "seat", raw[-1]
assert wall < 15, wall
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
PK.write_text("a packet\n")
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
try:
    dispatch.main(a)
    code = 0
except SystemExit as e:
    code = e.code
PK.write_text("a packet\n")
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
a = argparse.Namespace(role="builder", subject="L-spec-0001", packet=str(PK), path=None, cwd=str(REPO),
                       charter=None, project="t", mcp_config=None, timeout=60, max_usd=None, seat=False)
try:
    dispatch.main(a)
    code = 0
except SystemExit as e:
    code = e.code
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

# ── AC12 (dispatch half) · the 300s default boundary, on a fake clock ──────────
_ac12i_spawn = "L-research-r6ac12i"


def _fake_time_301():
    _fake_time_301.n += 1
    return 0.0 if _fake_time_301.n == 1 else 301.0


_fake_time_301.n = 0
real_wall_t0 = time.perf_counter()
with _mock.patch("time.time", _fake_time_301), _mock.patch("time.sleep", lambda s: None):
    try:
        dispatch.run_seat(_ac12i_spawn, ["claude", "-p"], "packet", str(REPO), 6000)
        raise AssertionError("expected Unserved at the 301s mark")
    except dispatch.Unserved:
        pass
real_wall_301 = time.perf_counter() - real_wall_t0
assert real_wall_301 < 5, f"the fake clock must have decided it, not real time: {real_wall_301}s"
_rm_seat(_ac12i_spawn)
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
wt12 = _dsn_worktree("ac12")
code, raw12, seen12 = drive("grader", "L-spec-0112", wt12, DSN_PROJECT, grade([met]))
ss12 = next(e for e in raw12 if e["type"] == "spawn-started")
assert ss12["role"] == "grader" and ss12["dsn_role"] == "readonly", ss12

# AC13: every OTHER role's spawn-started carries no dsn_role key at all.
# A fresh packet body, not the file-wide "a packet\n": an identical
# (packet, contract) pair already failed as L-research-0018 (weekly limit)
# above, and D120 refuses to re-spend on that exact combination.
PK.write_text("a packet for AC13\n")
code, types, evs, _ = spawn("research", out=research, path=rp)
PK.write_text("a packet\n")
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

print(f"dispatch: {N} spawns mocked, every check fired")
