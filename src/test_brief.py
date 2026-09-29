#!/usr/bin/env python3
"""One runnable check on brief's dossier rules. Run: python3 test_brief.py"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"] = str(TMP)
# Same reasoning as test_fold.py: carry.uncarried() reads V4_LEDGER_DIR/V4_INBOX_DIR,
# not DOIT_ROOT — pinned so this file never touches the operator's real inbox.
os.environ["V4_LEDGER_DIR"] = str(TMP / "v4-ledger-absent")
os.environ["V4_INBOX_DIR"] = str(TMP / "v4-inbox-absent")
SRC = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
import fold  # noqa: E402
import plain  # noqa: E402
import brief  # noqa: E402

# Explicit, relative timestamps — never wall-clock NOW (Assumptions: a fixture
# using real time would make fold.spec_state's owed-check branches flaky).
T = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
stamp = lambda d=0: (T - timedelta(days=d)).isoformat(timespec="seconds")


def ledger(**files):
    shutil.rmtree(TMP / "events", ignore_errors=True)
    (TMP / "events").mkdir(parents=True)
    for name, evs in files.items():
        (TMP / "events" / name).write_text(
            "".join(json.dumps({"v": 1, **e}) + "\n" for e in evs))
    return fold.read_events()


def write_content(item_id, text):
    (TMP / "content").mkdir(parents=True, exist_ok=True)
    (TMP / "content" / f"{item_id}.md").write_text(text)


# ── AC1 · charter dossier: 3 specs (accepted/written/killed), real Intent ────
C1, S_ACC, S_WR, S_KI = "L-charter-0900", "L-spec-0901", "L-spec-0902", "L-spec-0903"
write_content(C1, "# Charter AC1 Title\n\n## 1. Intent\n\n"
              "This charter proves the dossier core. It has more words after the period.\n")
events = ledger(**{
    "L-builder-01.jsonl": [
        {"ts": stamp(5), "type": "spec-written", "subject": S_ACC, "charter": C1},
        {"ts": stamp(4), "type": "build-started", "subject": S_ACC},
        {"ts": stamp(4), "type": "build-done", "subject": S_ACC},
    ],
    "L-grader-01.jsonl": [{"ts": stamp(3), "type": "verdict", "subject": S_ACC, "confirmed": True}],
    "L-reviewer-01.jsonl": [{"ts": stamp(3), "type": "review", "subject": S_ACC, "depth": "gates-only"}],
    "L-executor-01.jsonl": [{"ts": stamp(2), "type": "shipped", "subject": S_ACC}],
    "L-spec-writer-01.jsonl": [{"ts": stamp(4), "type": "spec-written", "subject": S_WR, "charter": C1}],
    "L-spec-writer-02.jsonl": [
        {"ts": stamp(4), "type": "spec-written", "subject": S_KI, "charter": C1},
        {"ts": stamp(3), "type": "spec-killed", "subject": S_KI},
    ],
    "L-planner-01.jsonl": [{"ts": stamp(6), "type": "l1-complete", "subject": C1}],
})
d1 = brief.dossier(C1, events, TMP, T)
assert set(d1.keys()) == {"id", "kind", "title", "plain", "catch_me_up", "status", "next",
                          "timeline", "spend", "children", "as_of"}, d1.keys()
assert d1["kind"] == "charter"
assert d1["status"] == {"state": "L1-complete", "phrase": plain.phrase("charter", "L1-complete")}, d1["status"]
assert d1["plain"]["source"] == "intent", d1["plain"]
assert [c["id"] for c in d1["children"]] == sorted([S_ACC, S_WR, S_KI]), d1["children"]
killed_row = next(c for c in d1["children"] if c["id"] == S_KI)
assert killed_row["phrase"] == "stopped", killed_row
expected_mid = (f"Where it stands: {plain.phrase('charter', 'L1-complete')}; "
                "1 of 2 specs proven, 0 being built, 1 waiting")
assert d1["catch_me_up"][1] == expected_mid, d1["catch_me_up"]
print("AC1 ok")

# ── AC2 · spec dossier, kind-spec overrides ──────────────────────────────────
S2 = "L-spec-0910"
events2 = ledger(**{"L-spec-writer-01.jsonl": [{"ts": stamp(1), "type": "spec-written", "subject": S2}]})
d2 = brief.dossier(S2, events2, TMP, T)
assert d2["kind"] == "spec" and d2["children"] == [], d2
ph2 = plain.phrase("spec", "written")
assert d2["catch_me_up"][1] == f"Where it stands: {ph2}", d2["catch_me_up"]
assert d2["timeline"] == {"filed": stamp(1), "planned": None, "l1_complete": None,
                          "first_merge": None, "last_merge": None,
                          "expected_done": None, "expected_done_why": "a spec has no forecast"}, d2["timeline"]
print("AC2 ok")

# ── AC3 · goal dossier: 2 charters, one L2-complete, one open w/ a written spec
G3, C3A, C3B, S3A, S3B = ("L-goal-0920", "L-charter-0921", "L-charter-0922",
                          "L-spec-0921", "L-spec-0922")
events3 = ledger(**{
    "L-thinker-01.jsonl": [
        {"ts": stamp(9), "type": "goal-filed", "subject": G3, "title": "Goal AC3",
         "path": str(TMP / "content" / f"{G3}.md")},
        {"ts": stamp(8), "type": "charter-filed", "subject": C3A, "goal": G3, "covers": "none"},
        {"ts": stamp(7), "type": "charter-filed", "subject": C3B, "goal": G3, "covers": "none"},
    ],
    "L-planner-01.jsonl": [{"ts": stamp(6), "type": "l1-complete", "subject": C3A}],
    "L-builder-01.jsonl": [
        {"ts": stamp(5), "type": "spec-written", "subject": S3A, "charter": C3A},
        {"ts": stamp(4), "type": "build-started", "subject": S3A},
        {"ts": stamp(4), "type": "build-done", "subject": S3A},
    ],
    "L-grader-01.jsonl": [{"ts": stamp(3), "type": "verdict", "subject": S3A, "confirmed": True}],
    "L-reviewer-01.jsonl": [{"ts": stamp(3), "type": "review", "subject": S3A, "depth": "gates-only"}],
    "L-executor-01.jsonl": [
        {"ts": stamp(2), "type": "shipped", "subject": S3A},
        {"ts": stamp(1), "type": "sweep-fixpoint", "subject": C3A},
    ],
    "L-charter-reviewer-01.jsonl": [{"ts": stamp(1), "type": "charter-review-complete", "subject": C3A}],
    "L-spec-writer-01.jsonl": [{"ts": stamp(4), "type": "spec-written", "subject": S3B, "charter": C3B}],
    "L-builder-spawn-a.jsonl": [
        {"ts": stamp(2), "type": "spawn-done", "subject": C3A, "spawn": "spawn-c3a", "charter": C3A,
         "backend": "seat", "cost_usd": None, "duration_ms": 600000},
    ],
    "L-builder-spawn-b.jsonl": [
        {"ts": stamp(2), "type": "spawn-done", "subject": C3B, "spawn": "spawn-c3b", "charter": C3B,
         "backend": "seat", "cost_usd": None, "duration_ms": 300000},
    ],
})
sp, ch, _ig, _by = fold.fold(events3)
assert ch[C3A]["state"] == "L2-complete", ch[C3A]["state"]
assert ch[C3B]["state"] == "open", ch[C3B]["state"]
d3 = brief.dossier(G3, events3, TMP, T)
assert d3["kind"] == "goal"
assert [c["id"] for c in d3["children"]] == sorted([C3A, C3B]), d3["children"]
open_row = next(c for c in d3["children"] if c["id"] == C3B)
assert len(open_row["specs"]) == 1, open_row
assert open_row["specs"][0]["waiting_on"] == f"{plain.NEXT['written'][0]} ({plain.NEXT['written'][1]})", open_row
expected_mid3 = f"Where it stands: {d3['status']['phrase']}; 1 of 2 specs proven, 0 being built, 1 waiting"
assert d3["catch_me_up"][1] == expected_mid3, d3["catch_me_up"]
hand_spend = brief._sum_spends([brief._spend_for_subject(C3A, events3), brief._spend_for_subject(C3B, events3)])
assert d3["spend"] == hand_spend, (d3["spend"], hand_spend)
print("AC3 ok")

# ── AC4 · brief.goals() ───────────────────────────────────────────────────────
rows = brief.goals(events3, TMP)
row = next(r for r in rows if r["id"] == G3)
assert row["state"] == plain.goal_state(["L2-complete", "open"]) == "being built", row
assert row["phrase"] == plain.phrase("goal", "being built"), row
empty_events = ledger()
assert brief.goals(empty_events, TMP) == []
print("AC4 ok")

# ── AC5 · spend.metered_usd / metered_unmeasured off raw terminal events ────
S5 = "L-spec-0930"
events5 = ledger(**{
    "L-spec-writer-01.jsonl": [{"ts": stamp(2), "type": "spec-written", "subject": S5}],
    "L-builder-seat.jsonl": [{"ts": stamp(1), "type": "spawn-done", "subject": S5, "spawn": "sp-seat",
                              "backend": "seat", "cost_usd": None}],
    "L-builder-cp.jsonl": [{"ts": stamp(1), "type": "spawn-done", "subject": S5, "spawn": "sp-cp",
                            "backend": "claude-p", "cost_usd": 1.23}],
    "L-builder-codex.jsonl": [{"ts": stamp(1), "type": "spawn-done", "subject": S5, "spawn": "sp-codex",
                               "backend": "codex", "cost_usd": None}],
})
d5 = brief.dossier(S5, events5, TMP, T)
assert d5["spend"]["metered_usd"] == 1.23, d5["spend"]
assert d5["spend"]["metered_unmeasured"] == 1, d5["spend"]
print("AC5 ok")

# ── AC6 · proving_lookup, four cases + dossier wiring on a "proving" charter ─
orig_run = subprocess.run


def _boom(*_a, **_kw):
    raise AssertionError("subprocess.run must not be called when PROVING_PATH is absent")


brief.PROVING_PATH = TMP / "does-not-exist.py"
subprocess.run = _boom
try:
    assert brief.proving_lookup("L-charter-0940", T) == (None, "Proving not derived yet")
finally:
    subprocess.run = orig_run
print("AC6a ok")

FAKE_B = TMP / "fake_proving_b.py"
FAKE_B.write_text("import sys\nsys.stderr.write('boom')\nsys.exit(1)\n")
brief.PROVING_PATH = FAKE_B
assert brief.proving_lookup("L-charter-0940", T) == (None, "Proving not derived yet: boom")
print("AC6b ok")

FAKE_C = TMP / "fake_proving_c.py"
FAKE_C.write_text(
    "import json\n"
    "print(json.dumps([{\"id\": \"L-charter-0940\", \"deadline\": \"2026-10-01T00:00:00Z\", "
    "\"next\": None, \"owner\": \"operator\"}]))\n"
)
brief.PROVING_PATH = FAKE_C
entry_c, err_c = brief.proving_lookup("L-charter-0940", T)
assert err_c is None and entry_c == {"id": "L-charter-0940", "deadline": "2026-10-01T00:00:00Z",
                                     "next": None, "owner": "operator"}, (entry_c, err_c)
print("AC6c ok")

entry_d, err_d = brief.proving_lookup("L-charter-9999", T)
assert entry_d is None and err_d == "not found", (entry_d, err_d)
print("AC6d ok")

# dossier-level wiring: two synthetic "proving" charters, empty `mine` each
# (a Proving/Reopened charter's specs are by construction build-done).
PROV_MATCH, PROV_NOMATCH = "L-charter-0940", "L-charter-0941"


def _fake_fold(_events):
    def _c(cid):
        evs = [{"type": "charter-filed", "subject": cid, "goal": None, "ts": stamp(5),
               "actor": "thinker", "_src": "fake:1"}]
        return {"id": cid, "evs": evs, "age": 0.0, "state": "proving", "briefs": 0,
                "owed": 0, "unbuilt": 0, "closable": None}
    charters = {PROV_MATCH: _c(PROV_MATCH), PROV_NOMATCH: _c(PROV_NOMATCH)}
    return {}, charters, [], {}


orig_fold = fold.fold
fold.fold = _fake_fold
try:
    brief.PROVING_PATH = FAKE_C
    d6c = brief.dossier(PROV_MATCH, [], TMP, T)
    assert d6c["next"] == {"text": "final checks and close", "owner": "operator"}, d6c["next"]
    assert d6c["timeline"]["expected_done"] == "2026-10-01T00:00:00Z", d6c["timeline"]
    assert d6c["timeline"]["expected_done_why"] is None, d6c["timeline"]

    for path, why in ((TMP / "does-not-exist.py", "Proving not derived yet"),
                      (FAKE_B, "Proving not derived yet: boom"),
                      (FAKE_C, "Proving not derived yet")):  # FAKE_C w/ no matching id -> "not found" -> surfaced same phrase
        brief.PROVING_PATH = path
        d6 = brief.dossier(PROV_NOMATCH, [], TMP, T)
        assert d6["next"] == {"text": "final checks and close", "owner": "executor"}, (path, d6["next"])
        assert d6["timeline"]["expected_done"] is None, (path, d6["timeline"])
        assert d6["timeline"]["expected_done_why"] == why, (path, d6["timeline"])
finally:
    fold.fold = orig_fold
print("AC6 dossier-wiring ok")

import re as _re  # noqa: E402
brief_src = (SRC / "brief.py").read_text()
assert not _re.search(r"^\s*(import proving|from proving)\b", brief_src, _re.MULTILINE), \
    "brief.py must never import proving.py (SD7)"
print("AC6e ok")

# ── AC7 · the CLI, three kinds + unknown id + empty --goals --json ──────────
G7, C7, S7 = "L-goal-0950", "L-charter-0950", "L-spec-0950"
write_content(C7, "# Test Charter\n\n## 1. Intent\n\nDoes a thing. More words follow.\n")
write_content(S7, "# Test Spec\n\nIn plain English: it does a thing.\n")
events7 = ledger(**{
    "L-thinker-01.jsonl": [
        {"ts": stamp(6), "type": "goal-filed", "subject": G7, "title": "Test Goal",
         "path": str(TMP / "content" / f"{G7}.md")},
        {"ts": stamp(5), "type": "charter-filed", "subject": C7, "goal": G7, "covers": "none"},
    ],
    "L-spec-writer-01.jsonl": [{"ts": stamp(4), "type": "spec-written", "subject": S7, "charter": C7}],
})

for item_id in (G7, C7, S7):
    direct = brief.dossier(item_id, events7, TMP, T)
    r = subprocess.run([sys.executable, "brief.py", item_id, "--json"], cwd=str(SRC),
                       capture_output=True, text=True)
    assert r.returncode == 0, (item_id, r.stderr)
    got = json.loads(r.stdout)
    got.pop("as_of", None)
    direct.pop("as_of", None)
    assert got == direct, (item_id, got, direct)
print("AC7a ok")

r_unknown = subprocess.run([sys.executable, "brief.py", "L-charter-9999999", "--json"], cwd=str(SRC),
                           capture_output=True, text=True)
assert r_unknown.returncode == 2, r_unknown
assert r_unknown.stdout == "", r_unknown.stdout
assert len(r_unknown.stderr.strip().splitlines()) == 1, r_unknown.stderr
print("AC7b ok")

ledger()  # empty ledger
r_goals_empty = subprocess.run([sys.executable, "brief.py", "--goals", "--json"], cwd=str(SRC),
                               capture_output=True, text=True)
assert r_goals_empty.returncode == 0, r_goals_empty
assert json.loads(r_goals_empty.stdout) == []
print("AC7c ok")

d7 = brief.dossier(C7, events7, TMP, T)
rendered = brief.render(d7).splitlines()
assert rendered[1:4] == d7["catch_me_up"], (rendered, d7["catch_me_up"])
print("AC7d ok")

# ── AC8 · doit's own dispatcher matches the direct form byte-for-byte ────────
DOIT = SRC.parent / "doit"
r_direct8 = subprocess.run([sys.executable, "brief.py", C7, "--json"], cwd=str(SRC),
                           capture_output=True, text=True)
r_doit8 = subprocess.run(["bash", str(DOIT), "brief", C7, "--json"], cwd=str(SRC.parent),
                         capture_output=True, text=True)
assert r_direct8.returncode == r_doit8.returncode
got_direct8 = json.loads(r_direct8.stdout)
got_doit8 = json.loads(r_doit8.stdout)
got_direct8.pop("as_of", None)
got_doit8.pop("as_of", None)
assert got_direct8 == got_doit8, (got_direct8, got_doit8)

r_goals_direct = subprocess.run([sys.executable, "brief.py", "--goals", "--json"], cwd=str(SRC),
                                capture_output=True, text=True)
r_goals_doit = subprocess.run(["bash", str(DOIT), "brief", "--goals", "--json"], cwd=str(SRC.parent),
                              capture_output=True, text=True)
assert r_goals_direct.stdout == r_goals_doit.stdout

r_help = subprocess.run(["bash", str(DOIT), "help"], cwd=str(SRC.parent), capture_output=True, text=True)
assert "brief" in r_help.stdout

r_bashn = subprocess.run(["bash", "-n", str(DOIT)], capture_output=True, text=True)
assert r_bashn.returncode == 0, r_bashn.stderr
print("AC8 ok")

print("all ok")
