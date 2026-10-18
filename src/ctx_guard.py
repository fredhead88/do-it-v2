#!/usr/bin/env python3
"""Tell a standing pane once to hand over at half context. Failures are silent."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shlex
import sys

THRESHOLD = 50
STALE_MIN = 10
FUTURE_MIN = 1
INSTRUCTION = {
    "planner": "Context is at {n}%. Finish the step you are on, write your handover file, then run `doit pane-end <subject> --handover <file> --reason ctx`.",
    "thinker": "Context is at {n}%. Finish the step you are on, write your handover file, then run `doit pane-end <subject> --handover <file> --reason ctx`.",
    "executor": "Context is at {n}%. Finish the step you are on, write your handover file, then run `doit pane-end --executor --handover <file> --reason ctx`.",
    "relay": "Context is at {n}%. Finish the step you are on, then run `doit pane-end --relay --reason ctx`.",
}
EVENTS = ("PostToolUse", "UserPromptSubmit")


def hook_args(role):
    """Claude settings: one command in each event's matcher envelope."""
    command = shlex.join(["/usr/bin/python3", str(Path(__file__).resolve()), "--role", role])
    return ["--settings", json.dumps({"hooks": {
        event: [{"hooks": [{"type": "command", "command": command}]}] for event in EVENTS
    }})]


def main(argv=None, stdin=None, root=None, now=None):
    marker = None
    reserved = False
    try:
        args = sys.argv[1:] if argv is None else argv
        if len(args) != 2 or args[0] != "--role" or args[1] not in INSTRUCTION:
            return 0
        payload = json.load(sys.stdin if stdin is None else stdin)
        sid, event = payload.get("session_id"), payload.get("hook_event_name")
        if not isinstance(sid, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid) is None:
            return 0
        if event not in EVENTS:
            return 0
        root = Path(root) if root is not None else Path(os.environ.get("DOIT_ROOT") or Path.home() / ".do-it")
        directory = root / "state" / "pane-ctx"
        state = json.loads((directory / f"{sid}.json").read_text())
        used = state.get("used_percentage")
        if type(used) not in (int, float) or not math.isfinite(used) or used < THRESHOLD:
            return 0
        observed = datetime.fromisoformat(state["observed_at"].replace("Z", "+00:00"))
        current = datetime.now(timezone.utc) if now is None else now
        age = (current - observed).total_seconds()
        if not -FUTURE_MIN * 60 <= age <= STALE_MIN * 60:
            return 0
        sentence = INSTRUCTION[args[1]].format(n=int(used))
        output = (json.dumps({"hookSpecificOutput": {
            "hookEventName": event, "additionalContext": sentence
        }}) if event == "PostToolUse" else sentence)
        marker = directory / f"{sid}.ctx-guard"
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        reserved = True
        os.close(fd)
        print(output, flush=True)
    except Exception:
        # A failed emission must not spend the marker. Other failures never
        # claim one, including a competing hook that already reserved it.
        if reserved:
            try:
                marker.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
