"""realm.py: the compact row shape, the tail offsets, and a bootstrap over a throwaway root."""
import json, os, pathlib, sys, tempfile
TMP = pathlib.Path(tempfile.mkdtemp())
(TMP / "events").mkdir(); (TMP / "seat").mkdir()
os.environ["DOIT_ROOT"] = str(TMP)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import realm  # noqa: E402

def w(name, rows):
    (TMP / "events" / name).write_text("".join(json.dumps(r) + "\n" for r in rows))

def test_compact_keeps_pipeline_rows_and_drops_noise():
    e = {"ts": "2026-09-25T05:00:00+00:00", "type": "spawn-done", "subject": "L-spec-0001", "spawn": "L-grader-0007",
         "duration_ms": 61000, "subagent_tokens": 12345, "status": "DONE", "model_used": "claude-sonnet-5"}
    c = realm.compact(e, "grader")
    assert c["r"] == "grader" and c["dur"] == 61000 and c["tok"] == 12345 and c["a"] == "grader"
    assert realm.compact({"ts": "x", "type": "worked"}, "x") is None
    assert realm.compact({"ts": "x", "type": "lesson", "text": "no problem field"}, "builder") is None
    assert realm.compact({"ts": "x", "type": "lesson", "problem": "disk-full", "text": "t"}, "thinker")["pb"] == "disk-full"

def test_bootstrap_and_offsets_over_a_throwaway_root():
    w("L-executor-0001.jsonl", [
        {"ts": "2026-09-25T04:00:00+00:00", "type": "tick", "lane": 3},
        {"ts": "2026-09-25T04:01:00+00:00", "type": "spawn-started", "subject": "L-spec-0001", "spawn": "L-grader-0001", "role": "grader"},
        {"ts": "2026-09-25T04:05:00+00:00", "type": "spawn-failed", "subject": "L-spec-0001", "spawn": "L-grader-0001", "why": "unserved after 300s"}])
    (TMP / "seat" / "L-grader-0001.claimed").touch()
    rows, raw, offsets = realm.read_all()
    assert [r["ty"] for r in rows] == ["tick", "spawn-started", "spawn-failed"]
    assert offsets["L-executor-0001.jsonl"] == (TMP / "events" / "L-executor-0001.jsonl").stat().st_size
    b = realm.bootstrap(0, states=[])
    assert b["n_events"] == 3 and b["stats"]["grader"]["fails"] == {"unserved": 1}
    assert b["events"][1]["cl"]  # claim time joined from the seat file
    # a torn tail is not consumed
    with open(TMP / "events" / "L-executor-0001.jsonl", "a") as fh:
        fh.write('{"ts":"2026-09-25T04:06:00+00:00","type":"tick"')
    _, _, off2 = realm.read_all()
    assert off2["L-executor-0001.jsonl"] == offsets["L-executor-0001.jsonl"]


# ---------------------------------------------------------------- omen model (realm_omens.py)
import datetime as _dt
import realm_omens as om  # noqa: E402

NOW = _dt.datetime(2026, 10, 15, 12, 0, tzinfo=_dt.timezone.utc)


def _ago(h):
    return (NOW - _dt.timedelta(hours=h)).isoformat(timespec="seconds")


def _row(ty, h, s=None, **kw):
    return {"t": _ago(h), "ty": ty, "s": s, **kw}


def _brief(h, pb, **kw):
    return _row("brief", h, "look:x", pb=pb, **kw)


def test_aliases_merge_serving_pane_variants_into_one_name():
    for v in ("serving-pane-started-for-claimed-seat", "serving-pane-launched-for-claimed-seats", "serving-pane-claim-race"):
        assert om.canonical(v) == "relay-serves-claimed-seat"
    assert om.canonical("look-packet-unserved") == "look-packet-unserved"


def test_recurring_needs_a_last_24h_occurrence_and_resolves_after():
    rows = [_brief(h, "look-packet-unserved", own="relay") for h in (200, 100, 60)]       # lifetime 3, last seen 60 h ago
    active, resolved = om.recurring(rows, [], NOW)
    assert active == [] and [r["id"] for r in resolved] == ["rec:look-packet-unserved"]     # quiet: resolved, not an omen
    rows.append(_brief(2, "look-packet-unserved", own="relay"))
    active, resolved = om.recurring(rows, [], NOW)
    assert [a["id"] for a in active] == ["rec:look-packet-unserved"] and resolved == []
    a = active[0]
    assert a["owner"] == "relay" and not a["needs_you"] and a["sev"] <= 1
    assert a["trend"].startswith("1 in 24h") and "4 since" in a["trend"]                    # lifetime is the trend line
    assert a["last"] == _ago(2) and a["since"] == _ago(2)                                   # a 58 h gap starts a new spell


def test_recurring_since_is_the_start_of_the_current_spell_and_variants_are_listed():
    rows = [_brief(h, pb) for h, pb in ((300, "serving-pane-claim-race"), (30, "serving-pane-claim-race"),
                                          (20, "serving-pane-started-for-claimed-seat"), (3, "serving-pane-for-claimed-seat"))]
    (a,), _ = om.recurring(rows, [], NOW)
    assert a["id"] == "rec:relay-serves-claimed-seat" and a["since"] == _ago(30)
    assert a["variants"] == ["serving-pane-claim-race", "serving-pane-for-claimed-seat", "serving-pane-started-for-claimed-seat"]


def test_operator_owner_is_needs_you_and_only_those_may_be_critical():
    rows = [_brief(h, "disk-full", own="operator") for h in (5, 4, 3, 2, 1, 1, 1, 1, 1, 1, 1)]
    (a,), _ = om.recurring(rows, [], NOW)
    assert a["needs_you"] and a["sev"] == 2
    rows = [_brief(h, "relay-thing", own="thinker") for h in [1] * 30]
    (b,), _ = om.recurring(rows, [], NOW)
    assert not b["needs_you"] and b["sev"] == 1
    assert om._item("x", "k", 2, "t", "d", "executor", "n", NOW, NOW)["sev"] == 1


def test_a_fix_in_flight_silences_the_problem_but_a_landed_fix_that_still_fires_does_not():
    rows = [_brief(h, "p-one", fix="L-spec-0001") for h in (3, 2, 1)]
    assert om.recurring(rows, [["L-spec-0001", "building"]], NOW)[0] == []
    (a,), _ = om.recurring(rows, [["L-spec-0001", "shipped"]], NOW)
    assert "still firing" in a["title"]


def test_blocked_counts_only_a_latest_blocked_with_no_later_start_or_ship():
    rows = [_row("build-done", 9, "L-spec-0001", st="BLOCKED"), _row("build-started", 8, "L-spec-0001"),       # re-dispatched
            _row("build-done", 9, "L-spec-0002", st="BLOCKED"),                                               # still blocked
            _row("build-done", 9, "L-spec-0003", st="BLOCKED"), _row("shipped", 5, "L-spec-0003"),           # shipped since
            _row("build-blocked", 9, "L-spec-0004", why="x")]
    states = [["L-spec-0001", "building"], ["L-spec-0002", "built"], ["L-spec-0003", "shipped"], ["L-spec-0004", "killed"]]
    hits = om.blocked_builders(om._by_spec(rows), states, NOW)
    assert [s for s, _ in hits] == ["L-spec-0002"]


def test_churn_counts_only_unfinished_specs_and_grades_in_the_last_48h():
    rows = [_row("verdict", h, "L-spec-0010", ok=False) for h in (60, 40, 30, 20)]       # 3 inside 48 h
    rows += [_row("verdict", h, "L-spec-0011", ok=False) for h in (100, 90, 80)]         # all older than 48 h
    rows += [_row("verdict", h, "L-spec-0012", ok=False) for h in (10, 9, 8)]            # shipped since
    it = om.churn_item(om._by_spec(rows), [["L-spec-0010", "reviewing"], ["L-spec-0011", "reviewing"], ["L-spec-0012", "shipped"]], NOW)
    assert it["subjects"] == ["L-spec-0010"] and "x3" not in it["detail"] and it["owner"] == "thinker"


def test_vanished_excludes_ended_spawns_and_shipped_specs():
    rows = [_row("spawn-started", 5, "L-spec-0020", sp="L-grader-1", role="grader"),                         # vanished
            _row("spawn-started", 5, "L-spec-0021", sp="L-grader-2", role="grader"), _row("spawn-stale", 4, "L-spec-0021", sp="L-grader-2"),
            _row("spawn-started", 5, "L-spec-0022", sp="L-grader-3", role="grader"), _row("shipped", 3, "L-spec-0022"),
            _row("spawn-started", 5, "L-spec-0023", sp="L-grader-4", role="grader"), _row("spawn-failed", 4, "L-spec-0023", sp="L-grader-4"),
            _row("spawn-started", 0.1, "L-spec-0024", sp="L-grader-5", role="grader")]                        # still young
    it = om.vanished_item(rows, [["L-spec-0020", "built"], ["L-spec-0022", "shipped"]], NOW)
    assert it["subjects"] == ["L-spec-0020"]


def test_gate_clean_is_not_merged_without_a_window_or_a_master_commit():
    rows = [_row("merge-gate-clean", 6, "L-spec-0030"), _row("merge-gate-clean", 6, "L-spec-0031"), _row("merge-gate-clean", 6, "L-spec-0032"),
            _row("window-verified", 5, "win-1", kept=["L-spec-0031"])]
    st = [["L-spec-0030", "reviewing"], ["L-spec-0031", "reviewing"], ["L-spec-0032", "reviewing"]]
    mi = om.merge_info(rows, om._by_spec(rows), st, {"L-spec-0032": _ago(1)})
    assert mi["L-spec-0030"]["merged"] is False
    assert mi["L-spec-0031"]["merged"] and mi["L-spec-0031"]["via"].startswith("window win-1")
    assert mi["L-spec-0032"]["via"] == "commit on master"
    rows.append(_row("merge-gate-clean", 1, "L-spec-0031"))       # re-gated after the window: the old window no longer counts
    assert om.merge_info(rows, om._by_spec(rows), st, {})["L-spec-0031"]["merged"] is False


def test_compute_drops_omens_whose_subjects_are_all_finished_and_counts_needs_you():
    rows = [_row("build-done", 9, "L-spec-0040", st="BLOCKED")] + [_brief(h, "disk-full", own="operator") for h in (3, 2, 1)]
    out = om.compute(rows, [["L-spec-0040", "killed"]], NOW)
    assert [i["id"] for i in out["items"]] == ["rec:disk-full"] and out["needs_you"] == 1 and out["health"] == 0
    assert out["v"] == 2 and out["at"] == _ago(0)

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
