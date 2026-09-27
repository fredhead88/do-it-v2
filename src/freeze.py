#!/usr/bin/env python3
"""freeze — the one place the Thinker's merge-freeze flag file is named.

L-charter-0042 R3: "A freeze is state, not a message." The flag
(`~/.do-it/state/master-frozen-for-codex`, or under `$DOIT_ROOT` when set —
the same fallback `fold.ROOT` uses) is written by the Thinker, by hand,
outside this module; this module only reads it. `freeze.py` imports nothing
of this repo's own (no import cycle with the two modules — `merge_gate.py`,
`fold.py` — that import it).
"""
import os
import pathlib
import re
from datetime import datetime, timezone

FLAG = pathlib.Path(os.environ.get("DOIT_ROOT", str(pathlib.Path.home() / ".do-it"))) \
    / "state" / "master-frozen-for-codex"

# HH:MMZ tried before the longer ISO alternative at each position (regex
# alternation tries left-to-right per position), so `cap 18:00Z` is never
# mis-parsed by the greedier ISO pattern matching a prefix of it.
_CAP_RE = re.compile(
    r"\bcap\s+(\d{1,2}:\d{2}Z|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2}))",
    re.IGNORECASE,
)


def state(flag=None):
    """`None` when the flag file (`flag` if given, else the module's own
    `FLAG` — read as a global, so a test override like `freeze.FLAG = ...`
    takes effect on the next call) does not exist. Otherwise a dict with
    exactly `text` (stripped contents), `since` (the flag file's mtime, as
    an ISO-8601 UTC string), and `cap` (the first `cap HH:MMZ`/ISO token in
    the text, case-insensitive, or `None` when none is present)."""
    flag = flag if flag is not None else FLAG
    if not flag.exists():
        return None
    text = flag.read_text().strip()
    since = datetime.fromtimestamp(flag.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    m = _CAP_RE.search(text)
    return {"text": text, "since": since, "cap": m.group(1) if m else None}
