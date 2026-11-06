"""Remember one answer per list of events, inside one process.

A pass asks the same whole-ledger question once per spec. The events do not change during
a pass, so the first answer serves the rest. Nothing is kept on disk, and a new or longer
list recomputes. Callers must not change the returned value.
"""
from functools import wraps


def once_per_list(fn):
    slots = {}

    @wraps(fn)
    def wrapper(events, *args):
        slot = slots.get(args)
        if slot and slot[0] is events and slot[1] == len(events):
            return slot[2]
        value = fn(events, *args)
        slots[args] = (events, len(events), value)
        return value
    return wrapper
