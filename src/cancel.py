#!/usr/bin/env python3
"""doit cancel <spawn> [--force] — end a spawn that is only waiting for a pane (L-spec-0755/R1).

For a seat spawn with a start event and no terminal event (`spawn-done`,
`spawn-failed`, `spawn-stale`) and no `$R/seat/<spawn>.claimed`:
  (a) its packet and cmd.json move into `$R/seat/cancelled/` — never deleted;
  (b) `$R/seat/<spawn>.cancelled` is written, which `dispatch.run_seat` polls;
  (c) SIGTERM goes to the recorded `waiter_pid`, only when `waiter_host` is this host
      and the pid AND its recorded start time still match;
  (d) one `spawn-failed reason=cancelled` is appended — the single terminal event.
A claimed spawn (a pane is working on it) is refused unless `--force`, which still
records the cancel and signals the waiter, and never touches the pane.

Only the executor, the operator and the thinker may cancel; the actor is the writing
ledger file's name (D90). `fold.fold` drops a `spawn-failed reason=cancelled` from any
other actor into `ignored`."""
import os
import pathlib
import re
import signal
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SPAWN_RE = re.compile(r"^L-[a-z-]+-\d+$")
ACTORS = {"executor", "operator", "thinker"}   # the actor set fold.fold() admits for reason=cancelled, restated for the pre-write refusal
START_TYPES = ("build-started", "spawn-started")
TERMINAL = ("spawn-done", "spawn-failed", "spawn-stale")


def actor_of(filename):
    parts = pathlib.Path(filename).stem.split("-")
    return "-".join(parts[1:-1]) if len(parts) >= 3 else pathlib.Path(filename).stem


def _is_terminal(e):
    if e.get("type") not in TERMINAL:
        return False
    # A cancel forged by an actor the fold ignores does not end anything here either.
    return not (e["type"] == "spawn-failed" and e.get("reason") == "cancelled"
                and e.get("actor") not in ACTORS)


def cancel(spawn, *, force=False, ledger_file=None):
    """Returns (exit_code, message). Writes nothing on a refusal."""
    import dispatch, fold, panes
    if not SPAWN_RE.match(spawn or ""):
        return 2, f"cancel: {spawn!r} is not a spawn id (L-<role>-NNNN)"
    ledger_file = ledger_file or os.environ.get("DOIT_LEDGER_FILE", "L-operator-local.jsonl")
    actor = actor_of(ledger_file)
    if actor not in ACTORS:
        return 1, (f"cancel: refused — actor {actor!r} (from {ledger_file}) may not cancel; "
                   f"only {', '.join(sorted(ACTORS))}")
    events = fold.read_events()
    start = next((e for e in events if e.get("type") in START_TYPES and e.get("spawn") == spawn), None)
    if start is None:
        return 1, f"cancel: refused — {spawn} has no start event in the ledger"
    if any(e.get("spawn") == spawn and _is_terminal(e) for e in events):
        return 1, f"cancel: refused — {spawn} already has a terminal event"
    seat = dispatch.SEAT
    claimed = (seat / f"{spawn}.claimed").is_file()
    if claimed and not force:
        return 1, f"cancel: refused — {spawn} is claimed (a pane is working); --force records the cancel anyway"
    role = start.get("role") or ("builder" if start["type"] == "build-started" else
                                 "-".join(spawn.split("-")[1:-1]))
    seat.mkdir(parents=True, exist_ok=True)
    (seat / "cancelled").mkdir(parents=True, exist_ok=True)
    for suffix in ("packet.md", "cmd.json"):          # (a) moved, never deleted
        src = seat / f"{spawn}.{suffix}"
        if src.exists():
            os.replace(src, seat / "cancelled" / src.name)
    (seat / f"{spawn}.cancelled").write_text(f"cancelled by {actor}\n")      # (b)
    signalled = False
    pid = start.get("waiter_pid")
    if pid and start.get("waiter_host") == socket.gethostname() and start.get("waiter_proc_start") \
            and panes._is_live(pid, {"procStart": start.get("waiter_proc_start")}):
        try:                                                                  # (c)
            os.kill(int(pid), signal.SIGTERM)
            signalled = True
        except (OSError, ValueError):
            pass
    base = {"subject": start.get("subject"), "project": start.get("project"), "spawn": spawn}
    path = fold.EVENTS / ledger_file
    path.parent.mkdir(parents=True, exist_ok=True)
    reason = dispatch.emit(path, {k: v for k, v in base.items() if v is not None}, "spawn-failed",
                           reason="cancelled", why=f"cancelled by {actor}", role=role)   # (d)
    if reason:
        return 1, f"cancel: the terminal event was refused: {reason}"
    return 0, f"cancelled {spawn} (waiter {'signalled' if signalled else 'not signalled'}{', forced' if claimed else ''})"


def main(argv):
    force = "--force" in argv
    args = [a for a in argv if a != "--force"]
    if len(args) != 1 or args[0].startswith("-"):
        print("usage: doit cancel <spawn> [--force]", file=sys.stderr)
        return 2
    code, msg = cancel(args[0], force=force)
    print(msg, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
