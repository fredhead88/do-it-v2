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
    assert not a["needs_you"] and a["owner"] == "thinker" and a["sev"] == 1    # a look brief naming the operator is NOT a request
    out = om.compute(rows, [], NOW)
    assert out["needs_you"] == 0 and out["health"] == 1
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


def test_needs_you_comes_only_from_operator_action_owed_rows_and_decision_requests():
    rows = [_row("owed-unobservable", 9, "L-spec-0060", crit="AC1", cap="operator-action", why="needs the operator's login"),
            _row("owed-unobservable", 9, "L-spec-0061", crit="AC1", cap="ro-sql", why="x"),
            _row("owed-unobservable", 9, "L-spec-0062", crit="AC1", cap="operator-action", why="y"), _row("owed-met", 5, "L-spec-0062", crit="AC1")]
    st = [["L-spec-0060", "shipped-owed-expired"], ["L-spec-0061", "shipped-owed-expired"], ["L-spec-0062", "shipped-owed-expired"]]
    out = om.compute(rows, st, NOW)
    (it,) = [i for i in out["items"] if i["needs_you"]]
    assert it["kind"] == "owedop" and it["subjects"] == ["L-spec-0060"]          # not 0061 (other capability), not 0062 (met)
    rows.append(_row("message-sent", 3, "shlomo", to="operator", why="please rule on X"))
    assert sum(i["needs_you"] for i in om.compute(rows, st, NOW)["items"]) == 2
    rows.append(_row("brief-answered", 1, "shlomo"))
    assert sum(i["needs_you"] for i in om.compute(rows, st, NOW)["items"]) == 1


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
    assert [i["id"] for i in out["items"]] == ["rec:disk-full"] and out["needs_you"] == 0 and out["health"] == 1
    assert out["v"] == 2 and out["at"] == _ago(0)

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


# ---------------------------------------------------------------- live lane (no main-branch module required)
def test_lane_loads_each_repo_without_import_residue():
    import itertools
    from unittest.mock import patch
    rows = [{"spec": "L-spec-7001", "rule": "build", "limit_min": 7,
             "since": _ago(3), "overdue": True, "reason": "dispatch <builder>",
             "reason_live": False, "blocker": "", "actor": "tick", "extra": [1, 2]}]
    with tempfile.TemporaryDirectory() as tmp:
        repos = {}
        for case in ("rows", "absent", "load-broken", "due-broken", "read-broken", "fold-broken", "shape-broken"):
            root = pathlib.Path(tmp) / case
            src = root / "src"
            src.mkdir(parents=True)
            repos[case] = root
            if case == "absent":
                continue
            (src / "fold.py").write_text('''from dataclasses import dataclass
@dataclass
class EventSource:
    name: str = 'live'
EVENTS = [{'actor': 'executor', '_src': 'live-reader', 'type': 'tick'}]
SPECS = {'L-spec-7001': {'state': 'written'}}
def read_events():
    return EVENTS
def fold(events):
    assert events is EVENTS
    return SPECS, {'not': 'specs'}, [], {}
''' + ("\ndef read_events(): raise RuntimeError('read-broken')\n" if case == "read-broken" else "")
                + ("\ndef fold(events): raise RuntimeError('fold-broken')\n" if case == "fold-broken" else "")
                + ("\ndef fold(events): return {}, {}\n" if case == "shape-broken" else ""))
            (src / "lane_rules.py").write_text('''import fold
from pathlib import Path
import json
LOG = Path(__file__).with_name('calls.jsonl')
def record(value):
    with LOG.open('a') as stream: stream.write(json.dumps(value) + '\\n')
def load():
    record({'case': CASE, 'call': 'load'})
    if CASE == 'load-broken': raise ValueError(CASE)
    return {}
def due(events, specs, now):
    record({'case': CASE, 'call': 'due', 'events': events, 'specs': specs, 'now': now.isoformat()})
    assert events is fold.EVENTS and specs is fold.SPECS
    if CASE == 'due-broken': raise RuntimeError(CASE)
    return ROWS
''' + f"\nCASE = {case!r}\nROWS = {rows!r}\n")
        # Multiple orders in one interpreter detect stale live-module caches.
        for order in itertools.chain([list(repos), list(reversed(repos))],
                                     itertools.permutations(("rows", "absent", "load-broken", "due-broken"))):
            for case in order:
                root = repos[case]
                before_path, before_modules = sys.path[:], sys.modules.copy()
                log = root / "src/calls.jsonl"
                if log.exists():
                    log.unlink()
                with patch.dict(os.environ, {"DOIT_LIVE_REPO": str(root)}):
                    with patch.object(realm, "read_all", return_value=([], [], {})), \
                         patch.object(realm, "sys_signals", return_value={}), \
                         patch.object(realm.realm_codex, "payload", return_value={}), \
                         patch.object(realm, "omens_model", return_value={}):
                        payload = realm.bootstrap(0, states=[], goals=[], proving=[], now=NOW)
                    history = realm.history_payload([], [], NOW, {})
                assert sys.path == before_path
                assert sys.modules == before_modules
                assert payload["lane_at"] == history["lane_at"] == NOW.isoformat(timespec="seconds")
                assert "wallclock" in history
                for result in (payload, history):
                    if case == "rows":
                        assert result["lane"] == rows and result["lane_error"] is None
                    else:
                        assert result["lane"] is None and result["lane_error"]
                        assert (str(root) if case == "absent" else "invalid four-part" if case == "shape-broken" else case) in result["lane_error"], (case, result)
                calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
                due = [c for c in calls if c["call"] == "due"]
                assert len(due) == (2 if case in ("rows", "due-broken") else 0)
                for call in due:
                    assert call["case"] == case and call["now"] == NOW.isoformat()
                    assert all(e["actor"] and e["_src"] == "live-reader" for e in call["events"])
                    assert call["specs"] == {"L-spec-7001": {"state": "written"}}


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


def test_lane_page_uses_rows_and_keeps_non_lane_waits():
    import re
    import subprocess
    page = realm.PAGE.read_text()
    body = _page_function(page, "computeBlockers")
    assert body and _page_function(page, "renderStuck")
    for lookup in ("spec-written", "build-done", "verdict", "review", "merge-gate-clean"):
        assert lookup not in body
    # Evaluate numeric products, so spelling a removed threshold differently cannot hide it.
    for expr in re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?:\s*\*\s*\d+(?:\.\d+)?)*", body):
        assert eval(expr, {"__builtins__": {}}, {}) not in (600, 900, 1800, 600000, 900000, 1800000), expr
    for name in re.findall(r"\b[A-Z][A-Z_]+\b", body):
        for expr in re.findall(r"(?:const|let)\s+" + name + r"\s*=\s*([\d\s*]+)[;,]", page):
            assert eval(expr, {"__builtins__": {}}, {}) not in (600, 900, 1800, 600000, 900000, 1800000)
    functions = ("applyLane", "applyHistory", "laneNotice", "laneDetails", "renderStuckTile",
                 "stateMap", "laneSplit", "computeHolds", "computeBlockers", "renderStuck", "stuckHtml0",
                 "escOwner", "computeOmens")
    constants = ("S", "toMs", "fmt", "fmtDur", "fmtAge", "esc", "LANE_PLACE", "WHO", "HOLD_RE", "LIFT_RE")
    declarations = []
    for name in constants:
        declarations.append(next(line for line in page.splitlines() if line.startswith("const " + name + " ") or "const " + name + "=" in line))
    # WHO_ shares the HOLD_RE line; LIFT_RE and all page functions are real source.
    script = "\n".join(declarations + [_page_function(page, n) for n in functions])
    script += r'''
const assert=require('node:assert/strict');
const els=new Map();const $=id=>{if(!els.has(id))els.set(id,{className:'',innerHTML:'',firstElementChild:{textContent:''}});return els.get(id);};
let LIVE=true, DATA={states:[]}, stream=null, REMOTE=false,lastHeard=0;
const CAP={builder:5};
const isRaven=()=>true, knownBug=()=>null, execBusy=()=>false, OM=()=>null,lastOf=()=>null;
const specCharter=()=>null,charterRows=()=>[],fixFor=()=>null,ownerFor=()=>null,sinceFor=()=>S.now,renderOmens=()=>{};
const NO_SELFCHECK=new Set(),FINISHED_RE=/accepted|shipped|killed/,DEFAULT_OWNER={shelf:'executor'},NEXT_ACTION={shelf:()=>['executor','dispatch a builder']};
const isOperator=o=>o==='operator'||o==='you';
const now=Date.parse('2026-10-15T12:00:00Z'),H=3600000;
function fresh(lane=[]){S.now=now;S.lane=lane;S.laneAt='2026-10-15T12:00:00Z';S.laneError=null;S.actors.clear();S.subjectEvents.clear();S.esc.clear();S.sys=null;S.wallclock=null;S.lastAct=0;S.lastTick=null;S.recent=[];S.raisedAt.clear();DATA={states:[]};LIVE=true;}
function row(rule,age=3,extra={}){return {spec:'L-spec-'+rule,rule,actor:'tick',since:new Date(now-age*H).toISOString(),limit_min:17,overdue:true,reason:'because <'+rule+'>',reason_live:false,blocker:'block <'+rule+'>',...extra};}
function has(text,part){assert.ok(text.includes(part),part+' missing in '+text);}
const rows=[row('build',5),row('grade',2,{actor:'grader'}),row('gate',.5,{actor:'-'}),row('wave-held',3,{reason_live:true}),row('future',1),row('review',1,{overdue:false})];
fresh(rows);computeBlockers();assert.equal(S.blockers.length,5);
for(const [rule,place,kind,who,need,sev] of [['build','tower','shelf','executor',true,2],['grade','court','grade','grader',true,1],['gate','gate','merge','executor',true,0],['wave-held','tower','shelf','wave',false,1],['future','tower','lane','executor',true,0]]){
 const b=S.blockers.find(b=>b.lane.rule===rule);assert.deepEqual([b.place,b.kind,b.who,b.need,b.sev],[place,kind,who,need,sev]);assert.equal(b.s,'L-spec-'+rule);assert.equal(b.since,Date.parse(b.lane.since));assert.equal(b.wait,now-b.since);
}
const panel=stuckHtml0();for(const r of rows){for(const value of [r.spec,r.rule,String(r.limit_min),r.since,esc(r.reason),esc(r.blocker)])has(panel,value);}
assert.ok(!panel.includes('because <build>'));assert.ok(!panel.includes('block <build>'));
assert.ok(!/\d/.test(panel.split('WHAT COUNTS AS STUCK')[1].replace(/<[^>]*>/g,'')));has(panel,'WITHIN LIMITS · 1');
const allRules={rebuild:['tower','shelf'],'dependency-held':['tower','shelf'],regrade:['court','grade'],review:['inn','review'],enqueue:['gate','merge'],merge:['gate','merge'],close:['keep','close'],idle:['tower','idle'],toString:['tower','lane'],constructor:['tower','lane']};
for(const [rule,mapping] of Object.entries(allRules)){fresh([row(rule)]);computeBlockers();assert.deepEqual([S.blockers[0].place,S.blockers[0].kind],mapping);}
fresh(null);S.laneError='load <failed>';computeBlockers();for(const text of [$('stuck').innerHTML,stuckHtml0()]){has(text,'rule table unreadable');has(text,'load &lt;failed&gt;');assert.ok(!text.toUpperCase().includes('NOTHING STUCK'));}assert.notEqual($('stuck').className,'calm');assert.equal($('h-stuck').firstElementChild.textContent,'unreadable');
fresh([]);computeBlockers();has($('stuck').innerHTML,'NOTHING STUCK');assert.equal($('stuck').className,'calm');
fresh([row('unrecognized',1,{reason:'undetermined: cannot decide'})]);computeBlockers();assert.equal(S.blockers.length,1);assert.equal(S.blockers[0].need,true);has(stuckHtml0(),'undetermined: cannot decide');has($('stuck').innerHTML,'1 spec');has($('h-stuck').firstElementChild.textContent,'1 ·');assert.notEqual($('h-stuck').className,'stat good');
// Histories far past every removed limit cannot recreate page-side lane waits.
fresh([]);for(const [i,ty] of ['spec-written','build-done','verdict','review','merge-gate-clean'].entries()){const s='L-spec-old'+i;DATA.states.push([s,['written','built','graded','reviewing','reviewing'][i]]);S.subjectEvents.set(s,[{ty,ms:now-100*H,st:'DONE',ok:true,nb:0}]);}computeBlockers();assert.deepEqual(S.blockers,[]);
// A hold, a freeze, a busy subject and red master CI cannot overrule lane rows.
fresh([row('build',5),row('gate',3)]);S.subjectEvents.set('L-spec-build',[{ty:'decision',a:'operator',ms:now-H,why:'hold L-spec-build until merge'}]);S.subjectEvents.set('L-charter-1',[{ty:'decision',a:'operator',ms:now-H,why:'merge freeze'}]);S.actors.set('busy',{subject:'L-spec-build',state:'working',start:now,tour:false});S.sys={ci:{red:1}};computeBlockers();assert.ok(S.holds.bySpec.has('L-spec-build'));assert.ok(S.holds.freeze);for(const b of S.blockers){assert.equal(b.need,true);assert.equal(b.text,b.lane.reason);}computeOmens();assert.ok(S.alarms.some(a=>a.kind==='shelf'));
fresh([row('wave-held',3,{reason_live:true})]);computeBlockers();assert.equal(S.blockers[0].need,false);has(S.blockers[0].text,'block <wave-held>');
function kept(){for(const [s,state] of [['L-spec-raven','written'],['L-spec-owed','shipped-owed']]){DATA.states.push([s,state]);S.subjectEvents.set(s,[{ty:'shipped',ms:now-30*H}]);}S.esc.set('L-spec-raven',[{ms:now-H,ow:'operator'}]);S.actors.set('queued',{sp:'L-builder-q',subject:'L-spec-q',role:'builder',state:'queued',start:now-3*60000,wp:'forge'});S.actors.set('working',{sp:'L-builder-w',subject:'L-spec-w',role:'builder',state:'working',start:now-H,wp:'forge'});}
fresh([]);kept();computeBlockers();assert.deepEqual(new Set(S.blockers.map(b=>b.kind)),new Set(['raven','owed','unclaimed','cap']));assert.equal(S.blockers.find(b=>b.kind==='raven').who,'you');has($('stuck').innerHTML,'YOUR ruling');
applyHistory({lane:[row('build')],lane_error:null,lane_at:'updated',history:{},wallclock:{}});assert.equal(S.lane.length,1);assert.equal(S.laneAt,'updated');
applyHistory({lane:null,lane_error:'refresh failed',lane_at:'failed',history:{}});assert.equal(S.lane,null);assert.equal(S.laneError,'refresh failed');has($('stuck').innerHTML,'rule table unreadable');
fresh([row('build')]);kept();LIVE=false;S.now=now-1;computeBlockers();assert.ok(S.blockers.every(b=>!b.lane));assert.deepEqual(new Set(S.blockers.map(b=>b.kind)),new Set(['raven','owed','unclaimed','cap']));for(const text of [$('stuck').innerHTML,stuckHtml0()]){has(text,'rule table: live only');assert.ok(!text.toUpperCase().includes('NOTHING STUCK'));}assert.notEqual($('stuck').className,'calm');
console.log('lane UI: mappings, rendering, unreadable, undetermined, overrides, omens, kept waits, refresh and replay passed');
'''
    # seek/reset must preserve the live result, while tick retains its numeric count separately.
    assert "S.lane=null" not in _page_function(page, "reset")
    assert "S.lane=e.ln" not in _page_function(page, "handle")
    assert "applyLane(d)" in _page_function(page, "loadData")
    assert "applyLane(d)" in _page_function(page, "applySnapshot")
    assert "stream.addEventListener('history',ev=>applyHistory(JSON.parse(ev.data)))" in page
    result = subprocess.run(["/usr/bin/node", "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
