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


if __name__ == '__main__':
    raise SystemExit(main())
