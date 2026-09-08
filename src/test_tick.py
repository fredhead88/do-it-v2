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

(dispatch.AGENTS / "executor.md").write_text("---\nname: executor\ntools: Read, Bash, Skill\nmodel: claude-opus-5\n---\n")
seen = {}


def fake(cmd, prompt, cwd, timeout):
    seen.update(cmd=cmd, prompt=prompt, ledger=os.environ.get("DOIT_LEDGER_FILE"))
    return argparse.Namespace(stdout=json.dumps({"is_error": False, "total_cost_usd": 0.5, "num_turns": 3,
                                                 "usage": {"input_tokens": 9, "output_tokens": 1}}))
dispatch.run_claude = fake
assert tick.main() == 0 and ticks()[-1]["spawned"] is True
assert "L-spec-0001 · written" in seen["prompt"] and "--agent" in seen["cmd"] and "executor" in seen["cmd"]
assert "Skill(subagent-driven-development)" in seen["cmd"][seen["cmd"].index("--disallowedTools") + 1], "D119: RETIRE denied by name"
assert seen["ledger"] == "L-executor-0001.jsonl", "the Executor's own appends land in its spawn file (D90)"
done = json.loads((TMP / "events" / "L-executor-0001.jsonl").read_text().splitlines()[-1])
assert done["type"] == "spawn-done" and done["cost_usd"] == 0.5 and done["spawn"] == "L-executor-0001"

held = open(TMP / "tick.lock", "w")
fcntl.flock(held, fcntl.LOCK_EX)
before = len(ticks())
assert tick.main() == 0 and len(ticks()) == before, "flock: a second tick is dropped, not queued"
print("tick: 5 checks pass")
