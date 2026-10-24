"""Ledger event authorization vocabulary, re-exported by fold."""

# Who may emit what. A type absent here is open to any actor (§2.5).
EMITS = {"verdict": {"grader"}, "review": {"reviewer"}, "shipped": {"executor"},
         "charter-retracted": {"operator"},
         # R10/L-spec-0192: `doit restore` may run as the operator or the executor;
         # `"drill"` (the automated proof run) stays exactly as it was.
         "restore-verified": {"drill", "operator", "executor"},
         # R10/L-spec-0192: added now so no later unit's append is dropped, though
         # the code emitting each is out of this footprint.
         "inbound-registered": {"operator", "thinker", "intake"},               # L-spec-0242
         "inbound-awaiting": {"intake"}, "inbound-awaiting-gone": {"intake"},   # SD1/SD15
         "inbound-pr-commented": {"intake"}, "inbound-closed": {"intake", "operator"},
         "inbound-note-listed": {"intake"}, "inbound-note-gone": {"intake"}, "note-answered": {"thinker", "operator"},
         "carry-failed": {"executor", "operator"},
         "backup-failed": {"backup"},
         # L-spec-0195/AC6: a verify-waiver substitutes a no-op for one &&-segment
         # of a grader's/reviewer's own verify script — never the builder's — so
         # only the two judging-adjacent actors who are not the builder may file one.
         "verify-waiver": {"executor", "operator"},
         "correction": {"operator"},                       # D111
         "spec-closed": {"operator"},                       # D112
         # ★ Asymmetric on purpose. REJECTING is the safe direction — a spurious
         # rejection costs rework, never a false pass — so any judging or gating
         # seat may do it, the Executor included (its merge gate is a gate).
         # CLEARING is the dangerous direction and is judging seats only.
         # R7/SD7: admits() lets builders through ONLY for COMMIT-SHAPE, the
         # dispatch wrapper's own row (written to the builder's ledger, D90).
         # Every other criterion remains restricted to judging/gating seats.
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
         # L-charter-0032/L-spec-0264: `deployer` (D90's filename-derived actor for
         # `L-deployer-local.jsonl`) joins the two existing actors on `deploy-landed`,
         # on the same footing.
         "deploy-landed": {"executor", "operator", "deployer"},
         "tree-reaped": {"executor", "operator"},
         # L-charter-0032/L-spec-0264, SD8/SD24: the deployer's own one-line-per-
         # attempt summary — nobody else may claim one, the same reasoning as
         # `planner-started`/`planner-ended` above (a seat that could write about a
         # deploy it did not make would retire a charter nobody planned).
         "deploy-attempt": {"deployer"},
         # SD8 gives the mechanism (an operator's `deploy-approved sha=<target>`
         # releases a hold); SD24 adds `thinker`, since the Thinker already deploys
         # albert-scott under the operator's standing approval (charter §3) and so
         # may release a hold too. `deployer` is deliberately excluded — it never
         # clears its own hold, the same asymmetry `criterion-cleared` has above.
         "deploy-approved": {"operator", "thinker"},
         # §3.12's fixpoint claim and the link that discharges one in-scope brief.
         # Both are L2 conjuncts in all but name: a seat that may stamp
         # `sweep-fixpoint` can close a charter over work it never swept, and a seat
         # that may stamp `brief-answered` can discharge a brief no spec answers.
         "sweep-fixpoint": {"executor", "operator"},
         # L-spec-0322/L-charter-0036 R1: `look` clears its own briefs (SD22) —
         # the one `EMITS` growth this spec's SD12 cap allows.
         "brief-answered": {"executor", "operator", "look"},
         # L-charter-0033/board-owners, Target 2: the mirror of brief-answered's
         # own restriction, one line above — the Thinker's TRIAGE clearing route.
         "brief-routed": {"thinker", "operator"},
         # L-charter-0033/board-owners, Target 3: "written by the spec-writer's
         # kill path or the Executor" (Seams) — the two seats already trusted to
         # end a spec's lifecycle. This spec adds no call site that emits it;
         # admission precedes the emitter (fold.py:36-37's own precedent).
         "inbound-covered": {"spec-writer", "executor"},
         # S15/S33 (a-6): the wake script's `verdict` re-grade is one route to
         # discharging an owed criterion; `owed-met` is the other — the Executor or
         # the operator, citing the evidence directly, no re-grade spawn required.
         # Restricted the same way `owed-ac` itself is restricted one level up: a
         # seat that could stamp this on any shipped spec would walk it to
         # `accepted` alone, which is exactly what the verdict+review pair exists
         # to prevent.
         # L-charter-0038/L-spec-0384, SD4: `owed-sweeper` joins the two actors
         # already trusted to discharge an owed criterion by citing evidence
         # directly — the sweep's own verdict on a check it ran. L-spec-1002
         # also admits the Thinker, subject to admits()'s evidence shape check.
         "owed-met": {"owed-sweeper", "executor", "operator", "thinker"},
         # L-charter-0038/L-spec-0384, SD4: new. Same three actors as
         # `owed-met` — a failed check is the other half of the same claim, and
         # the same asymmetry applies: a seat that could fail-then-clear its own
         # check on any shipped spec would walk it to `expired` alone.
         "owed-failed": {"owed-sweeper", "executor", "operator", "thinker"},
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
         "repo-edit": {"executor"},
         # L-charter-0034/L-spec-0279, SD15's permission table. A `fix-shipped`
         # claims a problem was actually fixed — the same asymmetry `shipped`
         # itself already has (one seat's claim, trusted without re-check) — so
         # only the seats that ship or operate may write one.
         "fix-shipped": {"executor", "operator", "thinker"},
         # Its only call site is `src/tick.py`, a sibling wave-2 unit outside
         # this footprint — added now on the same "don't drop a later unit's
         # append" precedent this dict already documents above for the
         # `inbound-*` entries (L-spec-0192).
         "problem-occurred": {"tick"}}

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
# R10/L-spec-0192: admits() lets the Executor re-date an already-declared owed
# criterion (move its wake_at), never declare one. EMITS alone is insufficient.
EMITS["owed-ac"].add("executor")
# Operator ruling 2026-10-04: the Thinker and the operator may DECLARE a criterion
# owed (post-deploy) when a grader cannot assess it in a view — e.g. a live_db leg
# that needs production data. Declaring is no longer spec-writer/spec-auditor only.
EMITS["owed-ac"].update({"thinker", "operator"})
OWED_DECLARERS = ("spec-writer", "spec-auditor", "thinker", "operator")
# L-spec-0276/R5 (SD25): a packet-lint `warn` finding becomes one visible,
# deduplicated event. The same four actors already admitted for
# `spec-carried`/`spec-written` (widened by the two more either hook site may
# run under — a spec-writer ledger, or a thinker session driving `doit carry`),
# since either `dispatch.py`'s `spec-written` site or `carry.py`'s
# `spec-carried` site may append one.
EMITS["spec-lint-warning"] = {"spec-writer", "executor", "operator", "thinker"}
# L-charter-0038/L-spec-0384, SD4/SD12 — these four doors land here solely
# because `fold.py` is this spec's exclusive `Writes:` grant; the call sites
# that walk through them belong to `look-wallclock-events` and
# `owed-sweep-driver`, out of this footprint.
EMITS["pane-paused"] = EMITS["ci-red"] = EMITS["ci-green"] = {"look"}
# `pane-resumed` is NOT a fresh door: `pane_resume.py:276` already writes it
# today (actor `tick`, D90) for an unrelated fact. Granting `{"look"}` alone
# would retroactively un-authorize that live emitter, so both actors are kept.
EMITS["pane-resumed"] = {"look", "tick"}
EMITS["message-answered"] = {"look", "planner", "executor", "thinker"}
# L-charter-0042/L-spec-0424 (ledger-vocabulary): the six event types this
# charter's wave-2 units need, admitted here — one dispatch (this unit) so no
# wave-2 sibling adds its own EMITS/REQUIRED row (Plan Seams: "Vocabulary →
# everything"). Each is scoped to exactly the one actor the requirement that
# creates it names, per §2.5's own rule.
EMITS["autodispatched"] = {"tick"}          # R1: the tick's own dispatch record
EMITS["autodispatch-failed"] = {"tick"}     # R1: the tick's own dispatch-skip record
EMITS["dependency-hold"] = {"thinker", "operator", "planner"}
EMITS["supervisor-code"] = {"up"}           # R2: `up.maybe_reexec`'s own version claim
EMITS["install-synced"] = {"tick"}          # R5: the tick's own once-per-sha install pass
EMITS["grader-view-built"] = {"grader"}     # R8: the grader's own view-build record
EMITS["grader-pane-started"] = {"tick"}     # R8: the tick's own per-cycle pane count
# L-spec-0481/R12.3-4: a grader/reviewer's own refusal-before-spend record and
# the shared/spec-local hold it opens — never any other actor's to write.
EMITS["grading-preflight-failed"] = {"grader", "reviewer"}
EMITS["capability-hold"] = {"grader", "reviewer"}
EMITS["criterion-routed"] = {"grader"}
# R7: widen `rejected-criterion`/`criterion-cleared` to also admit `builder`,
# on top of every actor already admitted. The bare `EMITS` set carries no
# per-criterion granularity — admits() narrows builders to COMMIT-SHAPE.
EMITS["rejected-criterion"].add("builder")
EMITS["criterion-cleared"].add("builder")
# L-charter-0046/L-spec-0470 (ledger-vocabulary): Proving's six event types,
# admitted here — one dispatch (this unit) so no sibling wave-1 unit adds its
# own EMITS/REQUIRED row (Plan Seams: "ledger-vocabulary -> everything"). Each
# is scoped to exactly the one actor set the requirement that creates it names.
EMITS["charter-proving"] = {"tick"}        # R1: the tick's own automatic move into Proving
EMITS["charter-closed"] = {"tick"}         # R1/R3: the tick's own close, never a hand edit
EMITS["charter-reopened"] = {"tick"}       # R4: the tick's own reopen, naming the failing check
# R3: an explicit ledger decision, never a builder or a grading seat (charter
# constraint, verbatim: "Waivers are explicit ledger decisions with a reason").
EMITS["owed-waived"] = {"operator", "thinker"}
# L-spec-1002 / SD1: vocabulary for the owed lifecycle's writers. Route and
# Thinker evidence restrictions live in admits(), alongside the older narrowings.
EMITS["owed-routed"] = {"sweep", "thinker", "operator"}
EMITS["owed-quarantined"] = {"sweep"}
EMITS["owed-corrective"] = {"thinker", "operator", "executor"}
EMITS["sweep-run"] = {"sweep"}
EMITS["owed-digest"] = {"owed-digest"}
# R3: the operator/Thinker accepting a killed spec's absence with no in-charter
# replacement — subject is always the KILLED SPEC, never the charter.
EMITS["kill-accepted"] = {"operator", "thinker"}
EMITS["charter-classified"] = {"executor", "operator"}   # R6: the backfill's own classification record
# L-charter-0042/L-spec-0486 (cleanup-on-finish), R15a: the tick's own per-spec
# worktree reap record — never `tree-reaped`'s actor set (executor/operator, the
# closing seat's claim), because this fires mid-charter, off the tick's own pass.
EMITS["worktree-reaped"] = {"tick"}
# L-charter-0042/L-spec-0484, R13(c): "a merge is not complete until origin
# carries it" — the tick's own push-pass record, never any other actor's
# (the actor is `L-tick-originpush.jsonl`'s own filename-derived "tick", D90).
EMITS["origin-pushed"] = {"tick"}

# L-spec-1104: admit the charter-0053 services before their emitters land.
EMITS["evidence-bundle"] = {"build-runner"}
EMITS["evidence-bundle-failed"] = {"build-runner"}
EMITS["base-red"] = {"base-red"}
EMITS["health-alarm"] = {"health"}
EMITS["owed-unverifiable"] = {"prod-runner", "operator"}
# L-spec-1133: queue, digest and operator-alert writers (SD3).
EMITS["queue-add"] = {"executor", "tick", "operator"}
EMITS["queue-remove"] = {"executor", "tick", "operator"}
EMITS["queue-batch"] = {"executor", "tick"}
EMITS["stuck-digest"] = {"digest"}
EMITS["operator-alert"] = {"look", "tick", "digest"}
EMITS["owed-met"].add("prod-runner")
EMITS["owed-failed"].add("prod-runner")
# owed-unobservable stays open to every actor, including prod-runner.

# L-spec-1120: admit the charter-0054 services before their emitters land.
EMITS["seat-usage"] = {"seat-usage"}
EMITS["balance"] = {"balance"}
EMITS["ci-shadow"] = {"ci-shadow"}
EMITS["ci-shadow-passed"] = {"ci-shadow", "operator"}
EMITS["ci-runner-set"] = {"ci-runner", "operator"}
EMITS["bill-check"] = {"gh-bill"}
EMITS["window-tree-verified"] = {"executor", "operator"}
EMITS["window-bisected"] = {"executor", "operator"}

# L-spec-1143: admit the train and batcher before their emitters land.
EMITS["window-candidate"] = {"executor", "tick", "operator"}
EMITS["window-cancelled"] = {"executor", "tick", "operator"}
EMITS["train-depth"] = {"executor", "tick", "operator"}
EMITS["batch-cap"] = {"executor", "tick", "operator"}

# L-spec-1189: smart-tests writers and the operator, per SD6.
EMITS["prequeue-green"] = {"executor", "tick", "operator"}
EMITS["prequeue-red"] = {"executor", "tick", "operator"}
EMITS["prequeue-undetermined"] = {"executor", "tick", "operator"}
EMITS["window-selection"] = {"executor", "tick", "operator"}
EMITS["selection-shadow"] = {"executor", "tick", "operator"}
EMITS["selection-cutover"] = {"executor", "tick", "operator"}
EMITS["selection-miss"] = {"executor", "tick", "operator"}
EMITS["window-rerun"] = {"executor", "tick", "operator"}
EMITS["base-red-nightly"] = {"base-red", "operator"}

REQUIRED_ROWS = {
    "resource-created": ("kind", "ref", "lane"),
    "resource-reaped": ("ref", "reason"),
    "seat-usage": ("five_hour_pct", "seven_day_pct", "five_hour_resets_at"),
    "balance": ("state_from", "state_to", "codex_pct"),
    "ci-shadow": ("sha", "tree", "kind"),
    "ci-shadow-passed": ("kind", "from", "to"),
    "ci-runner-set": ("variable", "from", "to"),
    "bill-check": ("date", "yday", "mtd"),
    "window-tree-verified": ("window", "sha", "tree"),
    "window-bisected": ("window", "halves"),
    "window-candidate": ("sha", "verified_on", "specs"),
    "window-cancelled": ("after", "reason"),
    "train-depth": ("depth", "reason"),
    "batch-cap": ("cap", "reason"),
    "prequeue-green": ("spec", "branch_sha", "base_sha", "scope"),
    "prequeue-red": ("spec", "branch_sha", "base_sha", "checks"),
    "prequeue-undetermined": ("spec", "branch_sha", "why"),
    "window-selection": ("window", "mode", "reason"),
    "selection-shadow": ("window", "selected_sha", "full_sha"),
    "selection-cutover": ("windows",),
    "selection-miss": ("nightly_sha", "ids", "windows"),
    "window-rerun": ("window", "target", "sha"),
    "base-red-nightly": ("sha",),
}

# resource-created intentionally stays open: every lane records its resources.
EMITS["resource-reaped"] = {"tick", "executor", "operator"}

# L-spec-1494 A3 (amended): only the failure-producing ledgers own this stamp.
EMITS["infra-failure"] = {"builder", "grader", "reviewer", "spec-writer", "research",
                          "probe", "owed-sweeper", "executor", "build-runner"}
