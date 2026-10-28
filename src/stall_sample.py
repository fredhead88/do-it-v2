#!/usr/bin/env python3
"""Record a retained, read-only stall census (SD12), including failures."""
import argparse
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sys
import threading

import autodispatch
import dispatch
import fold
from pace import _root, _time, _iso, _record, _read_samples

_CONTEXT_LOCK = threading.RLock()


@contextmanager
def _fold_root(root, now):
    """Scope legacy fold globals; bypass caches and restore correction state."""
    with _CONTEXT_LOCK:
        changes = ((fold, 'ROOT', root), (fold, 'EVENTS', root/'events'), (fold, 'NOW', now),
                   (fold, 'BOARD', root/'board.md'), (dispatch, 'ROOT', root),
                   (dispatch, 'EVENTS', root/'events'), (dispatch, 'CONTENT', root/'content'))
        previous = [(module, key, getattr(module, key)) for module, key, _ in changes]
        applied = list(fold.APPLIED)
        try:
            for module, key, value in changes:
                setattr(module, key, value)
            yield
        finally:
            for module, key, value in reversed(previous):
                setattr(module, key, value)
            fold.APPLIED[:] = applied


def sample(now, *, root=None):
    root = _root(root)
    now = _time(now)
    row = dict(ts=_iso(now), buckets={}, hold_kinds={}, builder_capacity=None, ok=False)
    try:
        row['builder_capacity'] = autodispatch.capacity(root=root)
        with _fold_root(root, now):
            events = fold.read_events(use_cache=False)
            rows = fold.stall_rows(events, now=now)
            buckets, holds = defaultdict(list), {}
            for item in rows:
                buckets[item['bucket']].append(item['spec'])
                if item.get('hold_kind') is not None:
                    holds[item['spec']] = item['hold_kind']
            row.update(buckets=dict(buckets), hold_kinds=holds, ok=True)
    except Exception as error:
        row.update(buckets={}, hold_kinds={}, ok=False, reason=f'{type(error).__name__}: {error}')
    return row


def record(now=None, *, root=None):
    root = _root(root)
    now = _time(now if now is not None else datetime.now(timezone.utc))
    return _record(root/'state/stall-samples.jsonl', sample(now, root=root), now)


def samples(since, *, root=None):
    return _read_samples(_root(root)/'state/stall-samples.jsonl', _time(since))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('sample',))
    parser.parse_args(argv)
    try:
        row = record()
        print(json.dumps(row))
        return 0 if row['ok'] else 1
    except Exception as error:
        print(f'stall sample: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
