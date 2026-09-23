#!/usr/bin/env python3
"""pane_end — a Planner pane ends its own OS process, and only when it may.

  pane_end.py <charter> --handover <path> [--root <do-it root>]

Step ⑦'s last act. The pane walks its OWN ancestor chain to the nearest process
named `claude` and sends it SIGTERM — never an inline `kill` the harness could
refuse, and never a keystroke into somebody's tmux. Process exit is the end
signal; there is no other.

It signals only when all four preconditions hold, in this order:

  1. an `l1-complete` for this charter is on the ledger AND its actor is inside
     `fold.EMITS["l1-complete"]` (`{"planner", "operator"}`). The actor is the
     ledger FILENAME (D90) — a bare type+subject filter would accept a row
     sitting in `L-builder-0001.jsonl`, which `fold.fold()` drops into
     `ignored` and the board never counts, so the pane would end on a claim
     the system does not hold.
  2. the handover file exists and is non-empty (content before event, §9.2
     rule 3 — the artifact is on disk before anything records it).
  3. `relay.pending_packets(events, root)` returns nothing outstanding.
     Ending over a pending `.packet.md` strands it with no server.
  4. the supervised marker (`DOIT_SUPERVISED`) is present and non-empty in the
     child environment — something must be able to replace this pane, or
     ending it is just destroying the session.

★ UNDETERMINED IS NEVER CLEAN. Every one of the four reads refusal as its
failure state: a sibling module that is not importable yet, a `/proc` entry
that will not read, a raised exception anywhere in a check — all identical to
"the precondition does not hold". The reverse default would end panes on the
strength of a missing file.

Then, and only then: append exactly one `planner-ended` (into the caller's own
ledger file, so D90 stamps it `planner`), confirm it landed, and signal.
Content first, event second, signal last — the pane must never die leaving no
record that it meant to.

Every effect that could touch a real process or a real ledger arrives through a
parameter the caller overrides, so `test_pane_end.py` sends no signal and
writes into no root but its own fixture.
"""
import argparse, importlib, os, pathlib, signal, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold, up  # noqa: E402

# ── the seams, pinned by name so a sibling's spec can be grepped against them ──
RELAY_MODULE = "relay"              # src/relay.py, written by relay-queries
RELAY_PENDING = "pending_packets"   # relay.pending_packets(events, root)
#                                     -> list[{"spawn": str, "age_min": float}]
SUPERVISED_ENV = "DOIT_SUPERVISED"  # the supervised_marker: an env var, NOT an import

CLAUDE = "claude"                   # the process name the walk stops at
L1 = "l1-complete"
ENDED = "planner-ended"


def ancestor_claude_pid(pid=None, proc_root=pathlib.Path("/proc")):
    """The nearest ANCESTOR named `claude`, or None. Never the immediate parent
    by assumption: the pane's chain is typically claude → shell → python, and a
    one-hop answer would signal the shell. Self is not considered — this process
    is the module's own interpreter, not the pane.

    Returns None rather than guessing on: an unreadable `/proc` entry, a
    malformed status file, pid 1 reached with no `claude`, or a parent cycle.
    None is a refusal upstream, which is the whole point.
    """
    proc_root = pathlib.Path(proc_root)
    try:
        cur = int(pid) if pid is not None else os.getpid()
    except (TypeError, ValueError):
        return None
    seen = set()
    while True:
        try:
            name, parent = _status(proc_root, cur)
        except (OSError, ValueError):
            return None
        if parent is None or parent in seen or parent <= 1:
            return None
        seen.add(parent)
        try:
            pname, _ = _status(proc_root, parent)
        except (OSError, ValueError):
            return None
        if pname == CLAUDE:
            return parent
        cur = parent


def _status(proc_root, pid):
    """(Name, PPid) out of `/proc/<pid>/status`. One file, both facts — `stat`
    carries the name inside parentheses that may themselves contain spaces and
    parentheses, and parsing that is how this kind of walk goes wrong."""
    name, ppid = None, None
    for line in (proc_root / str(pid) / "status").read_text().splitlines():
        if line.startswith("Name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("PPid:"):
            ppid = int(line.split(":", 1)[1].strip())
    return name, ppid


def supervised(child_env=None):
    """True only when the marker is PRESENT and NON-EMPTY. Absent, empty, or a
    mapping that raises on lookup all read False — a marker that could default
    to True is not an interlock (L-adr-0031)."""
    env = os.environ if child_env is None else child_env
    try:
        return bool(str(env.get(SUPERVISED_ENV) or "").strip())
    except Exception:
        return False


def authorized_l1(charter, events):
    """Is there an `l1-complete` for this charter from an actor the fold would
    COUNT? `EMITS` is applied in exactly one place — `fold.fold()` — so reading
    `read_events()` without re-applying it accepts rows the board ignores."""
    allowed = fold.EMITS.get(L1) or set()
    return any(e.get("type") == L1
               and fold.charter_id(e.get("subject")) == fold.charter_id(charter)
               and (not allowed or e.get("actor") in allowed)
               for e in events)


def bind_root(root):
    """`fold.ROOT` and `fold.EVENTS` are module globals bound at import from
    DOIT_ROOT. Accepting a `root` and merely passing it along leaves `--root`
    decorative while the reads and the append still go to whatever the
    environment said — and the tests, which set DOIT_ROOT before importing,
    would never notice. So bind it."""
    if root is None:
        return fold.EVENTS
    root = pathlib.Path(root)
    fold.ROOT, fold.EVENTS = root, root / "events"
    return fold.EVENTS


def _pending(pending_packets, events, root):
    """The relay's answer, or a refusal. An ImportError (the sibling has not
    merged), an AttributeError (it merged under another name) and any raised
    exception are all read as "outstanding" — never as "clear"."""
    fn = pending_packets
    if fn is None:
        importlib.invalidate_caches()
        fn = getattr(importlib.import_module(RELAY_MODULE), RELAY_PENDING)
    return list(fn(events, root))


def check_and_end(charter, handover, *, root=None, child_env=None,
                  pending_packets=None, kill=os.kill,
                  find_ancestor=ancestor_claude_pid):
    """(ended, reason). No partial effect on any refusal: nothing is appended
    and nothing is signalled unless all four checks pass and the pid resolves."""
    events_dir = bind_root(root)
    handover = pathlib.Path(handover)

    try:
        events = fold.read_events()
    except OSError as exc:
        return False, f"refused: the ledger under {events_dir} could not be read ({exc})"

    # 1 — an AUTHORIZED l1-complete, or nothing happens.
    if not authorized_l1(charter, events):
        return False, (f"refused: no authorized {L1} for {charter} on the ledger under "
                       f"{events_dir} (authorized actors: "
                       f"{', '.join(sorted(fold.EMITS.get(L1) or {'any'}))})")

    # 2 — the handover is on disk and says something. Content before event.
    try:
        size = handover.stat().st_size
    except OSError:
        return False, f"refused: handover file {handover} does not exist"
    if size == 0:
        return False, f"refused: handover file {handover} is empty (0 bytes)"

    # 3 — nothing outstanding at the seat relay, and an unanswerable question
    #     counts as outstanding.
    try:
        outstanding = _pending(pending_packets, events, fold.ROOT)
    except Exception as exc:
        return False, (f"refused: pending-packet check unresolved — "
                       f"{RELAY_MODULE}.{RELAY_PENDING} raised {type(exc).__name__}: {exc}")
    if outstanding:
        named = ", ".join(str(p.get("spawn", p) if isinstance(p, dict) else p)
                          for p in outstanding[:3])
        return False, f"refused: {len(outstanding)} pending seat packet(s) outstanding ({named})"

    # 4 — something can replace this pane.
    if not supervised(child_env):
        return False, (f"refused: the supervised marker {SUPERVISED_ENV} is absent or empty "
                       f"— no supervisor can replace this pane")

    # The pid is resolved BEFORE the append: a walk that cannot find the pane
    # must not leave a `planner-ended` behind for an end that never happened.
    pid = find_ancestor()
    if not pid:
        return False, (f"refused: no ancestor process named {CLAUDE!r} found above this one "
                       f"— nothing to signal")

    fold.append([ENDED, str(charter), f"handover={handover}"])   # re-read asserted inside
    kill(pid, signal.SIGTERM)
    return True, f"ended: SIGTERM to {CLAUDE} pid {pid}; {ENDED} appended for {charter}"


def _own_stem(child_env):
    """Which pane is `check_and_end_executor`'s own — `child_env`'s
    `DOIT_LEDGER_FILE`, falling back to `os.environ`'s (the convention
    `fold.append`/`up._child_env`/`think.py` already use, 0187 Unknowns §7).
    An unresolvable stem reads as `""`, which `up.quiet_point` never matches
    against any real `message-sent` row — a refusal, never a guess."""
    led = (child_env or {}).get("DOIT_LEDGER_FILE") or os.environ.get("DOIT_LEDGER_FILE") or ""
    return up._stem(led) if led else ""


def check_and_end_executor(handover, *, root=None, child_env=None,
                           pending_packets=None, kill=os.kill,
                           find_ancestor=ancestor_claude_pid):
    """R3's Executor-side ⑦: end THIS pane's own OS process, and only when it
    may. Mirrors `check_and_end`'s four-precondition shape — `up.quiet_point`
    in place of `l1-complete` (no charter argument: the pane's own quiet point
    IS the criterion), handover, pending packets, then the supervised marker —
    in that order, so a fresh root with nothing on it refuses deterministically
    at the FIRST precondition, ancestor-walk-independent (AC12(c)).

    Returns `0` on a clean end (after signalling), `1` on any refusal; prints
    the reason to stdout on success, stderr on refusal — this function does its
    own printing (unlike `check_and_end`, whose caller prints).

    Appends NO event of its own (0187 Assumption 4): `executor_loop`
    unconditionally appends `spawn-done` once the OS process exits, and this
    pane's own `message-sent` handover is already the durable record of why —
    a `check_and_end_executor`-authored event would be redundant."""
    events_dir = bind_root(root)
    handover = pathlib.Path(handover)
    child_env = {} if child_env is None else child_env
    stem = _own_stem(child_env)

    try:
        events = fold.read_events()
    except OSError as exc:
        print(f"refused: the ledger under {events_dir} could not be read ({exc})", file=sys.stderr)
        return 1

    # 1 — the pane's own quiet point: its own message-sent handover AND nothing
    #     REAL in flight (tick.spawn_in_flight — an escalation elsewhere never
    #     stalls this, Assumption 7/AC6(d)).
    if not up.quiet_point(events, stem):
        print(f"refused: {stem or '(no DOIT_LEDGER_FILE)'} has not reached a quiet point — no "
              f"message-sent handover on its own file yet, or something real is still in flight",
              file=sys.stderr)
        return 1

    # 2 — the handover is on disk and says something. Content before event.
    try:
        size = handover.stat().st_size
    except OSError:
        print(f"refused: handover file {handover} does not exist", file=sys.stderr)
        return 1
    if size == 0:
        print(f"refused: handover file {handover} is empty (0 bytes)", file=sys.stderr)
        return 1

    # 3 — nothing outstanding at the seat relay, and an unanswerable question
    #     counts as outstanding.
    try:
        outstanding = _pending(pending_packets, events, fold.ROOT)
    except Exception as exc:
        print(f"refused: pending-packet check unresolved — "
              f"{RELAY_MODULE}.{RELAY_PENDING} raised {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if outstanding:
        named = ", ".join(str(p.get("spawn", p) if isinstance(p, dict) else p)
                          for p in outstanding[:3])
        print(f"refused: {len(outstanding)} pending seat packet(s) outstanding ({named})",
              file=sys.stderr)
        return 1

    # 4 — something can replace this pane.
    if not supervised(child_env):
        print(f"refused: the supervised marker {SUPERVISED_ENV} is absent or empty "
              f"— no supervisor can replace this pane", file=sys.stderr)
        return 1

    # The pid is resolved BEFORE any effect: a walk that cannot find the pane
    # must signal nothing.
    pid = find_ancestor()
    if not pid:
        print(f"refused: no ancestor process named {CLAUDE!r} found above this one "
              f"— nothing to signal", file=sys.stderr)
        return 1

    kill(pid, signal.SIGTERM)
    print(f"ended: SIGTERM to {CLAUDE} pid {pid} — {stem}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pane-end", description=__doc__.splitlines()[0])
    ap.add_argument("charter", nargs="?", default=None,
                    help="the charter id this pane planned (L-charter-NNNN) — the Planner path")
    ap.add_argument("--executor", action="store_true",
                    help="the Executor path: quiet point in place of l1-complete, no charter")
    ap.add_argument("--handover", required=True, help="path to the handover file, already written")
    ap.add_argument("--root", default=None, help="the do-it root to read and append under")
    a = ap.parse_args(argv)
    if a.executor:
        return check_and_end_executor(a.handover, root=a.root)
    if not a.charter:
        ap.error("a charter (L-charter-NNNN), or --executor, is required")
    ended, reason = check_and_end(a.charter, a.handover, root=a.root)
    if not ended:
        print(reason, file=sys.stderr)
        return 1
    print(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
