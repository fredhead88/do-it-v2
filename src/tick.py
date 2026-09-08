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


def in_flight(ev):
    """Subjects with a spawn started and not ended — off the lane, or the next tick
    dispatches the same work twice. A start older than twice its role's cap with no
    terminal event is a dead wrapper: recorded once as spawn-stale, and the subject
    is back on the lane for the Executor's failed-spawn row."""
    started = [e for e in ev if e["type"] in ("build-started", "spawn-started")]
    ended = {e.get("spawn") for e in ev if e["type"] in ("spawn-done", "spawn-failed", "spawn-stale")}
    # An open escalation is the operator's: the subject leaves the lane until a
    # decision or an unblocked event lands after it — else every cron tick pays
    # for an Executor that reads the escalation and does nothing.
    last = {}
    for e in ev:
        if e["type"] in ("escalation-blocking", "decision", "unblocked") and e.get("subject"):
            last[e["subject"]] = e["type"]
    busy = {s for s, t in last.items() if t == "escalation-blocking"}
    for e in started:
        sid = e.get("spawn")
        if sid and sid in ended:
            continue
        # A start with no spawn id is not the wrapper's — a hand-written one, or a
        # pre-wrapper event. It can never be matched to a terminal event, so it is
        # aged out on the role's own cap and there is nothing to name in a
        # `spawn-stale`: the subject simply returns to the lane, where the
        # Executor's failed-spawn row is what looks at it.
        role = "-".join(sid.split("-")[1:-1]) if sid else (e.get("role") or "")
        cap = dispatch.ROLES.get(role, (None, 60, 0))[1]
        if (fold.NOW - fold.ts(e.get("ts"))).total_seconds() / 60 > 2 * cap:
            sid and dispatch.emit(TICK, {"spawn": sid}, "spawn-stale",
                                  subject=e.get("subject"), role=role, cap_min=cap)
        else:
            busy.add(e.get("subject"))
    return busy


def lane(specs, charters, busy=frozenset()):
    return sorted([f"{s['id']} · {s['state']}" for s in specs.values()
                   if s["state"] in ACTIONABLE and s["id"] not in busy]
                  + [f"{c['id']} · {c['state']}" for c in charters.values()
                     if c["state"] == "L1-complete" and c["id"] not in busy])


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
    todo, have = lane(specs, charters, in_flight(ev)), (dispatch.AGENTS / "executor.md").exists()
    dispatch.emit(TICK, {}, "tick", lane=len(todo), spawned=bool(todo and have))
    if not todo:
        print("tick: idle")
        return 0
    if not have:
        print(f"tick: {len(todo)} on the lane but no executor contract in {dispatch.AGENTS}", file=sys.stderr)
        return 1
    ledger = dispatch.alloc(fold.EVENTS, "L-executor-", ".jsonl")
    os.environ["DOIT_LEDGER_FILE"] = ledger.name          # its own appends land as itself (D90)
    os.environ["DOIT_GATE_LEDGER_FILE"] = ledger.name     # and so does the gate's verdict (its pane-era default was L-executor-0001)
    os.environ["PATH"] = f"{dispatch.HERE.parent}:{os.environ.get('PATH', '')}"   # `doit` resolves
    fm, base = dispatch.frontmatter("executor"), {"spawn": ledger.stem}
    schema = dispatch.AGENTS / "executor.schema.json"
    cmd = ["claude", "-p", "--agent", "executor", "--strict-mcp-config", "--permission-mode", "dontAsk",
           "--output-format", "json", "--max-budget-usd", str(USD), "--json-schema", schema.read_text(),
           "--allowedTools", ",".join(t.strip() for t in fm["tools"].split(",") if t.strip() != "StructuredOutput"),
           "--disallowedTools", ",".join(f"Skill({s})" for s in RETIRE)]
    prompt = (f"{ledger.stem}: take the next durable action on this lane, then exit.\n\n"
              + "\n".join(todo) + "\n\n" + board)
    try:
        res = json.loads(dispatch.run_claude(cmd, prompt, str(fold.ROOT), MINUTES * 60).stdout)
    except Exception as e:        # timeout or no JSON: recorded; the next tick re-scans the same lane
        dispatch.emit(ledger, base, "spawn-failed", why=f"{type(e).__name__}: {str(e)[:200]}")
        return 1
    u, out = res.get("usage") or {}, res.get("structured_output")
    bad = bool(res.get("is_error")) or out is None          # null output is a failed spawn (D116)
    if bad and (res.get("terminal_reason") == "api_error" or res.get("api_error_status")):
        dispatch.emit(ledger, base, "escalation-blocking", subject="executor",
                      why=f"seat unreachable ({res.get('api_error_status')}) — /login as the operator")
    kv = dict(cost_usd=res.get("total_cost_usd"), input_tokens=u.get("input_tokens"), lane=len(todo),
              output_tokens=u.get("output_tokens"), turns=res.get("num_turns"), cli=dispatch.CLI)
    if bad:
        kv["why"] = "null structured_output" if out is None and not res.get("is_error") else str(res.get("result"))[:300]
    else:
        kv.update(idle=out.get("idle"), actions=[f"{x['action']} {x['subject']}" for x in out.get("actions", [])])
    dispatch.emit(ledger, base, "spawn-failed" if bad else "spawn-done", **kv)
    print(f"tick: executor {ledger.stem} {'failed' if bad else 'done'}")
    return int(bad)


if __name__ == "__main__":
    sys.exit(main())
