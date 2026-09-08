#!/usr/bin/env python3
"""fold — derive every state from the ledger, render board.md.

  fold.py                                    fold, rewrite board.md
  fold.py states                             derived states, one per line
  fold.py events <subject>                   one subject's events, oldest first
  fold.py append <type> <subject> [k=v ...]  append one event, then re-fold

The ledger (~/.do-it/events/*.jsonl) is append-only and never edited. The ACTOR
of an event is the FILENAME it sits in, never a field inside it (D90, §2.5) —
an actor field written by the actor about itself is a stamp, and this design
derives every state precisely so nothing is stamped. Authorization lives here,
in the fold: an event from an actor not permitted to emit it is recorded and
ignored (§9.2), which leaves an audit trail of the attempt.
"""
import collections, json, os, pathlib, sys
from datetime import datetime, timezone

ROOT = pathlib.Path(os.environ.get("DOIT_ROOT", pathlib.Path.home() / ".do-it"))
EVENTS, BOARD = ROOT / "events", ROOT / "board.md"
PROJECT = os.environ.get("DOIT_PROJECT")          # §9.1/D93: a filter, not a second read

# Who may emit what. A type absent here is open to any actor (§2.5).
EMITS = {"verdict": {"grader"}, "review": {"reviewer"}, "shipped": {"executor"},
         "charter-retracted": {"operator"}, "restore-verified": {"drill"},
         "correction": {"operator"},                       # D111
         "spec-closed": {"operator"},                       # D112
         # ★ Asymmetric on purpose. REJECTING is the safe direction — a spurious
         # rejection costs rework, never a false pass — so any judging or gating
         # seat may do it, the Executor included (its merge gate is a gate).
         # CLEARING is the dangerous direction and is judging seats only.
         # The builder is kept out of both: otherwise it could clear the
         # rejections against its own build and reach `accepted` from one seat,
         # which is exactly what the verdict and review rows exist to prevent.
         "rejected-criterion": {"grader", "reviewer", "executor"},
         "criterion-cleared": {"grader", "reviewer"},
         # D101: the reviewer's blocking event, and it blocks HERE — see standing_rejects.
         "must-fix": {"reviewer"},
         # §3.11's L2 conjunct. A charter that could stamp its own review complete
         # is the acceptance hole one level up.
         "charter-review-complete": {"charter-reviewer"},
         "charter-review-not-complete": {"charter-reviewer"},
         # §3.2 rows 3 and 4: the cut and the Plan have one author, and the two
         # fable audits are blind to that author's rationale — which is only
         # meaningful if the artifact they point at is the Planner's. A
         # `plan-written` from any other seat also aims the charter-reviewer's
         # Blindness strip list (packet.py) at a file the Planner never wrote.
         "cut-written": {"planner"}, "plan-written": {"planner"},
         # §2.1/§3.4: the charter has one author, and `doit think --land` is the one
         # path that checks its five sections before the event points at it. A
         # charter-filed from anywhere else is a charter nothing checked — the
         # Planner would cut from it and the charter-reviewer would close against a
         # done-condition that may not exist.
         "charter-filed": {"thinker", "operator"},
         # §3.11's L1 conjunct, and until the first real charter reached it nobody
         # emitted this event at all: every spec of L-charter-0002 was accepted and
         # the charter stayed `open`, so the Executor's close row — sweep-fixpoint,
         # charter-reviewer, reap — was unreachable. L1 is the Planner's claim
         # (every requirement covered by a spec, every spec written, audited and
         # handed); the operator keeps it for an adopted charter whose Planner ran
         # before the command existed.
         "l1-complete": {"planner", "operator"},
         # §4.11's two scripts. Both write as the seat that ran them (D90: the
         # filename), and both make a claim only that seat may make. `deploy-landed`
         # says a sha is LIVE — the clearing direction, and the one event in the
         # ledger that a reader will trust without re-checking; `tree-reaped` is the
         # record of something destroyed. `deploy-started` and `deploy-failed` stay
         # open: reporting a failure is the safe direction (see rejected-criterion).
         "deploy-landed": {"executor", "operator"},
         "tree-reaped": {"executor", "operator"},
         # §3.12's fixpoint claim and the link that discharges one in-scope brief.
         # Both are L2 conjuncts in all but name: a seat that may stamp
         # `sweep-fixpoint` can close a charter over work it never swept, and a seat
         # that may stamp `brief-answered` can discharge a brief no spec answers.
         "sweep-fixpoint": {"executor", "operator"},
         "brief-answered": {"executor", "operator"}}

# §4.4's `May declare` line, one contract at a time — the fold authorizes (§4.6).
# A declaration lands as an event TYPED BY ITS TERM (dispatch.events_for), so a
# term outside the emitting role's list is recorded and ignored like any other
# stamp. `escaped` is on nobody's list on purpose: the builder cannot see the
# audit, so the fold derives it.
DECLARES = {
    "builder": "spec-ambiguity spec-contradiction spec-unbuildable adr-friction decision-wait "
               "footprint-miss loop approaches-exhausted budget-exceeded context-exhausted "
               "bad-cut worked",
    "charter-reviewer": "charter-gap hollow evidence-gap worked",
    "grader": "hollow card-quality evidence-gap gate-infra worked",
    "plan-auditor": "charter-gap seam-undefined bad-cut worked",
    "probe": "charter-gap",
    "research": "",                       # observes a codebase, not the system's health
    "reuse-scout": "unclassified",
    "reviewer": "hollow post-ship-defect regression rework blocked-external evidence-gap unverifiable",
    "spec-auditor": "false-premise stale-current-state premise-from-prose unverified-universal "
                    "reader-not-checked owed-ac wrong-evidence-type adversary-noise "
                    "superseded-by-concurrent-charter gate-gaming audit-scope-expanded",
    "spec-writer": "spec-unbuildable charter-gap seam-undefined adr-friction owed-ac",
}
for _role, _terms in DECLARES.items():
    for _term in _terms.split():
        EMITS.setdefault(_term, set()).add(_role)
# A correction may override anything but these: D90 takes the actor from the
# FILENAME, and a correction that could rewrite it reopens every check below.
UNCORRECTABLE = ("actor", "_src")
APPLIED = []          # corrections that actually landed, for HEALTH. See apply_corrections.

# §12.5 leaves expected dwell UNSET, to be derived from the stage timings the log
# carries. These are the fallback, used until the log has DWELL_MIN_N crossings of
# a stage; after that dwell_days() measures it. (enter, leave) per state.
DWELL_DAYS = {"written": 1, "building": 1, "graded": 1, "reviewing": 1}
STAGES = {"written": ("spec-written", "build-started"), "building": ("build-started", "build-done"),
          "graded": ("build-done", "verdict"), "reviewing": ("verdict", "review")}
DWELL_MIN_N = 3
# §2.5's `count(owed) <= K`. K is unset in §12.5, so the default denies rather
# than invents: no owed evidence may ride into a closed charter until K is measured.
K = int(os.environ.get("DOIT_K", "0"))

NOW = datetime.now(timezone.utc)


def ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return NOW


def age_days(e):
    return (NOW - ts(e.get("ts"))).total_seconds() / 86400


def apply_corrections(events):
    """§9.2 rule 5's other half (D111). An operator-only `correction` names ONE
    prior event by its `file:line` and overrides fields on it. It never deletes:
    the mistake and the fix both stay on the record, which is strictly more than
    an edit would have left. Voiding is an override like any other —
    `set {"type": "voided"}` — so one mechanism covers a mis-fired retraction and
    a mislabelled field alike. Anyone but the operator is ignored here, and
    ignored again in fold(), where the attempt is counted on HEALTH.

    Find the ref the way the ledger is already read:  grep -n . <file>.jsonl
    """
    fix, APPLIED[:] = {}, []
    for e in events:
        if e.get("type") == "correction" and e["actor"] == "operator" and e.get("ref"):
            fix.setdefault(e["ref"], {}).update(e.get("set") or {})
            APPLIED.append(e)
    for e in events:
        for k, v in fix.get(e["_src"], {}).items():
            if k not in UNCORRECTABLE:
                e[k] = v
    return events


def read_events():
    """Every event in every file, oldest first. A torn tail never wedges the fold."""
    out = []
    for f in sorted(EVENTS.glob("*.jsonl")):
        # L-<role>-<nnnn>: the role may itself carry hyphens (L-spec-writer-0007),
        # so strip one machine prefix and one trailing token, never split on all.
        parts = f.stem.split("-")
        actor = "-".join(parts[1:-1]) if len(parts) >= 3 else f.stem
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            e["actor"], e["_src"] = actor, f"{f.name}:{n}"
            out.append(e)
    # ★ Corrections apply BEFORE the project filter, never after (D111). The very
    # mis-write that motivated them was a wrong `project` value — filter first and
    # the correction can never reach the event it exists to fix.
    out = apply_corrections(sorted(out, key=lambda e: (str(e.get("ts", "")), e["_src"])))
    return [e for e in out if not PROJECT or e.get("project") == PROJECT]


def standing_rejects(evs):
    """Criteria a grader rejected or a reviewer marked `must-fix`, that nobody has
    cleared. A spec carrying any of these is NOT awaiting review — it is waiting on
    rework, and the board must not render the two the same way.

    ★ D101 put the reviewer's blocking event here rather than beside it: a
    `must-fix` the fold does not read is advice, and §5.3's rule is that ONE
    standing blocking criterion stops acceptance regardless of who wrote it. The
    wrapper happens to write a `rejected-criterion` alongside each one today; that
    is the wrapper's choice, and acceptance must not depend on it."""
    raised = {e.get("criterion") for e in evs if e["type"] in ("rejected-criterion", "must-fix")}
    return raised - {e.get("criterion") for e in evs if e["type"] == "criterion-cleared"}


def charter_review(evs):
    """The NEWEST charter-review verdict (§3.11's L2 conjunct), not merely the
    presence of a `complete` one: a charter reviewed complete, reopened, and
    reviewed again as not-complete must leave L2, and a set-membership test can
    never say so."""
    seen = [e["type"] for e in evs if e["type"].startswith("charter-review-")]
    return seen[-1] if seen else None


def deadline_passed(s):
    """An unparseable or absent deadline is PAST, never future: a guard that cannot
    establish its answer returns the failure state, and the failure state here is
    "the operator looks at it"."""
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)) < NOW


def overdue_questions(events):
    """§4.9: "wait indefinitely is a wedge, not a default". A `question` whose
    deadline has passed with nothing naming it is the operator's. A `decision` or
    an `unblocked` answers one by `ref` — the `file:line` the fold hands out as
    `src` on `doit events`, which is the same handle a correction uses (D111)."""
    answered = {e.get("ref") for e in events if e.get("type") in ("decision", "unblocked")}
    return [e for e in events if e.get("type") == "question"
            and e["_src"] not in answered and deadline_passed(e.get("deadline"))]


def open_briefs(events):
    """§3.12: completion is a fixpoint, not a checklist. An **in-scope** brief — one
    naming the charter `requirement` it serves, because §2.6 makes the sweep's
    judgment a citation and no citable requirement means `adjacent`, which is the
    Thinker's inbox and not this charter's — holds the sweep open until a spec
    answers it. `brief-answered` names one by `ref`: the `file:line` the fold hands
    out as `src`, the same handle a `decision` and a `correction` use.

    Without this, `sweep-fixpoint` is a stamp. The Executor's own row read "briefs
    open → nothing until specced", so an unanswered brief wedged the close silently
    while nothing stopped the fixpoint being written over it (D7, §7.3)."""
    answered = {e.get("ref") for e in events if e.get("type") == "brief-answered"}
    return [e for e in events if e.get("type") == "brief" and e.get("requirement")
            and e["_src"] not in answered]


def open_escalations(events):
    """Per subject, the NEWEST of escalation-blocking / decision / unblocked — the
    tick's rule verbatim (tick.py's lane filter). A resolved escalation that stays
    under NEEDS YOU while the tick has already put the subject back on the lane is
    two answers to one question, and the board is the one the operator reads."""
    last = {}
    for e in events:
        if e.get("type") in ("escalation-blocking", "decision", "unblocked") and e.get("subject"):
            last[e["subject"]] = e
    return [e for e in last.values() if e["type"] == "escalation-blocking"]


def caps():
    """(wall-clock minutes, dollars) per role, read from the one place each is
    declared — never a second copy here."""
    import dispatch, tick                                  # lazy: both import fold
    return {**{r: (m, u) for r, (_, m, u) in dispatch.ROLES.items()},
            "executor": (tick.MINUTES, tick.USD)}


def over_budget(events):
    """§4.4's Budget field, checked. The spawn's `usage` comes back in the JSON
    result and the wrapper puts it on `spawn-done`; a Budget nothing compares is
    decoration, and §4.4 correction 9 says `budget-exceeded` cannot fire without a
    cap. The actor is the file (D90), so the role — and therefore the cap — is not
    a field the spawn wrote about itself."""
    cap, out = caps(), []
    for e in events:
        if e.get("type") != "spawn-done" or e["actor"] not in cap:
            continue
        tmin, usd = cap[e["actor"]]
        cost, mins = e.get("cost_usd") or 0, (e.get("duration_ms") or 0) / 60000.0
        why = ([f"${cost:.2f} > ${usd}"] if cost > usd else []
               ) + ([f"{mins:.0f}m > {tmin}m"] if mins > tmin else [])
        if why:
            out.append(f"{e.get('spawn') or e['_src']} · {e['actor']} · " + " · ".join(why))
    return out


def spend_rows(events):
    """Seat spend per project, derived HERE at fold time from the ledger and from
    nothing else — no cache, no column, no file (§8.2: never a second truth).
    INFORMATION, never a gate: `over_budget` above owns caps, this sets none and
    fires nothing. Returns (label, dollars, spawns, unpriced), dollars descending
    then label ascending, the unattributed residue last.

    ★ Every project the ledger names gets a row, including one with no spawn at
    all — `$0.00 · 0 spawns` is a MEASUREMENT, and the row is standing so an
    unmeasured project cannot read as a free one. Spawns carrying no `project`
    (tick.py's own, whose base is `{"spawn": …}`) sum into their own row and into
    no named project's figure: unattributed spend is never spread (L-adr-0001).

    A `cost_usd` is a dollar when `float()` gives a finite non-bool. Anything else
    — absent, None, a non-numeric string, NaN/inf, `true` — is UNPRICED: it counts
    as a spawn, adds 0, and SAYS SO in the unpriced count, because a silent zero
    and a real zero are the one pair this row exists to tell apart. Parsing rather
    than rejecting is deliberate: `doit append … cost_usd=1.5` stores the STRING
    "1.5" (see append: a bare value is a string unless unambiguously JSON), and a
    replayed spawn drew real money.
    """
    seeds = {e["project"] for e in events if e.get("project")}
    # ★ R3: under §9.1 the filter value itself seeds a row, so a project with NO
    # event at all reads `$0.00 · 0 spawns` rather than as silence — zero-out-of-
    # zero-spawns is the measurement R3 exists for. Unfiltered there is no name
    # asserted from outside the ledger, so nothing is seeded and the row set stays
    # exactly what the ledger names (§8.2: a registry would be a second truth).
    # The seeded label is UNVOUCHED environment (DOIT_PROJECT) and passes through
    # the same whitespace collapse in row() below as any ledger label.
    rows = {p: [0.0, 0, 0] for p in ((seeds | {PROJECT}) if PROJECT else seeds)}
    for e in events:
        if e.get("type") not in ("spawn-done", "spawn-failed"):
            continue
        r = rows.setdefault(e.get("project") or None, [0.0, 0, 0])   # None: unattributed
        r[1] += 1
        v, usd = e.get("cost_usd"), None
        if not isinstance(v, bool):                      # a bool is not a price
            try:
                usd = float(v)
            except (TypeError, ValueError):
                usd = None
        if usd is None or usd != usd or usd in (float("inf"), float("-inf")):
            r[2] += 1
        else:
            r[0] += usd

    def row(p, usd, n, unpriced):
        # ★ `project` defaults to a DIRECTORY NAME (dispatch.py), so it is not
        # curated: a newline in one would forge a board line or an eleventh
        # section, which is the exact failure §8.3's fixed layout exists to
        # prevent. Collapse all whitespace — the label renders on one line or not
        # at all.
        lab = "(no project)" if p is None else " ".join(str(p).split()) or "(no project)"
        return (p is None, -usd, lab, usd, n, unpriced)
    return [r[2:] for r in sorted(row(p, *v) for p, v in rows.items())]


def dwell_days(by_subject):
    """Expected dwell per state, measured from the log's own stage crossings — the
    fallback stands until DWELL_MIN_N specs have crossed. Twice the MEDIAN crossing,
    not twice the longest: one spec that sat over a weekend would otherwise raise
    the bar for every spec after it, and a wedge alarm nothing can trip is worse
    than no alarm."""
    import statistics
    out = dict(DWELL_DAYS)
    for state, (enter, leave) in STAGES.items():
        obs = []
        for evs in by_subject.values():
            a = next((e for e in evs if e["type"] == enter), None)
            b = a and next((e for e in evs if e["type"] == leave
                            and ts(e["ts"]) >= ts(a["ts"])), None)
            if b:
                obs.append((ts(b["ts"]) - ts(a["ts"])).total_seconds() / 86400)
        if len(obs) >= DWELL_MIN_N:
            out[state] = 2 * statistics.median(obs)
    return out


def spec_state(evs, retracted):
    """accepted / shipped-owed-evidence / dropped, or the pipeline state it is stuck in."""
    types = {e["type"] for e in evs}
    open_rejects = standing_rejects(evs)
    if "shipped" in types and not open_rejects:
        graded = any(e["type"] == "verdict" and e.get("confirmed") for e in evs)
        if graded and "review" in types:
            return "accepted"                                        # §2.5 accepted()
        if any(e["type"] == "owed-ac" and ts(e.get("wake_at")) > NOW for e in evs):
            return "shipped-owed-evidence"                           # D25
    if "spec-closed" in types:
        # D112: the operator closed it without a build — the question it was
        # written to answer got answered another way. Terminal, and deliberately
        # NOT `accepted`: nothing here was graded, reviewed, or verified.
        return "closed-unbuilt"
    charter = next((e.get("charter") for e in reversed(evs) if e.get("charter")), None)
    if charter in retracted and "shipped" not in types:
        return "dropped"                                             # D76, terminal for alarms
    for state, marker in (("shipped", "shipped"), ("reviewing", "verdict"),
                          ("graded", "build-done"), ("building", "build-started"),
                          ("written", "spec-written")):
        if marker in types:
            return state
    return "unknown"


def fold(events):
    by_subject, ignored = collections.defaultdict(list), []
    for e in events:
        allowed = EMITS.get(e.get("type"))
        (ignored if allowed and e["actor"] not in allowed else
         by_subject[e.get("subject", "")]).append(e)

    retracted = {s for s, evs in by_subject.items()
                 if any(e["type"] == "charter-retracted" for e in evs)}
    specs, charters = {}, {}
    for sid, evs in by_subject.items():
        if sid.startswith("L-spec-"):
            specs[sid] = {"id": sid, "state": spec_state(evs, retracted), "evs": evs,
                          "rejects": len(standing_rejects(evs)),
                          "charter": next((e.get("charter") for e in reversed(evs)
                                           if e.get("charter")), None),
                          "age": age_days(evs[-1])}
        elif sid.startswith("L-charter-"):
            charters[sid] = {"id": sid, "evs": evs, "age": age_days(evs[-1])}

    for cid, c in charters.items():
        mine = [s for s in specs.values() if s["charter"] == cid]
        types = {e["type"] for e in c["evs"]}
        owed = sum(1 for s in mine if s["state"] == "shipped-owed-evidence")
        if cid in retracted:
            c["state"] = "retracted"
        elif (mine and all(s["state"] in ("accepted", "shipped-owed-evidence", "dropped",
                                          "closed-unbuilt") for s in mine)
              and "sweep-fixpoint" in types and owed <= K
              and not open_briefs(c["evs"])
              and charter_review(c["evs"]) == "charter-review-complete"):
            c["state"] = "L2-complete"
        elif "l1-complete" in types:
            c["state"] = "L1-complete"
        else:
            c["state"] = "open"
        c["owed"] = owed
        c["briefs"] = len(open_briefs(c["evs"]))
        c["unbuilt"] = sum(1 for s in mine if s["state"] == "closed-unbuilt")
    return specs, charters, ignored, by_subject


def wedged(spec, dwell=None):
    return spec["age"] > (dwell or DWELL_DAYS).get(spec["state"], 1e9)


def render(events, specs, charters, ignored, by_subject):
    looked = max((ts(e["ts"]) for e in events if e.get("type") == "observed"),
                 default=datetime.min.replace(tzinfo=timezone.utc))
    open_blocks = [e for e in events if e.get("type") == "blocked"
                   and not any(x.get("type") == "unblocked" and x.get("ref") == e.get("id")
                               for x in by_subject.get(e.get("subject", ""), []))]
    since = lambda t: [e for e in events if e.get("type") == t and ts(e["ts"]) > looked]
    pick = lambda *st: [s for s in specs.values() if s["state"] in st]
    dwell = dwell_days(by_subject)
    flag = lambda s: "  ⚠ WEDGE" if wedged(s, dwell) else ""

    # ★ Same collapse as spend_rows' labels, and for the same reason: DOIT_PROJECT
    # is operator environment with no event vouching for it, and a newline in it
    # forged an ELEVENTH section here — §8.3's ten are positional. Pre-existing on
    # this line; the spend row now carries the same value, so both are collapsed.
    scope = f" · project={' '.join(str(PROJECT).split())}" if PROJECT else ""
    L = [f"# board · {NOW.isoformat(timespec='seconds')} · fold @ {len(events)}{scope}", ""]

    def block(title, rows, note=""):
        L.append(f"## {title} ({len(rows)}){note}")
        L.extend("  " + r for r in rows)
        L.append("")

    block("NEEDS YOU", [f"{e.get('subject','?')} · {e.get('why','escalation')}"
                        for e in open_escalations(events)]
          + [f"{e.get('subject','?')} · unanswered past {e.get('deadline','no deadline')}"
             f" · {e.get('asks','?')}" for e in overdue_questions(events)])
    block("BLOCKED", [f"{e.get('subject','?')} · {e.get('why','?')} · owner "
                      f"{e.get('owner') or '⚠ NOBODY'} · {age_days(e):.1f}d" for e in open_blocks])
    block("WRITTEN, NOT PICKED UP", [f"{s['id']} · {s['age']:.1f}d{flag(s)}" for s in pick("written")])
    block("IN FLIGHT", [f"{s['id']} · {s['age']:.1f}d{flag(s)}" for s in pick("building")])
    # A standing rejection is the difference between "waiting to be looked at" and
    # "already looked at and failed". Same section, but the row may not read the same.
    block("AWAITING VERIFICATION",
          [f"{s['id']} · {s['state']} · {s['age']:.1f}d"
           + (f"  ⚠ {s['rejects']} REJECTED, needs rework" if s["rejects"] else "") + flag(s)
           for s in pick("graded", "reviewing", "shipped")])
    block("OWED EVIDENCE", [f"{s['id']} · wakes " + str(next(
        (e.get("wake_at") for e in s["evs"] if e["type"] == "owed-ac"), "?"))
        for s in pick("shipped-owed-evidence")])
    # An unbuilt close is invisible in every working section — terminal work does
    # not queue. It surfaces HERE, at the one moment someone asks "was this
    # actually done?" (D112).
    # ponytail: on a charter still `open` it is therefore visible nowhere. The ten
    # sections are fixed and the board answers "what needs you now", which a
    # terminal spec does not. Revisit if a cut spec is ever quietly lost this way.
    block("CHARTER CLOSE", [f"{c['id']} · {c['state']} · {c['owed']} owed (K={K})"
                            + (f" · {c['briefs']} in-scope brief(s) open" if c["briefs"] else "")
                            + (f" · {c['unbuilt']} closed unbuilt" if c["unbuilt"] else "")
                            for c in charters.values()
                            if c["state"] in ("L1-complete", "L2-complete", "retracted")])
    block("SHIPPED SINCE YOU LOOKED", [e.get("subject", "?") for e in since("shipped")])
    block("DECIDED WITHOUT YOU", [f"{e.get('subject','?')} · {e.get('why','?')} · revert "
                                  f"{e.get('revert','⚠ none')}" for e in since("decision")])

    drill = max((ts(e["ts"]) for e in events if e.get("type") == "restore-verified"), default=None)
    health = [f"restore drill: {'never run — the backup is unproven' if not drill else f'{(NOW-drill).days}d ago'}"]
    # D111: a silent correction is an edit with extra steps. Counted from what
    # APPLIED, not from `events` — corrections apply before §9.1's project filter,
    # so a filtered board would otherwise under-report its own repairs.
    if APPLIED:
        health.append(f"corrections applied: {len(APPLIED)} (last: {APPLIED[-1].get('ref','?')})")
    if ignored:
        health.append(f"unauthorized events recorded and ignored: {len(ignored)} "
                      f"(last: {ignored[-1]['_src']})")
    # §4.4 Budget, derived. Not a board section: a spawn that blew its cap is a
    # health signal, not a queue item, and §8.3's ten sections are positional.
    if (ob := over_budget(events)):
        health.append(f"budget-exceeded: {len(ob)} spawn(s) over cap (last: {ob[-1]})")
    # L-charter-0002: what the seats drew, per project. Beside budget-exceeded and
    # for the same reason stated two lines up — a HEALTH signal, never an eleventh
    # section — and unlike it, never a gate: these rows are strings, nothing reads
    # them. Under DOIT_PROJECT the events are already filtered (§9.1), so the row
    # is one project's attributed share and says so rather than claiming a total.
    for lab, usd, n, unpriced in spend_rows(events):
        health.append(f"spend · {lab} · ${usd:.2f} · {n} spawns"
                      + (f" · {unpriced} unpriced" if unpriced else "")
                      + (" · attributed only (§9.1 filter; L-adr-0001)" if PROJECT else "")
                      + " — list-price estimate of seat-drawn work, not money billed;"
                        " standing line, so an unmeasured project cannot read as a"
                        " free one (R2/R3)")
    # ★ §6.7b/§10.2: the override is explicit and writes an event, and THE OVERRIDE
    # COUNT IS ITSELF THE METRIC that says the 7-day window is set wrong. A count
    # nobody renders is not a metric, so it lands here rather than in the script.
    if (ov := [e for e in events if e.get("type") == "install-override"]):
        health.append(f"dependency install overrides: {len(ov)} "
                      f"(last: {ov[-1].get('subject','?')}) — §6.7b: the count says the cooldown is set wrong")
    if (iw := [e for e in events if e.get("type") == "install-warned"]):
        health.append(f"installs allowed on an inconclusive check: {len(iw)} "
                      f"(last: {iw[-1].get('subject','?')}) — §10.2: open, but recorded")
    # D117: the Executor is a tick. A last tick older than twice the interval is
    # the alarm — the same shape as a wake_at passed with no verdict.
    # ponytail: a tick event has no project, so a project-filtered board reads "never".
    tick = max((ts(e["ts"]) for e in events if e.get("type") == "tick"), default=None)
    interval = int(os.environ.get("DOIT_TICK_MIN", "5"))
    ago = (NOW - tick).total_seconds() / 60 if tick else None
    health.append("last tick: never — the Executor has not run (D117)" if not tick else
                  f"last tick: {ago:.0f}m ago" + (f"  ⚠ TICK STALE — over 2×{interval}m; is the cron line installed?"
                                                  if ago > 2 * interval else ""))
    L += ["## HEALTH"] + ["  " + h for h in health] + [""]

    BOARD.parent.mkdir(parents=True, exist_ok=True)
    BOARD.write_text("\n".join(L))
    return "\n".join(L)


def subject_project(subject):
    """A subject's project is whatever its own first event said. The caller's cwd
    is a last resort — a script run from a different directory than the one that
    filed the charter must not strand its events under a second project label,
    invisible to §9.1/D93's filtered board."""
    return next((e["project"] for e in read_events()
                 if e.get("subject") == subject and e.get("project")), None)


def append(argv):
    """Content before event, always (§9.2 rule 3). Never trust the write — re-read (rule 4)."""
    path = EVENTS / os.environ.get("DOIT_LEDGER_FILE", "L-operator-local.jsonl")
    e = {"v": 1, "ts": NOW.isoformat(timespec="seconds"), "type": argv[0], "subject": argv[1],
         "project": PROJECT or subject_project(argv[1]) or pathlib.Path.cwd().name}
    for kv in argv[2:]:
        k, _, v = kv.partition("=")
        # ★ A BARE VALUE IS A STRING unless it is UNAMBIGUOUSLY JSON. Bare numbers
        # are not: an all-digit git sha (`628758778891`) parsed as an integer and
        # went into the ledger unquoted — the same class as the earlier bug where
        # `9b7e02d` became `9`, which survived because that one happened to raise.
        # Identifiers outnumber arithmetic in this ledger, so the default flips.
        # Write `k:=<json>` when a number really is a number.
        if k.endswith(":"):
            k = k[:-1]
            try:
                e[k] = json.loads(v)
            except json.JSONDecodeError:
                e[k] = v
        elif v[:1] in "{[\"" or v in ("true", "false", "null"):
            try:
                e[k] = json.loads(v)
            except json.JSONDecodeError:
                e[k] = v
        else:
            e[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(e, sort_keys=True)
    with open(path, "a") as fh:
        fh.write(line + "\n")
    assert line in path.read_text().splitlines(), f"append to {path} did not land"
    return e


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "board"
    if cmd == "alloc":
        # §2.8: allocated by the fold at write time, max+1, before the event. The
        # import is here and not at the top because dispatch shells out to
        # `claude --version` at import and the board must not pay for that.
        import dispatch
        (ROOT / "content").mkdir(parents=True, exist_ok=True)
        if "--dir" in sys.argv:                      # probe's run directory (§4.6·10)
            p = dispatch.alloc(ROOT / "content", f"L-{sys.argv[2]}-", "")
            p.unlink(), p.mkdir()
            print(p)
            sys.exit(0)
        print(dispatch.alloc(ROOT / "content", f"L-{sys.argv[2]}-", ".md"))
        sys.exit(0)
    if cmd == "append":
        append(sys.argv[2:])
    ev = read_events()
    specs, charters, ignored, by_subject = fold(ev)
    if cmd == "events":                      # one subject's events, oldest first, as the fold read them
        for e in ev:
            if e.get("subject") == sys.argv[2]:
                print(json.dumps({**{k: v for k, v in e.items() if k != "_src"}, "src": e["_src"]}, sort_keys=True))
    elif cmd == "states":
        for sid, s in sorted({**specs, **charters}.items()):
            print(f"{sid}\t{s['state']}")
    else:
        print(render(ev, specs, charters, ignored, by_subject))
