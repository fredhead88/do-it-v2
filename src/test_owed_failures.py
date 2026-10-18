#!/usr/bin/env python3
"""Failure escalation regressions, including the production ledger round trip."""
import json
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).parent))
def test_owed_failures():
    ROOT = tempfile.TemporaryDirectory(prefix="owed-failures-")
    os.environ["DOIT_ROOT"] = ROOT.name
    os.environ["HOME"] = str(pathlib.Path(ROOT.name) / "home")
    os.environ.pop("DOIT_LEDGER_FILE", None)
    os.environ.pop("DOIT_PROJECT", None)
    import dispatch
    import fold
    import owed
    import owed_failures
    import sweep_owed

    NOW = datetime.now(timezone.utc).replace(microsecond=0)
    SID = "L-spec-9905"
    FIX = "L-spec-9906"
    fold.EVENTS.mkdir()


    def ev(event_type, hours, **fields):
        return {"type": event_type, "subject": SID, "ts": (NOW + timedelta(hours=hours)).isoformat(),
                "actor": "operator", "_src": f"L-operator-fixture.jsonl:{event_type}:{hours}", **fields}


    def fixture(criteria=("AC1",)):
        return [ev("owed-ac", -100, criterion=c, wake_at=(NOW - timedelta(hours=90)).isoformat())
                for c in criteria] + [ev("shipped", -90, actor="executor")] + [
            ev("owed-failed", -80, criterion=c, kind="unmet", evidence=f"{c} failed " + "x" * 220,
               _src=f"L-operator-fixture.jsonl:failure:{c}") for c in criteria]


    def specs(events, state="building"):
        return {SID: {"evs": events}, FIX: {"state": state, "evs": []}}


    def escalations():
        return [e for e in fold.read_events() if e["type"] == "escalation-blocking"]


    def clear():
        assert not any(e["type"] in ("unblocked", "decision") for e in fold.read_events())
        for path in fold.EVENTS.glob("*.jsonl"):
            path.unlink()


    # AC1/2: parallel arrays, exact fields, no actor on disk, dedupe by failure source.
    events = fixture(("AC2", "AC1"))
    assert owed_failures.FAILED_DEFAULT == (
        "the check stays failed and open, the spec stays shipped-owed-due, "
        "and the next digest lists it under SYSTEM HEALTH")
    assert owed_failures.FAILED_REVERT == (
        "an owed-corrective naming a spec, or an owed-waived with a reason, closes it")
    lines = owed_failures.escalate_failures(specs(events), NOW)
    assert lines == [f"sweep-owed: escalated {SID} kind=owed-failed criteria=['AC1', 'AC2']"]
    esc = escalations()[0]
    assert esc["criteria"] == ["AC1", "AC2"]
    assert esc["evidence"] == [events[-1]["evidence"], events[-2]["evidence"]]
    assert esc["failed_srcs"] == [events[-1]["_src"], events[-2]["_src"]]
    assert esc["addressed_to"] == "thinker" and esc["subject"] == SID
    assert esc["default"] == owed_failures.FAILED_DEFAULT
    assert esc["revert"] == owed_failures.FAILED_REVERT
    assert esc["deadline"] == (NOW + timedelta(hours=24)).isoformat(timespec="seconds")
    assert esc["why"] == "owed check AC1, AC2 failed: " + events[-1]["evidence"][:200]
    assert fold.open_questions([esc])[0]["asks"] == esc["why"]
    assert all("actor" not in json.loads(line) for line in sweep_owed.ledger_path().read_text().splitlines())
    assert owed_failures.escalate_failures(specs(events + [esc]), NOW) == []
    new_failure = ev("owed-failed", -1, criterion="AC1", evidence="new failure", kind="unmet")
    assert owed_failures.escalate_failures(specs(events + [esc, new_failure]), NOW)
    assert escalations()[-1]["failed_srcs"] == [new_failure["_src"]]
    clear()
    row = {"status": "failed", "criterion": "AC1", "evidence": "unlocated", "governing_at": NOW}
    with patch.object(owed, "checks", return_value=[row]):
        owed_failures.escalate_failures({SID: {"evs": []}}, NOW)
        sentinel = escalations()[0]
        assert sentinel["failed_srcs"] == ["unlocated:" + NOW.isoformat()]
        assert owed_failures.escalate_failures({SID: {"evs": [sentinel]}}, NOW) == []
    print("L1005 AC1 AC2 ok")

    # AC3: earliest answering instant, exact 24h boundary, later open coverage.
    clear()
    events = fixture()
    old = ev("escalation-blocking", -50, kind="owed-failed", criteria=["AC1"],
             failed_srcs=[events[-1]["_src"]])
    answers = [ev("decision", -30), ev("decision", -2)]
    assert owed.checks(events + [old, *answers], NOW)[0]["status"] == "failed"
    assert owed_failures.escalate_failures(specs(events + [old, answers[-1]]), NOW) == []
    before_reopen = {p.name: p.read_bytes() for p in fold.EVENTS.glob("*.jsonl")}
    assert owed_failures.escalate_failures(specs(events + [old, *answers]), NOW, dry_run=True) == [
        f"sweep-owed: DRY RUN would reopen {SID} kind=owed-failed criteria=['AC1'] ref={old['_src']}"]
    assert before_reopen == {p.name: p.read_bytes() for p in fold.EVENTS.glob("*.jsonl")}
    assert owed_failures.escalate_failures(specs(events + [old, *answers]), NOW)
    reopened = escalations()[0]
    assert reopened["ref"] == old["_src"]
    assert owed_failures.escalate_failures(specs(events + [old, *answers, reopened]), NOW) == []
    for answer_type in ("decision", "unblocked"):
        clear()
        answer = ev(answer_type, -24, ref=old["_src"], subject="other")
        assert owed_failures.escalate_failures(specs(events + [old, answer]), NOW)
        clear()
        assert owed_failures.escalate_failures(specs(events + [old, answer]), NOW - timedelta(seconds=1)) == []
    print("L1005 AC3 ok")

    # AC4/10/11: shipped rechecks once, dead and missing correctives escalate once.
    corrective = ev("owed-corrective", -40, criterion="AC1", corrective=FIX)
    correcting = events + [corrective]
    assert owed.checks(correcting, NOW)[0]["status"] == "correcting"
    for state in ("written", "building", "graded", "reviewing"):
        assert owed_failures.escalate_failures(specs(correcting, state), NOW) == []
    for state in ("shipped", "shipped-owed-due", "shipped-owed-evidence", "shipped-owed-expired",
                  "accepted", "closed-shipped"):
        clear()
        assert owed_failures.escalate_failures(specs(correcting, state), NOW, dry_run=True) == [
            f"sweep-owed: DRY RUN would recheck {SID} criterion=AC1"]
        assert fold.read_events() == []
        assert owed_failures.escalate_failures(specs(correcting, state), NOW) == [
            f"sweep-owed: recheck {SID} criterion=AC1"]
        routed = fold.read_events()[0]
        assert routed["type"] == "owed-routed" and routed["route"] == "recheck"
        assert routed["subject"] == SID and routed["criterion"] == "AC1"
        assert owed_failures.escalate_failures(specs(correcting + [routed], state), NOW) == []
        for kind, status in (("owed-met", "met"), ("owed-failed", "failed")):
            result = ev(kind, 1, criterion="AC1", evidence="rechecked", kind="unmet")
            assert owed.checks(correcting + [routed, result], NOW + timedelta(hours=2))[0]["status"] == status
            if status == "failed":
                assert owed_failures.escalate_failures(specs(correcting + [routed, result], state),
                                                       NOW + timedelta(hours=2))
    for state in ("killed", "void", "dropped", "closed-unbuilt"):
        clear()
        assert owed_failures.escalate_failures(specs(correcting, state), NOW)
        dead = escalations()[0]
        assert dead["failed_srcs"] == [f"dead-corrective:{FIX}:{corrective['_src']}"]
        assert FIX in dead["why"]
        assert owed_failures.escalate_failures(specs(correcting + [dead], state), NOW) == []
        live = ev("owed-corrective", -1, criterion="AC1", corrective="L-spec-9907")
        superseded = specs(correcting + [dead, live], state)
        superseded["L-spec-9907"] = {"state": "building", "evs": []}
        assert owed_failures.escalate_failures(superseded, NOW) == []
    clear()
    assert owed_failures.escalate_failures({SID: {"evs": correcting}}, NOW) == []
    older = {**corrective, "ts": (NOW - timedelta(hours=72)).isoformat()}
    assert owed_failures.escalate_failures({SID: {"evs": events + [older]}}, NOW)
    for kind, fields in (("owed-corrective", {"corrective": FIX}), ("owed-waived", {"reason": "accepted"})):
        clear()
        resolved = ev(kind, -1, criterion="AC1", **fields)
        assert owed_failures.escalate_failures(specs(events + [old, *answers, resolved]), NOW) == []
    assert not any(e["type"] in ("unblocked", "decision") for e in fold.read_events())
    print("L1005 AC4 AC10 AC11 ok")

    # AC6/7: production emit, newly opened file handles, fold, separate real corrective append.
    clear()
    source = fold.EVENTS / "L-operator-fixture.jsonl"
    source.write_text("".join(json.dumps({k: v for k, v in e.items() if k not in ("actor", "_src")}) + "\n"
                              for e in events if e["type"] != "shipped"))
    (fold.EVENTS / "L-executor-fixture.jsonl").write_text(json.dumps(events[1]) + "\n")
    fresh_specs = fold.fold(fold.read_events())[0]
    before = {p.name: p.read_bytes() for p in fold.EVENTS.glob("*.jsonl")}
    assert "DRY RUN would escalate" in owed_failures.escalate_failures(fresh_specs, NOW, dry_run=True)[0]
    assert before == {p.name: p.read_bytes() for p in fold.EVENTS.glob("*.jsonl")}
    owed_failures.escalate_failures(fresh_specs, NOW)
    fresh = fold.read_events()
    assert fold.fold(fresh)[0][SID]
    assert escalations()[0]["actor"] == "sweep"
    assert len(fold.open_escalations(fresh)) == 1
    with patch.object(fold, "NOW", NOW + timedelta(seconds=2)):
        fold.append(["owed-corrective", SID, "criterion=AC1", f"corrective={FIX}"], strict=True)
    assert fold.fold(fold.read_events())[0][SID]
    assert fold.open_escalations(fold.read_events()) == []
    with patch.object(dispatch, "emit", return_value="fixture refusal"):
        assert owed_failures.escalate_failures(specs(events), NOW) == [
            f"sweep-owed: escalation for {SID} refused: fixture refusal"]
    print("L1005 AC6 AC7 AC9 round trip ok")
    # L1113: unknown classification must never authorize a close.
    import checks
    text = "AC1 [backend]: fixture within 48 hours\nAC2 [backend]: fixture\n"
    base = {"criterion": "AC1", "capability": "operator-action"}
    cases = [(base, text, "needs operator"),
             ({**base, "last_unobservable_capability": "ro-dsn-grant"}, text, "ro-dsn-grant")]
    cases += [({**base, "capability": cap}, text, None)
              for cap in ("ro-dsn", "droplet", "browser", "other")]
    cases += [({**base, "criterion": "AC9"}, text, None), (base, "", None),
              ({**base, "status": "quarantined"}, text, None)]
    for row, body, want in cases:
        assert owed_failures.no_runner(row, body, None) == want
        print("L1113 AC1", row, "=>", want)
    with patch.object(checks, "record_for_owed", side_effect=ValueError("undetermined")):
        assert owed_failures.no_runner(base, text, None) is None
    print("L1113 AC1 classification exception => None")

    # Counts are admitted, distinct per subject/criterion/type, and UTC-calendar.
    month_start = NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    counts_events = [ev("owed-met", 0, criterion=f"AC{i}") for i in range(9)]
    counts_events += [counts_events[0], ev("owed-waived", 0, criterion="AC10"),
                      ev("owed-unverifiable", 0, criterion="AC11"),
                      ev("owed-met", 0, criterion="old", ts=(month_start-timedelta(seconds=1)).isoformat()),
                      ev("owed-unverifiable", 0, criterion="bad", actor="builder")]
    assert owed_failures.month_counts(counts_events, NOW) == {
        "met": 9, "waived": 1, "unverifiable": 1, "closed": 11, "unverifiable_today": 1}
    print("L1113 AC3 monthly counts", owed_failures.month_counts(counts_events, NOW))

    dispatch.CONTENT.mkdir(parents=True, exist_ok=True)
    dispatch.spec_path(SID).write_text(text)
    for label, hours, fields, want in [
        ("explicit valid", -25, {"valid_until": (NOW+timedelta(hours=1)).isoformat()}, True),
        ("explicit stale", -1, {"valid_until": (NOW-timedelta(seconds=1)).isoformat()}, False),
        ("explicit boundary", -1, {"valid_until": NOW.isoformat()}, False),
        ("default recent", -23, {}, True), ("default stale", -25, {}, False),
        ("observed_at overrides ts", -1, {"observed_at": (NOW-timedelta(hours=25)).isoformat()}, False),
        ("criterion window", -25, {}, True), ("criterion stale", -49, {}, False)]:
        clear()
        cid = "AC1" if label.startswith("criterion") else "AC2"
        failure = ev("owed-failed", hours, criterion=cid, evidence="unmet", **fields)
        events2 = [ev("owed-ac", -100, criterion=cid, wake_at=(NOW-timedelta(hours=90)).isoformat()),
                   ev("shipped", -90, actor="executor"), failure]
        result = owed_failures.escalate_failures(specs(events2), NOW)
        assert bool(result) == want, (label, result)
        assert owed.checks(events2, NOW)[0]["status"] == "failed"
        print("L1113 AC6", label, "escalated", want)
    for extended in (False, True):
        clear()
        fields = {"valid_until": (NOW+timedelta(hours=4)).isoformat()} if extended else {}
        failure = ev("owed-failed", -26, criterion="AC2", evidence="unmet", **fields)
        old2 = ev("escalation-blocking", -25, kind="owed-failed", criteria=["AC2"],
                  failed_srcs=[failure["_src"]])
        events2 = [ev("owed-ac", -100, criterion="AC2", wake_at=(NOW-timedelta(hours=90)).isoformat()),
                   ev("shipped", -90, actor="executor"), failure, old2,
                   ev("decision", -24, ref=old2["_src"])]
        assert bool(owed_failures.escalate_failures(specs(events2), NOW)) == extended
        assert owed.checks(events2, NOW)[0]["status"] == "failed"
        print("L1113 AC6 reopen", "extended" if extended else "default retired", extended)
    with patch.object(checks, "record_for_owed", return_value={"validity_h": None}):
        assert owed_failures.escalate_failures(specs(fixture()), NOW, dry_run=True)
    dispatch.spec_path(SID).unlink()
    assert owed_failures.escalate_failures(specs(fixture()), NOW, dry_run=True)
    print("L1113 AC6 sha-bound and unreadable remain failures")
    ROOT.cleanup()


if __name__ == "__main__":
    test_owed_failures()
