#!/usr/bin/env python3
"""L-spec-0755/R4: a ruling that amends an AC must reach the spec file before any
packet reads it. `stale_spec_reason` is the fail-loud guard `packet.Ctx.spec_file()`
calls; the free text of a decision never goes anywhere near a packet."""
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

AC_ID = re.compile(r"AC\d+")


def amended_ids(e):
    """The `AC<n>` ids a decision's `amends=` names. A token that is not exactly
    `AC<digits>` is never used; a value with no valid token is returned raw so the
    refusal still names what the operator typed."""
    raw = e.get("amends")
    if isinstance(raw, (list, tuple)):
        toks = [str(x).strip() for x in raw]
    else:
        toks = [t.strip() for t in str(raw or "").split(",")]
    toks = [t for t in toks if t]
    good = [t for t in toks if AC_ID.fullmatch(t)]
    return good or toks


def _dt(s):
    d = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def stale_spec_reason(events, subject, spec_path):
    """A refusal string when `subject` has a `decision` carrying `amends=AC<n>[,...]`
    whose ts is later than the spec file's mtime (floored to the second) and no
    `spec-written` for the subject landed after it; else None. A decision without
    `amends=` is unaffected. An unreadable mtime or ts refuses — undetermined is
    never clean."""
    amending = [e for e in events if e.get("subject") == subject and e.get("type") == "decision"
                and e.get("amends")]
    if not amending:
        return None
    try:
        mtime = datetime.datetime.fromtimestamp(int(os.stat(spec_path).st_mtime), datetime.timezone.utc)
    except (OSError, ValueError) as exc:
        return f"{subject}: cannot read the spec file's mtime ({exc}) with an amending decision on file"
    written = []
    for e in events:
        if e.get("subject") == subject and e.get("type") == "spec-written":
            try:
                written.append(_dt(e.get("ts")))
            except (ValueError, TypeError):
                continue
    stale = []
    for e in amending:
        try:
            when = _dt(e.get("ts"))
        except (ValueError, TypeError):
            return f"{subject}: a decision amending {e.get('amends')} carries an unreadable ts"
        if when > mtime and not any(w > when for w in written):
            stale.append((when, e))
    if not stale:
        return None
    ids = sorted({i for _, e in stale for i in amended_ids(e)})
    last = max(stale, key=lambda p: p[0])[1]
    return (f"{subject}: the decision at {last.get('ts')} amends {', '.join(ids)} but the spec file "
            f"({spec_path}) has not been edited since — edit the spec file, run `doit shape`, "
            "then rebuild the packet")


def checked_spec(events, subject, path, die):
    """`path`, unless an amending decision on `subject` (an `L-spec-*`) postdates it —
    then `die(reason)` (packet.Ctx.spec_file's seam)."""
    reason = str(subject).startswith("L-spec-") and stale_spec_reason(events, subject, path)
    return die(reason) if reason else path


def standing_item(events, owed):
    """Packet item 8 for a re-grade (R3(b)): WHICH criteria stand rejected — ids only,
    never the old reasons — as a one-element list, or [] when none stand."""
    import fold
    ids = sorted(s for s in fold.standing_rejects(events) if isinstance(s, str)
                 and s != "COMMIT-SHAPE" and s not in owed)
    return ["8. Standing rejections to re-judge (ids only; list each you now judge met in `cleared`): "
            + ", ".join(ids) + "."] if ids else []
