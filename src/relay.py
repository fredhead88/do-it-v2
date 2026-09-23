#!/usr/bin/env python3
"""relay — the fold queries that drive the Planner's charter queue (L-charter-0020).

  plannable(events, root)        -> (ready, waiting)   which charter is next, and why not the rest
  waiting_lines(events, root)    -> the PLANNER WAITING ON block, ONE shape for every renderer
  ledger_changed(root, mark)     -> (changed, mark)    has the ledger moved since a prior look
  sequencing(source, events)     -> a declared after:/alongside:/conflicts: block, or None
  pending_packets(events, root)  -> dispatched seats with no answer yet
  unserved(events, root)         -> seat dispatches nobody claimed in time (R6)
  planner_attempts(events, cid)  -> {attempts, last_reason, next}

Queries, and nothing else. Nothing here appends an event, opens a pane or spawns
anything: the launcher, the pane and the board import these and own every side
effect. That is why the module can be read by four callers at once without any of
them having to agree on an order.

**An unmeasured check is never reported as a pass** (L-adr-0030). Two rules carry
that: every `waiting` reason ends by naming the sort keys that were unavailable
for that charter, and a candidate whose cut file is not on disk is not silently
cleared — the footprint check is skipped AND `waiting_lines` says so, in a `note:`
line that stands whether or not anything is ready. A total order that was never
computed is never implied.
"""
import hashlib, os, pathlib, re, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import audit, fold  # noqa: E402

# R8's two types. The `EMITS` rows that AUTHORIZE them live in fold.py — one
# authorization table, never a second copy here (§8.2). These constants exist so
# no caller spells the type as a literal and drifts from the row.
PLANNER_STARTED = "planner-started"
PLANNER_ENDED = "planner-ended"

# The states a spec is in when it has already committed to a footprint (L-adr-0028).
HOLDING = ("written", "building")
# §3.5's count throttle, and the ONE place its number lives.
THROTTLE_N = 2
DRY = "PLANNER WAITING ON: no open charters"
WAITING_HEAD = "PLANNER WAITING ON"

# `after:` / `alongside:` / `conflicts:`, line-anchored, in a raw charter body.
# The bold form is admitted because a charter is prose a human wrote: `**after:**`
# is the same declaration and refusing it would make the block depend on markup.
BLOCK = re.compile(r"^\s*[-*]?\s*\**(after|alongside|conflicts)\**\s*:\s*(.*)$", re.I | re.M)
KEYS = ("after", "alongside", "conflicts")
DATE_LINE = re.compile(r"^\s*date:\s*(\S+)\s*$", re.I | re.M)


# ── the declared block ───────────────────────────────────────────────────────

def _ids(v):
    """The charter ids on one declared value. `think.covers`' convention: space- (or
    comma-) separated, and an explicit `none` is a declaration of nothing rather
    than an omission, exactly as `Covers: none` is."""
    if v is None:
        return []
    items = v if isinstance(v, (list, tuple)) else re.split(r"[,\s]+", str(v))
    out = []
    for raw in items:
        tok = str(raw).strip().strip("`*,")
        if not tok or tok.lower() in ("none", "null"):
            continue
        if tok not in out:
            out.append(tok)
    return out


def sequencing(source, events):
    """A charter's declared sequencing block, from EITHER source form.

    `source` is a mapping (`.get("after")` … — a `charter-filed` event, whose values
    the fold may hand back as a JSON array or as a space-separated string) or a
    `str` (the raw charter body, scanned line-anchored). Both forms exist because
    both callers exist: the plannability side holds an event, the landing side
    holds the file it is about to point at, and a seam one of them cannot read is
    not a seam.

    Returns `None` — never an empty block — when NONE of the three is present. That
    distinction is load-bearing: `plannable`'s throttle runs only where no block was
    declared at all, so "declared nothing" and "declared `alongside:` only" must not
    collapse into the same value.

    `unknown` lists every named charter id with no `charter-filed` event anywhere in
    `events`, a charter naming itself included: R4 draws no self-reference carve-out,
    and a typo'd id that silently gated nothing is the failure this catches."""
    found = {}
    if isinstance(source, str):
        for m in BLOCK.finditer(source):
            k = m.group(1).lower()
            found[k] = found.get(k, []) + _ids(m.group(2))
    elif hasattr(source, "get"):
        for k in KEYS:
            raw = source.get(k)
            if raw is not None:
                found[k] = _ids(raw)
    if not found:
        return None
    filed = {e.get("subject") for e in events if e.get("type") == "charter-filed"}
    block = {k: found.get(k, []) for k in KEYS}
    named = [c for k in KEYS for c in block[k]]
    block["unknown"] = sorted({c for c in named if c not in filed})
    return block


# ── landing, the one definition both the after: gate and the throttle use ────

def _landed(cid, specs, charters):
    """L-charter-0020's `landed`, derived and never stamped: the charter's own state
    is terminal, OR it owns at least one non-void spec and every one of them is
    `accepted` or `closed-shipped`.

    ★ A charter with ZERO specs is NOT landed here. `all()` over an empty set is
    True, so the obvious spelling would report every not-yet-planned charter as
    landed — which would make `after:` gate on nothing and the throttle count
    nothing. The vacuous-truth trap is the whole reason this is a function."""
    c = charters.get(cid)
    if c and c.get("state") in ("L2-complete", "retracted"):
        return True
    mine = [s for s in specs.values() if s["charter"] == cid and s["state"] != "void"]
    return bool(mine) and all(s["state"] in ("accepted", "closed-shipped") for s in mine)


def landed(charter_id, events):
    """`_landed` over a fold of `events` — the public form, for a caller that holds
    the ledger and not a fold."""
    specs, charters, _, _ = fold.fold(events)
    return _landed(charter_id, specs, charters)


# ── the sort keys ────────────────────────────────────────────────────────────

def _goal_context(events):
    """(date, {requirement ids}) of the NEWEST `goal-filed` event — `think.goal_path`'s
    rule, re-derived here rather than imported: a query module that imports the
    authoring CLI inherits its argparse and its side effects.

    Only the newest goal file is consulted, and that is a decision, not a shortcut:
    requirement ids are per-file labels (`L-goal-0001.md` and `L-goal-0002.md` both
    declare G1–G6), so scanning every goal file would hand one charter two dates."""
    e = next((e for e in reversed(events) if e.get("type") == "goal-filed" and e.get("path")), None)
    if not e:
        return None, set()
    p = pathlib.Path(str(e["path"]))
    text = p.read_text() if p.is_file() else ""
    date = e.get("date")
    if not date:
        m = DATE_LINE.search(text)
        date = m.group(1) if m else None
    ids = set(audit.LISTED.findall(audit.section(text, "Requirements") or ""))
    return (str(date) if date else None), ids


def _covers(evs):
    """The goal requirement ids a charter's own `charter-filed` event claims."""
    e = next((e for e in reversed(evs) if e.get("type") == "charter-filed"), None)
    return set(_ids(e.get("covers")) if e else [])


def _sort_key(cid, goal_date, set_order):
    """L-adr-0030's `(goal_date, set_order, charter_id)`, with a None sorting AFTER
    any present value at that position — a dated charter precedes an undated one and
    the comparison never crosses a None with a str."""
    return ((goal_date is None, goal_date or ""), (set_order is None, set_order or ""), cid)


def _unavailable(goal_date, set_order):
    """The disclosure clause every `waiting` reason carries. `set_order` is always
    `None` today — the charter set's order is prose no fold query reads (L-adr-0030) —
    so this parenthetical is what stops "not ordered by it" reading as "ordered"."""
    miss = [n for n, v in (("goal_date", goal_date), ("set_order", set_order)) if v is None]
    return f" (order keys unavailable: {', '.join(miss)})" if miss else ""


# ── the footprint check ──────────────────────────────────────────────────────

def cut_footprint(cid, root):
    """The union of a candidate's declared unit footprints, from `content/cut-<id>.md`
    via `audit.units` — the same parse `doit audit` runs, never a second one. `None`
    means THE FILE IS NOT THERE (the check cannot run); `[]` means a cut exists and
    declares no paths."""
    p = pathlib.Path(root) / "content" / f"cut-{cid}.md"
    if not p.is_file():
        return None
    return sorted({f for u in audit.units(p.read_text()) for f in u["footprint"]})


def _footprint_clash(cid, paths, events, specs):
    """The first collision between this candidate's cut and a spec of ANOTHER charter
    that is `written` or `building`. Read off the `spec-written` event's own
    `footprint` field (spec-writer.schema.json), never re-derived from a spec body:
    the event is what the spec committed to."""
    want = set(paths)
    for e in events:
        if e.get("type") != "spec-written":
            continue
        s = specs.get(e.get("subject"))
        if not s or s["state"] not in HOLDING or s["charter"] == cid:
            continue
        shared = sorted(want & set(e.get("footprint") or []))
        if shared:
            return f"footprint: {s['id']} ({s['state']}) holds {shared[0]}"
    return None


# ── the queue ────────────────────────────────────────────────────────────────

def open_charters(events, by_subject=None):
    """Every charter that is filed and not yet cut, complete or retracted — the queue
    the Planner draws from, in ledger order."""
    if by_subject is None:
        _, _, _, by_subject = fold.fold(events)
    out = []
    for sid, evs in by_subject.items():
        if not sid.startswith("L-charter-"):
            continue
        types = {e["type"] for e in evs}
        if "charter-filed" in types and not (types & {"cut-written", "l1-complete", "charter-retracted"}):
            out.append(sid)
    return out


def plannable(events, root=None):
    """(ready, waiting). `ready` is every open charter no check excludes, sorted by
    `(goal_date, set_order, charter_id)`; `waiting` is `[(charter_id, reason)]` for
    every other open charter, ONE reason each — the first check that hit, in this
    order: standing escalation, the declared block (`after:` then `conflicts:`), the
    count throttle, the footprint intersection.

    **The throttle rule, chosen once.** §3.5's count runs ONLY where `sequencing()`
    returned `None` — no block declared at all. Any declared block skips it, a block
    declaring `alongside:` and nothing else included; `alongside:` therefore gates
    nothing and excludes nothing from a tally that never runs for it. This is R4's
    own sentence and a recorded deviation from L-adr-0028's narrower bullet, which
    scoped the replacement to `alongside:` alone."""
    root = pathlib.Path(root or fold.ROOT)
    specs, charters, _, by_subject = fold.fold(events)
    escalated = {e.get("subject"): e for e in fold.open_escalations(events)}
    goal_date_of, goal_ids = _goal_context(events)
    blocked_count = sorted(cid for cid, c in charters.items()
                           if any(e["type"] == "cut-written" for e in c["evs"])
                           and not _landed(cid, specs, charters))

    ready, waiting = [], []
    for cid in open_charters(events, by_subject):
        evs = by_subject[cid]
        goal_date = goal_date_of if (goal_date_of and (_covers(evs) & goal_ids)) else None
        set_order = None                    # L-adr-0030: carried, never populated
        tail = _unavailable(goal_date, set_order)
        why = None
        if cid in escalated:
            why = f"escalation open: {escalated[cid].get('why', 'escalation')}"
        block = sequencing(next((e for e in reversed(evs) if e["type"] == "charter-filed"), {}), events)
        if why is None and block:
            for x in block["after"]:
                if not _landed(x, specs, charters):
                    why = f"after: {x} has not landed"
                    break
            for y in block["conflicts"] if why is None else []:
                hit = sorted(s["id"] for s in specs.values()
                             if s["charter"] == y and s["state"] in HOLDING)
                if hit:
                    why = f"conflicts: {y} holds {hit[0]} ({specs[hit[0]]['state']})"
                    break
        if why is None and block is None and len(blocked_count) >= THROTTLE_N:
            why = f"count throttle: {blocked_count[0]} not landed"
        if why is None:
            paths = cut_footprint(cid, root)
            if paths:                        # None = unmeasured (disclosed in waiting_lines)
                why = _footprint_clash(cid, paths, events, specs)
        (waiting.append((cid, why + tail)) if why else
         ready.append((_sort_key(cid, goal_date, set_order), cid)))
    return [cid for _, cid in sorted(ready)], waiting


def unmeasured_footprints(events, root=None):
    """The open charters whose footprint check could not run, because
    `content/cut-<id>.md` is not on disk. Skipping the check is correct — a charter
    is normally cut AFTER it is picked — but a skipped check that says nothing reads
    as a passed one, so this is what `waiting_lines` discloses."""
    root = pathlib.Path(root or fold.ROOT)
    return [cid for cid in open_charters(events) if cut_footprint(cid, root) is None]


def waiting_lines(events, root=None):
    """THE `PLANNER WAITING ON` text — one producer, so the board and the pane render
    the same lines by construction rather than by two authors agreeing.

    Never empty: a dry queue is a measurement and renders as one line of its own,
    which a renderer must show rather than omit. Deterministic: two calls on the same
    `events`/`root` return the same list."""
    root = pathlib.Path(root or fold.ROOT)
    ready, waiting = plannable(events, root)
    notes = [f"note: footprint check unmeasured for {cid} (no content/cut-{cid}.md)"
             for cid in unmeasured_footprints(events, root)]
    if not ready and not waiting:
        return [DRY]
    return ([WAITING_HEAD] + [f"  {cid} · {why}" for cid, why in waiting]
            + (["  (nothing waiting)"] if not waiting else []) + notes)


# ── the ledger watermark ─────────────────────────────────────────────────────

def ledger_changed(root, watermark=None):
    """(changed, watermark) over `$root/events/*.jsonl` — name, size and mtime of
    every ledger file, hashed. A supervisor polls this instead of re-folding, and
    `watermark=None` is the first look, which is ALWAYS changed: nothing has been
    seen yet, and a first look that reported "unchanged" would skip the first fold."""
    d = pathlib.Path(root or fold.ROOT) / "events"
    parts = []
    for f in sorted(d.glob("*.jsonl")) if d.is_dir() else []:
        st = f.stat()
        parts.append(f"{f.name}:{st.st_size}:{st.st_mtime_ns}")
    mark = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]
    return watermark != mark, mark


# ── dispatched seats with no answer ──────────────────────────────────────────

def pending_packets(events, root=None):
    """Seats whose packet was written and whose answer has not come back — the SPAWN
    rule, not the file rule: the packet exists, the `.output.json` does not, and the
    `spawn-started` has no `spawn-done`/`spawn-failed`/`spawn-stale` after it (the
    terminal set `tick.in_flight` already uses). A packet on disk with no
    `spawn-started` is not pending — nothing was dispatched."""
    seat = pathlib.Path(root or fold.ROOT) / "seat"
    started, terminal = {}, {}
    for e in events:
        sid = e.get("spawn")
        if not sid:
            continue
        if e.get("type") == "spawn-started":
            started[sid] = e
        elif e.get("type") in ("spawn-done", "spawn-failed", "spawn-stale"):
            terminal[sid] = e
    out = []
    for sid, e in sorted(started.items()):
        t = terminal.get(sid)
        if t is not None and fold.ts(t.get("ts")) >= fold.ts(e.get("ts")):
            continue
        if not (seat / f"{sid}.packet.md").is_file() or (seat / f"{sid}.output.json").exists():
            continue
        out.append({"spawn": sid,
                    "age_min": (fold.NOW - fold.ts(e.get("ts"))).total_seconds() / 60.0})
    return out


# ── unserved seats: a dispatched packet nobody claimed ───────────────────────

# The terminal set `pending_packets` and `tick.in_flight` already use — named here
# rather than re-derived, so a fourth definition of "resolved" never appears.
TERMINAL = {"spawn-done", "spawn-failed", "spawn-stale"}


def unserved(events, root=None, since_h=24):
    """R6/L-spec-0189: one `{spawn, role, subject, age_min, status}` row per
    seat-backend start event (`spawn-started{backend:"seat"}` for every role but the
    builder, `build-started{backend:"seat"}` for it — `role` is read off the event's
    own `role` field for the former, and is the literal `"builder"` for the latter,
    the only role `main()` ever emits `build-started` for) whose claim window has
    elapsed with no claim file ever landing on disk.

    `DOIT_SEAT_CLAIM_SEC`'s default (300) is read independently here, never by
    importing `dispatch` — `dispatch.py` imports `models` and shells out to
    `claude --version` at module scope on every import, a cost and a side effect this
    query module must not acquire just to read one integer (this file's own
    `_goal_context` precedent, on `think.goal_path`).

    A row whose seat was ever claimed (`$root/seat/<spawn>.claimed` on disk) is never
    listed, whatever else is true of it — a served seat is served, however long it
    then runs. Absent that: no terminal event yet -> `status: "pending"`, once the
    claim window has elapsed (never decayed by `since_h` — this unit adds no decay of
    its own; a pending row clears the moment ANY terminal event lands, `spawn-stale`
    included). A terminal `spawn-failed{reason: "unserved"}` timestamped within the
    last `since_h` hours -> `status: "failed-unserved"`. Any other terminal event —
    `spawn-done`, `spawn-stale`, or a `spawn-failed` whose `reason` is not
    `"unserved"` — excludes the row: the spawn resolved."""
    root = pathlib.Path(root or fold.ROOT)
    claim_sec = int(os.environ.get("DOIT_SEAT_CLAIM_SEC", 300))
    starts, terminal = {}, {}
    for e in events:
        sid = e.get("spawn")
        if not sid:
            continue
        t = e.get("type")
        if t in ("spawn-started", "build-started") and e.get("backend") == "seat":
            starts[sid] = e
        elif t in TERMINAL:
            terminal[sid] = e
    out = []
    for sid, e in sorted(starts.items()):
        if (root / "seat" / f"{sid}.claimed").exists():
            continue                                     # served — never listed
        role = e.get("role") if e.get("type") == "spawn-started" else "builder"
        term = terminal.get(sid)
        if term is not None:
            # "within the last since_h hours" is a past-facing window ending at
            # fold.NOW: an age of 0 or negative (the terminal event lands AT or
            # AFTER fold.NOW — the ordinary case for a real spawn observed live,
            # since fold.NOW is a snapshot taken once, earlier) is not "in the
            # last since_h hours", it has not aged INTO that window yet.
            if term.get("type") == "spawn-failed" and term.get("reason") == "unserved":
                age_s = (fold.NOW - fold.ts(term.get("ts"))).total_seconds()
                if 0 < age_s <= since_h * 3600:
                    out.append({"spawn": sid, "role": role, "subject": e.get("subject"),
                               "age_min": (fold.NOW - fold.ts(e.get("ts"))).total_seconds() / 60.0,
                               "status": "failed-unserved"})
            continue                                     # any other terminal: resolved
        elapsed = (fold.NOW - fold.ts(e.get("ts"))).total_seconds()
        if elapsed > claim_sec:
            out.append({"spawn": sid, "role": role, "subject": e.get("subject"),
                       "age_min": elapsed / 60.0, "status": "pending"})
    return out


# ── the Planner's attempt tally ──────────────────────────────────────────────

def planner_attempts(events, charter_id):
    """`{attempts, last_reason, next}` for one charter — R7's at-most-one-restart rule
    derived from the ledger, so the launcher keeps no state in memory and a restarted
    launcher cannot forget a failure.

    `mode: "serving"` pairs are ignored (a Planner serving an already-cut charter is
    not an attempt at cutting one). An unmatched trailing `planner-started` is the
    run happening NOW: it is never counted as a failure, or a launcher would escalate
    against its own live pane. A pair whose `reason` is `l1-complete` is a success and
    does not count either."""
    opened, pairs = None, []
    for e in events:
        if e.get("type") not in (PLANNER_STARTED, PLANNER_ENDED):
            continue
        if e.get("mode") == "serving":
            continue
        if not (e.get("subject") == charter_id or fold.charter_id(e.get("charter")) == charter_id):
            continue
        if e["type"] == PLANNER_STARTED:
            opened = e
        elif opened is not None:
            pairs.append((opened, e))
            opened = None
    attempts = sum(1 for _, end in pairs if end.get("reason") != "l1-complete")
    return {"attempts": attempts,
            "last_reason": pairs[-1][1].get("reason") if pairs else None,
            "next": "start" if attempts == 0 else "retry" if attempts == 1 else "escalate"}
