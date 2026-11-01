"""Answer an inbox source, preserving its ref and emitting recognized structured rulings.

  doit thinker-answer <src> '<why or canonical ruling>' [<revert>]
  doit thinker-answer <src> 'base-red-accepted tests/test_a.py::test_a'
  doit thinker-answer <src> 'drop-from-window L-spec-1234 unsafe --window win-1'
  doit thinker-answer <src> 'grant-extend L-spec-1234 src/exact.py'

JSON objects with kind/nodes/spec/paths/reason/window are also accepted.
"""
import json
import os
from pathlib import Path
import sys

import fold
import rulings
import merge_gate


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    source, answer = argv[:2]
    event = next((e for e in fold.read_events() if e.get('_src') == source), None)
    if event is None or event.get('type') not in ('question', 'escalation-blocking'):
        print('thinker-answer: source must name a question or escalation', file=sys.stderr)
        return 2
    try:
        ruling = rulings.parse(answer, subject=event['subject'], window=event.get('window'))
        if ruling and ruling['kind'] == 'grant-extend':
            merge_gate.check_grant(ruling['paths'], 'ruling grant-extend')
    except (ValueError, merge_gate.Undetermined) as exc:
        print('thinker-answer: '+str(exc), file=sys.stderr)
        return 2
    fields = dict(project=event.get('project') or 'albert-scott', ref=source, why=answer,
                  revert=argv[2] if len(argv) == 3 else 're-raise the escalation')
    if ruling:
        fields['ruling'] = ruling
    previous = os.environ.get('DOIT_LEDGER_FILE')
    try:
        ledger = os.environ.get('THINKER_LEDGER', 'L-thinker-local.jsonl')
        if fold.actor_of(Path(ledger).name) not in ('thinker', 'operator'):
            print('thinker-answer: THINKER_LEDGER must belong to thinker/operator', file=sys.stderr)
            return 2
        os.environ['DOIT_LEDGER_FILE'] = ledger
        fold.append(['decision', event['subject']] + [k+':='+json.dumps(v) for k, v in fields.items()])
    finally:
        if previous is None:
            os.environ.pop('DOIT_LEDGER_FILE', None)
        else:
            os.environ['DOIT_LEDGER_FILE'] = previous
    print('answered '+event['subject']+' ('+source+')')
    return 0


if __name__ == '__main__':
    sys.exit(main())
