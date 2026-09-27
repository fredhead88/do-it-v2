#!/usr/bin/env python3
"""Checks on sweep_owed — `doit sweep-owed` (L-charter-0038 R1/R2, L-spec-0388).
Run: python3 test_sweep_owed.py

One shared `DOIT_ROOT` tempdir for the whole file (the same convention
`test_tick.py`/`test_owed.py`/`test_fold.py` already use): fixtures are built
and torn down per block by writing/unlinking individual `events/*.jsonl`
files directly, never by swapping roots mid-file. Nothing here calls a real
`claude`/seat spawn or a real `ssh`/`psql`: every `doit packet`/`doit
dispatch` call goes through an injectable `Runner` (AC3) that records argv
and returns a canned result.
"""
import json, os, pathlib, re, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"] = str(TMP)
os.environ.pop("DOIT_PROJECT", None)
os.environ.pop("DOIT_LEDGER_FILE", None)          # AC8: the driver must not depend on this
(TMP / "events").mkdir(parents=True, exist_ok=True)
(TMP / "content").mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import crons, dispatch, fold, owed, sweep_owed  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime.now(timezone.utc)
EV = TMP / "events"
CONTENT = TMP / "content"
EPOCH = (NOW - timedelta(days=3650)).isoformat(timespec="seconds")


def iso(**kw):
    return (NOW + timedelta(**kw)).isoformat(timespec="seconds")


def write(name, *events):
    p = EV / name
    p.write_text("".join(json.dumps({"v": 1, "ts": NOW.isoformat(timespec="seconds"), **e}) + "\n"
                         for e in events))
    return p


def clear():
    for f in EV.glob("*.jsonl"):
        f.unlink()
    for f in CONTENT.glob("*"):
        if f.is_file():
            f.unlink()


def specs_now():
    ev = fold.read_events()
    return ev, fold.fold(ev)[0]


class FakeRunner:
    """Records every argv it is called with; `packet` calls return
    `packet_path` on stdout, everything else returns rc 0 with empty output —
    unless `fail_at` names the (0-based) call index to fail instead."""
    def __init__(self, packet_path="packet-path.md", fail_at=None):
        self.calls = []
        self.packet_path = packet_path
        self.fail_at = fail_at

    def run(self, argv):
        idx = len(self.calls)
        self.calls.append(list(argv))
        if self.fail_at == idx:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
        if len(argv) > 1 and argv[1] == "packet":
            return subprocess.CompletedProcess(argv, 0, stdout=self.packet_path + "\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def rows_of(manifest_path):
    text = manifest_path.read_text()
    body = text.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    return json.loads(body)


sweep_ledger = fold.EVENTS / sweep_owed.LEDGER_NAME

# ══════════════════════════════════════════════════════════════════════════════
# AC1 · dry-run: one due check, no open spawn — one line, nothing appended
# ══════════════════════════════════════════════════════════════════════════════
clear()
write("L-spec-writer-a1.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-a1", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-a1", "criterion": "AC1",
       "wake_at": iso(hours=-2), "line": 10})
write("L-executor-a1.jsonl", {"ts": EPOCH, "type": "shipped", "subject": "L-spec-a1"})

before1 = {p.name: p.read_text() for p in EV.glob("*.jsonl")}
runner1 = FakeRunner()
lines1 = sweep_owed.run(dry_run=True, runner=runner1, now=NOW)
after1 = {p.name: p.read_text() for p in EV.glob("*.jsonl")}
assert len(lines1) == 1, lines1
assert after1 == before1, "AC1: a dry-run must append nothing to any ledger file"
assert not runner1.calls, "AC1: a dry-run never calls packet/dispatch"
assert not list(CONTENT.glob("L-sweep-*.md")), "AC1: a dry-run allocates no manifest"
print("sweep-owed-0388 AC1 ok")

# ══════════════════════════════════════════════════════════════════════════════
# AC2/AC3 · 10 due checks, one project — batch the 8 smallest due_at; the
# packet-then-dispatch argv sequence, in order, exactly once
# ══════════════════════════════════════════════════════════════════════════════
clear()
PROJ_A = "proj-a"
for i in range(1, 11):
    sid = f"L-spec-b{i:02d}"
    write(f"L-spec-writer-b{i:02d}.jsonl",
          {"ts": EPOCH, "type": "spec-written", "subject": sid, "project": PROJ_A},
          {"ts": EPOCH, "type": "owed-ac", "subject": sid, "criterion": "AC1",
           "wake_at": iso(hours=-i), "line": i})
    write(f"L-executor-b{i:02d}.jsonl", {"ts": EPOCH, "type": "shipped", "subject": sid})

_, specs2 = specs_now()
due2 = sweep_owed.due_candidates(specs2, NOW)
b_due2 = [r for r in due2 if r["spec"].startswith("L-spec-b")]
assert len(b_due2) == 10, b_due2
assert [r["spec"] for r in b_due2][:2] == ["L-spec-b10", "L-spec-b09"], \
    "AC2: oldest-due_at (most overdue) sorts first"

runner2 = FakeRunner(packet_path=str(CONTENT / "packet-b.md"))
before_manifests2 = set(CONTENT.glob("L-sweep-*.md"))
sweep_owed.run(dry_run=False, runner=runner2, now=NOW)
new_manifests2 = set(CONTENT.glob("L-sweep-*.md")) - before_manifests2
assert len(new_manifests2) == 1, new_manifests2
manifest2 = new_manifests2.pop()
parsed2 = rows_of(manifest2)
assert len(parsed2) == 8, parsed2
got2 = [r["spec"] for r in parsed2]
assert got2 == [f"L-spec-b{i:02d}" for i in range(10, 2, -1)], \
    f"AC2: the 8 smallest-due_at checks, oldest first; the 2 least-overdue are left out: {got2}"
for r in parsed2:
    assert set(r) == {"spec", "criterion", "declared_src", "line", "spec_path"}, r
    assert r["spec_path"] == str(dispatch.spec_path(r["spec"])), r
print("sweep-owed-0388 AC2 ok")

assert len(runner2.calls) == 2, runner2.calls
sweep_id2 = manifest2.stem
assert runner2.calls[0] == [str(sweep_owed.doit_bin()), "packet", "owed-sweeper", sweep_id2], \
    runner2.calls[0]
assert runner2.calls[1] == [
    str(sweep_owed.doit_bin()), "dispatch", "owed-sweeper", sweep_id2,
    "--packet", runner2.packet_path, "--cwd", str(dispatch.ROOT / "repos" / PROJ_A),
    "--project", PROJ_A, "--detach",
], runner2.calls[1]
print("sweep-owed-0388 AC3 ok")

# ══════════════════════════════════════════════════════════════════════════════
# AC4 · two projects, one due check each — batch only the smaller-due_at one
# ══════════════════════════════════════════════════════════════════════════════
clear()
write("L-spec-writer-c1.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-c1", "project": "proj-x"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-c1", "criterion": "AC1",
       "wake_at": iso(hours=-5), "line": 1})
write("L-executor-c1.jsonl", {"ts": EPOCH, "type": "shipped", "subject": "L-spec-c1"})
write("L-spec-writer-c2.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-c2", "project": "proj-y"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-c2", "criterion": "AC1",
       "wake_at": iso(hours=-1), "line": 1})
write("L-executor-c2.jsonl", {"ts": EPOCH, "type": "shipped", "subject": "L-spec-c2"})

before4 = set(CONTENT.glob("L-sweep-*.md"))
runner4 = FakeRunner(packet_path=str(CONTENT / "packet-c.md"))
sweep_owed.run(dry_run=False, runner=runner4, now=NOW)
manifest4 = (set(CONTENT.glob("L-sweep-*.md")) - before4).pop()
parsed4 = rows_of(manifest4)
assert [r["spec"] for r in parsed4] == ["L-spec-c1"], parsed4
assert f"cwd: {dispatch.ROOT / 'repos' / 'proj-x'}" in manifest4.read_text()
print("sweep-owed-0388 AC4 ok")

# ══════════════════════════════════════════════════════════════════════════════
# AC5 · one open owed-sweeper spawn blocks batching entirely; past the window
# (twice the role's 45-minute cap) it no longer blocks
# ══════════════════════════════════════════════════════════════════════════════
clear()
write("L-spec-writer-d1.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-d1", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-d1", "criterion": "AC1",
       "wake_at": iso(hours=-2), "line": 1})
write("L-executor-d1.jsonl", {"ts": EPOCH, "type": "shipped", "subject": "L-spec-d1"})
write("L-owed-sweeper-d1.jsonl",
      {"ts": iso(minutes=-45), "type": "spawn-started", "role": "owed-sweeper",
       "subject": "L-sweep-d1", "spawn": "L-owed-sweeper-d1"})

before5 = set(CONTENT.glob("L-sweep-*.md"))
runner5 = FakeRunner()
sweep_owed.run(dry_run=False, runner=runner5, now=NOW)
assert set(CONTENT.glob("L-sweep-*.md")) == before5, "AC5: no manifest while a batch is open (45m < 90m)"
assert not runner5.calls, "AC5: no packet/dispatch call while a batch is open"

(EV / "L-owed-sweeper-d1.jsonl").unlink()
write("L-owed-sweeper-d1b.jsonl",
      {"ts": iso(minutes=-100), "type": "spawn-started", "role": "owed-sweeper",
       "subject": "L-sweep-d1b", "spawn": "L-owed-sweeper-d1b"})
runner5b = FakeRunner(packet_path=str(CONTENT / "packet-d.md"))
sweep_owed.run(dry_run=False, runner=runner5b, now=NOW)
assert set(CONTENT.glob("L-sweep-*.md")) != before5, "AC5: a 100-minute-old spawn (> 90m) no longer blocks"
print("sweep-owed-0388 AC5 ok")

# ══════════════════════════════════════════════════════════════════════════════
# AC6 · a recent owed-failed (6h ago) excludes a due check; the same shape at
# 13h ago includes it
# ══════════════════════════════════════════════════════════════════════════════
clear()
write("L-spec-writer-e1.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-e1", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-e1", "criterion": "AC1",
       "wake_at": iso(hours=-20), "line": 1})
write("L-executor-e1.jsonl",
      {"ts": EPOCH, "type": "shipped", "subject": "L-spec-e1"},
      {"ts": iso(hours=-6), "type": "owed-failed", "subject": "L-spec-e1", "criterion": "AC1",
       "evidence": "e", "kind": "unmet"})
_, specs6a = specs_now()
due6a = sweep_owed.due_candidates(specs6a, NOW)
assert not any(r["spec"] == "L-spec-e1" for r in due6a), "AC6: a 6h-old owed-failed excludes it"

write("L-executor-e1.jsonl",
      {"ts": EPOCH, "type": "shipped", "subject": "L-spec-e1"},
      {"ts": iso(hours=-13), "type": "owed-failed", "subject": "L-spec-e1", "criterion": "AC1",
       "evidence": "e", "kind": "unmet"})
_, specs6b = specs_now()
due6b = sweep_owed.due_candidates(specs6b, NOW)
assert any(r["spec"] == "L-spec-e1" for r in due6b), "AC6: a 13h-old owed-failed no longer excludes it"
print("sweep-owed-0388 AC6 ok")

# ══════════════════════════════════════════════════════════════════════════════
# AC7 · a droplet owed-unobservable (3 days ago) excludes a due check
# permanently — distinct from AC6's 12h window
# ══════════════════════════════════════════════════════════════════════════════
clear()
write("L-spec-writer-f1.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-f1", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-f1", "criterion": "AC1",
       "wake_at": iso(minutes=-5), "line": 1})
write("L-executor-f1.jsonl",
      {"ts": EPOCH, "type": "shipped", "subject": "L-spec-f1"},
      {"ts": iso(days=-3), "type": "owed-unobservable", "subject": "L-spec-f1", "criterion": "AC1",
       "capability": "droplet", "why": "no reach"})
_, specs7 = specs_now()
rows7 = [r for s in specs7.values() for r in owed.checks(s["evs"], NOW) if r["spec"] == "L-spec-f1"]
assert rows7 and rows7[0]["status"] == "due", rows7
due7 = sweep_owed.due_candidates(specs7, NOW)
assert not any(r["spec"] == "L-spec-f1" for r in due7), \
    "AC7: a droplet-capability owed-unobservable excludes permanently, regardless of age"
print("sweep-owed-0388 AC7 ok")

# ══════════════════════════════════════════════════════════════════════════════
# AC8/AC9/AC10/AC11 · the expiry escalation, its dedupe, and --dry-run
# ══════════════════════════════════════════════════════════════════════════════
clear()
write("L-spec-writer-g1.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-g1", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-g1", "criterion": "AC1",
       "wake_at": EPOCH, "line": 1})
write("L-executor-g1.jsonl", {"ts": EPOCH, "type": "shipped", "subject": "L-spec-g1"})

before8 = {p.name: p.stat().st_size for p in EV.glob("*.jsonl")}
sweep_owed.run(dry_run=False, runner=FakeRunner(), now=NOW)
after8 = list(EV.glob("*.jsonl"))
changed8 = [p.name for p in after8
           if p.name not in before8 or p.stat().st_size != before8.get(p.name)]
assert changed8 == [sweep_owed.LEDGER_NAME], \
    f"AC8: exactly one events/*.jsonl file changes — the driver's own hardcoded ledger: {changed8}"
raw8 = [json.loads(l) for l in sweep_ledger.read_text().splitlines()]
esc8 = [e for e in raw8 if e["type"] == "escalation-blocking" and e.get("subject") == "L-spec-g1"]
assert len(esc8) == 1, esc8
e8 = esc8[0]
assert e8["kind"] == "owed-expired", e8
assert json.loads(e8["criteria"]) == ["AC1"], e8
assert isinstance(json.loads(e8["evidence"]), list), e8
assert e8["default"] == sweep_owed.EXPIRED_DEFAULT, e8
assert e8["revert"] == sweep_owed.EXPIRED_REVERT, e8
deadline8 = datetime.fromisoformat(e8["deadline"])
assert abs((deadline8 - (NOW + timedelta(hours=72))).total_seconds()) < 60, e8
folded_esc8 = next(e for e in fold.read_events()
                   if e["type"] == "escalation-blocking" and e.get("subject") == "L-spec-g1")
assert folded_esc8["actor"] == "sweep", \
    f"AC8: the fold-derived (filename-only) actor of L-sweep-local.jsonl must be 'sweep': {folded_esc8}"
print("sweep-owed-0388 AC8 ok")

before9 = sweep_ledger.read_text()
sweep_owed.run(dry_run=False, runner=FakeRunner(), now=NOW)
after9 = sweep_ledger.read_text()
assert after9 == before9, "AC9: a second run against the same still-expired check appends nothing new"
print("sweep-owed-0388 AC9 ok")

clear()
write("L-spec-writer-h1.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-h1", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-h1", "criterion": "A", "wake_at": EPOCH, "line": 1},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-h1", "criterion": "B", "wake_at": EPOCH, "line": 2})
write("L-executor-h1.jsonl", {"ts": EPOCH, "type": "shipped", "subject": "L-spec-h1"})
write(sweep_owed.LEDGER_NAME,
      {"ts": iso(days=-1), "type": "escalation-blocking", "subject": "L-spec-h1", "kind": "owed-expired",
       "criteria": json.dumps(["A"]), "evidence": json.dumps([""]),
       "default": sweep_owed.EXPIRED_DEFAULT, "revert": sweep_owed.EXPIRED_REVERT,
       "deadline": iso(hours=71)})
sweep_owed.run(dry_run=False, runner=FakeRunner(), now=NOW)
raw10 = [json.loads(l) for l in sweep_ledger.read_text().splitlines()]
esc10 = [e for e in raw10 if e["type"] == "escalation-blocking" and e.get("subject") == "L-spec-h1"]
assert len(esc10) == 2, esc10
assert json.loads(esc10[0]["criteria"]) == ["A"], "AC10: the pre-existing A escalation is untouched"
assert json.loads(esc10[1]["criteria"]) == ["B"], \
    f"AC10: exactly one new escalation, naming only the newly-expired B, never re-listing A: {esc10[1]}"
print("sweep-owed-0388 AC10 ok")

clear()
write("L-spec-writer-i1.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-i1", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-i1", "criterion": "AC1", "wake_at": EPOCH, "line": 1})
write("L-executor-i1.jsonl", {"ts": EPOCH, "type": "shipped", "subject": "L-spec-i1"})
before11 = sweep_ledger.read_text() if sweep_ledger.exists() else ""
lines11 = sweep_owed.run(dry_run=True, runner=FakeRunner(), now=NOW)
after11 = sweep_ledger.read_text() if sweep_ledger.exists() else ""
assert after11 == before11, "AC11: a dry-run appends no escalation-blocking event"
assert any("L-spec-i1" in l and "owed-expired" in l for l in lines11), \
    f"AC11: a dry-run must still print the escalation it would have appended: {lines11}"
print("sweep-owed-0388 AC11 ok")

# ══════════════════════════════════════════════════════════════════════════════
# AC15 · agents/executor.md's owed row hands the work to the sweeper
# ══════════════════════════════════════════════════════════════════════════════
exec_text = (REPO_ROOT / "agents" / "executor.md").read_text()
assert "run the criterion's own declared observation" not in exec_text, \
    "AC15: the old phrase must be gone"
SD8_SENTENCE = ("owed checks are swept by `owed-sweeper` (`doit sweep-owed`); not yours to run. "
               "You may still record `owed-met` from evidence you already hold, and you may re-date.")
assert SD8_SENTENCE in exec_text, "AC15: the SD8 sentence must be present, verbatim"
print("sweep-owed-0388 AC15 ok")

# ══════════════════════════════════════════════════════════════════════════════
# AC16 · crons.toml carries the sweep-owed row, parsed with the real crons.rows()
# ══════════════════════════════════════════════════════════════════════════════
rows16 = crons.rows()
row16 = next((r for r in rows16 if r["name"] == "sweep-owed"), None)
assert row16 is not None, "AC16: crons.toml must carry a [[row]] named sweep-owed"
tokens16 = crons._tokens()
assert row16["where"] == "user", row16
assert row16["schedule"] == "*/30 * * * *", row16
assert "sweep-owed" in row16["command"] and tokens16["doit"] in row16["command"], row16
assert row16["path"] == tokens16["doit"], row16
assert "doit sweep-owed" in row16["sig"], row16
assert row16["owner"] == "thinker", row16
assert row16["project"] == "do-it-v2", row16
print("sweep-owed-0388 AC16 ok")

# ══════════════════════════════════════════════════════════════════════════════
# AC17 · `doit` carries a sweep-owed) case line; bash -n; a real subprocess run
# ══════════════════════════════════════════════════════════════════════════════
DOIT_SCRIPT = REPO_ROOT / "doit"
doit_text = DOIT_SCRIPT.read_text()
assert re.search(r"^\s*sweep-owed\)", doit_text, re.M), \
    "AC17: doit must carry a sweep-owed) case line"
assert "sweep_owed.py" in doit_text, "AC17: the case line must invoke src/sweep_owed.py"
bashn = subprocess.run(["bash", "-n", str(DOIT_SCRIPT)], capture_output=True, text=True)
assert bashn.returncode == 0, bashn.stderr

iso_root = pathlib.Path(tempfile.mkdtemp())
(iso_root / "events").mkdir()
env17 = {**os.environ, "DOIT_ROOT": str(iso_root)}
r17 = subprocess.run([str(DOIT_SCRIPT), "sweep-owed", "--dry-run"],
                     capture_output=True, text=True, env=env17)
assert r17.returncode == 0, (r17.returncode, r17.stdout, r17.stderr)
print("sweep-owed-0388 AC17 ok")
