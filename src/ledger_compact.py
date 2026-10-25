"""Declared, value-preserving view of ledger history; raw files stay untouched."""

PROJECTION: dict[str, tuple[str, ...]] = {
    'tree-reaped': ('retained', 'retained_reason'),
    'tick': ('recorded_reap',),
}


def compact(event: dict) -> dict:
    kind = event.get('type')
    if not isinstance(kind, str) or kind not in PROJECTION:
        return event
    result = event.copy()
    if kind == 'tree-reaped' and 'retained_count' not in result and isinstance(result.get('retained'), list):
        result['retained_count'] = len(result['retained'])
    for field in PROJECTION[kind]:
        result.pop(field, None)
    return result
