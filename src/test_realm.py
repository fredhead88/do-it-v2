"""realm.py: the compact row shape, the tail offsets, and a bootstrap over a throwaway root."""
import json, os, pathlib, sys, tempfile
TMP = pathlib.Path(tempfile.mkdtemp())
(TMP / "events").mkdir(); (TMP / "seat").mkdir()
os.environ["DOIT_ROOT"] = str(TMP)
os.environ["DOIT_LIVE_REPO"] = str(pathlib.Path(__file__).resolve().parent.parent)
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
    assert not a["needs_you"] and a["owner"] == "thinker" and a["sev"] == 1    # a look brief naming the operator is NOT a request
    out = om.compute(rows, [], NOW)
    assert out["needs_you"] == 0 and out["health"] == 2
    missing, = [i for i in out["items"] if i["id"] == "owed-digest-missing"]
    assert "no root" in missing["detail"]
    assert om._item("x", "k", 2, "t", "d", "operator", "n", NOW, NOW)["sev"] == 2
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
    assert [s for s, _, _ in hits] == ["L-spec-0002"] and not hits[0][2]


def test_blocked_with_a_later_decision_amendment_rewrite_unblock_or_answer_is_redispatch_pending():
    for ty in ("decision", "spec-amended", "spec-written", "unblocked", "brief-answered"):
        rows = [_row("build-done", 9, "L-spec-0050", st="BLOCKED"), _row(ty, 5, "L-spec-0050")]
        hits = om.blocked_builders(om._by_spec(rows), [["L-spec-0050", "graded"]], NOW)
        assert [(s, p) for s, _, p in hits] == [("L-spec-0050", True)], ty
        blocked, pending = om.blocked_items(hits, NOW)
        assert blocked is None and pending["owner"] == "executor" and pending["sev"] == 0
    rows = [_row("decision", 12, "L-spec-0051"), _row("build-done", 9, "L-spec-0051", st="BLOCKED")]     # decision came BEFORE the block
    (h,) = om.blocked_builders(om._by_spec(rows), [["L-spec-0051", "graded"]], NOW)
    assert h[2] is False


def test_needs_you_comes_from_digest_and_decision_requests():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        _write_digest(root, needs_you=[dict(spec="L-spec-0060", criterion="AC1", ask="please act", since=_ago(9))])
        rows = []
        st = [["L-spec-0060", "shipped-owed-expired"]]
        out = om.compute(rows, st, NOW, root=root)
        (it,) = [i for i in out["items"] if i["needs_you"]]
        assert it["kind"] == "owedop" and it["subjects"] == ["L-spec-0060"]
        rows.append(_row("message-sent", 3, "shlomo", to="operator", why="please rule on X"))
        assert om.compute(rows, st, NOW, root=root)["needs_you"] == 2
        rows.append(_row("brief-answered", 1, "shlomo"))
        assert om.compute(rows, st, NOW, root=root)["needs_you"] == 1


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
    assert [i["id"] for i in out["items"]] == ["owed-digest-missing", "rec:disk-full"] and out["needs_you"] == 0 and out["health"] == 2
    assert out["v"] == 2 and out["at"] == _ago(0)
    assert "no root" in out["items"][0]["detail"]

def test_codex_ledger_lifecycle_and_compact():
    import realm_codex
    rows = [
        {"ts": "2026-10-12T10:00:00Z", "type": "build-started", "spawn": "L-builder-7", "subject": "L-spec-7", "backend": "codex"},
        {"ts": "2026-10-12T10:01:00Z", "type": "backend-fallback", "spawn": "L-builder-7", "why": "weekly limit"},
        {"ts": "2026-10-12T10:02:00Z", "type": "build-done", "spawn": "L-builder-7", "status": "DONE"},
    ]
    with tempfile.TemporaryDirectory() as home:
        for n, state in [(1, "live"), (2, "fallback"), (3, "ended")]:
            data = realm_codex.payload(rows[:n], home=pathlib.Path(home), processes=set(), windows={})
            assert len(data["sessions"]) == 1
            s = data["sessions"][0]
            assert (s["kind"], s["state"], s["subject"]) == ("doit-build", state, "L-spec-7")
            if n > 1:
                assert s["fell_back"] == "fell back: weekly limit"
        assert realm.compact(rows[0], "builder")["backend"] == "codex"
        assert realm.compact(rows[1], "builder")["ty"] == "backend-fallback"


def test_codex_workers_allowlist_mtime_quota_and_paths():
    import realm_codex
    now = 1791806400
    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        folder = home / ".codex/sessions/2026/10/12"
        folder.mkdir(parents=True)
        worktree = home / ".do-it/worktrees/do-it-v2/audit"
        for ident, age, cwd in [("live", 10, worktree), ("idle", 600, home / "private"), ("ended", 2000, worktree), ("no-process", 10, worktree), ("old", 24000, worktree)]:
            rows = [
                {"type": "session_meta", "payload": {"id": ident, "cwd": str(cwd), "timestamp": "2026-10-12T10:00:00Z"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "intro\n# Technical audit\nFAKE_TOKEN_SECRET_MARKER\nprivate prompt"}]}},
                {"type": "turn_context", "payload": {"turn_id": "turn-1"}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 1234}}, "rate_limits": {"secondary": {"window_minutes": 10080, "used_percent": 71, "resets_at": now + 100}, "credits": {"balance": "20.00"}}}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# PRIVATE_SECOND_HEADING"}]}},
            ]
            f = folder / ("rollout-" + ident + ".jsonl")
            f.write_text("\n".join(map(json.dumps, rows)) + '\n{"torn":')
            os.utime(f, (now - age, now - age))
        data = realm_codex.payload([], home=home, now=now, processes={"live"}, windows={str(worktree): "codex-audit"})
        by_id = {s["id"]: s for s in data["sessions"]}
        assert set(by_id) == {"live", "idle", "ended", "no-process"}
        assert [by_id[x]["state"] for x in ["live", "idle", "ended", "no-process"]] == ["live", "idle", "ended", "idle"]
        assert by_id["live"]["subject"] == "Technical audit"
        assert by_id["live"]["cwd"] == "audit" and "cwd" not in by_id["idle"]
        assert by_id["live"]["tmux"] == "codex-audit"
        assert by_id["live"]["tokens"] == 1234 and by_id["live"]["turns"] == 1
        assert data["quota"] == {"weekly_used_pct": 71, "resets_at": now + 100, "credits_balance": "20.00"}
        serialized = json.dumps(data)
        for marker in ["FAKE_TOKEN_SECRET_MARKER", "PRIVATE_SECOND_HEADING", "private prompt", str(home)]:
            assert marker not in serialized


def _page_function(source, name):
    """Brace-match a real page function, including balanced template expressions."""
    start = source.index("function " + name + "(")
    opening = source.index("{", start)
    depth = 0
    for i in range(opening, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError("unclosed function " + name)


# ---------------------------------------------------------------- merge throughput (L-spec-1149)
import contextlib
import inspect
import shlex
import subprocess
from unittest import mock

THROUGHPUT_SENTINELS = {
    "queue_depth": 7,
    "in_flight": [{"window": "win-b", "specs": ["L-spec-b"], "since": "2026-10-19T01:00:00Z"},
                  {"window": "win-a", "specs": ["L-spec-a"], "since": "2026-10-19T02:00:00Z"}],
    "median_wait_min": 31.5, "p90_wait_min": 80, "merged_per_hour": 3.2,
    "busy_hours": 13, "shipped": 29, "stale_merges": 17, "unproven_merges": 19,
    "minutes_per_spec": 23.5, "selfhosted_minutes_per_spec": 27.5, "reason": "sentinel reason",
}


@contextlib.contextmanager
def throughput_stub(value=THROUGHPUT_SENTINELS, *, mode="ok"):
    with tempfile.TemporaryDirectory(prefix="realm-throughput-") as folder, contextlib.ExitStack() as stack:
        repo = pathlib.Path(folder)
        log = repo / "calls.jsonl"
        script = repo / "stub.py"
        script.write_text("import json, os, sys, time\n"
                          f"with open({str(log)!r}, 'a') as f:\n"
                          " f.write(json.dumps(dict(argv=sys.argv[1:], now=os.environ.get('DOIT_NOW'), "
                          f"stdin=sys.stdin.read(), status={2 if mode == 'exit' else 0}))+'\\n')\n"
                          + ("time.sleep(2)\n" if mode == "timeout" else "")
                          + ("print('ledger unreadable', file=sys.stderr)\nsys.exit(2)\n" if mode == "exit" else "")
                          + f"print({('not JSON' if mode == 'invalid' else json.dumps(value))!r})\n")
        if mode != "missing":
            (repo / "doit").write_text(f'#!/bin/sh\nexec /usr/bin/python3 {shlex.quote(str(script))} "$@"\n')
            (repo / "doit").chmod(0o700)
        stack.enter_context(mock.patch.multiple(realm, LIVE_REPO=repo, sys_signals=lambda: {},
                                               maker_index=lambda *a: {}, omens_model=lambda *a: {}))
        stack.enter_context(mock.patch.object(realm.realm_codex, "payload", return_value={}))
        stack.enter_context(mock.patch.object(realm, "views_payload", return_value={}))
        stack.enter_context(mock.patch.object(realm.HUB, "throughput", None))
        yield log


def throughput_bootstrap():
    return realm.bootstrap(0, states=[], goals=[], proving=[])


def test_1149_cli_passthrough_and_bootstrap():
    with throughput_stub() as log:
        d = throughput_bootstrap()
        assert d["throughput"] == THROUGHPUT_SENTINELS and d["throughput_error"] is None
        assert _dt.datetime.fromisoformat(d["throughput_at"]).tzinfo is not None
        call, = [json.loads(line) for line in log.read_text().splitlines()]
        assert call == dict(argv=["throughput", "--json"], now=d["throughput_at"], stdin="", status=0)
        print("1149-AC1 passthrough ok")


def test_1149_unavailable_and_admission_boundary():
    for mode, value, cause in [("missing", {}, "FileNotFoundError"), ("exit", {}, "exit 2"),
                                ("invalid", {}, "JSONDecodeError"), ("ok", [], "not an object"),
                                ("timeout", {}, "TimeoutExpired")]:
        # Leave process startup ample time under load; only this case tests a timeout.
        timeout = 0.1 if mode == "timeout" else 60
        with throughput_stub(value, mode=mode) as log, mock.patch.object(realm, "THROUGHPUT_TIMEOUT", timeout):
            modules, paths = set(sys.modules), list(sys.path)
            d = throughput_bootstrap()
            assert d["throughput"] is None and cause in d["throughput_error"], d
            assert set(sys.modules) == modules and sys.path == paths
            print(f"1149-AC2 {mode if mode != 'ok' else 'list'} ok")
    with throughput_stub() as log:
        w("L-builder-1149.jsonl", [{"ts": "2026-10-19T01:00:00Z", "type": "review",
                                    "subject": "L-spec-untrusted", "decision": "approve"}])
        try:
            throughput_bootstrap()
            call = json.loads(log.read_text())
            assert call["stdin"] == "" and call["argv"] == ["throughput", "--json"]
        finally:
            (TMP / "events" / "L-builder-1149.jsonl").unlink()
        print("1149-AC2 cli-only-admission ok")


def test_1149_cache_guard_and_sampler():
    keys = {"throughput", "throughput_error", "throughput_at"}
    with throughput_stub() as log:
        p = realm.throughput_payload()
        assert set(p) == keys
        assert {k: throughput_bootstrap()[k] for k in keys} == p
        assert len(log.read_text().splitlines()) == 1
        realm.HUB.throughput = {**p, "throughput_at": (_dt.datetime.now(_dt.timezone.utc)
                                                   - _dt.timedelta(minutes=11)).isoformat()}
        throughput_bootstrap()
        assert len(log.read_text().splitlines()) == 2
        print("1149-AC3 cache ok")
        with mock.patch.object(realm.subprocess, "run", side_effect=RuntimeError("broken")):
            failed = realm.throughput_payload()
        assert failed["throughput"] is None and "broken" in failed["throughput_error"]
        assert realm.HUB.throughput == failed
        print("1149-AC3 guard ok")
    assert "sample_once()" in inspect.getsource(realm.sample_forever)
    assert 'HUB.send("throughput", throughput_payload())' in inspect.getsource(realm.sample_once)
    assert "throughput" not in inspect.getsource(realm.heartbeat_forever)
    print("1149-AC3 wiring ok")
    for failure in (False, True):
        with throughput_stub() as log, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(realm, "history_add"))
            views = {"flow": None, "flow_error": "fixture", "flow_at": "flow-instant"}
            stack.enter_context(mock.patch.object(realm, "views_payload", return_value=views))
            stack.enter_context(mock.patch.object(realm, "derive_states", return_value=[]))
            send = stack.enter_context(mock.patch.object(realm.HUB, "send"))
            if failure:
                stack.enter_context(mock.patch.object(realm.subprocess, "run", side_effect=RuntimeError("broken")))
            realm.sample_once()
            assert [call.args[0] for call in send.call_args_list] == ["history", "throughput"]
            history = send.call_args_list[0].args[1]
            assert all(history[k] == v for k, v in views.items())
            assert "history" in history and "wallclock" in history
            p = send.call_args_list[1].args[1]
            assert set(p) == keys
            assert p["throughput"] == (None if failure else THROUGHPUT_SENTINELS)
            assert bool(p["throughput_error"]) == failure
            print(f"1149-AC10 {'failure' if failure else 'success'} ok")


def test_1149_real_page_under_node():
    page = realm.PAGE.read_text()
    merge = page[page.index("// Format only CLI-provided metrics;"):page.index("function openPanel(")]
    snapshot = next(line for line in page.splitlines() if line.startswith("function applySnapshot("))
    connect = page[page.index("function connect()"):page.index("let REMOTE=")]
    seek = next(line for line in page.splitlines() if line.startswith("function seek("))
    esc = next(line for line in page.splitlines() if line.startswith("const esc ="))
    source = inspect.getsource(realm.throughput_payload) + merge
    for forbidden in ("statistics.", "median(", "percentile", "merge-gate-clean", "'review'", "'shipped'"):
        assert forbidden not in source
    print("1149-AC7 cli-owned ok")
    assert 'id="h-merge"' in page and "$('h-merge').onclick=" in page
    assert "renderMerge();" in page[page.index("$('h-flight').firstElementChild"):page.index("let last=performance.now()")]
    js = r"""
const assert=require('node:assert/strict');
let DATA={}, LIVE=true, EVENTS=[], T0=0, T1=0, lastPush='', S={};
let stream, heard=false,lastHeard=0;
const header={firstElementChild:{textContent:''},title:''};
const $=id=>{assert.equal(id,'h-merge');return header;};
const applyViews=()=>{}, rerenderPanel=()=>{}, insertEvent=()=>{}, Codex={load:()=>{},reset:()=>{}};
const reset=()=>{},process=()=>{},renderLog=()=>{},computeOmens=()=>{};
const setTimeout=()=>{},setInterval=()=>{},api=x=>x;
const listeners={};class EventSource{addEventListener(k,f){listeners[k]=f;}}
""" + esc + "\n" + merge + "\n" + snapshot + "\n" + connect + "\n" + seek + r"""
const p=SENTINELS;
function apply(d){applySnapshot({events:[],...d});return header.firstElementChild.textContent;}
function includesAll(text){for(const v of ['7','2','32 min','80 min','3.2'])assert(text.includes(v),text);}
function unknown(text){assert(text.includes('unavailable'));assert(!/0 specs|none merging|0 min|about 0|idle|>[0-9]/.test(text),text);}
apply({throughput:p,throughput_error:null});includesAll(header.firstElementChild.textContent);includesAll(mergeHtml());
console.log('1149-AC4 payload ok');
apply({throughput:null,throughput_error:'x'});unknown(header.firstElementChild.textContent);assert(mergeHtml().includes('x'));assert(header.title.endsWith(' x'));
const empty={queue_depth:null,median_wait_min:null,p90_wait_min:null,merged_per_hour:null,reason:'unknown'};
apply({throughput:empty,throughput_error:null});unknown(header.firstElementChild.textContent);unknown(mergeHtml());assert(mergeHtml().includes('unknown'));
for(const value of ['9',false,{},NaN,Infinity]){DATA.throughput={...empty,queue_depth:value};assert.equal(mergeValues().queue,'?');}
console.log('1149-AC5 unavailable ok');
apply({throughput:p});apply({throughput:null,throughput_error:'boom',throughput_at:null});unknown(header.firstElementChild.textContent);assert(header.title.endsWith(' boom'));assert(mergeHtml().includes('boom'));
console.log('1149-AC6 snapshot-null ok');
DATA={};apply({});unknown(header.firstElementChild.textContent);console.log('1149-AC6 absent ok');
apply({throughput:p});LIVE=false;seek(Date.now());renderMerge();assert.equal(header.firstElementChild.textContent,'Merge information is available in live mode');assert.equal(mergeHtml(),'<h2>Merge information is available in live mode</h2>');
console.log('1149-AC8 scrub ok');
DATA={throughput:p,live:false};LIVE=DATA.live;renderMerge();assert.equal(header.firstElementChild.textContent,'Merge information is available in live mode');assert(!mergeHtml().includes('31.5'));
console.log('1149-AC8 snapshot ok');
LIVE=true;renderMerge();includesAll(header.firstElementChild.textContent);includesAll(mergeHtml());
const attack='<img src=x onerror=alert(1)>';
for(const field of ['reason','window','error','spec']){
 const bad={...p,in_flight:[{window:field==='window'?attack:'safe',specs:[field==='spec'?attack:'safe']}]};
 if(field==='reason')bad.reason=attack;
 apply({throughput:bad,throughput_error:field==='error'?attack:null});
 assert(mergeHtml().includes('&lt;img'));assert(!mergeHtml().includes('<img'));
 assert(header.title.startsWith('Shows finished specs')); // title is assigned as plain text, never HTML
 console.log('1149-AC9 '+field+' ok');
}
connect();listeners.throughput({data:JSON.stringify({throughput:p,throughput_error:null,throughput_at:'instant'})});includesAll(header.firstElementChild.textContent);
listeners.throughput({data:JSON.stringify({throughput:null,throughput_error:'stream boom',throughput_at:'later'})});unknown(header.firstElementChild.textContent);assert.equal(DATA.throughput_at,'later');assert(mergeHtml().includes('stream boom'));
console.log('1149-AC6 sse-null ok');
"""
    js = js.replace("SENTINELS", json.dumps(THROUGHPUT_SENTINELS))
    proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    print(proc.stdout, end="")


def _write_digest(root, **kw):
    data = dict(at=NOW.isoformat(), needs_you=[], health=[])
    data.update(kw)
    path = root / "state" / "owed-digest.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_digest_needs_you_exact_ask():
    asks = ['please "act"\n<b>' + 'long ask ' * 50, "second action", "third action"]
    rows = [dict(spec=f"L-spec-{i}", criterion="AC1", ask=ask,
                 since="2026-10-15T09:00:00.123Z", route="ro-dsn-grant", owner="thinker")
            for i, ask in enumerate(asks)]
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        _write_digest(root, needs_you=rows, extra="ignored")
        out = om.compute([], [[r["spec"], st] for r, st in zip(rows, ["killed", "void", "dropped"])], NOW, root=root)
        assert len(out["items"]) == out["needs_you"] == 3 and out["health"] == 0
        for row in rows:
            it, = [i for i in out["items"] if i["subjects"] == [row["spec"]]]
            assert it["detail"] == row["ask"] and it["since"] == row["since"]
            assert it["needs_you"] is True and it["owner"] == "operator" and it["kind"] == "owedop"
        assert len({i["id"] for i in out["items"]}) == 3
    print("owed digest needs_you exact ask ok")


def test_digest_health_rows():
    kinds = ["quarantined", "quarantined", "failed", "expired", "sweep-failing", "future-kind"]
    rows = [dict(kind=k, spec=f"L-spec-{i}", criterion=f"AC{i}", since=_ago(3),
                 **({"text": f"text {i}"} if i < 4 else {"reason": "reason fallback"} if i == 4 else {}))
            for i, k in enumerate(kinds)]
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        _write_digest(root, health=rows)
        states = [[r["spec"], "killed" if i == 0 else "shipped-owed-expired"] for i, r in enumerate(rows)]
        first = om.compute([], [], NOW, root=root)
        second = om.compute([], states, NOW, root=root)
        assert first == second
        assert first["needs_you"] == 0 and first["health"] == 5 and len(first["items"]) == 5
        for kind in set(kinds):
            it, = [i for i in first["items"] if i["id"] == "owedhealth:" + kind]
            grouped = [r for r in rows if r["kind"] == kind]
            assert it["sev"] == 1 and it["owner"] == "thinker" and it["needs_you"] is False
            assert str(len(grouped)) in it["title"]
            for row in grouped:
                for text in (row["spec"], row["criterion"], row.get("text", row.get("reason", kind))):
                    assert text in it["detail"]
        # Groups are not capped at the legacy five rows.
        many = [dict(rows[0], spec=f"L-spec-many-{i}") for i in range(20)]
        _write_digest(root, health=many)
        it, = om.compute([], [], NOW, root=root)["items"]
        assert "20" in it["title"] and all(r["spec"] in it["detail"] for r in many)
        _write_digest(root, health=rows, at=_ago(3))
        stale = om.compute([], states, NOW, root=root)
        assert stale["health"] == 6
        assert {i["id"] for i in stale["items"]} == {i["id"] for i in first["items"]} | {"owed-digest-stale"}
    print("owed digest health rows ok")


def test_digest_missing_and_stale():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        cases = ["missing", "unreadable", "empty", "json", "list", "needs_you", "health",
                 "at missing", "at invalid", "at null", "future", "stale", "no root"]
        for case in cases:
            path = _write_digest(root)
            if case == "missing":
                path.unlink()
            elif case == "unreadable":
                path.chmod(0o000)
                if os.geteuid() == 0:
                    path.chmod(0o600)
                    print("owed digest missing and stale unreadable SKIPPED: process is root")
                    continue
            elif case in ("empty", "json", "list"):
                path.write_text({"empty": "", "json": "{bad", "list": "[]"}[case])
            elif case in ("needs_you", "health"):
                _write_digest(root, **{case: {}})
            elif case.startswith("at "):
                data = json.loads(path.read_text())
                if case == "at missing":
                    del data["at"]
                else:
                    data["at"] = None if case == "at null" else "unparseable"
                path.write_text(json.dumps(data))
            elif case == "future":
                _write_digest(root, at=(NOW + _dt.timedelta(minutes=10)).isoformat())
            elif case == "stale":
                _write_digest(root, at=(NOW - _dt.timedelta(minutes=121)).isoformat())
            try:
                it, = om.compute([], [], NOW, root=None if case == "no root" else root)["items"]
                assert it["id"] == ("owed-digest-stale" if case == "stale" else "owed-digest-missing"), case
                assert it["kind"] == "owedhealth" and it["owner"] == "executor" and it["sev"] == 1
                assert it["needs_you"] is False and "owed-digest.json" in it["detail"]
                assert ("no root" if case == "no root" else str(path)) in it["detail"]
                assert ": " in it["detail"] and it["detail"].split(": ", 1)[1]
                if case == "stale":
                    assert "stale" in it["title"].lower()
            finally:
                if path.exists():
                    path.chmod(0o600)
            print(f"owed digest missing and stale {case} ok")
        for minutes in (119, 120, -5):
            _write_digest(root, at=(NOW - _dt.timedelta(minutes=minutes)).isoformat())
            assert om.compute([], [], NOW, root=root)["items"] == []
        _write_digest(root, at=_ago(3), needs_you=[dict(spec="L-spec-1", ask="still visible", since=_ago(4))])
        out = om.compute([], [["L-spec-1", "shipped"]], NOW, root=root)
        assert out["needs_you"] == 1 and out["health"] == 1
        assert any(i["detail"] == "still visible" for i in out["items"])


def test_no_owed_derivation_in_realm():
    import re
    assert not hasattr(om, "operator_action_owed")
    assert not any(t.startswith("owed-") for t in om.WANT)
    event = dict(type="owed-unobservable", ts=_ago(1), subject="L-spec-1", capability="operator-action")
    assert om.reduce_event(event) is None
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "events").mkdir()
        (root / "events" / "ledger.jsonl").write_text(json.dumps(event) + "\n")
        rows = om.scan(root / "events", {})
        out = om.compute(rows, [["L-spec-1", "shipped"]], NOW, root=root)
        assert [i["id"] for i in out["items"]] == ["owed-digest-missing"]
    allow = {
        "realm_omens.py": "followed windowed owed- owed-sweep owed-sweeper owedop owedhealth owed-digest owed-digest-missing owed-digest-stale owed_digest_items",
        "realm.py": "owed owed-ac owed-met owed_checks owed_close_rate owed_overdue owed-sweeper owed-digest",
        "realm.html": "followed windowed unwindowed shadowed h-owed owed owed-ac owed-met owed-check owed-checks owed_checks owed_close_rate owed_overdue owed-due owed-evidence owed-expired owed-waiting owed-sweeper owedop owedhealth shipped-owed-due",
    }
    for filename, allowed in allow.items():
        source = realm.PAGE.with_name(filename).read_text()
        tokens = set(re.findall(r"[a-z0-9_-]*owed[a-z0-9_-]*", source))
        assert tokens <= set(allowed.split()), (filename, tokens - set(allowed.split()))
        forbidden = {"operator_action_owed", "owed-unobservable", "owed-failed", "owed-waived"}
        if filename == "realm_omens.py":
            forbidden |= {"owed-ac", "owed-met"}
        assert not tokens & forbidden
    print("no owed derivation in realm ok")


def test_omens_model_rereads_digest():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "events").mkdir()
        rows = [dict(spec=f"L-spec-{i}", ask=f"act {i}", since=_ago(3)) for i in range(3)]
        _write_digest(root, needs_you=rows)
        with mock.patch.object(realm, "ROOT", root), mock.patch.dict(realm._OM, master=({}, realm.time.monotonic())):
            first = realm.omens_model([], NOW)
            generation, cached = realm._OM["generation"], realm._OM["rows"]
            _write_digest(root, at=_ago(0.01), needs_you=rows[1:])
            second = realm.omens_model([], NOW)
            assert first["needs_you"] == 3 and second["needs_you"] == 2
            assert {i["detail"] for i in second["items"]} == {"act 1", "act 2"}
            assert realm._OM["generation"] == generation and realm._OM["rows"] is cached
    assert 'HUB.send("omens", omens_model(HUB.states))' in inspect.getsource(realm.heartbeat_forever)
    handler = inspect.getsource(realm.Handler.do_GET)
    assert 'u.path == "/api/omens"' in handler and 'self._json(omens_model(HUB.states))' in handler
    print("omens_model rereads the digest ok")


def test_owed_omens_render_by_section():
    page = realm.PAGE.read_text()
    names = ("computeOmens", "stateMap", "omensTopHtml", "omenFoot")
    constants = ("SERVER_KINDS", "NO_SELFCHECK", "FINISHED_RE", "DEFAULT_OWNER", "OMEN_HELP", "NEXT_ACTION", "S", "esc", "toMs")
    declarations = []
    for name in constants:
        start = page.index("const " + name + (" =" if name in ("esc", "toMs", "S") else "="))
        end = page.index("\n};", start) + 3 if name in ("OMEN_HELP", "NEXT_ACTION") else page.index("\n", start)
        declarations.append(page[start:end])
    script = "\n".join(declarations + [_page_function(page, n) for n in names])
    script += r'''
const assert=require('node:assert/strict');
let DATA={states:[['L-spec-1','shipped'],['L-spec-2','shipped']]},LIVE=true,REMOTE=false,stream=null,lastHeard=0;
const CAP={},execBusy=()=>false,lastOf=()=>null,knownBug=()=>null,specCharter=()=>null,charterRows=()=>[];
const laneSplit=()=>({close:[]}),renderOmens=()=>{},fixFor=()=>null,ownerFor=()=>null,sinceFor=a=>a.srvSince;
const loopState=()=>({badge:{cls:'open',text:'open'}}),omenWhen=()=>'',hhmm=()=>'';
const isOperator=o=>o==='operator'||o==='you';
const ask='please "act"\n<b>'+('long action '.repeat(40));
const OM=()=>({items:[
 {id:'owedop:1',kind:'owedop',sev:1,owner:'operator',title:'operator action',detail:ask,subjects:['L-spec-1'],since:'2026-10-15T10:00:00Z'},
 {id:'owedhealth:failed',kind:'owedhealth',sev:1,owner:'thinker',title:'1 failed check',detail:'health detail',subjects:['L-spec-2'],since:'2026-10-15T10:00:00Z'}]});
S.now=Date.parse('2026-10-15T12:00:00Z');computeOmens();
assert.equal(S.alarms.length,2);
assert.equal(S.alarms.find(a=>a.kind==='owedop').needs,true);
assert.equal(S.alarms.find(a=>a.kind==='owedhealth').needs,false);
const html=omensTopHtml(),need=html.split('<h4>NEEDS YOU (1)</h4>')[1].split('<h4>SYSTEM HEALTH (1)</h4>')[0];
const health=html.split('<h4>SYSTEM HEALTH (1)</h4>')[1];
assert(need.includes(esc(ask)));assert(!need.includes('<b>'+('long action')));assert(!need.includes(ask));
assert(health.includes('health detail'));assert(!need.includes('health detail'));assert(!health.includes(esc(ask)));
for(const kind of ['owedop','owedhealth']){
 assert(SERVER_KINDS.has(kind));assert(NO_SELFCHECK.has(kind));assert(DEFAULT_OWNER[kind]);assert(OMEN_HELP[kind]);
 assert(!JSON.stringify(OMEN_HELP[kind]).includes('expired production checks'));
}
assert(NEXT_ACTION.owedop);assert(!NEXT_ACTION.owedop.toString().includes('expired production checks'));
assert(!DEFAULT_OWNER.owedop.includes('expired production checks'));
console.log('owed omens render by section ok');
'''
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    print(proc.stdout, end="")


def test_operator_header_rounding_units_zero_and_tooltips():
    from html.parser import HTMLParser

    class HeaderTiles(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tiles = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "div" and "stat" in attrs.get("class", "").split():
                self.tiles.append(attrs)

    page = realm.PAGE.read_text()
    parser = HeaderTiles()
    parser.feed(page)
    assert len(parser.tiles) == 16
    for tile in parser.tiles:
        assert tile.get("title", "").endswith("."), tile
    tiles = {tile["id"]: tile for tile in parser.tiles if "id" in tile}
    assert "no builder has started" in tiles["h-shelf"]["title"]
    assert '<span>ready, waiting for a builder</span>' in page
    assert 'sh.firstElementChild.textContent=shelfText(S.shelf.size,oldest)' in page
    helpers = page[page.index("// Format only CLI-provided metrics;"):page.index("function openPanel(")]
    mobile = page[page.index("const M_TILES="):page.index("function mOmensHtml(")]
    esc = next(line for line in page.splitlines() if line.startswith("const esc ="))
    script = r"""
const assert=require('node:assert/strict');
let DATA={},LIVE=true,S={alarms:[]};
const cells=TILES;
for(const cell of Object.values(cells))Object.assign(cell,{firstElementChild:{textContent:''},className:'stat click'});
cells.stuck={classList:{contains:()=>true},innerHTML:'No work stuck'};
const $=id=>cells[id],rerenderPanel=()=>{};
""" + helpers + "\n" + esc + "\n" + mobile + r"""
for(const [value,want] of [[0,'0 min'],[0.49,'0 min'],[0.5,'1 min'],[31.5,'32 min'],[89,'89 min'],[89.99,'90 min'],[90,'1.5 h'],[120,'2 h'],[502.05,'8.4 h'],[2003.0333333333,'33.4 h'],[2880,'48 h'],[2880.01,'2 days'],[3024,'2.1 days'],[4320,'3 days']]){
 assert.equal(humanWait(value),want);
}
for(const v of [null,undefined,'90',false,{},NaN,Infinity,-1])assert.equal(humanWait(v),'unavailable');
const minute=60000;
for(const [n,age,want] of [[0,0,'0 specs'],[1,0,'1 spec · oldest waiting 0 m'],[7,563*minute,'7 specs · oldest waiting 9 h 23 m'],[1,59.6*minute,'1 spec · oldest waiting 1 h'],[2,-minute,'2 specs · oldest waiting 0 m'],[2,2880*minute,'2 specs · oldest waiting 48 h'],[2,2963*minute,'2 specs · oldest waiting 2 d 1 h 23 m']])assert.equal(shelfText(n,age),want);
DATA.throughput={queue_depth:84,in_flight:[],median_wait_min:502.05,p90_wait_min:2003.0333333333,merged_per_hour:1.56};
const expected='84 specs waiting to merge · none merging right now · typical wait 8.4 h (slowest 1 in 10: 33.4 h) · about 1.6 merged per hour';
renderMerge();assert.equal(cells['h-merge'].firstElementChild.textContent,expected);
const panel=mergeHtml();for(const v of ['8.4 h','33.4 h','1.6'])assert(panel.includes(v));
for(const raw of ['502.05','2003.0333333333','1.56']){assert(!mergeText().includes(raw));assert(!panel.includes(raw));}
console.log('HEADER RENDER: MERGE: '+mergeText());
console.log('HEADER RENDER: READY, WAITING FOR A BUILDER: '+shelfText(7,563*minute));
const phone=mNowHtml();assert(phone.includes(expected));assert(phone.includes('ready, waiting for a builder'));assert(!phone.includes('<span>shelf</span>'));
for(const title of [cells['h-merge'].title,cells['h-shelf'].title])assert(phone.includes(title));
DATA.throughput={queue_depth:0,in_flight:[],median_wait_min:0,p90_wait_min:0,merged_per_hour:0};
assert.equal(mergeText(),'0 specs waiting to merge · none merging right now · typical wait 0 min (slowest 1 in 10: 0 min) · about 0 merged per hour');
DATA.throughput={queue_depth:1,in_flight:[{}],median_wait_min:90,p90_wait_min:3000,merged_per_hour:2};
assert.equal(mergeText(),'1 spec waiting to merge · 1 batch merging right now · typical wait 1.5 h (slowest 1 in 10: 2.1 days) · about 2 merged per hour');
for(const v of [-1,1.25,NaN,Infinity,'84',null]){DATA.throughput.queue_depth=v;assert.equal(mergeValues().queue,'?');}
for(const v of [-1,NaN,Infinity,'1.56',null]){DATA.throughput.merged_per_hour=v;assert.equal(mergeValues().rate,'unavailable');}
DATA.throughput=null;renderMerge();assert(mergeText().includes('merge queue unavailable'));assert(cells['h-merge'].title.startsWith('Shows finished specs'));
LIVE=false;renderMerge();assert.equal(mergeText(),'Merge information is available in live mode');assert(cells['h-merge'].title.startsWith('Shows finished specs'));
console.log('operator header: duration boundaries, rounding, singulars, zeros, unknowns, panel, phone, and tooltips ok');
"""
    script = script.replace("TILES", json.dumps(tiles), 1)
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout, end="")


# ---------------------------------------------------------------- lock-free Realm reader
@contextlib.contextmanager
def lockfree_fixture():
    import realm_ledger
    with tempfile.TemporaryDirectory(prefix="realm-lockfree-") as folder, contextlib.ExitStack() as stack:
        root = pathlib.Path(folder)
        events = root / 'events'; events.mkdir()
        file = events / 'L-executor-fixture.jsonl'
        file.write_text(json.dumps(dict(type='tick', ts=NOW.isoformat(), subject='fixture'))+'\n')
        ledger = realm_ledger.Ledger()
        stack.enter_context(mock.patch.dict(os.environ, DOIT_ROOT=str(root), DOIT_READ_CACHE='1',
                            DOIT_LIVE_REPO=str(pathlib.Path(realm.__file__).parent.parent)))
        stack.enter_context(mock.patch.multiple(realm, ROOT=root, EVENTS=events, SEAT=root/'seat',
                                              LEDGER=ledger, HUB=realm.Hub()))
        stack.enter_context(mock.patch.dict(realm._OM, generation=None, rows=[], master=({}, realm.time.monotonic())))
        stack.enter_context(mock.patch.dict(realm._GLANCE, key=None))
        stack.enter_context(mock.patch.object(realm.realm_codex, 'process_sessions', return_value=set()))
        stack.enter_context(mock.patch.object(realm.realm_codex, 'tmux_windows', return_value={}))
        stack.enter_context(mock.patch.object(realm.realm_omens, 'master_merges', return_value={}))
        try:
            yield root, events, file, ledger
        finally:
            if ledger.watch is not None: ledger.watch.close()


def test_realm_never_opens_ledger_lock():
    with lockfree_fixture() as (root, events, file, ledger):
        def exercise():
            with realm.ledger_snapshot() as (_, raw, folded):
                assert raw and len(folded) == 4
            assert realm.read_all()[1]
            hash(realm.ledger_generation())
            assert realm.derive_states() is not None
            assert realm.derive_goals() is not None
            assert realm.derive_proving() is not None
            assert realm.omens_model([]) is not None
            assert 'sessions' in realm.codex_payload()
            assert realm.views_payload(NOW)['flow_at']
            assert realm.glance_payload(NOW).get('error') is None
        exercise()
        with file.open('a') as f: f.write(json.dumps(dict(type='tick', ts=NOW.isoformat()))+'\n')
        exercise()
        assert not list((root/'cache').glob('ledger-*'))
        # Use the exact live module bundle/root exercised above for the control.
        with ledger.live(realm.live_src(), root) as fold:
            result = fold.read_events()
            assert len(result) == 2
        assert list((root/'cache').glob('ledger-*.lock'))


def test_subprocess_sites_enumerated_and_cached_child_reads():
    import ast
    import collections
    import subprocess
    src = pathlib.Path(realm.__file__).parent
    sites = collections.Counter()
    methods = {'run', 'Popen', 'call', 'check_output', 'check_call'}
    class Sites(ast.NodeVisitor):
        def __init__(self, filename): self.filename, self.function = filename, '<module>'
        def visit_FunctionDef(self, node):
            previous = self.function; self.function = node.name
            self.generic_visit(node); self.function = previous
        visit_AsyncFunctionDef = visit_FunctionDef
        def visit_Attribute(self, node):
            if isinstance(node.value, ast.Name) and node.value.id == 'subprocess' and node.attr in methods:
                sites[self.filename, self.function] += 1
            self.generic_visit(node)
    for path in sorted(src.glob('realm*.py')):
        Sites(path.name).visit(ast.parse(path.read_text()))
    # Every subprocess attribute, including defaults, has a classified occurrence.
    table = {
        ('realm.py', 'ci_status'): (1, 'no ledger'),
        ('realm.py', 'fires_register'): (1, 'ledger'),
        ('realm.py', 'pane_status'): (2, 'no ledger'),
        ('realm.py', 'quota_status'): (2, 'no ledger'),
        ('realm.py', 'sys_signals'): (2, 'no ledger'),
        ('realm.py', 'tick_omens'): (1, 'ledger'),
        ('realm.py', 'throughput_payload'): (1, 'ledger'),
        ('realm.py', '_view_payload'): (1, 'ledger'),
        ('realm.py', 'derive_dossier'): (1, 'ledger'),
        ('realm.py', 'ask_thinker'): (1, 'append'),
        ('realm.py', 'main'): (1, 'no ledger'),
        ('realm_codex.py', 'tmux_windows'): (1, 'no ledger'),
        ('realm_omens.py', 'master_merges'): (1, 'no ledger'),
    }
    assert sites == collections.Counter({key: value[0] for key, value in table.items()}), (sites, table)
    with lockfree_fixture() as (root, events, file, ledger):
        for name in ['omens.py', 'fires.py']: (root/name).touch()
        with mock.patch.dict(realm._FIRES, at=0, v=None), mock.patch.object(realm, 'BRIEF', src/'brief.py'), \
             mock.patch.object(realm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '[]', '')) as run:
            for call in [realm.tick_omens, realm.fires_register, realm.throughput_payload,
                         lambda: realm.derive_dossier('L-spec-1')]:
                call()
                env = run.call_args.kwargs['env']
                assert env['DOIT_READ_CACHE'] == '1' and env['DOIT_ROOT'] == str(root)
                assert env['DOIT_LEDGER_LOCK_WAIT_S'] == os.environ.get('DOIT_LEDGER_LOCK_WAIT_S', '30')
                if run.call_args.args[0][1:3] == ['throughput', '--json']: assert env['DOIT_NOW']
            realm.ask_thinker('stuck', [])
            assert run.call_args.kwargs['env']['DOIT_READ_CACHE'] == '1'
        expected_count = 1
        for name in ['omens.py', 'fires.py']:
            script = root/name
            marker = ("print('info|ledger-read|'+str(n))" if name == 'omens.py' else
                      "print(json.dumps([{'class':'ledger-read','n':n}]))")
            script.write_text('import sys, json\nsys.path.insert(0, '+repr(str(src))+')\n'
                              'import fold\nevents=fold.read_events()\nn=len(events)\n'+marker+'\n')
            if name == 'omens.py':
                value = realm.tick_omens()
                assert value and any(r['name'] == 'ledger-read' and r['detail'] == str(expected_count)
                                     for r in value['lines']), value
            else:
                with mock.patch.dict(realm._FIRES, at=0, v=None): value = realm.fires_register()
                assert value and any(r['cls'] == 'ledger-read' and r['n'] == expected_count for r in value), value
            assert list((root/'cache').glob('ledger-*.lock'))
            assert list((root/'cache').glob('ledger-*.sqlite3'))
            with tempfile.TemporaryDirectory(prefix='realm-child-control-') as folder:
                control = pathlib.Path(folder); (control/'events').mkdir()
                (control/'events'/file.name).write_bytes(file.read_bytes())
                args = [sys.executable, str(script)] + (['--json'] if name == 'fires.py' else [])
                result = subprocess.run(args, capture_output=True, text=True, timeout=30,
                         env={**os.environ, 'DOIT_ROOT': str(control), 'DOIT_READ_CACHE':'1'})
                assert result.returncode == 0, result.stderr
                if name == 'omens.py': assert result.stdout.strip() == 'info|ledger-read|1'
                else: assert json.loads(result.stdout) == [{'class':'ledger-read', 'n':expected_count}]
                assert expected_count > 0 and list((control/'cache').glob('ledger-*.lock'))


def generation_layout_uses(source):
    """Catch consumers treating the snapshot token as metadata, including local aliases."""
    import ast
    tree = ast.parse(source)
    aliases = set()
    def token(node):
        return (isinstance(node, ast.Attribute) and node.attr == 'generation'
                and isinstance(node.value, ast.Name) and node.value.id == 'LEDGER'
                or isinstance(node, ast.Name) and node.id in aliases)
    # Resolve simple aliases to a fixed point, including assignments chained through aliases.
    for _ in range(len(list(ast.walk(tree)))):
        before = set(aliases)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and token(node.value):
                aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
            if isinstance(node, ast.AnnAssign) and node.value and token(node.value) and isinstance(node.target, ast.Name):
                aliases.add(node.target.id)
        if aliases == before: break
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and token(node.value): bad.append((node.lineno, ast.unparse(node)))
        if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and token(node.iter):
            bad.append((getattr(node, 'lineno', 0), ast.unparse(node.iter)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {'iter','list','tuple'}:
            if any(token(arg) for arg in node.args): bad.append((node.lineno, ast.unparse(node)))
        if isinstance(node, ast.Assign) and token(node.value):
            if any(isinstance(t, (ast.Tuple, ast.List)) for t in node.targets): bad.append((node.lineno, ast.unparse(node)))
    return bad


def test_live_rows_between_refolds_and_omens_rows_reused():
    import ast
    src = pathlib.Path(realm.__file__).parent
    paths = [p for p in sorted(src.glob('realm*.py')) if p.name != 'realm_ledger.py']
    violations = {str(p): generation_layout_uses(p.read_text()) for p in paths}
    assert not any(violations.values()), (list(map(str, paths)), violations)
    for expression in ['x=LEDGER.generation[0]', 'for x in LEDGER.generation: pass',
                       'x=[a for a in LEDGER.generation]', 'x={a for a in LEDGER.generation}',
                       'x={a:a for a in LEDGER.generation}', 'x=(a for a in LEDGER.generation)',
                       'x=iter(LEDGER.generation)', 'x=list(LEDGER.generation)', 'x=tuple(LEDGER.generation)',
                       'a,b=LEDGER.generation', '[a,b]=LEDGER.generation']:
        assert generation_layout_uses(expression), expression
        assert generation_layout_uses('alias=LEDGER.generation\n'+expression.replace('LEDGER.generation', 'alias')), expression
    assert not generation_layout_uses('x=hash(LEDGER.generation)\ny=(LEDGER.generation, 1)\nz=x==LEDGER.generation')
    with lockfree_fixture() as (root, events, file, ledger):
        # A controlled clock keeps all tail work inside the bound.
        with mock.patch.object(realm.dt, 'datetime', wraps=_dt.datetime) as clock:
            clock.now.return_value = NOW
            rows, raw, offsets = realm.read_all()
            folds, generation = ledger.folds, ledger.generation
            seq = realm.HUB.seq
            class PollDone(BaseException): pass
            polls = 0
            def poll(_):
                nonlocal polls
                polls += 1
                if polls > 1: raise PollDone()
                with file.open('a') as f:
                    f.write(json.dumps(dict(type='tick', subject='arrived', ts=(NOW+_dt.timedelta(seconds=1)).isoformat()))+'\n')
            with mock.patch.object(realm.time, 'sleep', side_effect=poll):
                try: realm.tail_forever()
                except PollDone: pass
            assert any(row['s'] == 'arrived' for row in realm.since(seq)['ledger'])
            assert ledger.folds == folds and ledger.generation == generation
            rows, raw, offsets = realm.read_all()
            # The held snapshot's offsets must describe its events, not the pending read.
            for name, offset in offsets.items():
                lines = (events/name).read_bytes()[:offset].decode().splitlines()
                expected = [json.loads(line) for line in lines if line.strip()]
                actual = [{k:v for k,v in e.items() if k not in {'actor','_src'}} for e in raw if e['_src'].startswith(name+':')]
                assert actual == expected
            compact = [c for e in raw for c in [realm.compact(e, e['actor'])] if c]
            assert rows == sorted(compact, key=lambda r: r['t'])
            with mock.patch.object(realm.realm_omens, 'reduce_event', wraps=realm.realm_omens.reduce_event) as reduce:
                realm.omens_model([], NOW)
                n = len(raw); assert reduce.call_count == n
                clock.now.return_value = NOW+_dt.timedelta(seconds=60)
                realm.omens_model([], clock.now.return_value)  # publishes the pending append
                assert reduce.call_count == n+2
                clock.now.return_value = NOW+_dt.timedelta(seconds=120)
                realm.omens_model([], clock.now.return_value)  # clock-only fold retains reduced rows
                assert reduce.call_count == n+2
                with file.open('a') as f: f.write(json.dumps(dict(type='tick', ts=NOW.isoformat()))+'\n')
                clock.now.return_value = NOW+_dt.timedelta(seconds=130)
                realm.omens_model([], clock.now.return_value)
                assert reduce.call_count == n+2+3


def test_ac1_spec_text_returns_criteria():
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "content").mkdir()
        long = "A" * 260 + "."
        (root / "content/L-spec-9001.md").write_text(
            "# Test spec\n## Requirements\n"
            "AC1 [backend]: First sentence. Second sentence omitted.\n"
            "check: scratch\nreview_path: internal\n\n"
            f"AC2 [ui]: {long}\ncheck: scratch\nreview_path: internal\n\n"
            "AC3 [backend]: Third criterion! Another sentence.\ncheck: scratch\nreview_path: internal\n")
        with patch.object(realm, "ROOT", root), patch.object(realm, "_SPEC_CACHE", {}):
            result = realm.spec_text("L-spec-9001")
            assert result["criteria"] == [dict(id="AC1", text="First sentence."),
                                          dict(id="AC2", text="A" * 240),
                                          dict(id="AC3", text="Third criterion!")]
            assert result["criteria_error"] is None
            assert realm.spec_criteria("## Acceptance criteria\n- **Legacy label**: Kept. Not this.\n") == ([dict(id="Legacy label", text="Kept.")], None)


def test_ac2_every_real_spec_has_criteria_or_a_named_error():
    from unittest.mock import patch
    root = pathlib.Path("/home/albert/.do-it")
    files = sorted((root / "content").glob("L-spec-*.md"))
    assert files, "real content directory has no specs"
    good = errors = 0
    with patch.object(realm, "ROOT", root), patch.object(realm, "_SPEC_CACHE", {}):
        for file in files:
            result = realm.spec_text(file.stem)
            assert result is not None, file.name
            if result["criteria"]:
                assert result["criteria_error"] is None, file.name
                good += 1
            else:
                assert isinstance(result["criteria_error"], str) and result["criteria_error"], file.name
                errors += 1
    assert good + errors == len(files)
    print(f"AC2 real content: files={len(files)} criteria={good} named_errors={errors}")


def test_ac3_criteria_error_cache_and_route():
    import hashlib, http.server, threading, urllib.request
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "content").mkdir()
        f = root / "content/L-spec-9001.md"
        with patch.object(realm, "ROOT", root), patch.object(realm, "_SPEC_CACHE", {}):
            f.write_text("# Missing\n## Goal\nSomething.\n")
            result = realm.spec_text("L-spec-9001")
            assert result["criteria"] == [] and "no criteria" in result["criteria_error"]
            with patch.object(pathlib.Path, "read_text", side_effect=AssertionError("cache re-read")):
                assert realm.spec_text("L-spec-9001") is result
            f.write_text("# Empty\n## Acceptance criteria\nNothing parseable here.\n")
            os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 1_000_000))
            result = realm.spec_text("L-spec-9001")
            assert result["criteria"] == [] and "unparseable" in result["criteria_error"]
            f.write_text("# Rewritten\n## Requirements\nAC1 [backend]: New criterion.\n")
            os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 2_000_000))
            assert realm.spec_text("L-spec-9001")["criteria"] == [dict(id="AC1", text="New criterion.")]
            realm._SPEC_CACHE.clear()
            with patch.object(pathlib.Path, "read_text", side_effect=OSError("unreadable")):
                assert realm.spec_text("L-spec-9001") is None
            class Handler(realm.Handler):
                def _authorized(self, qs): return True
                def log_message(self, *args): pass
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}/api/spec?id="
                for sid in ["L-spec-9001", "L-spec-9999", "../escape", "L-spec-../escape"]:
                    with urllib.request.urlopen(base + sid) as response:
                        body = response.read()
                        payload = json.loads(body)
                        if sid == "L-spec-9001":
                            assert payload["criteria"] and payload["criteria_error"] is None
                        else:
                            assert payload == {"error": "no file"}
                        print(json.dumps(dict(url=response.url, status=response.status,
                                              body_sha256=hashlib.sha256(body).hexdigest(),
                                              body_excerpt=body.decode())))
                with patch.object(pathlib.Path, "is_file", side_effect=AssertionError("invalid id touched disk")):
                    assert realm.spec_text("../escape") is None
                    assert realm.spec_text("L-spec-7/escape") is None
            finally:
                server.shutdown(); server.server_close(); thread.join()


def _spec_panel_node(assertions):
    import subprocess
    page = realm.PAGE.read_text()
    # Load the actual page entry points and helpers, without the canvas boot.
    helpers = page[page.index("function openActor("):page.index("function narrate(")]
    helpers += "\n" + page[page.index("const specInfo="):page.index("function chronicleHtml(")]
    helpers += "\n" + page[page.index("const BUCKET="):page.index("function charterRows(")]
    esc = next(line for line in page.splitlines() if line.startswith("const esc ="))
    codex = (realm.HERE / "realm_codex.js").read_text()
    inspect = codex[codex.index("  function sessionRows("):codex.index("  function target(")]
    script = r"""
const assert=require('node:assert/strict');
let DATA={states:[['L-spec-7','building']],specs:{'L-spec-7':{title:'Shared title',criteria:[{id:'AC1',text:'Safe <img>.'}],criteria_error:null,charter:'L-charter-0064'}},titles:{},spec_charter:{},codex:{sessions:[]}},stream=true;
let S={steps:[{item:'L-spec-7',stage:'building',state:'action',reason:'ready <now>'}],subjectEvents:new Map(),actors:new Map(),esc:new Map(),decided:new Map(),now:1000};
let panel='',panelView=null,fetches=0,links=[];
const window={},document={querySelectorAll:selector=>selector==='a.link[data-subj]'?links:[]},$=()=>({scrollTop:0,innerHTML:''});
const openPanel=(html,view)=>{panel=html;panelView=view;},bindPix=()=>{};
const fmt=x=>String(x),fmtDur=x=>String(x),charterOfSubject=s=>specCharter(s),intentLine=()=>'',spritePix=()=>'',escOwner=()=>'',fmtAge=()=>'';
const narrate=e=>({cls:'',text:e.ty}),toMs=x=>Date.parse(x)||0;
const PACKET={},ROLE_NAME={builder:'Builder'},MODEL={builder:'Claude'},CAP={builder:30},CX={builder:{quest:'Build'}};
const fetch=async()=>{fetches++;return {ok:false};};
const builds=new Map(),workers=new Map();let source={sessions:[],quota:{weekly_used_pct:71}};
""" + esc + "\n" + helpers + "\n" + inspect + "\nconst Codex={sessionExtra};\n" + assertions
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_ac4_spec_detail_html_order_and_states():
    _spec_panel_node(r"""
S.subjectEvents.set('L-spec-7',Array.from({length:41},(_,i)=>({ms:i,ty:'event-'+i})));
const html=specDetailHtml('L-spec-7',{title:'CODEX SESSION',rows:[['State','live<script>']]});
let last=-1;for(const label of ['L-spec-7','Shared title','STAGE','NEXT STEP','CRITERIA','CHARTER','LATEST EVENTS','CODEX SESSION']){const i=html.indexOf(label);assert(i>last,label);last=i;}
for(const text of ['building','action','ready &lt;now&gt;','Safe &lt;img&gt;.','live&lt;script&gt;'])assert(html.includes(text),text);
assert.equal((html.match(/<li class=/g)||[]).length,40);assert(!html.includes('event-0<'));
S.steps=[];assert(specDetailHtml('L-spec-7').includes('unknown'));
DATA.specs['L-spec-7'].criteria=[];DATA.specs['L-spec-7'].criteria_error='unparseable <input>';
const bad=specDetailHtml('L-spec-7');assert(bad.includes('criteria unreadable'));assert(!bad.includes('<ul class="criteria">'));assert(!bad.includes('no criteria listed'));
DATA.specs['L-spec-7'].criteria_error=null;assert(specDetailHtml('L-spec-7').includes('no criteria listed'));
""")


def test_ac6_session_section_only_for_spec_subjects():
    _spec_panel_node(r"""
(async()=>{
DATA.codex.sessions=[{id:'old',kind:'doit-build',subject:'L-spec-7',last:'2026-10-25T01:00:00Z',tmux:'old-window'},{id:'new',kind:'doit-build',subject:'L-spec-7',last:'2026-10-25T02:00:00Z',tmux:'new-window'}];
openSubject('L-spec-7');assert(panel.includes('CODEX SESSION'));assert(panel.includes('new-window'));assert(!panel.includes('old-window'));
for(const entry of ['lane marker','stuck row','timeline link']){
 const link={dataset:{subj:'L-spec-7'}};links=[link];bindLinks();panel='';link.onclick();assert(panel.includes('CODEX SESSION'),entry);assert(panel.includes('new-window'),entry);
}
DATA.codex.sessions=[{kind:'worker',subject:'L-spec-7'}];openSubject('L-spec-7');assert(!panel.includes('CODEX SESSION'));
for(const subject of ['L-charter-0064','L-spec-7-extra','x-L-spec-7','a-slug']){
 builds.set('test',{session:{subject,kind:'doit-build',state:'live'}});inspect('test');assert(panel.includes('codex-inspector'));assert(!panel.includes('CRITERIA'));assert(!panel.includes('STAGE'));
}
inspect();assert(panel.includes('Codex Gate'));assert(!panel.includes('CRITERIA'));
assert.equal(fetches,0);
builds.set('test',{session:{subject:'L-spec-7',kind:'doit-build',state:'live'}});inspect('test');assert(panel.includes('CODEX SESSION'));assert(panel.includes('Shared title'));
await Promise.resolve();assert.equal(fetches,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")
    page = realm.PAGE.read_text()
    assert "openSubject(it.s)" in page  # timeline row
    assert "openSubject(a.dataset.subj)" in page  # lane/stuck/dossier links
    assert "subject:openSubject" in page  # map marker bubble


def test_ac8_footprint_routes_and_no_new_polling():
    import re, subprocess
    repo = realm.HERE.parent
    build = subprocess.check_output(["git", "log", "-1", "--format=%H", "--grep=^build(L-spec-1526):"], cwd=repo, text=True).strip()
    # Scope the footprint promise to this build, so later unrelated commits stay legal.
    base = build + "^" if build else "HEAD"
    tip = [build] if build else []
    allowed = {'src/realm.py','src/realm.html','src/realm_codex.js','src/test_realm.py','e2e/realm-codex.spec.ts'}
    changed = subprocess.check_output(["git", "diff", "--name-only", base, *tip], cwd=repo, text=True).splitlines()
    assert set(changed) <= allowed, changed
    parent = subprocess.check_output(["git", "show", base + ":src/realm.py"], cwd=repo, text=True)
    def routes(source):
        get = source[source.index("    def do_GET(self):"):]
        return set(re.findall(r'"(/[^"\s]*)"', get))
    current = subprocess.check_output(["git", "show", build + ":src/realm.py"], cwd=repo, text=True) if build else (realm.HERE / "realm.py").read_text()
    assert routes(parent) == routes(current)
    diff = subprocess.check_output(["git", "diff", base, *tip, "--", "src/realm.html", "src/realm_codex.js"], cwd=repo, text=True)
    added = '\n'.join(line[1:] for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++'))
    assert not re.search(r'\b(?:setInterval|setTimeout|EventSource)\s*\(', added)
    assert not re.search(r'\bfetch\s*\(', added)  # existing ensureSpec call is reused



# Canonical flow/health regression fixtures: no live ledger or production writes.
FLOW_STEPS = [dict(item='L-spec-'+str(i), item_kind='spec', stage=stage,
                   state='hold' if i < 3 else 'action', action=None if i < 3 else 'build',
                   owner=['operator','executor','sweeper'][i % 3], trigger=None,
                   reason='reason '+str(i), clears='decision', since='2026-10-27T00:00:00Z')
              for i, stage in enumerate(['building','checking','merging','building','planned','idea'])]
HEALTH_VIEW = dict(finishing_per_day=3, intake_per_day=5,
                   usage=dict(codex=dict(used=12,limit=100),claude=dict(used=None,limit=100)),
                   disk_used_fraction=.42, tokens_per_live_spec=dict(codex=123,claude=None))

@contextlib.contextmanager
def views_stub():
    with tempfile.TemporaryDirectory(prefix='realm-views-') as folder, contextlib.ExitStack() as stack:
        repo=pathlib.Path(folder)
        program=repo/'doit'
        program.write_text('#!/usr/bin/python3\nimport json,sys,time\nfrom pathlib import Path\n'
            'root=Path(__file__).parent\nname=sys.argv[1]\n'
            'with (root/"calls").open("a") as f: f.write(name+"\\n")\n'
            'cfg=json.loads((root/(name+".json")).read_text())\n'
            'time.sleep(cfg.get("sleep",0))\nprint(cfg["stdout"])\nsys.exit(cfg.get("exit",0))\n')
        program.chmod(0o700)
        def configure(name='next', value=None, **kw):
            if value is None: value=dict(counts={},steps=FLOW_STEPS) if name=='next' else HEALTH_VIEW
            (repo/(name+'.json')).write_text(json.dumps(dict(stdout=json.dumps(value),**kw)))
            realm.VIEW_CACHE.clear()
        configure(); configure('health')
        # Allow interpreter startup under suite load; timeout cases still exceed this.
        stack.enter_context(mock.patch.multiple(realm,LIVE_REPO=repo,VIEW_TIMEOUT=1))
        stack.enter_context(mock.patch.dict(realm.VIEW_CACHE,clear=True))
        yield repo,configure

@contextlib.contextmanager
def views_server():
    import http.server, threading
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),realm.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try: yield 'http://127.0.0.1:'+str(server.server_port)
    finally: server.shutdown(); server.server_close(); thread.join()

def view_get(base,path):
    import urllib.request,hashlib
    with urllib.request.urlopen(base+path) as response:
        body=response.read(); value=json.loads(body)
        print(json.dumps(dict(url=base+path,status=response.status,body_sha256=hashlib.sha256(body).hexdigest(),body_excerpt=body.decode()[:180])))
        return value

def test_flow_view():
    v=realm.flow_view(FLOW_STEPS)
    assert v['stages']==dict(idea=1,planned=1,building=2,checking=1,merging=1,live=None)
    assert v['stuck']==FLOW_STEPS[:3]
    for stage,count in v['stages'].items(): print(stage,count)
    assert realm.flow_view([])==dict(stages=dict(idea=0,planned=0,building=0,checking=0,merging=0,live=None),stuck=[])
    assert realm.flow_view([],live_count=4)['stages']['live']==4
    for bad in [None,{},[dict(stage='future',state='hold')],[dict(stage='idea')],[dict(stage='idea',state='future')]]:
        try: realm.flow_view(bad)
        except ValueError: pass
        else: raise AssertionError(bad)

def test_api_flow():
    with views_stub() as (repo,configure),views_server() as base:
        good=view_get(base,'/api/flow')
        assert good['flow']==realm.flow_view(FLOW_STEPS) and good['steps']==FLOW_STEPS
        assert good['flow_error'] is None and good['flow_at']
        cases=[dict(exit=1),dict(exit=2),dict(stdout='bad json'),dict(value=[]),dict(value={}),
               dict(value={'steps':[{'stage':'future','state':'hold'}]}),dict(sleep=1.5)]
        for case in cases:
            value=case.pop('value',None); stdout=case.pop('stdout',None)
            configure(value=value,**case)
            if stdout:
                cfg=repo/'next.json'; cfg.write_text(json.dumps({'stdout':stdout}))
            bad=view_get(base,'/api/flow')
            assert bad['flow'] is None and bad['steps'] is None and bad['flow_error'],bad
            configure(); assert view_get(base,'/api/flow')['flow']==good['flow']
        # Successful cache is replaced with unknown when the next refresh fails.
        configure(exit=2); assert view_get(base,'/api/flow')['flow'] is None

def test_api_health():
    with views_stub() as (repo,configure),views_server() as base:
        good=view_get(base,'/api/health'); assert good['health']==HEALTH_VIEW and good['health_error'] is None
        for cfg in [dict(exit=1),dict(exit=2),dict(stdout='bad'),dict(stdout='[]'),dict(stdout='{}'),dict(sleep=1.5)]:
            configure('health')
            path=repo/'health.json';value=json.loads(path.read_text());value.update(cfg);path.write_text(json.dumps(value))
            bad=view_get(base,'/api/health');assert bad['health'] is None and bad['health_error']
        configure('health');assert view_get(base,'/api/health')['health']==HEALTH_VIEW

def test_flow_in_bootstrap_and_history():
    import datetime as dt
    with views_stub() as (repo,configure),contextlib.ExitStack() as stack:
        for name in ['read_all','sys_signals','maker_index','omens_model','glance_payload','codex_payload']:
            stack.enter_context(mock.patch.object(realm,name,return_value=([],[],{}) if name=='read_all' else {}))
        now=dt.datetime.now(dt.timezone.utc)
        boot=realm.bootstrap(0,states=[],goals=[],proving=[],now=now)
        hist=realm.history_payload([],[],now,{})
        required={'flow','flow_error','flow_at','health','health_error','health_at'}
        for payload in [boot,hist]:
            assert required<=payload.keys() and not any(k.startswith('lane') for k in payload)
        assert (repo/'calls').read_text().splitlines().count('next')==1
        assert (repo/'calls').read_text().splitlines().count('health')==1
        realm.views_payload(now+dt.timedelta(seconds=31))
        assert (repo/'calls').read_text().splitlines().count('next')==2
        for failure in [False,True]:
            configure(exit=2 if failure else 0)
            with mock.patch.object(realm.HUB,'send') as send:
                realm.sample_once()
            payload=next(call.args[1] for call in send.call_args_list if call.args[0]=='history')
            assert required<=payload.keys()
            assert bool(payload['flow_error'])==failure
            assert (payload['flow'] is None)==failure
        assert 'snap = bootstrap(' in inspect.getsource(realm.push_forever)
        assert 'snap = bootstrap(' in inspect.getsource(realm.main)

def test_blockers_readers_and_ask():
    import urllib.request,urllib.error
    assert realm.ASK_PANELS==('stuck','omens','proving')
    with views_server() as base,mock.patch.object(realm.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
        items=['L-spec-0 · hold · operator · reason 0']
        request=urllib.request.Request(base+'/api/ask-thinker',data=json.dumps(dict(panel='stuck',items=items,note='fixture')).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request) as response: assert json.load(response)['ok']
        assert run.call_args.args[0]==[str(realm.LIVE_REPO/'doit'),'append','realm-ask','realm:stuck','panel=stuck','items:='+json.dumps(items),'why=fixture']
        request=urllib.request.Request(base+'/api/ask-thinker',data=b'{"panel":"flow"}')
        try: urllib.request.urlopen(request)
        except urllib.error.HTTPError as error: assert error.code==400
        else: raise AssertionError('bad panel accepted')
        assert run.call_count==1
    test_flow_page_under_node()



def test_flow_page_under_node():
    page=realm.PAGE.read_text()
    funcs=['applyViews','applyHistory','renderFlowTile','stageBadges','drawStageBadges',
           'flowHtml','healthHtml','stuckHtml0','askItems','applySnapshot','loadData','hit']
    functions='\n'.join(_page_function(page,name) for name in funcs)
    constants='\n'.join(line for line in page.splitlines() if line.startswith(('const FLOW_STAGES=','const STAGE_PLACES=','const viewText=','const esc =')))
    shelf=next(line for line in page.splitlines() if line.startswith("$('h-shelf').onclick="))
    script=r"""
const assert=require('node:assert/strict');
let DATA={},LIVE=true,REMOTE=false,EVENTS=[],lastT='',lastHeard=0,T0=0,T1=0,lastPush='',CAP={};
const SEEN=new Set(),rowKey=e=>e.t,toMs=x=>Date.parse(x)||0,fmt=x=>String(x);
const nodes={};const $=id=>nodes[id]??=( {firstElementChild:{textContent:''},textContent:'',className:''});
const S={flow:null,health:null,steps:null,shelf:new Map([['shelf-item',1]]),now:1000,actors:new Map(),effects:[]};
let panel='',rerenders=0;const listPanel=(a,b,rows)=>{assert.equal(rows()[0].s,'shelf-item');};
const rerenderPanel=()=>{panel=flowHtml()+healthHtml();rerenders++;};
const Codex={load:()=>{}},applyThroughput=()=>{},insertEvent=()=>{},fmtDur=String;
const g={fillText:(s)=>drawn.push(s)},mapFont=()=>'',PLACES=Object.fromEntries(['keep','tower','forge','court','gate','town'].map(k=>[k,{x:1,y:2,h:3}]));
const standing=[];let drawn=[];
"""+constants+'\n'+functions+'\n'+shelf+'\n'+r"""
const steps=STEPS;
const flow={stages:{idea:1,planned:1,building:2,checking:1,merging:1,live:null},stuck:steps.slice(0,2)};
const health=HEALTH;
const good={flow,health,steps,events:[],window:['2026-10-27','2026-10-28'],live:true};
loadData(good);
let html=flowHtml();for(const [stage,count] of Object.entries(flow.stages))assert(html.includes(stage+' · '+viewText(count)));
for(const row of flow.stuck)for(const field of ['item','owner','reason','since'])assert(html.includes(row[field]));
assert.equal((html.match(/class="act stuck-row"/g)||[]).length,2);assert(html.includes('hold'));
assert.equal($('h-stuck').firstElementChild.textContent,'2');
assert.deepEqual(stageBadges().map(b=>b.place),['keep','tower','forge','court','gate','town']);
for(const badge of stageBadges())assert.equal(badge.count,flow.stages[badge.stage]);
drawStageBadges();assert(drawn.includes('live unknown'));
html=healthHtml();for(const text of ['finishing per day · 3','intake per day · 5','codex usage · 12 / 100','claude usage · unknown / 100','disk · 42.0%','codex tokens per live spec · 123','claude tokens per live spec · unknown'])assert(html.includes(text),text);
assert.deepEqual(askItems('stuck'),flow.stuck.map(b=>`${b.item} · ${b.state} · ${b.owner} · ${b.reason}`));
$('h-shelf').onclick();hit({x:-100,y:-100});
S.flow={...flow,stuck:[{...steps[0],reason:'<img onerror=1>'}]};assert(stuckHtml0().includes('&lt;img onerror=1&gt;'));assert(!stuckHtml0().includes('<img'));
for(const apply of [applyHistory,applySnapshot,loadData]){
 for(const missing of [false,true]){
  applyViews(good);apply({...good,...(missing?{}:{flow:null,flow_error:'x',health:null,health_error:'x'}) ,...(missing?{flow:undefined,health:undefined}:{})});
  for(const b of stageBadges())assert.equal(b.count,null);
  assert.equal($('h-stuck').firstElementChild.textContent,'unknown');
  for(const stage of FLOW_STAGES)assert(flowHtml().includes(stage+' · unknown'));
  drawn=[];drawStageBadges();assert.equal(drawn.filter(x=>x.endsWith('unknown')).length,6);
  assert(!healthHtml().includes('42.0%'));assert(healthHtml().includes('finishing per day · unknown'));
 }
}
applyViews(good);applyHistory({});assert.equal(S.flow,null);assert.equal(S.health,null);
console.log('flow page: bootstrap, history, snapshot, missing/null, six badges, health, escaping, shelf and ask passed');
"""
    script=script.replace('STEPS',json.dumps(FLOW_STEPS)).replace('HEALTH;',json.dumps(HEALTH_VIEW)+';')
    result=subprocess.run(['/usr/bin/node','-e',script],text=True,capture_output=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    print(result.stdout.strip())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
