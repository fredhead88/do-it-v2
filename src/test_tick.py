#!/usr/bin/env python3
"""One runnable check on the tick. Run: python3 test_tick.py"""
import argparse, fcntl, json, os, pathlib, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"], os.environ["DOIT_NO_POKE"] = str(TMP), "1"
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import dispatch, fold, tick  # noqa: E402

ticks = lambda: [json.loads(l) for l in tick.TICK.read_text().splitlines()]
assert tick.main() == 0 and ticks()[-1] == {**ticks()[-1], "lane": 0, "spawned": False}, "idle: no model spawned"

(TMP / "events" / "L-planner-0001.jsonl").write_text(json.dumps(
    {"v": 1, "ts": "2026-09-08T10:00:00+00:00", "type": "spec-written", "subject": "L-spec-0001"}) + "\n")
dispatch.AGENTS = TMP / "agents"
dispatch.AGENTS.mkdir()
assert tick.main() == 1 and ticks()[-1]["lane"] == 1 and ticks()[-1]["spawned"] is False, "lane, no contract: loud"

(dispatch.AGENTS / "executor.md").write_text("---\nname: executor\ntools: Read, Bash, Skill, StructuredOutput\nmodel: claude-opus-5\n---\n")
(dispatch.AGENTS / "executor.schema.json").write_text('{"type": "object"}')
seen, out = {}, {"idle": False, "actions": [{"action": "dispatch-grader", "subject": "L-spec-0001", "spawned": "", "why": "w"}]}


def fake(cmd, prompt, cwd, timeout):
    seen.update(cmd=cmd, prompt=prompt, ledger=os.environ.get("DOIT_LEDGER_FILE"), gate=os.environ.get("DOIT_GATE_LEDGER_FILE"))
    return argparse.Namespace(stdout=json.dumps({"is_error": False, "total_cost_usd": 0.5, "num_turns": 3,
                                                 "usage": {"input_tokens": 9, "output_tokens": 1},
                                                 "structured_output": seen.get("out")}))
dispatch.run_claude = fake
assert tick.main() == 1, "null structured_output is a failed spawn"
assert json.loads((TMP / "events" / "L-executor-0001.jsonl").read_text().splitlines()[-1])["why"] == "null structured_output"
seen["out"] = out
assert tick.main() == 0 and ticks()[-1]["spawned"] is True
assert "L-spec-0001 · written" in seen["prompt"] and "--agent" in seen["cmd"] and "executor" in seen["cmd"]
assert "Skill(subagent-driven-development)" in seen["cmd"][seen["cmd"].index("--disallowedTools") + 1], "D119: RETIRE denied by name"
assert seen["ledger"] == seen["gate"] == "L-executor-0002.jsonl", "the Executor's appends and the gate's verdict land in its spawn file (D90)"
assert "--json-schema" in seen["cmd"] and "StructuredOutput" not in seen["cmd"][seen["cmd"].index("--allowedTools") + 1]
done = json.loads((TMP / "events" / "L-executor-0002.jsonl").read_text().splitlines()[-1])
assert done["type"] == "spawn-done" and done["cost_usd"] == 0.5 and done["spawn"] == "L-executor-0002"
assert done["actions"] == ["dispatch-grader L-spec-0001"] and done["idle"] is False, "the tick's actions are ledger facts"

# in flight: a start with no terminal event keeps the subject off the lane; a stale one puts it back
ev_file = TMP / "events" / "L-grader-0009.jsonl"
ev_file.write_text(json.dumps({"v": 1, "ts": fold.NOW.isoformat(timespec="seconds"), "type": "spawn-started",
                               "role": "grader", "subject": "L-spec-0001", "spawn": "L-grader-0009"}) + "\n")
assert tick.main() == 0 and ticks()[-1]["lane"] == 0, "an in-flight grader keeps its subject off the lane"
old_ts = (fold.NOW - __import__("datetime").timedelta(minutes=45)).isoformat(timespec="seconds")
ev_file.write_text(json.dumps({"v": 1, "ts": old_ts, "type": "spawn-started", "role": "grader",
                               "subject": "L-spec-0001", "spawn": "L-grader-0009"}) + "\n")
tick.main()
stale = [json.loads(l) for l in tick.TICK.read_text().splitlines() if '"spawn-stale"' in l]
assert stale and stale[-1]["spawn"] == "L-grader-0009" and ticks()[-1]["lane"] == 1, "past 2× the cap: stale, back on the lane"
tick.main()
assert len([l for l in tick.TICK.read_text().splitlines() if '"spawn-stale"' in l]) == 1, "stale is recorded once"
ev_file.unlink()

# a start with NO spawn id — hand-written, or written before the wrapper existed.
# It crashed the tick on the first real charter: `sid.split` on None, and every
# tick after it was dead. It cannot be matched to a terminal event, so it ages
# out on the role's cap and names nothing.
ev_file = TMP / "events" / "L-builder-0009.jsonl"
ev_file.write_text(json.dumps({"v": 1, "ts": fold.NOW.isoformat(timespec="seconds"),
                               "type": "build-started", "subject": "L-spec-0001"}) + "\n")
assert tick.main() == 0 and ticks()[-1]["lane"] == 0, "an anonymous start does not crash the tick"
old = (fold.NOW - __import__("datetime").timedelta(days=12)).isoformat(timespec="seconds")
ev_file.write_text(json.dumps({"v": 1, "ts": old, "type": "spawn-started",
                               "role": "grader", "subject": "L-spec-0001"}) + "\n")
before_stale = len([l for l in tick.TICK.read_text().splitlines() if '"spawn-stale"' in l])
assert tick.main() == 0 and ticks()[-1]["lane"] == 1, "aged out: back on the lane"
assert len([l for l in tick.TICK.read_text().splitlines() if '"spawn-stale"' in l]) == before_stale, \
    "nothing to name: a spawn-stale with no spawn id would be unmatchable and repeat every tick"
ev_file.unlink()

# an open escalation keeps the subject off the lane; a decision after it puts it back
esc = TMP / "events" / "L-executor-0090.jsonl"
esc.write_text(json.dumps({"v": 1, "ts": fold.NOW.isoformat(timespec="seconds"), "type": "escalation-blocking",
                           "subject": "L-spec-0001", "why": "seat", "spawn": "L-executor-0090"}) + "\n")
assert tick.main() == 0 and ticks()[-1]["lane"] == 0, "an escalated subject is the operator's, not the lane's"
esc.write_text(esc.read_text() + json.dumps({"v": 1, "ts": fold.NOW.isoformat(timespec="seconds"), "type": "decision",
                                             "subject": "L-spec-0001", "why": "w", "revert": "r", "spawn": "L-executor-0090"}) + "\n")
tick.main()
assert ticks()[-1]["lane"] == 1, "a decision after the escalation returns it to the lane"
esc.unlink()

# A closed charter stays on the lane until it is reaped. §4.11's reaper refuses
# anything not L2-complete or retracted, and the lane used to admit only
# L1-complete: the reap was unreachable by any tick, and L-charter-0001 sat
# retracted with its worktree standing (2026-09-08).
ch = TMP / "events" / "L-operator-tick.jsonl"   # retracted is the operator's (fold.EMITS)
ch.write_text("".join(json.dumps(e) + "\n" for e in [
    {"v": 1, "ts": "2026-09-08T09:00:00+00:00", "type": "charter-filed", "subject": "L-charter-0003"},
    {"v": 1, "ts": "2026-09-08T10:00:00+00:00", "type": "charter-retracted", "subject": "L-charter-0003",
     "why": "w"}]))
specs, charters, _, _ = fold.fold(fold.read_events())
assert charters["L-charter-0003"]["state"] == "retracted"
assert "L-charter-0003 · retracted" in tick.lane(specs, charters), \
    "a charter that is reapable and unreaped is the Executor's, or doit reap never runs"
ch.write_text(ch.read_text() + json.dumps({"v": 1, "ts": "2026-09-08T11:00:00+00:00", "type": "tree-reaped",
                                           "subject": "L-charter-0003", "reaped": [], "retained": []}) + "\n")
specs, charters, _, _ = fold.fold(fold.read_events())
reaped = {e.get("subject") for e in fold.read_events() if e["type"] == "tree-reaped"}
assert "L-charter-0003 · retracted" not in tick.lane(specs, charters, reaped=reaped), \
    "once reaped it leaves the lane for good, or every tick pays to reap it again"
ch.unlink()

held = open(TMP / "tick.lock", "w")
fcntl.flock(held, fcntl.LOCK_EX)
before = len(ticks())
assert tick.main() == 0 and len(ticks()) == before, "flock: a second tick is dropped, not queued"
print("tick: 16 checks pass")
