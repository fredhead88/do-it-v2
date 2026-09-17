#!/usr/bin/env python3
"""panes — a pane's identity: its name, whether it is live, and what never landed.

Three functions, one module, no imports out of the stdlib:

  pane_name(ledger_file)              the pane's launch name AND message address
  live_panes(sessions_dir, events)    one record per pane running RIGHT NOW
  unrecorded_messages(events)         decisions/findings that travelled by message only

A pane is named after its own ledger file (`-n L-thinker-0068`) so that the
address `SendMessage` needs and the actor the fold derives from the FILENAME
(D90) are the same string. An operator-typed name (`think-spend`) is addressable
by nobody: it has no relationship to the file the session writes.

Liveness is a predicate over the harness's own per-pid session files, never a
directory listing: `<pid>.json` with an all-digit stem, that pid alive on this
host, and the file's `procStart` equal to that pid's own start time as the OS
reports it — the last conjunct is the pid-reuse guard, and without it a register
of "who is running" is a register of who ran. Where the OS start time cannot be
read, pid-liveness alone decides and the record is still returned; there is no
third state and no "maybe" row.

`fold` is deliberately NOT imported. It would bind every age here to `fold.NOW`,
the import-time timestamp at fold.py:126, which goes stale the moment a
long-running caller (the Executor's loop, the board) holds this module open.
Ages come from a `now` the caller may inject.

Everything read out of a session file is DATA: `name` and `cwd` pass through
verbatim, and the only derived path is the STRING `"<name>.jsonl"`, computed
only when the name is an `L-<role>-NNNN` id whose role is a real ledger role. A
pane named `../../etc/passwd` therefore yields `ledger_file: None` and opens
nothing.
"""
import json, os, pathlib, re
from datetime import datetime, timezone

SESSIONS = pathlib.Path.home() / ".claude" / "sessions"

# The ledger roles that really exist, measured under ~/.do-it/events on
# 2026-09-17. This list is what keeps `ledger_file` from being fabricated out of
# an arbitrary pane name; a new role joins it when a role joins the system.
ROLES = frozenset("builder charter-reviewer executor grader plan-auditor planner probe "
                  "research reviewer spec-auditor spec-writer thinker".split())
# L-<role>-<nnnn>, where the role may itself carry hyphens (L-spec-writer-0007) —
# the same shape `fold.read_events` derives the actor from.
LEDGER_ID = re.compile(r"^L-([a-z][a-z0-9-]*[a-z0-9])-(\d{4,})$")


def pane_name(ledger_file):
    """The pane's launch name and message address, from its ledger file.

    Accepts all three forms this repo already passes around — a full path, a
    filename (`ledger.name`), or a bare stem (`ledger.stem`) — because both
    `.name` and `.stem` appear within four lines of each other in
    `think.py:open_session`, and a function that accepted only one of them would
    be a second naming rule rather than the one.
    """
    return pathlib.PurePath(str(ledger_file)).name.removesuffix(".jsonl")


def ledger_role(name):
    """The role of an `L-<role>-NNNN` pane name, when that role is a real one.
    Anything else — a derived harness name, a traversal string, a content id
    whose role is `spec` — is None, and None is what keeps a path out."""
    m = LEDGER_ID.match(str(name or ""))
    return m.group(1) if m and m.group(1) in ROLES else None


def proc_start(pid):
    """A pid's own start time, as the OS reports it: field 22 of
    `/proc/<pid>/stat`, the same value the harness writes into `procStart`
    (measured equal for both live panes on 2026-09-17). The comm field can
    contain spaces and parentheses, so the split starts after the LAST `)`.
    None when it cannot be read — a kernel without procfs, or a pid that died
    between the probe and the read."""
    try:
        raw = pathlib.Path(f"/proc/{int(pid)}/stat").read_text()
        return raw[raw.rindex(")") + 2:].split()[19]
    except Exception:
        return None


def alive(pid):
    """Signal 0 is the probe and the only signal this module ever sends.
    EPERM means the process exists and is not ours — still alive."""
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _is_live(pid, meta):
    """R14's predicate, stated once. procStart is compared only when BOTH sides
    have it: a file the harness wrote without one, or a host whose start time is
    unreadable, falls back to pid-liveness rather than inventing a third state
    (and rather than silently dropping a pane the operator can see)."""
    if not alive(pid):
        return False
    mine, theirs = proc_start(pid), meta.get("procStart")
    if mine is None or theirs in (None, ""):
        return True
    return str(theirs) == str(mine)


def _ts(v):
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _last_event_age_days(ledger_file, events, now):
    """Age in days of the newest event in that ledger file. The join is on
    `_src` (`"<ledger-file-name>:<line-number>"`, set by fold.read_events), so
    no file is opened here — the events are already in hand."""
    if not ledger_file:
        return None
    newest = None
    for e in events or ():
        src = str(e.get("_src") or "")
        if src.rpartition(":")[0] != ledger_file:
            continue
        t = _ts(e.get("ts"))
        if t and (newest is None or t > newest):
            newest = t
    return None if newest is None else (now - newest).total_seconds() / 86400


def live_panes(sessions_dir=None, events=(), now=None):
    """One record per pane running right now, joined to its ledger.

    Reads only. Never signals anything but the liveness probe, never writes,
    and never raises on a file it cannot parse — a half-written session file
    during a pane's startup must not take the board down with it.
    """
    d = pathlib.Path(os.path.expanduser(str(sessions_dir))) if sessions_dir else SESSIONS
    now = now or datetime.now(timezone.utc)
    out = []
    try:
        files = sorted(d.glob("*.json"))
    except OSError:
        return out
    for f in files:
        # Only an all-digit stem is a session file. `*.key` and stray `*.md`
        # files sit in this directory too (measured) and are ignored in silence.
        if not f.stem.isdigit():
            continue
        try:
            meta = json.loads(f.read_text())
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        pid = int(f.stem)
        try:
            if not _is_live(pid, meta):
                continue
        except Exception:
            continue
        name = meta.get("name")
        role = ledger_role(name)
        ledger_file = f"{name}.jsonl" if role else None
        out.append({
            "name": name,
            # Verbatim from the file: whether the operator passed `-n` is the
            # difference between a pane that can be addressed and one that
            # cannot, and it is not this module's to decide.
            "name_source": meta.get("nameSource"),
            "contract": meta.get("agent") or role,
            "cwd": meta.get("cwd"),
            # The harness's own field, passed through. `shell` is real and
            # measured; coercing status into busy/idle would erase it.
            "status": meta.get("status"),
            "ledger_file": ledger_file,
            "last_event_age_days": _last_event_age_days(ledger_file, events, now),
        })
    return out


def unrecorded_messages(events):
    """R13, under ONE predicate: a `message-sent` event carrying a `decision` or
    a `finding` whose `src` is absent, or whose `src` names no event in the
    ledger, is a decision that travelled only by message — said once here, in
    the spec's Produces, and in AC5. A `ping` carries nothing to lose and is
    never counted."""
    events = list(events or ())
    known = {e.get("_src") for e in events}
    return sum(1 for e in events
               if e.get("type") == "message-sent"
               and e.get("carries") in ("decision", "finding")
               and (not e.get("src") or e.get("src") not in known))
