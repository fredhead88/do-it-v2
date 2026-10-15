"""Turn failed owed checks into replayable escalations and corrective rechecks."""
import json
from datetime import timedelta

import dispatch
import fold
import owed

FAILED_DEFAULT = "the check stays failed and open, the spec stays shipped-owed-due, and the next digest lists it under SYSTEM HEALTH"
FAILED_REVERT = "an owed-corrective naming a spec, or an owed-waived with a reason, closes it"
SHIPPED = {"shipped", "shipped-owed-due", "shipped-owed-evidence", "shipped-owed-expired",
           "accepted", "closed-shipped"}
DEAD = {"killed", "void", "dropped", "closed-unbuilt"}


def _array(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def failure_resolved(escalation, events):
    """Only admitted, later fixes/waivers for every named criterion close it."""
    if (escalation.get("type") != "escalation-blocking"
            or escalation.get("kind") != "owed-failed"):
        return False
    criteria = _array(escalation.get("criteria"))
    covered = {e.get("criterion") for e in events
               if e.get("type") in ("owed-corrective", "owed-waived")
               and e.get("subject") == escalation.get("subject")
               and fold.ts(e.get("ts")) > fold.ts(escalation.get("ts"))
               and fold.admits(e, e.get("actor")) is None}
    return bool(criteria) and all(c in covered for c in criteria)


def _covers(esc, criterion, src):
    return (criterion, src) in zip(_array(esc.get("criteria")), _array(esc.get("failed_srcs")))


def _reopen(esc, evs, now):
    anchors = [fold.ts(e.get("ts")) for e in evs if e.get("type") in ("decision", "unblocked")
               and (e.get("ref") == esc.get("_src") or
                    (e.get("subject") == esc.get("subject") and
                     fold.ts(e.get("ts")) > fold.ts(esc.get("ts"))))]
    return bool(anchors) and now >= min(anchors) + timedelta(hours=24)


def escalate_failures(specs, now, *, dry_run=False):
    """Escalate new failures, reopen overdue answers, and recheck shipped fixes.

    Identity is (criterion, failure source), including an explicit sentinel when
    the failure cannot be located. No decisions or unblocked events are emitted.
    """
    from sweep_owed import ledger_path  # lazy: sweep_owed imports this module

    lines = []
    for sid, spec in specs.items():
        evs = spec["evs"]
        escs = [e for e in evs if e.get("type") == "escalation-blocking"
                and e.get("kind") == "owed-failed"]
        answered = fold.answered(evs)
        groups = {}
        for row in owed.checks(evs, now):
            criterion = row["criterion"]
            related = [e for e in evs if e.get("criterion") == criterion]
            status = row["status"]
            if status not in ("failed", "correcting"):
                continue
            why_dead = None
            if status == "correcting":
                correctives = [e for e in related if e.get("type") == "owed-corrective"
                               and e.get("corrective") == row.get("corrective")
                               and fold.ts(e.get("ts")) > row["governing_at"]]
                if not correctives:
                    continue
                corrective = correctives[-1]
                fix_id = corrective["corrective"]
                fixed_at = fold.ts(corrective.get("ts"))
                fix_state = specs.get(fix_id, {}).get("state")
                if fix_state in SHIPPED:
                    if any(e.get("type") == "owed-routed" and e.get("route") == "recheck"
                           and fold.ts(e.get("ts")) > fixed_at for e in related):
                        continue
                    if dry_run:
                        lines.append(f"sweep-owed: DRY RUN would recheck {sid} criterion={criterion}")
                    else:
                        reason = dispatch.emit(ledger_path(), {"subject": sid}, "owed-routed",
                                               route="recheck", criterion=criterion)
                        lines.append(f"sweep-owed: escalation for {sid} refused: {reason}" if reason else
                                     f"sweep-owed: recheck {sid} criterion={criterion}")
                    continue
                if fix_state not in DEAD and not (fix_id not in specs and
                                                   now >= fixed_at + timedelta(hours=72)):
                    continue
                src = f"dead-corrective:{fix_id}:{corrective['_src']}"
                why_dead = f"dead corrective {fix_id}"
            else:
                failures = [e for e in related if e.get("type") == "owed-failed"
                            and fold.ts(e.get("ts")) > row["governing_at"]]
                failure = failures[-1] if failures else None
                src = (failure.get("_src") if failure else None) or (
                    "unlocated:" + row["governing_at"].isoformat())
                failure_at = fold.ts(failure.get("ts")) if failure else row["governing_at"]
                if any(e.get("type") in ("owed-corrective", "owed-waived") and
                       fold.ts(e.get("ts")) > failure_at for e in related):
                    continue
            matching = [e for e in escs if _covers(e, criterion, src)]
            ref = None
            if matching:
                prior = matching[-1]
                if prior["_src"] not in answered or not _reopen(prior, evs, now):
                    continue
                if any(e["_src"] not in answered and criterion in _array(e.get("criteria"))
                       and fold.ts(e.get("ts")) >= fold.ts(prior.get("ts")) for e in escs):
                    continue
                ref = prior["_src"]
            groups.setdefault(ref, []).append((criterion, row.get("evidence") or "", src, why_dead))

        for ref, failures in groups.items():
            failures.sort(key=lambda item: item[0])
            criteria = [f[0] for f in failures]
            evidence = [f[1] for f in failures]
            why = f"owed check {', '.join(criteria)} failed: {str(evidence[0])[:200]}"
            dead = [f"{f[0]}: {f[3]}" for f in failures if f[3]]
            if dead:
                why += "; " + "; ".join(dead)
            action = "reopened" if ref else "escalated"
            if dry_run:
                action = "DRY RUN would reopen" if ref else "DRY RUN would escalate"
            else:
                extra = {"ref": ref} if ref else {}
                reason = dispatch.emit(ledger_path(), {"subject": sid}, "escalation-blocking",
                                       kind="owed-failed", criteria=criteria, evidence=evidence,
                                       failed_srcs=[f[2] for f in failures], addressed_to="thinker",
                                       default=FAILED_DEFAULT, revert=FAILED_REVERT,
                                       deadline=(now + timedelta(hours=24)).isoformat(timespec="seconds"),
                                       why=why, **extra)
                if reason:
                    lines.append(f"sweep-owed: escalation for {sid} refused: {reason}")
                    continue
            suffix = f" ref={ref}" if ref else ""
            lines.append(f"sweep-owed: {action} {sid} kind=owed-failed criteria={criteria}{suffix}")
    return lines
