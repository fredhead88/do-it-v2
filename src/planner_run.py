#!/usr/bin/env python3
"""One foreground Planner owner, sharing the supervisor's lifecycle and lock."""
import argparse
import os
import json
import pathlib
import time

import ledger_files
import panes
import sys

import dispatch
import fold
import up


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('charter')
    parser.add_argument('--mode', required=True, choices=('charter', 'ctx-restart'))
    parser.add_argument('--attempt', required=True, type=int)
    args = parser.parse_args(argv)
    try:
        up._planner_args(args.charter, args.mode, args.attempt)
        fold.EVENTS.mkdir(parents=True, exist_ok=True)
        # Setup must also be behind the lock: an occupied invocation does no allocation or install.
        fd = up._planner_lock()
        if fd is None:
            return 3
        try:
            contract = dispatch.AGENTS / 'planner.md'
            if not contract.is_file():
                raise FileNotFoundError('Planner contract missing')
            up.install(contract)
            events = fold.read_events()
            handover = up.ctx_handover(events, args.charter)
            prompt = args.charter
            if handover:
                prompt += f'\nRead the handover file {handover} before continuing.'
            up._run_planner(args.charter, args.mode, args.attempt, prompt, None, up.code_sha(),
                            wrapper_owned=True)
        finally:
            os.close(fd)
    except Exception as exc:
        # Error class only: setup diagnostics must not copy ambient secrets into records/output.
        print(f'planner-run: {type(exc).__name__}', file=sys.stderr)
        return 1
    return 0


def restart_pane(pane_name: str) -> bool | None:
    """Only a freshly persisted, wrapper-owned successor confirms a restart."""
    if panes.ledger_role(pane_name) != "planner":
        return False
    ledger_files.assert_readable(fold.EVENTS)
    try:
        text = ledger_files.read_text(fold.EVENTS, f"{pane_name}.jsonl")
    except FileNotFoundError:
        return False
    own = [json.loads(line) for line in text.splitlines() if line.strip()]
    end = next((e for e in reversed(own) if e.get("type") == "planner-ended"
                and (e.get("planner") or e.get("spawn") or pane_name) == pane_name), None)
    if not end or end.get("reason") not in ("ctx", "held") or not end.get("subject"):
        return False
    ended_at = panes._ts(end.get("ts"))
    if ended_at is None:
        return False
    events = fold.read_events()
    if any(e.get("type") == "planner-started" and e.get("subject") == end["subject"]
           and (panes._ts(e.get("ts")) is None or panes._ts(e["ts"]) >= ended_at) for e in events):
        return False
    before = {str(e.get("_src")) for e in events if e.get("type") == "planner-started"}
    result = up.launch_planner(end["subject"], "ctx-restart", 1)
    if result in ("alive", "starting"):
        return False
    if result != "started":
        raise RuntimeError("Planner restart admission undetermined")
    deadline = time.monotonic() + min(5.0, up.RESTART_CONFIRM_SECONDS)
    while True:
        ledger_files.assert_readable(fold.EVENTS)
        for event in fold.read_events():
            source = str(event.get("_src"))
            stem = pathlib.Path(source.split(":")[0]).stem
            if (event.get("type") == "planner-started" and source not in before
                    and event.get("subject") == end["subject"] and event.get("mode") == "ctx-restart"
                    and event.get("attempt") == 1 and type(event.get("wrapper_pid")) is int
                    and event["wrapper_pid"] > 0 and panes.ledger_role(stem) == "planner"
                    and event.get("planner") == event.get("spawn") == stem):
                return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(.05, remaining))


if __name__ == '__main__':
    raise SystemExit(main())
