#!/usr/bin/env python3
"""fold — derive every state from the ledger, render board.md.

  fold.py                                    fold, rewrite board.md
  fold.py states                             derived states, one per line
  fold.py events <subject>                   one subject's events, oldest first
  fold.py append <type> <subject> [k=v ...]  append one event, then re-fold
  fold.py review-owed <charter>              "owed"/"not-owed" — read-only (R11)

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
# Where the harness writes one json per live session — the directory pane-identity
# reads to answer "which panes are up" (R14). Measured on this box 2026-09-17:
# ~/.claude/sessions/<pid>.json. Overridable so a test never depends on the
# operator's real sessions, and so a relocated harness is a config change.
SESSIONS = pathlib.Path(os.environ.get("DOIT_SESSIONS_DIR",
                                       pathlib.Path.home() / ".claude" / "sessions"))
PROJECT = os.environ.get("DOIT_PROJECT")          # §9.1/D93: a filter, not a second read

# Who may emit what. A type absent here is open to any actor (§2.5).
EMITS = {"verdict": {"grader"}, "review": {"reviewer"}, "shipped": {"executor"},
         "charter-retracted": {"operator"},
         # R10/L-spec-0192: `doit restore` may run as the operator or the executor;
         # `"drill"` (the automated proof run) stays exactly as it was.
         "restore-verified": {"drill", "operator", "executor"},
         # R10/L-spec-0192: added now so no later unit's append is dropped, though
         # the code emitting each is out of this footprint.
         "inbound-registered": {"operator", "thinker"},
         "carry-failed": {"executor", "operator"},
         "backup-failed": {"backup"},
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
         # L-spec-0129: a structured, per-charter override of the DONE
         # done-condition's exit-0 clause — subject is the CHARTER id (R3: the
         # ruling is charter-level, every spec under it inherits it), never a
         # spec. `packet.py`'s `done_condition()` reads it off the charter's own
         # event stream, which `fold()` has already authorized here — a
         # `builder`/`grader`/`reviewer` actor's attempt lands in `ignored`, not
         # `by_subject`, so the packet builder never even sees it (R2). Same
         # actor set as `l1-complete`: a charter-level claim, not a build-time one.
         "done-condition-override": {"planner", "operator"},
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
         "brief-answered": {"executor", "operator"},
         # S15/S33 (a-6): the wake script's `verdict` re-grade is one route to
         # discharging an owed criterion; `owed-met` is the other — the Executor or
         # the operator, citing the evidence directly, no re-grade spawn required.
         # Restricted the same way `owed-ac` itself is restricted one level up: a
         # seat that could stamp this on any shipped spec would walk it to
         # `accepted` alone, which is exactly what the verdict+review pair exists
         # to prevent.
         "owed-met": {"executor", "operator"},
         # L-charter-0020 R8: the Planner's own run, opened and closed on the ledger
         # so `relay.planner_attempts` can pair them into a tally and the launcher
         # keeps no state in memory. Restricted to the Planner for the same reason
         # `cut-written` is: a seat that could write `planner-ended reason=l1-complete`
         # about a Planner it is not would retire a charter nobody planned, and a seat
         # that could write `planner-started` would keep one off the queue forever.
         "planner-started": {"planner"}, "planner-ended": {"planner"},
         # L-adr-0043's four new types. Each is scoped to exactly the actor(s) the
         # requirement that created it names, and no wider — EMITS is this system's
         # only authorization gate, and a name opened too far is a stamp waiting to
         # happen.
         # R4: carry-over is the OPERATOR's own command. Not the Thinker (which
         # would be re-deciding what a v4 record already decided) and not the
         # Planner (which would be cutting a spec that is already cut).
         # R10/L-spec-0192: the Executor's own `doit carry` also writes this event
         # (carry.py already runs it through fold.append()); the operator's grant
         # is unchanged.
         "spec-carried": {"operator", "executor"},
         # R8: a conflict is a builder RE-DISPATCH, and the Executor is the only
         # seat that dispatches. A builder recording its own rework attempt would
         # be counting its own collisions.
         "conflict-rework": {"executor"},
         # R13/L-adr-0037: the three PANE contracts, and only those. A dispatched
         # sub-agent does not address a pane — it returns to the seat that spawned
         # it — so a `message-sent` from one is a stamp about a channel it does not
         # have.
         "message-sent": {"planner", "executor", "thinker"},
         # R15/L-adr-0038: the Executor is the only seat with a repository in reach
         # and the only one forbidden to edit it, which is exactly why the event
         # exists and why nobody else may write one on its behalf.
         "repo-edit": {"executor"}}

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
# R10/L-spec-0192: the Executor may RE-DATE an already-declared owed criterion
# (move its `wake_at`), never declare a new one — `fold()` below enforces the
# narrower grant at fold time (subject-scoped, not `check_append`'s business);
# `EMITS` membership alone only gets an executor-authored `owed-ac` as far as
# that second gate.
EMITS["owed-ac"].add("executor")
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


def charter_id(v):
    """The charter an event names, as an ID. Half the ledger's `charter` fields are
    a PATH (`/…/content/L-charter-0002.md`) because that is what the packet hands
    the role, and the ledger is append-only, so both forms are permanent input to
    every future fold. Comparing the raw field to a charter id made L-spec-0005
    belong to no charter at all: `doit reap` skipped its worktree and reported
    `retained: []` — success — while the branch stood, and the charter-reviewer's
    packet named one card for a charter that shipped two. Worse, the L2 conjuncts
    are quantified over `mine`, so a charter could close over a spec it owns that
    was never built. Found closing L-charter-0002 (2026-09-08)."""
    return pathlib.PurePath(str(v)).name.removesuffix(".md") if v else v


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


def charter_review_owed(charter_evs):
    """R11 — the sole predicate for "is a charter-review owed for this charter":
    `_closable`'s `review_owed` conjunct and `doit review-owed` both read this and
    nothing else, so the L2 conjunct and the Executor's dispatch decision can never
    disagree about the same fact.

    `False` only when the charter's own NEWEST `charter-filed` event's `covers`
    field is the explicit string `none`/`null` (case-insensitive, surrounding
    whitespace ignored — the D98/§12.2 convention `think.covers()`/`relay._ids()`
    already write at land time). Every other case — a non-empty id list, an absent
    `charter-filed` event, or a raw JSON `null` `covers` value (a tolerated
    defensive input with no instance on the live ledger today, not a legacy
    population) — falls through UNCHANGED to today's rule: undetermined is never
    clean, only the explicit empty spelling buys "no review owed"."""
    filed = [e for e in charter_evs if e["type"] == "charter-filed"]
    if filed:
        covers = filed[-1].get("covers")
        spelling = "" if covers is None else str(covers)
        if spelling.strip().lower() in ("none", "null"):
            return False
    return charter_review(charter_evs) != "charter-review-complete"


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


# R3's shape, in one place, read by the refusal (append), by the health count
# (malformed_escalations) and by the board (open_questions) — three readers, one
# rule, so a refused write and a counted row can never disagree about what
# "malformed" means.
ESCALATION_TRIPLE = ("default", "deadline", "revert")


def _has(e, k):
    """A field is PRESENT when it carries a non-blank string. `default=` with an
    empty value is the exact shape a hurried pane writes, and treating it as
    present would let the refusal be satisfied by typing the field name."""
    return bool(str(e.get(k) or "").strip())


def escalation_ok(e):
    """§4.9/R3: an escalation-blocking carries `default`+`deadline`+`revert` — all
    three, because a default with no deadline never fires and a deadline with no
    revert fires into something nobody can undo — OR it names the irreversible act
    that forbids a default at all (`irreversible`). The disjunction is the whole
    rule: "wait indefinitely is a wedge, not a default" cannot apply to a deploy
    that has already gone out."""
    return all(_has(e, k) for k in ESCALATION_TRIPLE) or _has(e, "irreversible")


# R3/L-spec-0192 — the required-fields door: exactly these three types carry a
# check beyond bare `EMITS` membership; every other type is membership-only.
# `escalation-blocking`'s own shape is `escalation_ok` (an OR, not a field list)
# and stays special-cased in `required_reason` below; these two are a plain
# field list.
REQUIRED = {"owed-ac": ("criterion",), "spec-carried": ("source", "tier", "audited_at")}


def required_reason(e):
    """Field-only, actor-blind: `None` when `e` carries what its type requires (or
    requires nothing — every type outside `REQUIRED` and `escalation-blocking`).
    Never re-reads the ledger, never looks at actor — the actor question is
    `check_append`'s alone. This is the door `append()` and `dispatch.emit()`
    both apply: a missing required field is refused, an actor/type mismatch is
    not (ADR-0028-3) — that stays recorded-and-ignored at fold time."""
    t = e.get("type")
    if t == "escalation-blocking":
        if escalation_ok(e):
            return None
        return (f"escalation-blocking {e.get('subject', '?')} carries neither "
                "default= AND deadline= AND revert= (a reversible escalation) nor "
                "irreversible=<the act that forbids a default> (an irreversible one)")
    fields = REQUIRED.get(t)
    if fields:
        missing = [k for k in fields if not _has(e, k)]
        if missing:
            return (f"{t} {e.get('subject', '?')} missing required field(s): "
                    + ", ".join(f"{k}=" for k in missing))
    return None


def check_append(event, actor):
    """The one predicate deciding whether a write survives ANY door that calls it
    with the real actor: `required_reason` above, plus `EMITS` actor membership.
    Pure — one event dict, one actor string in, a reason string or `None` out;
    never re-reads the ledger, no side effect (load-bearing: AC16/AC17/AC21 call
    this, and `spec_state`, directly). `append()`/`emit()` apply `required_reason`
    alone — an actor/type mismatch is never refused at either door, only
    recorded-and-ignored at fold time; a caller needing the full any-reason
    refusal (`carry-both-ledgers`, the Executor's `owed-ac` re-date, `doit
    restore`) calls THIS directly with the real actor and refuses on any
    non-`None` return (ADR-0028-3)."""
    reason = required_reason(event)
    if reason:
        return reason
    allowed = EMITS.get(event.get("type"))
    if allowed is not None and actor not in allowed:
        return (f"actor mismatch: {actor!r} may not emit {event.get('type')!r} "
                f"(allowed: {sorted(allowed)})")
    return None


def malformed_escalations(events):
    """Escalations already ON the ledger that carry neither shape. `append()`
    refuses these at the door, but the door is not the only entrance:
    `dispatch.py` and `tick.py` write `escalation-blocking` through
    `dispatch.emit()`, which never passes through here, and the ledger is
    append-only so every pre-R3 escalation is permanent input to every future
    fold. Counting them is the only enforcement those two call sites have until a
    spec in their own footprint updates them."""
    return [e for e in events if e.get("type") == "escalation-blocking" and not escalation_ok(e)]


def open_questions(events):
    """Every open escalation and every overdue question, each with the fields the
    operator needs to ACT rather than to go and look: the default that will be
    applied, the deadline it applies at, the undo, and how long it has been
    waiting.

    The undo column is asymmetric on purpose. An ESCALATION carries either a
    `revert` or the `irreversible` act that is the reason it has no default; a
    QUESTION has neither — its mandatory four are asks/blocks/default/deadline
    (§ "Escalation is a ledger append, not a message") and the revert belongs to
    the `decision` that answers it. So a question row says so in words rather
    than rendering an empty column the reader must interpret, and it is never
    padded with an invented revert."""
    rows = []
    for e in open_escalations(events):
        rows.append({"kind": "escalation", "subject": e.get("subject", "?"),
                     "asks": e.get("why") or "escalation",
                     "default": e.get("default"), "deadline": e.get("deadline"),
                     "revert": e.get("revert"), "irreversible": e.get("irreversible"),
                     "malformed": not escalation_ok(e),
                     "age_days": age_days(e), "src": e["_src"]})
    for e in overdue_questions(events):
        rows.append({"kind": "question", "subject": e.get("subject", "?"),
                     "asks": e.get("asks", "?"),
                     "default": e.get("default"), "deadline": e.get("deadline"),
                     "revert": "revert n/a — question", "irreversible": None,
                     "malformed": False,
                     "age_days": age_days(e), "src": e["_src"]})
    return rows


def question_line(r):
    """One NEEDS YOU row from one open_questions row. A malformed escalation is
    rendered DIFFERENTLY from a compliant one — same section, but the reader must
    not have to notice an absence: the marker names what is missing, because the
    row cannot be acted on until someone supplies it."""
    if r["malformed"]:
        return (f"{r['subject']} · {r['asks']} · ⚠ malformed — carries neither "
                f"default+deadline+revert nor a named irreversible act "
                f"({r['src']}) · {r['age_days']:.1f}d")
    if r["kind"] == "question":
        return (f"{r['subject']} · unanswered past {r['deadline'] or 'no deadline'}"
                f" · {r['asks']} · default {r['default'] or '⚠ none'}"
                f" · {r['revert']} · {r['age_days']:.1f}d")
    # Both may be present — an escalation can carry a revert AND still name an act
    # that is already irreversible. Neither is dropped: the revert the operator
    # would run and the thing it cannot undo are different facts.
    undo = " · ".join(([f"revert {r['revert']}"] if _has(r, "revert") else [])
                      + ([f"irreversible: {r['irreversible']}"] if _has(r, "irreversible") else []))
    absent = "n/a — irreversible act named"
    return (f"{r['subject']} · {r['asks']}"
            f" · default {r['default'] or absent}"
            f" · deadline {r['deadline'] or absent}"
            f" · {undo} · {r['age_days']:.1f}d")


def decide_overdue(events):
    """§4.9's other half, mechanised: a REVERSIBLE overdue question — one carrying
    a recorded default — is settled BY that default, as a `decision` event, and
    the existing DECIDED WITHOUT YOU block shows it without one line of new
    rendering. An irreversible one (no default) is untouched and stays the
    operator's, which is the entire point of the distinction.

    ★ Re-reads the ledger immediately before each write and never trusts the
    `events` snapshot it was handed — append()'s own rule ("never trust the write
    — re-read", §9.2 rule 4) extended to a write that DEPENDS on a read. Two
    back-to-back calls therefore append exactly one decision, even when the second
    is fed the stale pre-first-call snapshot a buggy caller would still be
    holding. Returns what it appended, which on a settled ledger is []."""
    out = []
    for q in overdue_questions(events):
        if not _has(q, "default"):
            continue                       # irreversible: no default to apply
        if not any(x["_src"] == q["_src"] for x in overdue_questions(read_events())):
            continue                       # answered since the snapshot — including by us
        out.append(append(["decision", q.get("subject") or "-", f"ref={q['_src']}",
                           "why=applied the recorded default, unanswered past deadline: "
                           + str(q.get("default"))]))
    return out


def free_standing(events):
    """R6: an accepted spec that names NO charter, with the review tier it was
    accepted at. A free-standing spec merges, ships and is accepted through the
    same gate as any other — and that promise is unobserved unless the board can
    say how many took the route and at what depth, which is the number that says
    whether the lane is being used as designed or as a way around the gate."""
    specs = fold(events)[0]
    out = []
    for sid, s in sorted(specs.items()):
        if s["state"] != "accepted" or s["charter"]:
            continue
        tier = next((e.get("depth") for e in reversed(s["evs"])
                     if e["type"] == "review" and e.get("depth")), None)
        out.append({"id": sid, "tier": tier})
    return out


class Closable(dict):
    """The five named conjuncts, PLUS a three-value positional read for the
    consumer already on main.

    Reconciled at the conflict re-dispatch (L-adr-0044). `src/tick.py` — landed,
    green, and outside this unit's `Writes:` — calls
    `(getattr(fold, "closable", None) or closable_fallback)(events, c)` and
    unpacks a 3-tuple `(accepted, fixpoint, review_owed)`; its own fallback
    docstring says it stands in "until `the-fold-and-the-board` lands the real one
    in `fold`". Landing a bare 5-key dict there does not raise a shape error a
    reader could act on — it either unpacks to three KEY STRINGS (every one
    truthy: a charter would silently leave the lane) or dies with a ValueError in
    `tick.lane()`. Neither is a report.

    So the positional read exists and is FOLDED EXACTLY as `closable_fallback`
    folds it — `owed_within_k` rides with `all_accepted`, `no_open_briefs` rides
    with `sweep_derived` — because those are the conjuncts each one belongs to,
    and a triple that dropped them would report a charter closable that `fold()`
    holds at `L1-complete`. The five names remain the contract; the triple is a
    lossless projection of it, not a second answer.

    Only `__iter__` is overridden: `dict(c)`, `{**c}`, `c.keys()`, `c.items()`,
    `in`, `[]` and `json.dumps` all keep dict semantics (measured). The one
    changed behaviour is bare `for x in c`, which yields the three booleans
    rather than key strings — read `.keys()` when you want names."""
    ORDER = ("all_accepted", "sweep_derived", "review_owed")

    def __iter__(self):
        return iter((self["all_accepted"] and self["owed_within_k"],
                     self["sweep_derived"] and self["no_open_briefs"],
                     self["review_owed"]))


def _closable(charter_evs, mine):
    """§3.11's L2-complete predicate, the five conjuncts the code actually
    requires — NOT the three the seam prose names. `owed <= K` and "no open
    in-scope brief" are carried out with the other three precisely so that no
    caller can reconstruct L2 without them: a consumer that ANDs only the three
    named facts closes a charter over owed evidence and unanswered briefs.

    Polarity is pinned here and nowhere else. `review_owed` is TRUE when a
    charter-review is still owed, so L2-complete is
    `all_accepted and sweep_derived and owed_within_k and no_open_briefs and not
    review_owed` — written once so neither a builder nor a consumer picks it.

    `review_owed` is `charter_review_owed(charter_evs)` — R11: a `Covers: none`
    charter never owes a review nothing gates the dispatch of, no matter what
    `charter_review` alone would answer."""
    types = {e["type"] for e in charter_evs}
    return Closable(
        all_accepted=all(s["state"] in ("accepted", "shipped-owed-evidence", "dropped",
                                        "closed-unbuilt", "closed-shipped") for s in mine),
        sweep_derived="sweep-fixpoint" in types,
        owed_within_k=sum(1 for s in mine if s["state"] == "shipped-owed-evidence") <= K,
        no_open_briefs=not open_briefs(charter_evs),
        review_owed=charter_review_owed(charter_evs))


def l2_complete(c):
    """The one place the five conjuncts are ANDed. Callers read this, never their
    own conjunction of the dict."""
    return (c["all_accepted"] and c["sweep_derived"] and c["owed_within_k"]
            and c["no_open_briefs"] and not c["review_owed"])


def closable(events, charter):
    """The L2 predicate for ONE charter, callable before the ledger carries
    `sweep-fixpoint` — or any event at all — for it. `fold()` derives charter
    state through the same `_closable`, so this can never drift from the board.

    `charter` is the charter ID, matching every other id-taking function in this
    file — or one of `fold()`'s own charter dicts, which is what the landed
    `tick.lane()` passes and what `tick.closable_fallback` already accepts
    (`charter["id"] if isinstance(charter, dict) else charter`). Taking both is
    the reconciliation, not a second signature: an unhashable dict reaching
    `charter in charters` is a TypeError inside the fold, which is the least
    useful place for it to surface.

    A charter the ledger has never named is not an error: it returns the honest
    all-false-but-the-vacuous-ones answer rather than raising, because "is this
    closable yet" is a question asked BEFORE the work, not only after it."""
    cid = charter["id"] if isinstance(charter, dict) else charter
    specs, charters, _, by_subject = fold(events)
    if cid in charters:
        return Closable(charters[cid]["closable"])
    mine = [s for s in specs.values() if s["charter"] == cid and s["state"] != "void"]
    return _closable(by_subject.get(cid, []), mine)


def _cell(d, *keys):
    """The first of `keys` the producer actually carries, else `?`. A key present
    but None is NOT a value — panes.py sets `status` to None when the harness file
    omits it, and rendering the word `None` would read as a measurement."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return "?"


def _panes():
    """(module, None) or (None, why-not). `src/panes.py` is pane-identity's
    footprint and a CONCURRENT wave-1 unit (L-adr-0044), so this module must run
    whether or not it has merged yet — and must SAY which, because a board that
    silently omits its live-pane section is indistinguishable from a board with no
    live panes. Same lazy-import idiom as caps() above, for the same reason: the
    import is paid for only by the caller that needs it."""
    try:
        import panes                                       # lazy: a concurrent unit
        return panes, None
    except Exception as x:                                 # noqa: BLE001
        return None, f"src/panes.py not importable ({type(x).__name__}: {x}) — pane-identity, L-adr-0044"


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


def spend_by_model(events):
    """Token spend per model, derived HERE at fold time and nowhere else (§8.2) —
    retro step 9 (operator ask, 2026-09-15): the seat route now carries a real
    four-way split (dispatch.py's `input_tokens`/`output_tokens`/`cache_read`/
    `cache_creation`, from src/usage.py via stamp.sh), and this is its board
    rendering. `claude -p` and codex already carried a split; a seat spawn from
    before stamp.sh learned to compute it carries only the OLDER blended
    `subagent_tokens` — the two are summed into separate columns and never
    combined, because they measure different things (see src/usage.py's
    docstring: cache reads bill on every call that re-reads them, so a
    cumulative billed sum is not the same number as a point-in-time blend).

    Returns (rows, unmeasured): rows keyed by model (`model_used`, else
    `model_requested`, else the pre-map `model` — spend_rows' own priority),
    each `{n, split_n, input, output, cache_read, cache_creation, blended,
    blended_n, weighted, weighted_n}`; `unmeasured` counts spawns carrying
    neither the split nor a blended figure. `weighted` sums only the split
    spawns — a blended-only figure cannot be decomposed into the four price
    ratios, so it is never guessed into the weighted total. `split_n` (not
    just a truthy sum) is what the renderer checks before showing the raw
    columns — a spawn genuinely measured at all-zero tokens must still render
    as measured, not fall through to looking unmeasured."""
    import models
    weights = models.load_weights()
    rows, unmeasured = {}, 0
    for e in events:
        if e.get("type") not in ("spawn-done", "spawn-failed"):
            continue
        model = e.get("model_used") or e.get("model_requested") or e.get("model") or "(unknown)"
        r = rows.setdefault(model, dict(n=0, split_n=0, input=0, output=0, cache_read=0, cache_creation=0,
                                        blended=0, blended_n=0, weighted=0.0, weighted_n=0))
        r["n"] += 1
        four = (e.get("input_tokens"), e.get("output_tokens"), e.get("cache_read"), e.get("cache_creation"))
        if all(v is not None for v in four):
            r["split_n"] += 1
            r["input"] += four[0]; r["output"] += four[1]
            r["cache_read"] += four[2]; r["cache_creation"] += four[3]
            w = weights.get(model)
            if w:
                r["weighted"] += (four[0] * w["input"] + four[1] * w["output"]
                                  + four[2] * w["cache_read"] + four[3] * w["cache_creation"])
                r["weighted_n"] += 1
        elif e.get("subagent_tokens") is not None:
            r["blended"] += e["subagent_tokens"]
            r["blended_n"] += 1
        else:
            unmeasured += 1
    return rows, unmeasured


def spend_detail(events, subject):
    """Per-spawn token/time rows for `doit spend <charter|spec|spawn-id>` — the
    same ledger the board folds from, filtered to one subject. A charter's rows
    are every spawn whose `charter` field (normalized through charter_id, since
    it may be a path) names it, OR whose own subject IS the charter (a
    plan-auditor or charter-reviewer spawn); a spec's rows are every spawn on
    that subject; anything else is read as a bare spawn id, one row."""
    import models
    weights = models.load_weights()
    by_spawn = collections.defaultdict(list)
    for e in events:
        if e.get("spawn"):
            by_spawn[e["spawn"]].append(e)

    def wants(spawn_id, terminal):
        if subject.startswith("L-charter-"):
            return charter_id(terminal.get("charter")) == subject or terminal.get("subject") == subject
        if subject.startswith("L-spec-"):
            return terminal.get("subject") == subject
        return spawn_id == subject

    rows = []
    for spawn_id, evs in by_spawn.items():
        terminal = next((e for e in evs if e["type"] in ("spawn-done", "spawn-failed")), None)
        if terminal is None or not wants(spawn_id, terminal):
            continue
        model = terminal.get("model_used") or terminal.get("model_requested") or terminal.get("model")
        four = {k: terminal.get(k) for k in ("input_tokens", "output_tokens", "cache_read", "cache_creation")}
        weighted = None
        if all(v is not None for v in four.values()):
            w = weights.get(model)
            if w:
                weighted = (four["input_tokens"] * w["input"] + four["output_tokens"] * w["output"]
                            + four["cache_read"] * w["cache_read"] + four["cache_creation"] * w["cache_creation"])
        dur = terminal.get("duration_ms")
        start = min((ts(e["ts"]) for e in evs), default=None)
        rows.append({"spawn": spawn_id, "role": terminal.get("actor"), "model": model, **four,
                     "subagent_tokens": terminal.get("subagent_tokens"), "weighted": weighted,
                     "duration_min": dur / 60000.0 if dur is not None else None,
                     "start": start.isoformat(timespec="seconds") if start else None,
                     "end": terminal.get("ts"), "failed": terminal["type"] == "spawn-failed"})
    rows.sort(key=lambda r: r["start"] or "")
    return rows


# Stage name -> the event type whose EARLIEST occurrence marks it, for the charter
# wall clock (§4.2). `cut-written` can happen more than once (a bad-cut finding
# sends the Planner back); the FIRST is the charter's actual start, matching what
# an operator reading the ledger from the top would call "when this began".
CHARTER_STAGE_EVENTS = [("cut-written", "cut-written"), ("l1-complete", "l1-complete"),
                        ("charter-review-complete", "charter-review-complete"),
                        ("tree-reaped", "tree-reaped")]


def charter_stage_clock(events, cid, specs):
    """The stage wall clock (§4.2): cut-written -> l1-complete -> first spec
    shipped -> last spec shipped -> charter-review-complete -> tree-reaped,
    derived purely from event timestamps already on the ledger — no new event,
    no second truth. A stage this ledger never crossed is OMITTED, not zeroed:
    a charter closed without a review must not render a false review-to-reap
    span. Returns ([(gap_name, minutes), ...], total_minutes_or_None)."""
    charter_evs = [e for e in events if e.get("subject") == cid]

    def earliest(t):
        cand = [ts(e["ts"]) for e in charter_evs if e["type"] == t]
        return min(cand) if cand else None

    mine = [s for s in specs.values() if s["charter"] == cid]
    shipped = sorted(ts(e["ts"]) for s in mine for e in s["evs"] if e["type"] == "shipped")
    stages = [(name, earliest(t)) for name, t in CHARTER_STAGE_EVENTS[:2]]
    stages += [("first shipped", shipped[0] if shipped else None),
              ("last shipped", shipped[-1] if shipped else None)]
    stages += [(name, earliest(t)) for name, t in CHARTER_STAGE_EVENTS[2:]]
    present = [(name, t) for name, t in stages if t is not None]
    gaps = [(f"{a[0]} → {b[0]}", (b[1] - a[1]).total_seconds() / 60)
            for a, b in zip(present, present[1:])]
    total = (present[-1][1] - present[0][1]).total_seconds() / 60 if len(present) >= 2 else None
    return gaps, total


def render_spend(subject, events, specs):
    """`doit spend <charter|spec|spawn-id>` — a per-spawn table plus totals, and
    for a charter, the stage wall clock. Read-only: never writes board.md or
    anything else (fold's own report on demand, not a second board)."""
    rows = spend_detail(events, subject)
    L = [f"# spend · {subject} · {len(rows)} spawn(s)", ""]
    tot = collections.Counter()
    unmeasured = 0
    for r in rows:
        four_present = all(r[k] is not None for k in ("input_tokens", "output_tokens", "cache_read", "cache_creation"))
        if four_present:
            wtxt = f"{r['weighted']:,.0f}" if r["weighted"] is not None else "unweighted"
            for k in ("input_tokens", "output_tokens", "cache_read", "cache_creation"):
                tot[k] += r[k]
            if r["weighted"] is not None:
                tot["weighted"] += r["weighted"]
        elif r["subagent_tokens"] is not None:
            wtxt = f"blended {r['subagent_tokens']:,}"
            tot["blended"] += r["subagent_tokens"]
        else:
            wtxt = "unmeasured"
            unmeasured += 1
        dur = f"{r['duration_min']:.1f}m" if r["duration_min"] is not None else "—"
        # ★ Distinct labels, not `k.split('_')[0]` — "cache_read" and "cache_creation"
        # both start "cache", and a label collision here silently hid one column.
        labels = {"input_tokens": "in", "output_tokens": "out",
                  "cache_read": "cache_read", "cache_creation": "cache_creation"}
        four = " ".join(f"{labels[k]} {r[k]:,}" if r[k] is not None else f"{labels[k]} —"
                        for k in ("input_tokens", "output_tokens", "cache_read", "cache_creation"))
        L.append(f"  {r['spawn']} · {r['role'] or '?'} · {r['model'] or '?'} · {four} · "
                 f"weighted {wtxt} · {dur} · {r['start'] or '?'}→{r['end'] or '?'}"
                 + ("  FAILED" if r["failed"] else ""))
    L.append("")
    L.append(f"totals: {len(rows)} spawn(s) · input {tot['input_tokens']:,} · "
             f"output {tot['output_tokens']:,} · cache_read {tot['cache_read']:,} · "
             f"cache_creation {tot['cache_creation']:,} · weighted {tot['weighted']:,.0f} "
             f"input-equiv tokens · blended {tot['blended']:,} (unsplit) · unmeasured {unmeasured}")
    if subject.startswith("L-charter-"):
        gaps, total = charter_stage_clock(events, subject, specs)
        L += ["", "## stage wall clock"]
        if not gaps:
            L.append("  (fewer than two stages crossed on this ledger)")
        for name, mins in gaps:
            L.append(f"  {name} · {mins:.1f} min")
        if total is not None:
            L.append(f"  total (first stage crossed → last) · {total:.1f} min")
    return "\n".join(L)


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


def verdict_confirmed(v, owed_criteria):
    """A `verdict` event's confirmed-ness, but over EVALUABLE rows (S15/S33): a
    `cannot-assess` row whose criterion an `owed-ac` on this subject already named
    does not zero it — that row was never gradeable and declaring it owed is
    exactly what said so up front. The literal `confirmed` field (set at grade
    time, from the model's own `all(met)`, before the fold could know which
    criteria were owed) is trusted first; this only widens it, and only for rows
    that are BOTH `cannot-assess` AND owed. A real `unmet` never reaches here —
    it stands as a `rejected-criterion` and blocks the caller before this is
    consulted. A `cannot-assess` on a criterion nobody declared owed is left
    exactly as it was: not confirmed."""
    if v.get("confirmed"):
        return True
    ca = {c for c in (v.get("cannot_assess") or []) if c}
    if not ca or not ca <= owed_criteria:
        return False
    return v.get("matches_intent") == "yes" and v.get("card_ok") == "yes"


def spec_state(evs, retracted):
    """killed / accepted / shipped-owed-due / shipped-owed-evidence / dropped /
    closed-shipped / closed-unbuilt / void, or the pipeline state it is stuck in."""
    types = {e["type"] for e in evs}
    # R3/L-spec-0192: `killed` is TERMINAL the instant a `spec-killed` event
    # exists — checked first and unconditionally, so it survives any later stage
    # event on the same subject (AC9): a spec-writer that kills a spec never
    # un-kills it by writing anything more.
    if "spec-killed" in types:
        return "killed"
    # S32/S33 (a-6): an allocation whose spec-writer spawn failed and never wrote a
    # spec is not stuck in a pipeline state — it is VOID. The actor is the filename
    # (D90), so "its spec-writer" is read off the events' own actor, never a body
    # field a failed spawn never got to write. `fold()` below drops a void spec out
    # of `mine` entirely, so it cannot bind — or hold open — its charter's L2
    # conjunct the way the ghost L-spec-0004 did.
    if ("spec-written" not in types and "spec-killed" not in types
            and any(e["type"] == "spawn-started" and e["actor"] == "spec-writer" for e in evs)
            and any(e["type"] == "spawn-failed" and e["actor"] == "spec-writer" for e in evs)):
        return "void"
    open_rejects = standing_rejects(evs)
    if "shipped" in types and not open_rejects:
        # S15/S33: `owed-ac` now carries the criterion it owes (the schema
        # requires it), and `owed-met` — the Executor or the operator, citing the
        # evidence — discharges one without a re-grade spawn. `accepted` needs
        # every owed criterion met, not merely a confirmed verdict; short of that,
        # an UNMET owed criterion with a future `wake_at` is `shipped-owed-evidence`
        # (D25), and one whose `wake_at` has already passed is `shipped-owed-due`
        # (R7/L-spec-0192) — distinct from, and taking priority over, evidence when
        # a subject carries both.
        #
        # `verdict_owed` (spec-writer/spec-auditor authored ONLY) is what
        # `verdict_confirmed` below widens over — an executor's re-date moves
        # WHEN a criterion is due, never WHICH criteria count as owed (R10,
        # AC21). `owed_criteria` (any authorized actor) is what decides
        # unmet/due/evidence: it can never admit a criterion `verdict_owed`
        # would not also admit, because `fold()`'s own subject-scoped
        # classification only keeps an executor-authored `owed-ac` when a
        # matching spec-writer/spec-auditor declaration already sits in `evs`.
        verdict_owed = {e.get("criterion") for e in evs if e["type"] == "owed-ac" and e.get("criterion")
                        and e.get("actor") in ("spec-writer", "spec-auditor")}
        owed_criteria = {e.get("criterion") for e in evs if e["type"] == "owed-ac" and e.get("criterion")}
        met_criteria = {e.get("criterion") for e in evs if e["type"] == "owed-met" and e.get("criterion")}
        graded = any(e["type"] == "verdict" and verdict_confirmed(e, verdict_owed) for e in evs)
        if graded and "review" in types and owed_criteria <= met_criteria:
            return "accepted"                                        # §2.5 accepted()
        # Due takes priority over evidence (R7). A criterion-bearing owed-ac is
        # grouped by criterion, and the LATEST one (`evs` is ts-sorted) governs
        # its due-ness — the re-date rule (R10): an executor's later event
        # overrides an earlier spec-writer/auditor one for the SAME criterion.
        # A criterion-less owed-ac (the pre-schema shape, D25) has no grouping
        # key and is read per event, exactly as before this unit.
        last_wake, loose = {}, []
        for e in evs:
            if e["type"] != "owed-ac":
                continue
            c = e.get("criterion")
            (last_wake.__setitem__(c, e.get("wake_at")) if c else loose.append(e.get("wake_at")))

        def dated(w):
            return w is not None and str(w).strip() != ""
        wakes_unmet = [w for c, w in last_wake.items() if c not in met_criteria] + loose
        if any(dated(w) and ts(w) <= NOW for w in wakes_unmet):
            return "shipped-owed-due"                                # R7
        if any(dated(w) and ts(w) > NOW for w in wakes_unmet):
            return "shipped-owed-evidence"                           # D25
    if "spec-closed" in types:
        # D112 + S33: the operator's only close instrument used to read
        # `closed-unbuilt` even over a spec that was built, graded, reviewed and
        # merged — a false label that cost two operator rulings to work around by
        # hand. A `shipped` event on the subject means it WAS built: label it
        # truthfully and let `fold()` below count it as done for L2, same as
        # `closed-unbuilt`. Deliberately NOT `accepted`: nothing here says every
        # criterion was proven, only that the operator closed the question another
        # way after a real build existed.
        return "closed-shipped" if "shipped" in types else "closed-unbuilt"
    charter = charter_id(next((e.get("charter") for e in reversed(evs) if e.get("charter")), None))
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
        etype, subj = e.get("type"), e.get("subject", "")
        allowed = EMITS.get(etype)
        if allowed is not None and e["actor"] not in allowed:
            ignored.append(e)
            continue
        # R10/L-spec-0192: `EMITS["owed-ac"]` now admits the executor, but ONLY to
        # RE-DATE — never to declare. This is `fold()`'s own subject-scoped
        # classification, not `check_append`'s (which sees one event and never
        # the rest of the stream): an executor-authored `owed-ac` survives here
        # ONLY when the subject's stream already carries an EARLIER (by ts — and
        # `events` arrives here already ts-sorted, so "earlier" is "already in
        # `by_subject[subj]`") spec-writer/spec-auditor `owed-ac` for the
        # IDENTICAL criterion. Any other criterion is ignored exactly like any
        # other unauthorized emit.
        if etype == "owed-ac" and e["actor"] not in ("spec-writer", "spec-auditor"):
            crit = e.get("criterion")
            prior = bool(crit) and any(
                x["type"] == "owed-ac" and x.get("criterion") == crit
                and x["actor"] in ("spec-writer", "spec-auditor") for x in by_subject[subj])
            if not prior:
                ignored.append(e)
                continue
        by_subject[subj].append(e)

    retracted = {s for s, evs in by_subject.items()
                 if any(e["type"] == "charter-retracted" for e in evs)}
    specs, charters = {}, {}
    for sid, evs in by_subject.items():
        if sid.startswith("L-spec-"):
            specs[sid] = {"id": sid, "state": spec_state(evs, retracted), "evs": evs,
                          "rejects": len(standing_rejects(evs)),
                          "charter": charter_id(next((e.get("charter") for e in reversed(evs)
                                                     if e.get("charter")), None)),
                          "age": age_days(evs[-1])}
        elif sid.startswith("L-charter-"):
            charters[sid] = {"id": sid, "evs": evs, "age": age_days(evs[-1])}

    for cid, c in charters.items():
        # S32/S33 (a-6): a `void` allocation — a spec-writer spawn that failed and
        # never produced a `spec-written` — does not bind this charter's L2
        # conjunct at all, in either direction: not counted against it (the ghost
        # L-spec-0004 held L-charter-0004 at L1 for exactly this reason), and not
        # counted FOR it either. It is simply not `mine`.
        mine = [s for s in specs.values() if s["charter"] == cid and s["state"] != "void"]
        types = {e["type"] for e in c["evs"]}
        owed = sum(1 for s in mine if s["state"] == "shipped-owed-evidence")
        # R11: the predicate lives in `_closable` now, not inline here — one
        # implementation, reachable by a caller that needs to ask before the
        # sweep, and no second copy to drift. Derivation is unchanged, K included.
        c["closable"] = _closable(c["evs"], mine)
        if cid in retracted:
            c["state"] = "retracted"
        # An operational charter (S25/S35: a deploy, a drill) has NO specs — its
        # deliverable is a record. `all()` over an empty set is True, so such a
        # charter closes on the operator's l1-complete + the sweep fixpoint + a
        # complete charter review, exactly the three things that ARE its lane.
        elif l2_complete(c["closable"]):
            c["state"] = "L2-complete"
        elif "l1-complete" in types:
            c["state"] = "L1-complete"
        else:
            c["state"] = "open"
        c["owed"] = owed
        c["briefs"] = len(open_briefs(c["evs"]))
        c["unbuilt"] = sum(1 for s in mine if s["state"] == "closed-unbuilt")
    return specs, charters, ignored, by_subject


def owed_due(specs):
    """R7/L-spec-0192: one `{spec, criterion, due_at, days_overdue, src}` row per
    due-and-unmet owed criterion, across every spec in `specs` (`fold()`'s own
    dict) — the data the board's own `shipped-owed-due` rendering and, later, a
    lane item of its own (agents/executor.md — a later unit's Writes grant, not
    this one's) both read. Mirrors `spec_state`'s own re-date-governs rule: the
    LATEST `owed-ac` event per criterion decides due-ness, whichever authorized
    actor wrote it. A not-yet-due or already-met criterion is not a row."""
    out = []
    for sid, s in specs.items():
        evs = s["evs"]
        owed_criteria = {e.get("criterion") for e in evs if e["type"] == "owed-ac" and e.get("criterion")}
        met_criteria = {e.get("criterion") for e in evs if e["type"] == "owed-met" and e.get("criterion")}
        last_wake, last_src = {}, {}
        for e in evs:
            if e["type"] == "owed-ac" and e.get("criterion"):
                last_wake[e["criterion"]] = e.get("wake_at")
                last_src[e["criterion"]] = e.get("_src")
        for c in owed_criteria - met_criteria:
            w = last_wake.get(c)
            if w is None or not str(w).strip():
                continue
            due_at = ts(w)
            if due_at <= NOW:
                out.append({"spec": sid, "criterion": c, "due_at": w,
                            "days_overdue": (NOW - due_at).total_seconds() / 86400,
                            "src": last_src.get(c)})
    return out


def wedged(spec, dwell=None):
    return spec["age"] > (dwell or DWELL_DAYS).get(spec["state"], 1e9)


def in_flight_deploys(events):
    """§4.11's `deploy-started` with no terminal event after it, per subject —
    the wrapper may have been killed, and the ledger otherwise has no event that
    says the deploy ended. Newest deploy-related event per subject decides:
    `deploy-landed` / `deploy-failed` / `deploy-refused` all close it, only a
    trailing `deploy-started` is still open (S34)."""
    DEPLOY_TYPES = ("deploy-started", "deploy-landed", "deploy-failed", "deploy-refused")
    last = {}
    for e in events:
        if e.get("type") in DEPLOY_TYPES and e.get("subject") is not None:
            last[e["subject"]] = e
    return [e for e in last.values() if e["type"] == "deploy-started"]


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
    # would forge an extra section here — §8.3's sections (ten, plus the SPEND
    # block added by retro step 9) are positional. Pre-existing on this line; the
    # spend row now carries the same value, so both are collapsed.
    scope = f" · project={' '.join(str(PROJECT).split())}" if PROJECT else ""
    L = [f"# board · {NOW.isoformat(timespec='seconds')} · fold @ {len(events)}{scope}", ""]

    # R5/R6: whatever relay-queries' waiting_lines(events, ROOT) returns, verbatim
    # and in order, immediately under the title and above NEEDS YOU. A PURE
    # PASS-THROUGH: no reordering, no dedup, no truncation, no re-wrapping, and no
    # "## " header of fold.py's own making — whether a returned line carries one is
    # relay's contract, and this file has no authority to strip a prefix it did not
    # add. Not a twelfth positional section: it is the question ("what is the
    # planner waiting on?") the rest of the board cannot answer. A dry queue still
    # renders its one line — "nothing waiting" IS the answer, never a reason to
    # omit the slot (R6).
    # L-adr-0033 degrade: ImportError ONLY. A relay that imports and then raises is
    # a bug in a landed producer and must propagate — swallowing it would hide a
    # live failure behind a "not merged yet" line that is no longer true.
    waiting_unavailable = False
    try:
        from relay import waiting_lines
    except ImportError:
        waiting_unavailable = True
    else:
        L += list(waiting_lines(events, ROOT)) + [""]

    def block(title, rows, note=""):
        L.append(f"## {title} ({len(rows)}){note}")
        L.extend("  " + r for r in rows)
        L.append("")

    # R3: the row carries what it takes to DECIDE — the default that will be
    # applied, the deadline, the undo or the irreversible act that is the reason
    # there is no default, and the age. A row that says only "this is blocked"
    # sends the operator to the ledger to find out what blocking means.
    block("NEEDS YOU", [question_line(r) for r in open_questions(events)])
    block("BLOCKED", [f"{e.get('subject','?')} · {e.get('why','?')} · owner "
                      f"{e.get('owner') or '⚠ NOBODY'} · {age_days(e):.1f}d" for e in open_blocks])
    block("WRITTEN, NOT PICKED UP", [f"{s['id']} · {s['age']:.1f}d{flag(s)}" for s in pick("written")])
    # A deploy-started with no landed/failed/refused after it is a wrapper that
    # may be dead — visible here, not silently lost (S34).
    block("IN FLIGHT", [f"{s['id']} · {s['age']:.1f}d{flag(s)}" for s in pick("building")]
          + [f"deploy in flight · {e.get('subject','?')} · {str(e.get('sha',''))[:7]} · "
             f"{age_days(e):.1f}d" for e in in_flight_deploys(events)])
    # A standing rejection is the difference between "waiting to be looked at" and
    # "already looked at and failed". Same section, but the row may not read the same.
    # R7/L-spec-0192: `shipped-owed-due` widens this pick by one token, pending
    # the sibling unit's own section (ADR-0028-6) — a due criterion is at least
    # as much "awaiting verification" as a plain `shipped` spec is.
    block("AWAITING VERIFICATION",
          [f"{s['id']} · {s['state']} · {s['age']:.1f}d"
           + (f"  ⚠ {s['rejects']} REJECTED, needs rework" if s["rejects"] else "") + flag(s)
           for s in pick("graded", "reviewing", "shipped", "shipped-owed-due")])
    def owed_line(s):
        wake = str(next((e.get("wake_at") for e in s["evs"]
                         if e["type"] == "owed-ac" and e.get("wake_at")), "?"))
        # a-6: a spec can carry more than one owed criterion, and `owed-met` may
        # discharge some before the rest wake — that partial state is meaningful
        # (it is progress the operator would otherwise have to grep for) and gets
        # a note; a spec with NO owed-met yet renders exactly as before. A spec
        # whose owed-met covers EVERY owed criterion has already derived
        # `accepted` (spec_state) and simply is not in this list at all.
        met = sorted({e.get("criterion") for e in s["evs"]
                      if e["type"] == "owed-met" and e.get("criterion")})
        return f"{s['id']} · wakes {wake}" + "".join(f"  ·  {c} met, awaiting fold" for c in met)

    block("OWED EVIDENCE", [owed_line(s) for s in pick("shipped-owed-evidence")])
    # An unbuilt close is invisible in every working section — terminal work does
    # not queue. It surfaces HERE, at the one moment someone asks "was this
    # actually done?" (D112).
    # ponytail: on a charter still `open` it is therefore visible nowhere. The
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

    # SPEND — retro step 9 (operator ask, 2026-09-15): a per-model token block of
    # its own, alongside (not instead of) the per-project dollar line already on
    # HEALTH below. §8.3 called the layout above "ten sections, fixed, never
    # reshuffled" — this IS the sanctioned eleventh, added deliberately for this
    # ask; `doit spend <charter|spec|spawn-id>` (render_spend) is the drill-down.
    spend_by, spend_unmeasured = spend_by_model(events)

    def spend_row(model, r):
        parts = [f"{r['n']} spawns"]
        if r["split_n"]:
            parts.append(f"in {r['input']:,} out {r['output']:,} cache_read {r['cache_read']:,} "
                        f"cache_creation {r['cache_creation']:,}")
            parts.append(f"weighted {r['weighted']:,.0f} input-equiv tokens"
                        if r["weighted_n"] else "weighted n/a — no [weights] for this model")
        if r["blended_n"]:
            parts.append(f"blended {r['blended']:,} tokens ({r['blended_n']} unsplit seat spawn(s))")
        return f"{model} · " + " · ".join(parts)
    block("SPEND", [spend_row(m, r) for m, r in sorted(spend_by.items())]
          + [f"unmeasured: {spend_unmeasured} spawn(s) carry no usage at all"])

    # LIVE PANES — R14, the sanctioned TWELFTH section (§8.3's ten, plus SPEND at
    # retro step 9). Anchored POSITIONALLY: last block, immediately before the
    # HEALTH list, never at a line number, so a block merged in elsewhere in
    # render() cannot move it and it cannot move one.
    #
    # ★ src/panes.py is a CONCURRENT unit (L-adr-0044). The import is lazy and
    # its absence is a STATED unavailability, never a crash and never silence:
    # the board is the one surface the operator reads, and a render() that raises
    # because a sibling has not merged yet takes every other section with it.
    panes_mod, panes_err = _panes()
    if panes_mod is None:
        pane_rows = [f"unavailable — {panes_err}"]
    else:
        try:
            # ★ Each cell via .get with a `?` fallback: the Plan's Seams pin
            # live_panes' per-pane keys in PROSE only, so a key-name mismatch at
            # merge must degrade to a visible `?` rather than a KeyError.
            #
            # ★★ Reconciled at merge (L-adr-0044, the conflict re-dispatch): the
            # LANDED src/panes.py names two of those cells `last_event_age_days`
            # and `status`, where the Seams prose said "age of its last event" and
            # "harness status". Both spellings are read, landed name first — the
            # producer's own key is authoritative, and dropping the prose name
            # would silently un-test the shape this unit was specified against.
            # Measured before this line existed: a real board rendered `last event
            # ? · ?` for both live panes.
            pane_rows = [f"{p.get('name','?')} · {p.get('contract','?')} · {p.get('cwd','?')}"
                         f" · {p.get('ledger_file','?')}"
                         f" · last event {_cell(p, 'last_event_age_days', 'age_days')}"
                         f" · {_cell(p, 'status', 'harness_status')}"
                         for p in panes_mod.live_panes(SESSIONS, events)]
        except Exception as x:                       # noqa: BLE001 — see above
            pane_rows = [f"unavailable — panes.live_panes raised {type(x).__name__}: {x}"]
    block("LIVE PANES", pane_rows)

    drill = max((ts(e["ts"]) for e in events if e.get("type") == "restore-verified"), default=None)
    health = [f"restore drill: {'never run — the backup is unproven' if not drill else f'{(NOW-drill).days}d ago'}"]
    # The degrade half of the block above (L-adr-0033). It lands HERE rather than at
    # the block's own position so that slot stays a pure pass-through of relay's
    # lines and nothing else — same loudness, a surface fold.py already owns. The
    # wording is fixed by the spec, not chosen here: it is asserted literally.
    if waiting_unavailable:
        health.append("PLANNER WAITING ON: unavailable — relay-queries not merged (L-adr-0033)")
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
    # R3 — escalations already on the ledger that the append path would refuse.
    # `dispatch.py` and `tick.py` do not go through append(), and the ledger is
    # append-only, so this count is the ONLY enforcement reaching those writes.
    # A standing line: zero is the measurement that says the refusal is holding.
    me = malformed_escalations(events)
    health.append(f"malformed escalations: {len(me)} — carry neither default+deadline+revert"
                  " nor a named irreversible act (R3)"
                  + (f" (last: {me[-1].get('subject','?')} · {me[-1]['_src']})" if me else ""))
    # R15/L-adr-0038 — the Executor does not edit a repository. A session that
    # should read zero says so when it does not, plainly and with no threshold:
    # the count IS the finding.
    re_ = [e for e in events if e.get("type") == "repo-edit"]
    health.append(f"repo-edit events: {len(re_)} — the Executor does not edit a repository (L-adr-0038)"
                  + (f" (last: {re_[-1].get('subject','?')} · {re_[-1].get('path','?')})" if re_ else ""))
    # R13/L-adr-0037 — "a message that never became a record". The count is
    # pane-identity's to compute; unavailable is stated, never silently zero,
    # because a zero nobody measured reads exactly like a clean one.
    pm, perr = _panes()
    try:
        health.append(f"unrecorded messages: {pm.unrecorded_messages(events)}"
                      " — a message carrying a decision or a finding that no later event names"
                      if pm is not None else
                      f"unrecorded messages: unavailable — {perr}")
    except Exception as x:                                 # noqa: BLE001
        health.append(f"unrecorded messages: unavailable — panes.unrecorded_messages raised "
                      f"{type(x).__name__}: {x}")
    # R6 — the free-standing lane, observed. A standing line for the same reason
    # as the two above: "nobody used it" and "nobody measured it" are different
    # facts and must not render the same.
    fs = free_standing(events)
    health.append(f"free-standing accepted specs: {len(fs)} — accepted with no charter"
                  + (" (" + ", ".join(f"{s['id']} at {s['tier'] or '⚠ no tier'}" for s in fs) + ")"
                     if fs else ""))
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
    # ★ R3, and it is CONTENT BEFORE EVENT (§9.2 rule 3) applied to the event's own
    # fields: `required_reason` is the one required-fields door (`escalation-
    # blocking`, `owed-ac`, `spec-carried` — L-spec-0192), refused at the door
    # with the missing requirement NAMED. Nothing is written — the raise precedes
    # the open(). An actor/type mismatch is never refused here (ADR-0028-3) — it
    # stays recorded-and-ignored at fold time.
    reason = required_reason(e)
    if reason:
        raise SystemExit(f"refused: {reason}. Nothing was written. (R3)")
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
    elif cmd == "spend":                     # retro step 9: read-only, never writes board.md
        if len(sys.argv) < 3:
            sys.exit("usage: doit spend <charter|spec|spawn-id>")
        print(render_spend(sys.argv[2], ev, specs))
    elif cmd == "review-owed":               # R11: read-only, the same predicate _closable reads
        if len(sys.argv) < 3:
            sys.exit("usage: doit review-owed <charter>")
        print("owed" if charter_review_owed(by_subject.get(sys.argv[2], [])) else "not-owed")
    else:
        print(render(ev, specs, charters, ignored, by_subject))
