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
assert cmd[cmd.index("--allowedTools") + 1] == "Read,Glob,Grep,Write,Bash", "the allow list is the contract's tools: line"

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
print(f"dispatch: {N} spawns mocked, every check fired")
