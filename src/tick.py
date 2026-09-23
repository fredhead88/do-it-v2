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
import fcntl, os, pathlib, re, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import carry, dispatch, fold, tree_cleanup  # noqa: E402

# Waiting for the Executor's next durable action. `building` is in flight, not waiting.
# R7/L-spec-0192: `shipped-owed-due` joins — a due criterion is the Executor's own
# action to take (owed_met/a re-grade), not merely a state to observe.
ACTIONABLE = {"written", "graded", "reviewing", "shipped", "shipped-owed-due"}
# §10.5's RETIRE list, verbatim — every pane launcher denies it by name (D119).
# NOT dead with the spawn path: fold.caps(), think.pane_cmd() and up.pane_cmd() read these.
RETIRE = ("subagent-driven-development", "executing-plans", "writing-plans", "requesting-code-review",
          "receiving-code-review", "finishing-a-development-branch", "dispatching-parallel-agents")
MINUTES, USD = 20, 5          # the Executor's declared cap, read by fold.caps() (§4.4 correction 9)
TICK = fold.EVENTS / "L-tick-local.jsonl"
# §3.11's L2 conjunction, quantified over `mine` — a spec in one of these is done for its charter.
SPEC_DONE = ("accepted", "shipped-owed-evidence", "dropped", "closed-unbuilt", "closed-shipped")


def in_flight(ev):
    """Subjects with a spawn started and not ended — off the lane, or the next tick
    dispatches the same work twice. A start older than twice its role's cap with no
    terminal event is a dead wrapper: recorded once as spawn-stale, and the subject
    is back on the lane for the Executor's failed-spawn row.

    A `blocked` event is deliberately NOT read here (R7). The Executor writes one
    when two units share a footprint, and a footprint collision is that unit's own
    wait — never a reason to strike a second, merely adjacent subject off the lane.
    Only an open `escalation-blocking` gates, because that one is the operator's."""
    started = [e for e in ev if e["type"] in ("build-started", "spawn-started")]
    ended = {e.get("spawn") for e in ev if e["type"] in ("spawn-done", "spawn-failed", "spawn-stale")}
    # An open escalation is the operator's: the subject leaves the lane until a
    # decision or an unblocked event lands after it — else every cron tick pays
    # for an Executor that reads the escalation and does nothing.
    last = {}
    for e in ev:
        if e["type"] in ("escalation-blocking", "decision", "unblocked") and e.get("subject"):
            last[e["subject"]] = e["type"]
    busy = {s for s, t in last.items() if t == "escalation-blocking"}
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
        if (fold.NOW - fold.ts(e.get("ts"))).total_seconds() / 60 > 2 * cap:
            sid and dispatch.emit(TICK, {"spawn": sid}, "spawn-stale",
                                  subject=e.get("subject"), role=role, cap_min=cap)
        else:
            busy.add(e.get("subject"))
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


def main():
    """Fold, compute the lane, append exactly one `tick`. Never a spawn (L-adr-0035).

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
        print("tick: one is running; this one is dropped")
        return 0
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
    todo = lane(specs, charters, in_flight(ev), reaped, events=ev, inbound=inbound)
    # The one liveness fact: `fold` reads the newest of these for staleness, and
    # `lane` is the count — the whole record this process leaves behind.
    kv = {"lane": len(todo)}
    if errors:
        kv["carry_error"] = "; ".join(errors)
    dispatch.emit(TICK, {}, "tick", **kv)
    print("tick: idle" if not todo else f"tick: {len(todo)} on the lane\n" + "\n".join(todo))
    return 0


if __name__ == "__main__":
    sys.exit(main())
