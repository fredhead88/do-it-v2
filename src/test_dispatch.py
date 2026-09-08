#!/usr/bin/env python3
"""One runnable check on the dispatch wrapper. Run: python3 test_dispatch.py
The spawn is mocked; every after-the-fact check is exercised against the
failure it was written for (D116, D120)."""
import argparse, json, os, pathlib, subprocess, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"], os.environ["DOIT_NO_POKE"] = str(TMP), "1"
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import dispatch, fold  # noqa: E402

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
assert cmd[cmd.index("--allowedTools") + 1] == "Read,Glob,Grep,Write", "the allow list is the contract's tools: line"

code, types, evs, _ = spawn("research", result={"is_error": True, "terminal_reason": "api_error",
                                                "api_error_status": 401, "result": "Not logged in"}, path=rp)
assert code == 1 and types == ["escalation-blocking", "spawn-failed"], types
assert "/login" in evs[0]["why"], "an unreachable seat is the operator's, never retried"

code, types, evs, _ = spawn("research", out=research, path=rp)
assert code == 1 and "nothing at" in evs[0]["why"], "D120 W3: a success claim with no file on disk fails"

PK.write_text("a packet carrying the hypothesis\n")     # its own bytes: a contaminated packet is never re-sent
code, types, evs, _ = spawn("research", out={**research, "contamination": True}, path=rp, side=lambda: rp.write_text("d"))
assert code == 1 and "contamination" in evs[0]["why"]
PK.write_text("a packet\n")

code, types, evs, _ = spawn("research", out={**research, "path": "content/L-research-0009.md"}, path=rp)
assert code == 1 and "path mismatch" in evs[0]["why"]

stray = REPO / "stray.txt"
code, types, evs, _ = spawn("research", out=research, path=rp, side=lambda: stray.write_text("x"))
assert code == 1 and "repo status changed" in evs[0]["why"], "a non-builder spawn that touches the repo fails"
stray.unlink()

code, types, evs, _ = spawn("research", out=research, path=rp)
assert code == 0 and types == ["research-filed", "spawn-done"], types
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
code, types, evs, _ = spawn("spec-writer", out={**sw, "status": "killed", "killed_by_check": 2, "escalations": []})
assert code == 0 and types == ["spec-killed", "spawn-done"] and evs[0]["check"] == 2, "killed needs no file"

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

# ★ `probe` runs OUTSIDE every repo on purpose (§4.6·10, §9.5). Read as
# undetermined, that refuses the one contract that spends at planning time
# before it spends anything at all.
OUTSIDE = TMP / "run-dir"
OUTSIDE.mkdir()
assert dispatch.porcelain(OUTSIDE) == dispatch.NOT_A_REPO, "no repo is a definite answer, not an undetermined one"
assert dispatch.porcelain(REPO) is not None and dispatch.porcelain(REPO) != dispatch.NOT_A_REPO
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
print(f"dispatch: {N} spawns mocked, every check fired")
