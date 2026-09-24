#!/usr/bin/env python3
"""tick — fold the ledger, compute the actionable lane, record one liveness fact.

  tick.py     one tick: fold, compute, record. Scheduled (cron) and poked (every
              dispatch ends with one). One at a time per ledger — flock — and a
              dropped duplicate loses nothing: the durable list is the queue.

A tick starts no Executor, under any backend (L-adr-0035). It is a pure
fold-and-record: it spends nothing, it can be run at any frequency by anything,
and the one event it appends is what `fold` reads for staleness. The Executor is
a supervised pane that reads the same lane for itself; nothing here launches it.
"""
import argparse, fcntl, os, pathlib, re, socket, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import carry, dispatch, fold, intake, notes, pane_resume, panes, tree_cleanup  # noqa: E402
# `relay` is imported LAZILY, inside `wait()` only — importing tick.py must not
# force `relay` (and its own `audit` import) into sys.modules for every caller,
# `pane_end.py` among them, that only wants `up.quiet_point`'s tick.py half.

# Waiting for the Executor's next durable action. `building` is in flight, not waiting.
# R7/L-spec-0192: `shipped-owed-due` joins — a due criterion is the Executor's own
# action to take (owed_met/a re-grade), not merely a state to observe.
ACTIONABLE = {"written", "graded", "reviewing", "shipped", "shipped-owed-due"}
# §10.5's RETIRE list, verbatim — every pane launcher denies it by name (D119).
# NOT dead with the spawn path: fold.caps(), think.pane_cmd() and up.pane_cmd() read these.
RETIRE = ("subagent-driven-development", "executing-plans", "writing-plans", "requesting-code-review",
          "receiving-code-review", "finishing-a-development-branch", "dispatching-parallel-agents")
MINUTES, USD = 20, 5          # the Executor's declared cap, read by fold.caps() (§4.4 correction 9)
TICK_NAME = "L-tick-local.jsonl"
POLL_STEP = 0.05              # tick.wait's poll granularity — fine enough for AC3's <1s wake
# §3.11's L2 conjunction, quantified over `mine` — a spec in one of these is done for its charter.
SPEC_DONE = ("accepted", "shipped-owed-evidence", "dropped", "closed-unbuilt", "closed-shipped")


def tick_path():
    """`L-tick-local.jsonl` under the CURRENT `fold.EVENTS` — resolved fresh on
    every call, never cached at import. `tick.wait` rebinds `fold.ROOT`/
    `fold.EVENTS` to a caller-given root (AC17), and a path computed once at
    import time would keep pointing at the importing process's own DOIT_ROOT no
    matter what root a later call named."""
    return fold.EVENTS / TICK_NAME


def _bind_root(root):
    """Rebind `fold.ROOT`/`fold.EVENTS` to `root`, exactly as `pane_end.bind_root`
    already does — a bare `root` parameter never redirects the module globals
    `dispatch.emit`/`tick_path()` actually read. `None` leaves today's binding
    (this process's own DOIT_ROOT) alone. Duplicated here rather than imported
    from `pane_end` to avoid a cycle: `pane_end` imports `up`, and `up` imports
    `tick`."""
    if root is None:
        return fold.EVENTS
    root = pathlib.Path(root)
    fold.ROOT, fold.EVENTS = root, root / "events"
    return fold.EVENTS


def _spawn_busy(ev):
    """Subjects with a REAL spawn started and not ended — never escalation-busy.
    A start older than twice its role's cap with no terminal event is a dead
    wrapper: recorded once as `spawn-stale` (on the CURRENT `tick_path()`, never
    a stale import-time path), and the subject is back on the lane for the
    Executor's failed-spawn row.

    A same-host, confirmed-dead waiter goes stale immediately, independent of
    elapsed time: when the started event carries a truthy `waiter_pid` and
    `waiter_host` equal to this process's own hostname, `panes._is_live` (reused
    verbatim, built with a synthetic `{"procStart": waiter_proc_start}` meta
    dict) decides liveness — a confirmed-dead waiter never gets the benefit of
    the doubt an unreadable pid/start-time does. An event missing `waiter_pid`/
    `waiter_host`, or naming a different host, falls through to the existing
    elapsed-time check unchanged."""
    started = [e for e in ev if e["type"] in ("build-started", "spawn-started")]
    ended = {e.get("spawn") for e in ev if e["type"] in ("spawn-done", "spawn-failed", "spawn-stale")}
    busy = set()
    for e in started:
        sid = e.get("spawn")
        if sid and sid in ended:
            continue
        # A start with no spawn id is not the wrapper's — a hand-written one, or a
        # pre-wrapper event. It can never be matched to a terminal event, so it is
        # aged out on the role's own cap and there is nothing to name in a
        # `spawn-stale`: the subject simply returns to the lane, where the
        # Executor's failed-spawn row is what looks at it.
        role = "-".join(sid.split("-")[1:-1]) if sid else (e.get("role") or "")
        cap = dispatch.ROLES.get(role, (None, 60, 0))[1]
        waiter_pid = e.get("waiter_pid")
        if waiter_pid and e.get("waiter_host") == socket.gethostname() and \
                not panes._is_live(waiter_pid, {"procStart": e.get("waiter_proc_start")}):
            sid and dispatch.emit(tick_path(), {"spawn": sid}, "spawn-stale",
                                  subject=e.get("subject"), role=role, cap_min=cap, reason="waiter-dead")
            continue
        # L-spec-0269/R1: the subject's own recorded `window_min` (its offered
        # claim window), plus the role's cap for the working wait after a claim
        # — replacing the flat `2 * cap`, which read a spec whose window was
        # raised as a dead wrapper before it had had its full chance to be
        # claimed. An event with no recorded `window_min` (every pre-existing
        # one, and every non-seat-backend one) falls back to `cap`, reproducing
        # `2 * cap` exactly.
        window = e.get("window_min") or cap
        if (fold.NOW - fold.ts(e.get("ts"))).total_seconds() / 60 > window + cap:
            sid and dispatch.emit(tick_path(), {"spawn": sid}, "spawn-stale",
                                  subject=e.get("subject"), role=role, cap_min=cap)
        else:
            busy.add(e.get("subject"))
    return busy


def spawn_in_flight(ev):
    """`in_flight`'s busy set, MINUS the escalation-busy addition — real
    unterminated spawns only. `up.quiet_point`'s own "nothing in flight" half
    calls this, not `in_flight`, so an open `escalation-blocking` on an
    UNRELATED subject elsewhere never stalls an Executor pane's own end.
    Measured: `in_flight`'s merged `busy` set otherwise stalls every Executor
    pane behind any one open operator escalation, anywhere — contrary to R3's
    Goal (0187 Assumption 7, AC6(d))."""
    return _spawn_busy(ev)


def in_flight(ev):
    """Subjects with a spawn started and not ended, PLUS any subject whose
    newest escalation/decision/unblocked row is an open `escalation-blocking` —
    off the lane, or the next tick dispatches the same work twice, or hands the
    Executor a subject the operator is still deciding. `tick.lane`'s own use of
    this is UNCHANGED by 0187 (R7): a lane candidate legitimately wants both
    kinds excluded. `spawn_in_flight` above is the split that does not.

    A `blocked` event is deliberately NOT read here (R7). The Executor writes one
    when two units share a footprint, and a footprint collision is that unit's own
    wait — never a reason to strike a second, merely adjacent subject off the lane.
    Only an open `escalation-blocking` gates, because that one is the operator's."""
    # An open escalation is the operator's: the subject leaves the lane until a
    # decision or an unblocked event lands after it — else every cron tick pays
    # for an Executor that reads the escalation and does nothing.
    last = {}
    for e in ev:
        if e["type"] in ("escalation-blocking", "decision", "unblocked") and e.get("subject"):
            last[e["subject"]] = e["type"]
    busy = {s for s, t in last.items() if t == "escalation-blocking"}
    busy |= _spawn_busy(ev)
    return busy


def closable_fallback(events, charter):
    """`closable(events, charter)` until `the-fold-and-the-board` lands the real one
    in `fold` — (every spec accepted, sweep fixpoint derived, charter-review owed).

    It mirrors `fold.fold()`'s L2 conjunction in FULL, all five conjuncts, not the
    three the seam names: the brief conjunct and `K` ride with the fact they belong
    to (an open in-scope brief is exactly what holds the sweep short of a fixpoint;
    an owed criterion over K is exactly what holds a spec short of accepted). A
    fallback missing either would report a charter closable that `fold.fold()` holds
    at `L1-complete` — dropping it off the lane before the Executor's brief-author
    row can fire, and stranding it short of reap forever."""
    cid = charter["id"] if isinstance(charter, dict) else charter
    evs = charter["evs"] if isinstance(charter, dict) else [e for e in events if e.get("subject") == cid]
    specs = fold.fold(events)[0] if events else {}
    mine = [s for s in specs.values() if s["charter"] == cid and s["state"] != "void"]
    owed = sum(1 for s in mine if s["state"] == "shipped-owed-evidence")
    accepted = all(s["state"] in SPEC_DONE for s in mine) and owed <= fold.K
    fixpoint = "sweep-fixpoint" in {e["type"] for e in evs} and not fold.open_briefs(evs)
    return accepted, fixpoint, fold.charter_review(evs) != "charter-review-complete"


def _carry_packet_busy(busy):
    """L-spec-0190 R9 (fix 5) — sources whose own carry packet names a spec_id
    that is itself in-flight. A filesystem read, not a new event type (EMITS
    has no room for one; A11): glob `content/carry-*.packet.md`, parse the
    spec_id out of each filename, and read the file's trailing non-blank line
    for `Source: v4 spec <X>.`; `<X>` is busy exactly when that spec_id is
    itself in `busy`. No write to any packet file — read-only."""
    out = set()
    content = fold.ROOT / "content"
    if not content.is_dir():
        return out
    for p in sorted(content.glob("carry-*.packet.md")):
        name = p.name
        if not (name.startswith("carry-") and name.endswith(".packet.md")):
            continue
        spec_id = name[len("carry-"):-len(".packet.md")]
        if spec_id not in busy:
            continue
        try:
            lines = [l for l in p.read_text().splitlines() if l.strip()]
        except OSError:
            continue
        if not lines:
            continue
        m = re.match(r"Source:\s*v4 spec\s+(.+)\.$", lines[-1].strip())
        if m:
            out.add(m.group(1).strip())
    return out


def lane(specs, charters, busy=frozenset(), reaped=frozenset(), events=(), inbound=()):
    """A charter that is CLOSED and not yet reaped is still the Executor's.

    §4.11's reaper refuses any charter that is not `L2-complete` or `retracted`,
    and this lane admitted only `L1-complete` — so the moment a charter became
    reapable it left the lane, and `doit reap` was unreachable by any tick. Found
    closing L-charter-0002 (2026-09-08): the charter-reviewer returned complete,
    the fold derived L2 in the same fold, and the tick went idle with two
    worktrees standing. L-charter-0001 had been `retracted` since August with its
    worktree still on disk for the same reason — the sixth defect invisible to a
    passing suite, because nothing tested the lane past L1.

    A charter at `L1-complete` whose `closable()` reads all-accepted, fixpoint
    derived and no review owed is NOT on the lane (R11): there is no decision left
    for the Executor to take, the fold moves it to `L2-complete` on its own, and it
    comes back here once — for the reap. One with a review still owed appears
    exactly as before, because dispatching the charter-reviewer IS the Executor's
    judgment to make.

    A spec's presence never depends on any charter (R6): a `charter: null` spec is
    on the lane on its own terms, and no charter's state, closability or absence
    from `charters` can take it off.

    L-spec-0190 R9: `inbound` is `carry.uncarried()`'s own list of
    `{source, kind, registered_at, attempts, last_error}` dicts. One
    `"inbound:<source> · uncarried"` row lands per item whose `source` is
    neither in `busy` (a direct subject match on an open `escalation-blocking`
    — `in_flight()`'s existing behavior, unchanged) nor the target of a
    packet-detected in-flight carry (`_carry_packet_busy`, fix 5)."""
    def waiting(c):
        if c["state"] in tree_cleanup.CLOSED:
            return c["id"] not in reaped
        if c["state"] != "L1-complete":
            return False
        if not events:
            return True      # nothing to judge closability from: the charter stays the Executor's
        accepted, fixpoint, review_owed = (getattr(fold, "closable", None) or closable_fallback)(events, c)
        return not (accepted and fixpoint and not review_owed)
    packet_busy = _carry_packet_busy(busy)
    return sorted([f"{s['id']} · {s['state']}" for s in specs.values()
                   if s["state"] in ACTIONABLE and s["id"] not in busy]
                  + [f"{c['id']} · {c['state']}" for c in charters.values()
                     if c["id"] not in busy and waiting(c)]
                  + [f"inbound:{i['source']} · uncarried" for i in inbound
                     if i.get("source") not in busy and i.get("source") not in packet_busy])


def _record():
    """One flock-guarded fold+lane+append — the shared core `main()` and
    `wait()` both run under the IDENTICAL non-blocking lock (this module's own
    doctrine: "one at a time per ledger — a dropped duplicate loses nothing,
    the durable list is the queue"). Returns the lane list on a real append,
    `None` when a concurrent tick/wait already holds the lock — the append is
    dropped, not queued (0187 Assumption 5).

    L-spec-0190 R9: `carry.uncarried(ev)` and `carry.sync_v4(ev)` each run once
    per tick, unconditionally (idle or not), each independently exception-guarded
    (fix 8) — one raising call never silences the other's contribution nor the
    tick's own heartbeat. A caught exception adds nothing of its own to the lane;
    the same `tick` event instead carries an additive `carry_error` field naming
    it, never a second event type."""
    fold.EVENTS.mkdir(parents=True, exist_ok=True)
    lock = open(fold.ROOT / "tick.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    ev = fold.read_events()
    # SD11 (L-spec-0242, extended by L-spec-0244 and L-spec-0243): `intake.run(ev)`
    # fires off the FIRST read, its own try/except so a raise never stops the
    # tick's own heartbeat (mirroring `carry_error` below); `notes.run(ev,
    # note_prs)` and `pr_closeout.run(ev, spec_prs)` both follow, each reading
    # its own slice of the SAME `intake.run(ev)` call's return, and both sit on
    # the SAME `intake_error` field, `; `-joined — then the ledger is RE-READ
    # before anything else, so a same-tick `inbound-registered`/
    # `inbound-awaiting`/`inbound-note-listed`/`inbound-note-gone`/
    # `inbound-closed`/`inbound-pr-commented` append is what
    # `carry.uncarried`/`fold.fold`/`lane` see.
    intake_errors = []
    r = {}
    try:
        r = intake.run(ev) or {}
    except Exception as e:
        intake_errors.append(f"run: {e}")
    try:
        notes.run(ev, r.get("note_prs") or [])
    except Exception as e:
        intake_errors.append(f"notes: {e}")
    try:
        import pr_closeout
        pr_closeout.run(ev, r.get("spec_prs", []))
    except Exception as e:
        intake_errors.append(f"pr_closeout: {e}")
    ev = fold.read_events()
    specs, charters, _, _ = fold.fold(ev)
    reaped = {e.get("subject") for e in ev if e["type"] == "tree-reaped"}
    errors = []
    try:
        inbound = carry.uncarried(ev)
    except Exception as e:
        inbound = []
        errors.append(f"uncarried: {e}")
    try:
        carry.sync_v4(ev)
    except Exception as e:
        errors.append(f"sync_v4: {e}")
    # L-spec-0274/R3: a stalled Claude pane says why, and this restarts what
    # can be restarted — its own try/except, on this SAME re-read `ev`, never
    # merged into `carry_error` (a different concern, a different field).
    pane_resume_error = None
    try:
        pane_resume.run(ev)
    except Exception as e:
        pane_resume_error = f"{e}"
    todo = lane(specs, charters, in_flight(ev), reaped, events=ev, inbound=inbound)
    # The one liveness fact: `fold` reads the newest of these for staleness, and
    # `lane` is the count — the whole record this process leaves behind.
    kv = {"lane": len(todo)}
    if errors:
        kv["carry_error"] = "; ".join(errors)
    if intake_errors:
        kv["intake_error"] = "; ".join(intake_errors)
    if pane_resume_error:
        kv["pane_resume_error"] = pane_resume_error
    dispatch.emit(tick_path(), {}, "tick", **kv)
    return todo


def main():
    """Fold, compute the lane, append exactly one `tick`. Never a spawn (L-adr-0035)."""
    todo = _record()
    if todo is None:
        print("tick: one is running; this one is dropped")
        return 0
    print("tick: idle" if not todo else f"tick: {len(todo)} on the lane\n" + "\n".join(todo))
    return 0


def _events_state(exclude_name):
    """name -> (size, mtime_ns) for every `events/*.jsonl` file EXCEPT
    `exclude_name` — the exclusion-aware snapshot `wait()` cross-checks a
    positive from `relay.ledger_changed` against, so its OWN appended `tick`
    (on return) can never register as a real change to itself or a concurrent
    waiter (AC16)."""
    d = fold.EVENTS
    out = {}
    if d.is_dir():
        for f in sorted(d.glob("*.jsonl")):
            if f.name == exclude_name:
                continue
            st = f.stat()
            out[f.name] = (st.st_size, st.st_mtime_ns)
    return out


def wait(root=None, max_s=300):
    """Block until the ledger moves or `max_s` seconds pass, then tick exactly
    once and return which happened: `"changed"` or `"interval"`.

    AC17: rebinds `fold.ROOT`/`fold.EVENTS` to `root` FIRST (as
    `pane_end.bind_root` already does), so its own tick lands under the given
    root, not the importing process's DOIT_ROOT.

    AC2: never reports "changed" on its own first look. `relay.ledger_changed`
    reads `watermark=None` as ALWAYS changed (nothing has been seen yet), so a
    baseline is established once, up front, and that first "changed" is
    discarded rather than compared against.

    AC16: `L-tick-local.jsonl` is excluded from what counts as "changed" —
    `relay.ledger_changed`'s hash is the heartbeat this polls, but a positive
    from it is trusted only once a SEPARATE, exclusion-aware snapshot
    (`_events_state`, every `events/*.jsonl` file except the tick file itself)
    confirms a REAL file moved. Otherwise every wake would append a tick that
    wakes the next waiter — a feedback loop."""
    import relay                                             # noqa: PLC0415 — lazy; see the top-of-file note
    _bind_root(root)
    _, mark = relay.ledger_changed(fold.ROOT)               # baseline; its own "changed" is discarded
    excl = _events_state(TICK_NAME)
    end = time.monotonic() + max_s
    reason = "interval"
    while True:
        changed, mark = relay.ledger_changed(fold.ROOT, mark)
        if changed:
            new_excl = _events_state(TICK_NAME)
            if new_excl != excl:
                reason = "changed"
                break
            excl = new_excl          # only the tick file moved — not a real change; keep polling
        if time.monotonic() >= end:
            break
        time.sleep(min(POLL_STEP, max(0.0, end - time.monotonic())))
    _record()                        # exactly one tick per RETURNING call (AC4); a lock-drop loses
    return reason                    # nothing per this module's own doctrine (Assumption 5)


def _wait_cli(argv=None):
    ap = argparse.ArgumentParser(prog="doit wait",
                                 description="Block until the ledger moves or SEC seconds pass, then tick once.")
    ap.add_argument("--max", type=float, default=300.0, dest="max_s",
                    help="max seconds to wait (default 300)")
    a = ap.parse_args(argv)
    print(wait(max_s=a.max_s))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "wait":
        sys.exit(_wait_cli(sys.argv[2:]))
    sys.exit(main())
