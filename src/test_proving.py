#!/usr/bin/env python3
"""One runnable check on proving.py's rules. Run: python3 test_proving.py"""
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import owed      # noqa: E402
import proving   # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOIT_PATH = str(REPO_ROOT / "doit")

NOW = datetime.now(timezone.utc)


def stamp(d=0):
    """`d` days ago as an ISO-8601 string (negative `d` is `d` days in the
    future) — `test_owed.py`'s own convention."""
    return (NOW - timedelta(days=d)).isoformat(timespec="seconds")


def at(d=0):
    """Same offset, as a `datetime` rather than a string — floored to whole
    seconds to match `stamp()`'s `isoformat(timespec="seconds")` round-trip
    through `datetime.fromisoformat`."""
    return (NOW - timedelta(days=d)).replace(microsecond=0)


def ev(etype, ts, **kw):
    return {"type": etype, "ts": ts, **kw}


def spec(id_, state, evs, charter, rejects=0):
    return {"id": id_, "state": state, "evs": evs, "charter": charter, "rejects": rejects}


def test_ac1():
    assert proving.LABEL == "Proving", proving.LABEL
    assert proving.GRACE_DAYS == 3, proving.GRACE_DAYS
    assert proving.UNDATED_DAYS == 7, proving.UNDATED_DAYS
    assert proving.DISCHARGED == ("met", "waived", "dropped"), proving.DISCHARGED
    assert proving.BUILD_DONE == ("accepted", "shipped", "shipped-owed-due", "shipped-owed-evidence",
                                  "shipped-owed-expired", "closed-shipped", "closed-unbuilt", "killed",
                                  "dropped"), proving.BUILD_DONE
    print("AC1 ok")


def test_ac2():
    charter = [ev("l1-complete", stamp(5))]
    mine_accepted = [spec("S2", "accepted", [], "L-charter-9002")]
    assert proving.phase(charter, mine_accepted) == "proving"

    mine_building = [spec("S2", "building", [], "L-charter-9002")]
    assert proving.phase(charter, mine_building) is None

    mine_rework = [spec("S2", "shipped", [], "L-charter-9002", rejects=2)]
    assert proving.phase(charter, mine_rework) is None

    mine_shipped_ok = [spec("S2", "shipped", [], "L-charter-9002", rejects=0)]
    assert proving.phase(charter, mine_shipped_ok) == "proving"

    charter_retracted = charter + [ev("charter-retracted", stamp(1))]
    assert proving.phase(charter_retracted, mine_accepted) is None

    assert proving.phase([], mine_accepted) is None
    print("AC2 ok")


def test_ac3():
    mine_killed = [spec("S3", "killed", [], "L-charter-9003")]
    T0, T1 = stamp(30), stamp(20)
    charter = [ev("cut-written", stamp(35)), ev("planner-started", T0), ev("planner-ended", T1)]
    assert proving.phase(charter, mine_killed) == "proving"

    T2 = stamp(5)                                          # planner re-engaged after ending
    charter_reengaged = charter + [ev("planner-started", T2)]
    assert proving.phase(charter_reengaged, mine_killed) is None

    assert proving.phase(charter, []) is None               # len(mine) >= 1 required
    print("AC3 ok")


def test_ac4():
    charter_base = [ev("l1-complete", stamp(40))]
    mine_accepted = [spec("S4", "accepted", [], "L-charter-9004")]

    T0 = stamp(30)
    charter_t0 = charter_base + [ev("charter-proving", T0)]
    assert proving.phase(charter_t0, mine_accepted) == "proving"
    assert proving.entered_at(charter_t0) == at(30), proving.entered_at(charter_t0)

    T1 = stamp(20)
    charter_t1 = charter_t0 + [ev("charter-reopened", T1, spec="S4", criterion="AC1", failed_src="f:1")]
    assert proving.phase(charter_t1, mine_accepted) == "reopened"

    T2 = stamp(10)
    charter_t2 = charter_t1 + [ev("charter-proving", T2)]
    assert proving.phase(charter_t2, mine_accepted) == "proving"
    assert proving.entered_at(charter_t2) == at(10), proving.entered_at(charter_t2)

    assert proving.entered_at(charter_base) is None
    print("AC4 ok")


def test_ac5():
    charter_base = [ev("l1-complete", stamp(40))]
    T0, T1 = stamp(30), stamp(20)
    charter_t0 = charter_base + [ev("charter-proving", T0)]
    charter_t1 = charter_t0 + [ev("charter-reopened", T1, spec="S5", criterion="AC1", failed_src="f:1")]

    mine_plain = [spec("S5", "accepted", [], "L-charter-9005")]
    assert proving.should_enter(charter_t0, mine_plain) is False           # only T0, no reopen
    assert proving.should_enter(charter_t1, mine_plain) is False           # reopened, no fix yet

    T1_5 = stamp(15)                                                       # > T1
    mine_met = [spec("S5", "accepted", [ev("owed-met", T1_5, criterion="AC1")], "L-charter-9005")]
    assert proving.should_enter(charter_t1, mine_met) is True

    mine_redate = [spec("S5", "accepted",
                        [ev("owed-ac", T1_5, criterion="AC1", actor="executor")], "L-charter-9005")]
    assert proving.should_enter(charter_t1, mine_redate) is True

    mine_wrong = [spec("S5", "accepted", [ev("owed-met", T1_5, criterion="AC2")], "L-charter-9005")]
    assert proving.should_enter(charter_t1, mine_wrong) is False

    assert proving.should_enter(charter_base, mine_plain) is True          # never entered, build-done

    mine_not_done = [spec("S5", "building", [], "L-charter-9005")]
    assert proving.should_enter(charter_base, mine_not_done) is False      # phase() is None
    print("AC5 ok")


def test_ac6():
    T0 = stamp(50)                                          # owed-ac
    T1 = stamp(40)                                          # charter-proving -> entered_at
    T2 = stamp(30)                                          # owed-failed, after both

    ship = ev("shipped", stamp(60))
    ac = ev("owed-ac", T0, criterion="AC1")
    fail = ev("owed-failed", T2, criterion="AC1", kind="unmet", _src="f:9")
    charter = [ev("charter-proving", T1)]

    mine = [spec("S", "shipped-owed-due", [ship, ac, fail], "L-charter-9006")]
    assert proving.to_reopen(charter, mine, NOW) == [
        {"spec": "S", "criterion": "AC1", "failed_src": "f:9"}]

    charter_recorded = charter + [ev("charter-reopened", stamp(10), spec="S", criterion="AC1",
                                     failed_src="f:9")]
    assert proving.to_reopen(charter_recorded, mine, NOW) == []

    mine_stale = [spec("S", "shipped-owed-due", [ship, ac, {**fail, "kind": "stale"}],
                       "L-charter-9006")]
    assert proving.to_reopen(charter, mine_stale, NOW) == []

    fail_early = {**fail, "ts": stamp(45)}                  # after ac(T0=50), before entered_at(T1=40)
    mine_early = [spec("S", "shipped-owed-due", [ship, ac, fail_early], "L-charter-9006")]
    assert proving.to_reopen(charter, mine_early, NOW) == []

    ac2 = ev("owed-ac", T0, criterion="AC2")
    met2 = ev("owed-met", T2, criterion="AC2")
    mine_multi = [spec("S", "shipped-owed-due", [ship, ac, fail, ac2, met2], "L-charter-9006")]
    assert proving.to_reopen(charter, mine_multi, NOW) == [
        {"spec": "S", "criterion": "AC1", "failed_src": "f:9"}]

    assert proving.to_reopen([], mine, NOW) == []            # no charter-proving -> entered_at is None
    print("AC6 ok")


def test_ac7():
    T0 = at(30)
    checks = {"S7": [{"criterion": "AC1", "line": "verify the thing",
                      "due_at": T0 + timedelta(days=4), "status": "waiting"}]}
    mine = [spec("S7", "accepted", [], "L-charter-9007")]

    charter_swept = [ev("l1-complete", stamp(40)), ev("charter-proving", stamp(30)),
                     ev("sweep-fixpoint", stamp(1))]
    out = proving.summary("L-charter-9007", charter_swept, mine, NOW, title="t", review_owed=False,
                          open_briefs=[], checks=checks)
    assert out["items"] == [{"kind": "owed-check", "spec": "S7", "criterion": "AC1",
                             "text": "verify the thing", "due_at": proving._iso(T0 + timedelta(days=4)),
                             "status": "waiting", "owner": "owed-sweeper"}], out["items"]

    charter_unswept = [ev("l1-complete", stamp(40)), ev("charter-proving", stamp(30))]
    out2 = proving.summary("L-charter-9007", charter_unswept, mine, NOW, title="t", review_owed=False,
                           open_briefs=[], checks=checks)
    sweep_items = [it for it in out2["items"] if it["kind"] == "sweep"]
    assert len(out2["items"]) == 2 and len(sweep_items) == 1, out2["items"]
    assert sweep_items[0]["due_at"] == proving._iso(T0) and sweep_items[0]["owner"] == "executor"
    assert out2["next"]["kind"] == "sweep", out2["next"]        # sweep(T0) earlier than owed-check(T0+4d)

    checks_early = {"S7": [{**checks["S7"][0], "due_at": T0 - timedelta(days=1)}]}
    out3 = proving.summary("L-charter-9007", charter_unswept, mine, NOW, title="t", review_owed=False,
                           open_briefs=[], checks=checks_early)
    assert out3["next"]["kind"] == "owed-check", out3["next"]

    checks_tie = {"S7": [{**checks["S7"][0], "due_at": T0}]}
    out4 = proving.summary("L-charter-9007", charter_unswept, mine, NOW, title="t", review_owed=False,
                           open_briefs=[], checks=checks_tie)
    assert out4["next"]["kind"] == "owed-check", out4["next"]    # tie: kind order picks owed-check
    print("AC7 ok")


def test_ac8():
    charter = [ev("l1-complete", stamp(20)), ev("charter-proving", stamp(10)),
              ev("sweep-fixpoint", stamp(1))]

    mine_shipped = [spec("S8", "shipped", [], "L-charter-9008", rejects=0)]
    out = proving.summary("L-charter-9008", charter, mine_shipped, NOW, title="t8", review_owed=False,
                          open_briefs=[], checks={"S8": []})
    assert len(out["items"]) == 1 and out["items"][0]["kind"] == "spec-review", out["items"]
    assert out["items"][0]["due_at"] == proving._iso(at(10)) and out["items"][0]["owner"] == "executor"

    kill_ev = ev("spec-killed", stamp(5), superseded_by="L-spec-ghost")
    mine_killed = [spec("S8k", "killed", [kill_ev], "L-charter-9008")]
    out_k = proving.summary("L-charter-9008", charter, mine_killed, NOW, title="t8", review_owed=False,
                            open_briefs=[], checks={"S8k": []})
    killed_items = [it for it in out_k["items"] if it["kind"] == "killed-spec"]
    assert len(killed_items) == 1 and killed_items[0]["owner"] == "thinker", out_k["items"]

    accept_ev = ev("kill-accepted", stamp(2), actor="operator")
    mine_killed_ok = [spec("S8k", "killed", [kill_ev, accept_ev], "L-charter-9008")]
    out_k2 = proving.summary("L-charter-9008", charter, mine_killed_ok, NOW, title="t8",
                             review_owed=False, open_briefs=[], checks={"S8k": []})
    assert not any(it["kind"] == "killed-spec" for it in out_k2["items"]), out_k2["items"]

    mine_plain = [spec("S8n", "accepted", [], "L-charter-9008")]
    out_rev = proving.summary("L-charter-9008", charter, mine_plain, NOW, title="t8", review_owed=True,
                              open_briefs=[], checks={"S8n": []})
    rev_items = [it for it in out_rev["items"] if it["kind"] == "charter-review"]
    assert len(rev_items) == 1 and rev_items[0]["owner"] == "executor", out_rev["items"]

    mine_with_review = [spec("S8n", "shipped", [], "L-charter-9008", rejects=0)]
    out_gated = proving.summary("L-charter-9008", charter, mine_with_review, NOW, title="t8",
                                review_owed=True, open_briefs=[], checks={"S8n": []})
    assert not any(it["kind"] == "charter-review" for it in out_gated["items"]), out_gated["items"]

    charter_alt = [ev("cut-written", stamp(35)), ev("planner-started", stamp(30)),
                   ev("planner-ended", stamp(20)), ev("sweep-fixpoint", stamp(1))]
    mine_alt = [spec("S8alt", "killed", [], "L-charter-9008")]
    out_alt = proving.summary("L-charter-9008", charter_alt, mine_alt, NOW, title="t8",
                              review_owed=False, open_briefs=[], checks={"S8alt": []})
    alt_items = [it for it in out_alt["items"] if it["kind"] == "l1-missing"]
    assert len(alt_items) == 1 and alt_items[0]["owner"] == "operator", out_alt["items"]
    print("AC8 ok")


def test_ac9():
    now9 = datetime.fromisoformat("2026-01-01T12:00:00+00:00")
    entered9 = now9 - timedelta(days=1)

    def stamp9(dt):
        return dt.isoformat(timespec="seconds")

    charter = [ev("l1-complete", stamp9(now9 - timedelta(days=10))),
              ev("charter-proving", stamp9(entered9)),
              ev("sweep-fixpoint", stamp9(now9 - timedelta(hours=1)))]
    mine = [spec("S9", "accepted", [], "L-charter-9009")]

    row_a = {"criterion": "AC1", "line": None, "due_at": now9 + timedelta(days=2), "status": "waiting"}
    row_b = {"criterion": "AC2", "line": None, "due_at": None, "status": "waiting"}
    out = proving.summary("L-charter-9009", charter, mine, now9, title="t9", review_owed=False,
                          open_briefs=[], checks={"S9": [row_a, row_b]})
    assert len(out["items"]) == 2 and all(it["kind"] == "owed-check" for it in out["items"]), out["items"]
    assert out["next"]["due_at"] == proving._iso(now9 + timedelta(days=2)), out["next"]
    assert out["deadline"] == proving._iso(now9 + timedelta(days=9)), out["deadline"]
    assert out["colour"] == "on-time", out["colour"]

    row_a_today = {**row_a, "due_at": now9 - timedelta(hours=1)}
    out_today = proving.summary("L-charter-9009", charter, mine, now9, title="t9", review_owed=False,
                                open_briefs=[], checks={"S9": [row_a_today, row_b]})
    assert out_today["colour"] == "due-today", out_today["colour"]

    row_a_over = {**row_a, "due_at": now9 - timedelta(days=2)}
    out_over = proving.summary("L-charter-9009", charter, mine, now9, title="t9", review_owed=False,
                               open_briefs=[], checks={"S9": [row_a_over, row_b]})
    assert out_over["colour"] == "overdue", out_over["colour"]

    ac_ev = ev("owed-ac", stamp9(entered9 - timedelta(days=5)), criterion="AC1")
    fail_unmet = ev("owed-failed", stamp9(entered9 + timedelta(hours=2)), criterion="AC1",
                    kind="unmet", evidence="e")
    mine_unmet = [spec("S9", "accepted", [ac_ev, fail_unmet], "L-charter-9009")]
    out_unmet = proving.summary("L-charter-9009", charter, mine_unmet, now9, title="t9",
                                review_owed=False, open_briefs=[], checks={"S9": [row_a, row_b]})
    item_ac1 = next(it for it in out_unmet["items"] if it["criterion"] == "AC1")
    assert item_ac1["owner"] == "thinker", item_ac1

    fail_stale = {**fail_unmet, "kind": "stale"}
    mine_stale = [spec("S9", "accepted", [ac_ev, fail_stale], "L-charter-9009")]
    out_stale = proving.summary("L-charter-9009", charter, mine_stale, now9, title="t9",
                                review_owed=False, open_briefs=[], checks={"S9": [row_a, row_b]})
    item_ac1_stale = next(it for it in out_stale["items"] if it["criterion"] == "AC1")
    assert item_ac1_stale["owner"] == "owed-sweeper", item_ac1_stale

    charter_nosweep = [e for e in charter if e["type"] != "sweep-fixpoint"]
    out_nosweep = proving.summary("L-charter-9009", charter_nosweep, mine, now9, title="t9",
                                  review_owed=False, open_briefs=[], checks={"S9": [row_a, row_b]})
    sweep_item = next(it for it in out_nosweep["items"] if it["kind"] == "sweep")
    assert sweep_item["due_at"] == proving._iso(entered9), sweep_item
    assert out_nosweep["colour"] == "overdue", out_nosweep["colour"]
    assert out_nosweep["next"]["kind"] == "sweep", out_nosweep["next"]
    print("AC9 ok")


def test_ac10():
    T0 = at(30)
    checks = {"S10": [{"criterion": "AC1", "line": "x", "due_at": T0 + timedelta(days=4),
                       "status": "waiting"}]}
    mine = [spec("S10", "accepted", [], "L-charter-9010")]
    charter = [ev("l1-complete", stamp(40)), ev("charter-proving", stamp(30)),
              ev("sweep-fixpoint", stamp(1))]
    out = proving.summary("L-charter-9010", charter, mine, NOW, title="t", review_owed=False,
                          open_briefs=[], checks=checks)

    expected_keys = {"id", "title", "phase", "label", "entered_at", "remaining", "next",
                     "deadline", "owner", "colour", "items"}
    assert set(out.keys()) == expected_keys, out.keys()
    assert isinstance(out["deadline"], str) and datetime.fromisoformat(out["deadline"])
    if out["entered_at"] != "entry not recorded":
        assert isinstance(out["entered_at"], str) and datetime.fromisoformat(out["entered_at"])
    for it in out["items"]:
        assert isinstance(it["due_at"], str) and datetime.fromisoformat(it["due_at"])
    if out["next"] is not None:
        assert isinstance(out["next"]["due_at"], str) and datetime.fromisoformat(out["next"]["due_at"])
    json.dumps(out)                                          # raises nothing
    print("AC10 ok")


def test_ac11():
    ship = ev("shipped", stamp(20))
    ac = ev("owed-ac", stamp(25), criterion="AC1", wake_at=stamp(15))
    evs = [ac, ship]
    mine = [spec("S11", "shipped-owed-due", evs, "L-charter-9011")]
    charter = [ev("l1-complete", stamp(40)), ev("charter-proving", stamp(35)),
              ev("sweep-fixpoint", stamp(1))]

    out_none = proving.summary("L-charter-9011", charter, mine, NOW, title="t11", review_owed=False,
                               open_briefs=[])
    computed = {"S11": owed.checks(evs, NOW)}
    out_computed = proving.summary("L-charter-9011", charter, mine, NOW, title="t11", review_owed=False,
                                   open_briefs=[], checks=computed)
    assert json.dumps(out_none, sort_keys=True) == json.dumps(out_computed, sort_keys=True)

    real_rows = owed.checks(evs, NOW)
    assert real_rows and real_rows[0]["status"] != "met", real_rows
    forced = [{**real_rows[0], "status": "met"}]
    out_forced = proving.summary("L-charter-9011", charter, mine, NOW, title="t11", review_owed=False,
                                 open_briefs=[], checks={"S11": forced})
    assert out_forced["remaining"] != out_none["remaining"] or out_forced["items"] != out_none["items"]
    print("AC11 ok")


def test_ac12():
    doit_text = (REPO_ROOT / "doit").read_text()
    assert re.search(r"^\s*proving\)", doit_text, re.MULTILINE), "doit: no proving) case"
    assert "proving.py" in doit_text, "doit: no proving.py reference"
    bash_check = subprocess.run(["bash", "-n", str(REPO_ROOT / "doit")], capture_output=True)
    assert bash_check.returncode == 0, bash_check.stderr

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "events").mkdir()
        env = {**os.environ, "DOIT_ROOT": str(root)}
        env.pop("DOIT_PROJECT", None)
        result = subprocess.run([DOIT_PATH, "proving", "--json"], env=env, capture_output=True, text=True)
        assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
        assert result.stdout.strip() == "[]", result.stdout

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        events_dir = root / "events"
        events_dir.mkdir()
        now12 = datetime.now(timezone.utc)

        def iso(dt):
            return dt.isoformat(timespec="seconds")

        (events_dir / "L-operator-9101.jsonl").write_text("\n".join([
            json.dumps({"type": "l1-complete", "subject": "L-charter-9001", "charter": "L-charter-9001",
                       "ts": iso(now12 - timedelta(days=5))}),
            json.dumps({"type": "spec-closed", "subject": "L-spec-9001", "charter": "L-charter-9001",
                       "ts": iso(now12 - timedelta(days=4))}),
        ]) + "\n")
        # `wake_at`, not `due_at`, is `owed-ac`'s own input field (owed.py never
        # reads `due_at` off the raw event) — carried anyway since it costs
        # nothing and no `shipped` event exists for L-spec-9001 either way, so
        # `owed.checks()`'s row is `due_at=None` regardless (shipped_at is None,
        # the "closed-unbuilt"/no-shipped shape this AC deliberately seeds) and
        # falls to the UNDATED_DAYS default. What this AC actually proves —
        # that the sort key is `deadline`, not `id` — holds either way: ANY
        # owed-ac criterion on L-charter-9001 pushes its deadline
        # (entered_at-or-now + UNDATED_DAYS + GRACE_DAYS) strictly past
        # L-charter-9002's zero-owed-row minimum (entered_at-or-now + GRACE_DAYS).
        (events_dir / "L-spec-writer-9101.jsonl").write_text(json.dumps({
            "type": "owed-ac", "subject": "L-spec-9001", "charter": "L-charter-9001",
            "criterion": "AC1", "wake_at": iso(now12 + timedelta(days=10)), "line": "x",
            "ts": iso(now12 - timedelta(days=3))}) + "\n")

        (events_dir / "L-operator-9102.jsonl").write_text("\n".join([
            json.dumps({"type": "l1-complete", "subject": "L-charter-9002", "charter": "L-charter-9002",
                       "ts": iso(now12 - timedelta(days=5))}),
            json.dumps({"type": "spec-closed", "subject": "L-spec-9002", "charter": "L-charter-9002",
                       "ts": iso(now12 - timedelta(days=4))}),
        ]) + "\n")

        env = {**os.environ, "DOIT_ROOT": str(root)}
        env.pop("DOIT_PROJECT", None)
        result = subprocess.run([DOIT_PATH, "proving", "--json"], env=env, capture_output=True, text=True)
        assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
        data = json.loads(result.stdout)
        ids = [row["id"] for row in data]
        assert ids == ["L-charter-9002", "L-charter-9001"], ids
        row9001 = next(r for r in data if r["id"] == "L-charter-9001")
        assert row9001["phase"] == "proving", row9001

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        events_dir = root / "events"
        events_dir.mkdir()
        (events_dir / "L-x-0001.jsonl").mkdir()              # IsADirectoryError on read
        env = {**os.environ, "DOIT_ROOT": str(root)}
        env.pop("DOIT_PROJECT", None)
        result = subprocess.run([DOIT_PATH, "proving", "--json"], env=env, capture_output=True, text=True)
        assert result.returncode != 0, result.returncode
        assert result.stdout == "", result.stdout
    print("AC12 ok")


test_ac1()
test_ac2()
test_ac3()
test_ac4()
test_ac5()
test_ac6()
test_ac7()
test_ac8()
test_ac9()
test_ac10()
test_ac11()
test_ac12()
print("proving: all checks pass")
