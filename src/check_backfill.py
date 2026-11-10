#!/usr/bin/env python3
"""Publish explicit declarations from shipped specs; dry-run unless --apply."""
import argparse
from collections import Counter
import json
import os
import re
import sys
import time

import checks
import fold
import grading_env
import verification

SPEC_ID = re.compile(r'L-spec-[0-9]{4,}')
LEDGER = 'L-check-backfill-local.jsonl'


def declarations(text, sid, sha):
    """Isolate AC blocks, retaining the parser's complete Verification context."""
    section = verification.section(text) or ''
    for cid, block in sorted(grading_env._criterion_blocks(text).items()):
        if not re.search(r'^\s*check:', block, re.M):
            continue
        try:
            record, = checks.declared(block + '\n\n' + section, sid, sha)
            yield cid, record, None
        except ValueError as error:
            # Parser errors describe fields, but never echo untrusted values.
            reason = str(error).removeprefix(cid + ': ')
            safe = ('check: must sit directly below the AC line',
                    'check: requires each declaration key once, in order',
                    'customer_data must be true or false', 'invalid data_source',
                    'invalid answered_by', 'invalid timing', 'No closing quotation',
                    'No escaped character')
            yield cid, None, reason if reason in safe else 'invalid declaration syntax or fields'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--spec')
    args = parser.parse_args(argv)
    if args.spec is not None and not SPEC_ID.fullmatch(args.spec):
        print('check-backfill: --spec: invalid spec ID', file=sys.stderr)
        return 1
    started = time.process_time()
    print('check-backfill: apply' if args.apply else 'check-backfill: dry-run (declared means would append)')
    counts = Counter(declared=0, **{'already-declared': 0, 'malformed': 0})
    errors = 0
    previous = os.environ.get('DOIT_LEDGER_FILE')
    try:
        histories = fold.fold(fold.read_events())[3]
        shipped = {sid: next(e for e in reversed(history) if e['type'] == 'shipped')
                   for sid, history in histories.items()
                   if isinstance(sid, str) and SPEC_ID.fullmatch(sid)
                   and any(e['type'] == 'shipped' for e in history)}
        if args.spec is not None and args.spec not in shipped:
            print(f'{args.spec}: not a shipped subject in selected scope')
            errors += 1
        selected = [args.spec] if args.spec in shipped else [] if args.spec else sorted(shipped)
        os.environ['DOIT_LEDGER_FILE'] = LEDGER
        for sid in selected:
            event = shipped[sid]
            sha, project = event.get('sha'), event.get('project')
            if (not isinstance(sha, str) or not re.fullmatch(r'[0-9a-fA-F]{7,64}', sha)
                    or not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', project)):
                print(f'{sid}: unusable shipped sha/project')
                errors += 1
                continue
            try:
                content = (fold.ROOT / 'content').resolve(strict=True)
                path = (content / (sid + '.md')).resolve(strict=True)
                if not path.is_relative_to(content):
                    raise ValueError('escape')
                text = path.read_text(encoding='utf-8')
            except (OSError, ValueError, RuntimeError):
                print(f'{sid}: missing/unreadable content or path escape')
                errors += 1
                continue
            for cid, record, reason in declarations(text, sid, sha):
                status = 'malformed' if reason else ('already-declared' if
                         checks.record_for(histories[sid], sid, cid) == record else 'declared')
                if status == 'declared' and args.apply:
                    try:
                        destination = fold.EVENTS / LEDGER
                        if not destination.resolve().is_relative_to(fold.EVENTS.resolve()):
                            raise ValueError('ledger escape')
                        fields = record.to_event()
                        fold.append(['check-declared', sid, 'project:=' + json.dumps(project)] +
                                    [key + ':=' + json.dumps(value) for key, value in fields.items()], strict=True)
                    except (OSError, ValueError, RuntimeError, SystemExit):
                        status, reason = 'malformed', 'append refused'
                counts[status] += 1
                if status == 'malformed':
                    errors += 1
                print(f'{sid}/{cid} {status}' + (f' {reason}' if reason else ''))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        print('check-backfill: ledger scan unavailable', file=sys.stderr)
        errors += 1
    finally:
        if previous is None:
            os.environ.pop('DOIT_LEDGER_FILE', None)
        else:
            os.environ['DOIT_LEDGER_FILE'] = previous
    print('summary: ' + ' '.join(f'{key}={counts[key]}' for key in
                               ('declared', 'already-declared', 'malformed')) +
          f' errors={errors} cpu_seconds={time.process_time() - started:.6f}')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
