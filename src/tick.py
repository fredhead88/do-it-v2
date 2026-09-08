#!/usr/bin/env python3
"""tick — fold, then spawn the Executor only if the fold shows an actionable lane (D117).

  tick.py     one tick: fold, decide, spawn-or-exit. Scheduled (cron) and poked
              (every dispatch ends with one). One at a time per ledger — flock —
              and a dropped duplicate loses nothing: the durable list is the queue.

An idle tick spawns no model. The Executor is a job, not a pane: it takes the
next durable action on the lane and exits; its sub-agents are detached dispatches
whose terminal events the next tick sees.
"""
import fcntl, json, os, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dispatch, fold  # noqa: E402

# Waiting for the Executor's next durable action. `building` is in flight, not waiting.
ACTIONABLE = {"written", "graded", "reviewing", "shipped"}
# §10.5's RETIRE list, verbatim — a driver spawn denies it by name (D119).
RETIRE = ("subagent-driven-development", "executing-plans", "writing-plans", "requesting-code-review",
          "receiving-code-review", "finishing-a-development-branch", "dispatching-parallel-agents")
MINUTES, USD = 20, 5          # one durable action per tick — never uncapped (§4.4 correction 9)
TICK = fold.EVENTS / "L-tick-local.jsonl"


def lane(specs, charters):
    return sorted([f"{s['id']} · {s['state']}" for s in specs.values() if s["state"] in ACTIONABLE]
                  + [f"{c['id']} · {c['state']}" for c in charters.values() if c["state"] == "L1-complete"])


def main():
    fold.EVENTS.mkdir(parents=True, exist_ok=True)
    lock = open(fold.ROOT / "tick.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("tick: one is running; this one is dropped")
        return 0
    ev = fold.read_events()
    specs, charters, ignored, by_subject = fold.fold(ev)
    board = fold.render(ev, specs, charters, ignored, by_subject)
    todo, have = lane(specs, charters), (dispatch.AGENTS / "executor.md").exists()
    dispatch.emit(TICK, {}, "tick", lane=len(todo), spawned=bool(todo and have))
    if not todo:
        print("tick: idle")
        return 0
    if not have:
        print(f"tick: {len(todo)} on the lane but no executor contract in {dispatch.AGENTS}", file=sys.stderr)
        return 1
    ledger = dispatch.alloc(fold.EVENTS, "L-executor-", ".jsonl")
    os.environ["DOIT_LEDGER_FILE"] = ledger.name          # its own appends land as itself (D90)
    fm, base = dispatch.frontmatter("executor"), {"spawn": ledger.stem}
    cmd = ["claude", "-p", "--agent", "executor", "--strict-mcp-config", "--permission-mode", "dontAsk",
           "--output-format", "json", "--max-budget-usd", str(USD),
           "--allowedTools", ",".join(t.strip() for t in fm["tools"].split(",")),
           "--disallowedTools", ",".join(f"Skill({s})" for s in RETIRE)]
    prompt = (f"{ledger.stem}: take the next durable action on this lane, then exit.\n\n"
              + "\n".join(todo) + "\n\n" + board)
    try:
        res = json.loads(dispatch.run_claude(cmd, prompt, str(fold.ROOT), MINUTES * 60).stdout)
    except Exception as e:        # timeout or no JSON: recorded; the next tick re-scans the same lane
        dispatch.emit(ledger, base, "spawn-failed", why=f"{type(e).__name__}: {str(e)[:200]}")
        return 1
    u, bad = res.get("usage") or {}, bool(res.get("is_error"))
    if bad and (res.get("terminal_reason") == "api_error" or res.get("api_error_status")):
        dispatch.emit(ledger, base, "escalation-blocking", subject="executor",
                      why=f"seat unreachable ({res.get('api_error_status')}) — /login as the operator")
    kv = dict(cost_usd=res.get("total_cost_usd"), input_tokens=u.get("input_tokens"), lane=len(todo),
              output_tokens=u.get("output_tokens"), turns=res.get("num_turns"), cli=dispatch.CLI)
    if bad:
        kv["why"] = str(res.get("result"))[:300]
    dispatch.emit(ledger, base, "spawn-failed" if bad else "spawn-done", **kv)
    print(f"tick: executor {ledger.stem} {'failed' if bad else 'done'}")
    return int(bad)


if __name__ == "__main__":
    sys.exit(main())
