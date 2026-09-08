#!/usr/bin/env python3
"""up — start the Planner pane, and PRINT the cron line for the tick (D117, D95).

  up.py [--print-only]

One pane and one job. The pane is this script's business: `claude --agent planner`
with §10.5's RETIRE list denied by name (D119) and its own ledger file, so what it
appends lands as the actor `planner` (D90).

**The job is cron's, and cron is the operator's.** The line is printed, never
installed: editing the operator's cron table is an irreversible act on a thing outside this
system, and §4.9 routes those to a human rather than to an unattended process.
`test_up.py` holds that as a check, because it is exactly the rule a later edit
would helpfully break.
"""
import os, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch, fold, tick  # noqa: E402

DOIT = HERE.parent / "doit"


def cron_line():
    """The tick's schedule. DOIT_TICK_MIN is the same variable the fold's
    tick-stale line reads, so a line installed from here cannot disagree with the
    staleness alarm that watches it."""
    every = os.environ.get("DOIT_TICK_MIN", "5")
    return (f"*/{every} * * * * DOIT_ROOT={fold.ROOT} {DOIT} tick "
            f">> {fold.ROOT}/logs/tick.log 2>&1")


def pane_cmd():
    """Interactive, so no -p, no --json-schema, no --output-format: the pane's
    Output is the files and events it writes. The agent file's tools: line is the
    sandbox; the deny list is the only form a retire list has (D119)."""
    return ["claude", "--agent", "planner",
            "--disallowedTools", ",".join(f"Skill({s})" for s in tick.RETIRE)]


INSTALLED = pathlib.Path.home() / ".claude" / "agents" / "planner.md"


def install(contract):
    """`--agent planner` resolves through ~/.claude/agents, not through this repo —
    measured: a pane launched without this exits 1 with `--agent 'planner' not found`
    AFTER the cron line has been printed, which reads like success. The ten contracts
    are symlinked there already; this is the eleventh link, made idempotently and
    never over somebody else's file."""
    if INSTALLED.is_symlink() and INSTALLED.resolve() == contract.resolve():
        return
    if INSTALLED.exists() or INSTALLED.is_symlink():
        sys.exit(f"up: {INSTALLED} is not this repo's contract — resolve it by hand:\n"
                 f"  ln -sfn {contract} {INSTALLED}")
    INSTALLED.parent.mkdir(parents=True, exist_ok=True)
    INSTALLED.symlink_to(contract)
    print(f"# linked {INSTALLED} -> {contract}")


def main(print_only=False):
    contract = dispatch.AGENTS / "planner.md"
    if not contract.exists():
        sys.exit(f"up: no planner contract at {contract} — the pane is the contract (D116)")
    install(contract)
    (fold.ROOT / "events").mkdir(parents=True, exist_ok=True)
    (fold.ROOT / "logs").mkdir(parents=True, exist_ok=True)
    ledger = dispatch.alloc(fold.EVENTS, "L-planner-", ".jsonl")
    env = {**os.environ, "DOIT_LEDGER_FILE": ledger.name,
           "PATH": f"{HERE.parent}:{os.environ.get('PATH', '')}"}
    cmd = pane_cmd()
    print("# the tick is cron's, and cron is yours — install this line yourself:")
    print(cron_line())
    print(f"# planner pane: {ledger.stem} · {' '.join(cmd)}")
    if print_only:
        return cmd, env
    os.execvpe(cmd[0], cmd, env)


if __name__ == "__main__":
    main("--print-only" in sys.argv[1:])
