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
import hashlib, json, os, pathlib, re, subprocess, sys, tempfile
from contextlib import ExitStack
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["HOME"] = str(TMP / "home")
HOME_ORPHANS = pathlib.Path.home() / ".do-it/state/sweep-owed-orphans.json"
HOME_ORPHANS_BEFORE = None

os.environ["DOIT_ROOT"] = str(TMP)
# L-spec-9004: a real, disposable DOIT_SCRATCH — never nested under TMP/DOIT_ROOT
# above (scratch.root() refuses a resolved root at/under DOIT_ROOT) — mirroring
# test_crons.py's/test_launch.py's own convention.
os.environ["DOIT_SCRATCH"] = str(pathlib.Path(tempfile.mkdtemp(prefix="sweep-owed-test-scratch-")))
os.environ.pop("DOIT_PROJECT", None)
os.environ.pop("DOIT_LEDGER_FILE", None)          # AC8: the driver must not depend on this
(TMP / "events").mkdir(parents=True, exist_ok=True)
(TMP / "content").mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import crons, dispatch, fold, owed, scratch, sweep_owed  # noqa: E402

# Collection may have already imported these modules for another fixture root.
ROOT_PATCHES = ExitStack()
ROOT_PATCHES.enter_context(patch.object(fold, "PROJECT", None))
for module in (dispatch, fold, sweep_owed.packet):
    for attr in ("ROOT", "CONTENT", "EVENTS", "PACKETS"):
        if hasattr(module, attr):
            ROOT_PATCHES.enter_context(patch.object(module, attr, TMP if attr == "ROOT" else TMP / attr.lower()))

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime.now(timezone.utc).replace(microsecond=0)
EV = TMP / "events"
CONTENT = TMP / "content"
EPOCH = (NOW - timedelta(days=3650)).isoformat(timespec="seconds")


def iso(**kw):
    return (NOW + timedelta(**kw)).isoformat(timespec="seconds")


def write(name, *events):
    for e in events:
        if e.get("type") == "spec-written":
            sp = dispatch.spec_path(e["subject"])
            if not sp.exists():
                criteria = [r["criterion"] for r in events if r.get("type") == "owed-ac"] or ["AC1"]
                sp.write_text("## Acceptance criteria\n" + "".join(
                    f"{c} [backend]: fixture check\n" for c in criteria))
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
SCRATCH_ROOT = pathlib.Path(os.environ["DOIT_SCRATCH"])
before_scratch1 = set(SCRATCH_ROOT.rglob("*")) if SCRATCH_ROOT.exists() else set()
runner1 = FakeRunner()
lines1 = sweep_owed.run(dry_run=True, runner=runner1, now=NOW)
after1 = {p.name: p.read_text() for p in EV.glob("*.jsonl")}
after_scratch1 = set(SCRATCH_ROOT.rglob("*")) if SCRATCH_ROOT.exists() else set()
assert "sweep-owed: resolvable: 1 resolved, 0 unresolved" in lines1, lines1
assert after1 == before1, "AC1: a dry-run must append nothing to any ledger file"
assert not runner1.calls, "AC1: a dry-run never calls packet/dispatch"
assert not list(CONTENT.glob("L-sweep-*.md")), "AC1: a dry-run allocates no manifest"
# AC14 (L-spec-9004): a --dry-run drive creates no directory under scratch.root()
# at all — no "sweeps" top-level entry, no per-sweep subdirectory.
assert after_scratch1 == before_scratch1, "AC14: a dry-run must create no new scratch entry"
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
# AC13 (L-spec-9004): --cwd is a fresh, private scratch.sub("sweeps")/<sweep-id>
# directory, mode 0o700, existing on disk — never the shared $R/repos/<project>
# checkout the manifest's own `cwd:` field agreed with before this change.
expected_cwd2 = str(scratch.sub("sweeps") / sweep_id2)
assert expected_cwd2 != str(dispatch.ROOT / "repos" / PROJ_A), \
    "AC13: never the shared checkout"
assert pathlib.Path(expected_cwd2).is_dir(), expected_cwd2
assert (os.stat(expected_cwd2).st_mode & 0o777) == 0o700, oct(os.stat(expected_cwd2).st_mode)
assert f"cwd: {expected_cwd2}" in manifest2.read_text(), "AC13: manifest cwd: field must agree"
assert runner2.calls[1] == [
    str(sweep_owed.doit_bin()), "dispatch", "owed-sweeper", sweep_id2,
    "--packet", runner2.packet_path, "--cwd", expected_cwd2,
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
sweep_id4 = manifest4.stem
assert f"cwd: {scratch.sub('sweeps') / sweep_id4}" in manifest4.read_text()
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
# AC6 · a failed row stays out of the due-only sweep, at 6h and at 13h
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
assert not any(r["spec"] == "L-spec-e1" for r in due6b), "AC6: a 13h-old failure stays failed"
assert owed.checks(specs6b["L-spec-e1"]["evs"], NOW)[0]["status"] == "failed"
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
assert rows7 and rows7[0]["status"] == "blocked", rows7
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
write("L-executor-g1.jsonl", {"ts": EPOCH, "type": "shipped", "subject": "L-spec-g1"},
      {"ts": iso(hours=-1), "type": "owed-unobservable", "subject": "L-spec-g1",
       "criterion": "AC1", "capability": "zzz", "why": "no route"})

before8 = {p.name: p.stat().st_size for p in EV.glob("*.jsonl")}
sweep_owed.run(dry_run=False, runner=FakeRunner(), now=NOW)
after8 = list(EV.glob("*.jsonl"))
changed8 = [p.name for p in after8
           if p.name not in before8 or p.stat().st_size != before8.get(p.name)]
assert changed8 == [sweep_owed.LEDGER_NAME], \
    f"AC8: exactly one events/*.jsonl file changes — the driver's own hardcoded ledger: {changed8}"
raw8 = [json.loads(l) for l in sweep_ledger.read_text().splitlines()]
esc8 = [e for e in raw8 if e["type"] == "escalation-blocking" and e.get("subject") == "L-spec-g1"]
assert esc8 == [], "0682: an open operator ask prevents an expiry escalation"
assert [(r["spec"], r["criterion"]) for r in __import__("owed_asks").queue(fold.read_events(), NOW)] == [
    ("L-spec-g1", "AC1")], "0682: the waiting check remains visible as an operator ask"
print("sweep-owed-0388 AC8 ok")

before9 = sweep_ledger.read_text()
sweep_owed.run(dry_run=False, runner=FakeRunner(), now=NOW)
after9 = sweep_ledger.read_text()
assert after9[len(before9):].count('"type": "sweep-run"') == 1
assert '"escalation-blocking"' not in after9[len(before9):]
print("sweep-owed-0388 AC9 ok")

clear()
write("L-spec-writer-h1.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-h1", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-h1", "criterion": "A", "wake_at": EPOCH, "line": 1},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-h1", "criterion": "B", "wake_at": EPOCH, "line": 2})
write("L-executor-h1.jsonl", {"ts": EPOCH, "type": "shipped", "subject": "L-spec-h1"},
      *[{"ts": iso(hours=-1), "type": "owed-unobservable", "subject": "L-spec-h1",
         "criterion": c, "capability": "zzz", "why": "no route"} for c in ("A", "B")])
write(sweep_owed.LEDGER_NAME,
      {"ts": iso(days=-1), "type": "escalation-blocking", "subject": "L-spec-h1", "kind": "owed-expired",
       "criteria": json.dumps(["A"]), "evidence": json.dumps([""]),
       "default": sweep_owed.EXPIRED_DEFAULT, "revert": sweep_owed.EXPIRED_REVERT,
       "deadline": iso(hours=71)})
sweep_owed.run(dry_run=False, runner=FakeRunner(), now=NOW)
raw10 = [json.loads(l) for l in sweep_ledger.read_text().splitlines()]
esc10 = [e for e in raw10 if e["type"] == "escalation-blocking" and e.get("subject") == "L-spec-h1"]
assert len(esc10) == 1, esc10
assert json.loads(esc10[0]["criteria"]) == ["A"], "AC10: the pre-existing A escalation is untouched"
assert {r["criterion"] for r in __import__("owed_asks").queue(fold.read_events(), NOW)} == {"A", "B"}, \
    "0682: both checks keep their asks; neither adds an expiry escalation"
print("sweep-owed-0388 AC10 ok")

clear()
write("L-spec-writer-i1.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-i1", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-i1", "criterion": "AC1", "wake_at": EPOCH, "line": 1})
write("L-executor-i1.jsonl", {"ts": EPOCH, "type": "shipped", "subject": "L-spec-i1"},
      {"ts": iso(hours=-1), "type": "owed-unobservable", "subject": "L-spec-i1",
       "criterion": "AC1", "capability": "zzz", "why": "no route"})
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

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0432 (L-charter-0042 R6/SD13) · sweep_owed.answered_specs and the
# exclusion it drives in sweep_expired/due_candidates
# ══════════════════════════════════════════════════════════════════════════════

# ── AC7 · SPEC_A answered by a later same-spec decision, no ref=; SPEC_B, an
# identical unanswered escalation, is not ─────────────────────────────────────
clear()
T0_7, T1_7 = iso(hours=-50), iso(hours=-40)
write("L-owed-sweeper-432g.jsonl",
      {"ts": T0_7, "type": "escalation-blocking", "subject": "L-spec-432g-a", "kind": "owed-expired",
       "criteria": json.dumps(["OTHER"]), "evidence": json.dumps([""]),
       "default": sweep_owed.EXPIRED_DEFAULT, "revert": sweep_owed.EXPIRED_REVERT,
       "deadline": iso(hours=22)},
      {"ts": T0_7, "type": "escalation-blocking", "subject": "L-spec-432g-b", "kind": "owed-expired",
       "criteria": json.dumps(["OTHER"]), "evidence": json.dumps([""]),
       "default": sweep_owed.EXPIRED_DEFAULT, "revert": sweep_owed.EXPIRED_REVERT,
       "deadline": iso(hours=22)})
write("L-spec-writer-432g.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-432g-a", "project": "proj-a"},
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-432g-b", "project": "proj-a"})
write("L-executor-432g.jsonl",
      {"ts": EPOCH, "type": "shipped", "subject": "L-spec-432g-a"},
      {"ts": EPOCH, "type": "shipped", "subject": "L-spec-432g-b"},
      {"ts": T1_7, "type": "decision", "subject": "L-spec-432g-a", "why": "answers a, no ref"})
_, specs7_432 = specs_now()
answered7 = sweep_owed.answered_specs(specs7_432)
assert "L-spec-432g-a" in answered7 and "L-spec-432g-b" not in answered7, answered7
print("answered-0432 AC7 ok")

# ── AC8 · a further criterion whose governing owed-ac predates the answer,
# already expired, is not (re-)escalated ─────────────────────────────────────
clear()
T0_8, T1_8 = iso(hours=-50), iso(hours=-40)
write("L-owed-sweeper-432h.jsonl",
      {"ts": T0_8, "type": "escalation-blocking", "subject": "L-spec-432h", "kind": "owed-expired",
       "criteria": json.dumps(["OTHER"]), "evidence": json.dumps([""]),
       "default": sweep_owed.EXPIRED_DEFAULT, "revert": sweep_owed.EXPIRED_REVERT,
       "deadline": iso(hours=22)})
write("L-spec-writer-432h.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-432h", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-432h", "criterion": "C_before",
       "wake_at": iso(hours=-240), "line": 1})
write("L-executor-432h.jsonl",
      {"ts": EPOCH, "type": "shipped", "subject": "L-spec-432h"},
      {"ts": T1_8, "type": "decision", "subject": "L-spec-432h", "why": "answers, no ref"},
      {"ts": iso(hours=-1), "type": "owed-unobservable", "subject": "L-spec-432h",
       "criterion": "C_before", "capability": "zzz", "why": "no route"})
_, specs8_432 = specs_now()
rows8_432 = [r for r in owed.checks(specs8_432["L-spec-432h"]["evs"], NOW) if r["criterion"] == "C_before"]
assert rows8_432 and rows8_432[0]["status"] == "expired", rows8_432
before8_432 = sweep_ledger.read_text() if sweep_ledger.exists() else ""
lines8_432 = sweep_owed.sweep_expired(specs8_432, NOW)
after8_432 = sweep_ledger.read_text() if sweep_ledger.exists() else ""
assert after8_432 == before8_432, "AC8: no new escalation-blocking appended for C_before"
assert not any("L-spec-432h" in l for l in lines8_432), lines8_432
print("answered-0432 AC8 ok")

# ── AC9 · the same criterion re-dated strictly AFTER the answer — expired
# status unchanged, but IS escalated (a later owed-ac re-date puts it back) ──
write("L-spec-writer-432h.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-432h", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-432h", "criterion": "C_before",
       "wake_at": iso(hours=-240), "line": 1},
      {"ts": iso(hours=-30), "type": "owed-ac", "subject": "L-spec-432h", "criterion": "C_before",
       "wake_at": iso(hours=-240), "line": 1})
_, specs9_432 = specs_now()
rows9_432 = [r for r in owed.checks(specs9_432["L-spec-432h"]["evs"], NOW) if r["criterion"] == "C_before"]
assert rows9_432 and rows9_432[0]["status"] == "expired", rows9_432
lines9_432 = sweep_owed.sweep_expired(specs9_432, NOW)
assert any("L-spec-432h" in l and "C_before" in l for l in lines9_432), lines9_432
raw9_432 = [json.loads(l) for l in sweep_ledger.read_text().splitlines()] if sweep_ledger.exists() else []
esc9_432 = [e for e in raw9_432 if e["type"] == "escalation-blocking" and e.get("subject") == "L-spec-432h"
           and "C_before" in json.loads(e["criteria"])]
assert esc9_432 == [], "0682: re-dating an open ask does not remove its expiry protection"
assert any(r["spec"] == "L-spec-432h" and r["criterion"] == "C_before"
           for r in __import__("owed_asks").queue(fold.read_events(), NOW)), "the re-dated ask stays visible"
print("answered-0432 AC9 ok")

# ── AC10 · a `due` (not expired) criterion, governing owed-ac before the
# answer, stays in due_candidates (SD11); so does an unrelated spec ─────────
clear()
T0_10, T1_10 = iso(hours=-50), iso(hours=-40)
write("L-owed-sweeper-432i.jsonl",
      {"ts": T0_10, "type": "escalation-blocking", "subject": "L-spec-432i", "kind": "owed-expired",
       "criteria": json.dumps(["OTHER"]), "evidence": json.dumps([""]),
       "default": sweep_owed.EXPIRED_DEFAULT, "revert": sweep_owed.EXPIRED_REVERT,
       "deadline": iso(hours=22)})
write("L-spec-writer-432i.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-432i", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-432i", "criterion": "C_due",
       "wake_at": iso(hours=-1), "line": 1})
write("L-executor-432i.jsonl",
      {"ts": EPOCH, "type": "shipped", "subject": "L-spec-432i"},
      {"ts": T1_10, "type": "decision", "subject": "L-spec-432i", "why": "answers, no ref"})
write("L-spec-writer-432ic.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-432i-c", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-432i-c", "criterion": "C_due",
       "wake_at": iso(hours=-1), "line": 1})
write("L-executor-432ic.jsonl", {"ts": EPOCH, "type": "shipped", "subject": "L-spec-432i-c"})
_, specs10_432 = specs_now()
rows10_432 = [r for r in owed.checks(specs10_432["L-spec-432i"]["evs"], NOW) if r["criterion"] == "C_due"]
assert rows10_432 and rows10_432[0]["status"] == "due", rows10_432
due10_432 = sweep_owed.due_candidates(specs10_432, NOW)
assert any(r["spec"] == "L-spec-432i" for r in due10_432), due10_432
assert any(r["spec"] == "L-spec-432i-c" for r in due10_432), \
    "AC10: an unrelated, unanswered spec's due row is present"
print("answered-0432 AC10 ok")

# ── AC11 · a live (non-dry-run) run: no `escalated SPEC_A` line, no
# packet/dispatch call naming SPEC_A's criterion; an unrelated due check IS
# batched and dispatched ─────────────────────────────────────────────────────
clear()
T0_11, T1_11 = iso(hours=-50), iso(hours=-40)
write("L-owed-sweeper-432j.jsonl",
      {"ts": T0_11, "type": "escalation-blocking", "subject": "L-spec-432j", "kind": "owed-expired",
       "criteria": json.dumps(["OTHER"]), "evidence": json.dumps([""]),
       "default": sweep_owed.EXPIRED_DEFAULT, "revert": sweep_owed.EXPIRED_REVERT,
       "deadline": iso(hours=22)})
write("L-spec-writer-432j.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-432j", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-432j", "criterion": "C_before",
       "wake_at": iso(hours=-240), "line": 1})
write("L-executor-432j.jsonl",
      {"ts": EPOCH, "type": "shipped", "subject": "L-spec-432j"},
      {"ts": T1_11, "type": "decision", "subject": "L-spec-432j", "why": "answers, no ref"},
      {"ts": iso(hours=-1), "type": "owed-unobservable", "subject": "L-spec-432j",
       "criterion": "C_before", "capability": "zzz", "why": "no route"})
write("L-spec-writer-432k.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-432k", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-432k", "criterion": "AC1",
       "wake_at": iso(hours=-1), "line": 1})
write("L-executor-432k.jsonl", {"ts": EPOCH, "type": "shipped", "subject": "L-spec-432k"})
before11_432 = set(CONTENT.glob("L-sweep-*.md"))
runner11_432 = FakeRunner(packet_path=str(CONTENT / "packet-432.md"))
lines11_432 = sweep_owed.run(dry_run=False, runner=runner11_432, now=NOW)
assert not any("escalated L-spec-432j" in l for l in lines11_432), lines11_432
new_manifests11 = set(CONTENT.glob("L-sweep-*.md")) - before11_432
assert len(new_manifests11) == 1, new_manifests11
parsed11 = rows_of(new_manifests11.pop())
assert [r["spec"] for r in parsed11] == ["L-spec-432k"], \
    f"AC11: the unrelated spec's due check is batched, SPEC_A's C_before is not: {parsed11}"
assert len(runner11_432.calls) == 2, \
    f"AC11: exactly one packet+dispatch pair, naming only the unrelated spec: {runner11_432.calls}"
print("answered-0432 AC11 ok")

# ── AC12 · the regression: SPEC_A answered at t1 via its OWN escalation e_old;
# a DIFFERENT criterion (C_before) is re-dated after t1 and already escalated,
# at t3, itself UNANSWERED — SPEC_A's own newest owed-expired escalation is now
# unanswered, and the naive "newest escalation, then check answered" reading
# would drop SPEC_A entirely. Two more never-escalated criteria (C_before2
# expired, C_due due) must stay excluded across TWO sweep runs. ─────────────
clear()
T0_12, T1_12, T2_12, T3_12 = iso(hours=-96), iso(hours=-80), iso(hours=-60), iso(hours=-50)
write("L-owed-sweeper-432m.jsonl",
      {"ts": T0_12, "type": "escalation-blocking", "subject": "L-spec-432m", "kind": "owed-expired",
       "criteria": json.dumps(["E_OLD"]), "evidence": json.dumps([""]),
       "default": sweep_owed.EXPIRED_DEFAULT, "revert": sweep_owed.EXPIRED_REVERT,
       "deadline": iso(hours=22)})
write("L-spec-writer-432m.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-432m", "project": "proj-a"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-432m", "criterion": "C_before",
       "wake_at": iso(hours=-240), "line": 1},
      {"ts": T2_12, "type": "owed-ac", "subject": "L-spec-432m", "criterion": "C_before",
       "wake_at": iso(hours=-240), "line": 1},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-432m", "criterion": "C_before2",
       "wake_at": iso(hours=-240), "line": 2},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-432m", "criterion": "C_due",
       "wake_at": iso(hours=-1), "line": 3})
write("L-executor-432m.jsonl",
      {"ts": EPOCH, "type": "shipped", "subject": "L-spec-432m"},
      {"ts": T1_12, "type": "decision", "subject": "L-spec-432m", "why": "answers e_old, no ref"},
      *[{"ts": iso(hours=-1), "type": "owed-unobservable", "subject": "L-spec-432m",
         "criterion": c, "capability": "zzz", "why": "no route"} for c in ("C_before", "C_before2")])
# C_before's own re-date (T2_12 > T1_12) is already expired and already
# escalated once, at T3_12 (> T2_12), with no later decision on this spec —
# individually UNANSWERED, and it is SPEC_A's own newest owed-expired escalation.
write("L-owed-sweeper-432m.jsonl",
      {"ts": T0_12, "type": "escalation-blocking", "subject": "L-spec-432m", "kind": "owed-expired",
       "criteria": json.dumps(["E_OLD"]), "evidence": json.dumps([""]),
       "default": sweep_owed.EXPIRED_DEFAULT, "revert": sweep_owed.EXPIRED_REVERT,
       "deadline": iso(hours=22)},
      {"ts": T3_12, "type": "escalation-blocking", "subject": "L-spec-432m", "kind": "owed-expired",
       "criteria": json.dumps(["C_before"]), "evidence": json.dumps([""]),
       "default": sweep_owed.EXPIRED_DEFAULT, "revert": sweep_owed.EXPIRED_REVERT,
       "deadline": iso(hours=22)})
_, specs12_432 = specs_now()
rows12_432 = {r["criterion"]: r for r in owed.checks(specs12_432["L-spec-432m"]["evs"], NOW)}
assert rows12_432["C_before"]["status"] == "expired", rows12_432["C_before"]
assert rows12_432["C_before2"]["status"] == "expired", rows12_432["C_before2"]
assert rows12_432["C_due"]["status"] == "due", rows12_432["C_due"]
answered12_432 = sweep_owed.answered_specs(specs12_432)
assert "L-spec-432m" in answered12_432, \
    "★ AC12: SPEC_A stays answered (via e_old) even though its newest owed-expired escalation is not"
runner12a_432 = FakeRunner()
lines12a_432 = sweep_owed.sweep_expired(specs12_432, NOW)
assert not any("C_before2" in l for l in lines12a_432), lines12a_432
due12a_432 = sweep_owed.due_candidates(specs12_432, NOW)
assert any(r["spec"] == "L-spec-432m" and r["criterion"] == "C_due" for r in due12a_432), due12a_432
# a second run, same ledger plus C_before's own already-landed escalation (no
# further writes): still no C_before2 escalation; C_due remains eligible
_, specs12b_432 = specs_now()
lines12b_432 = sweep_owed.sweep_expired(specs12b_432, NOW)
assert not any("C_before2" in l for l in lines12b_432), lines12b_432
due12b_432 = sweep_owed.due_candidates(specs12b_432, NOW)
assert any(r["spec"] == "L-spec-432m" and r["criterion"] == "C_due" for r in due12b_432), due12b_432
answered12b_432 = sweep_owed.answered_specs(specs12b_432)
assert "L-spec-432m" in answered12b_432, \
    "★ AC12: still answered (at t1, via e_old) after the second run"
print("answered-0432 AC12 ok")


# 2026-10-11 regression: the batch's read-only .env must exist BEFORE `doit packet owed-sweeper`
# runs, or the packet tells the sweeper "not available (capability=ro-dsn)" and the check is
# retired forever by _blocked_permanently. Uses a disposable checkout, never the real one.
_co = pathlib.Path(TMP) / "repos" / "proj-dsn"
_co.mkdir(parents=True, exist_ok=True)
_co.joinpath(".env").write_text("SUPABASE_DB_URL_RO=postgresql://ro_fixture@127.0.0.1:1/x\n")
_seen = {}


class _OrderRunner:
    def run(self, argv):
        if argv[1] == "packet":
            sid = argv[-1]
            d = scratch.sub("sweeps") / sid
            _seen["env_at_packet"] = (d / ".env").is_file()
            return subprocess.CompletedProcess(argv, 0, stdout=str(d / "packet.md"), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


sweep_owed.dispatch_batch("proj-dsn", [{"spec": "L-spec-432k", "criterion": "AC1", "spec_path": "x",
                                        "declared_src": "x", "line": 1}], _OrderRunner())
assert _seen.get("env_at_packet") is True, f"read-only .env not provisioned before packet build: {_seen}"
print("dsn-before-packet ok")


# L-spec-1004: regression for one unresolved row blocking an entire batch.
def fixture_1004(criteria=("AC1", "AC2", "AC3")):
    clear()
    write("L-spec-writer-1004.jsonl",
          {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-1004-fixture", "project": "proj-a"},
          *[{"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-1004-fixture",
             "criterion": c, "wake_at": iso(hours=-2), "line": 1} for c in criteria])
    write("L-executor-1004.jsonl",
          {"ts": EPOCH, "type": "shipped", "subject": "L-spec-1004-fixture"})
    sp = dispatch.spec_path("L-spec-1004-fixture")
    sp.write_text("## Acceptance criteria\nAC1 [backend]: present\nAC3 [backend]: present\n")
    return sp


def records_1004(kind):
    return [e for e in fold.read_events() if e["type"] == kind]


sp1004 = fixture_1004()
row1004 = {"spec": sp1004.stem, "criterion": "AC1"}
assert sweep_owed.resolvable(row1004) == (True, "")
assert not sweep_owed.resolvable({**row1004, "criterion": "AC2"})[0]
assert not sweep_owed.resolvable({**row1004, "spec_path": str(CONTENT / "missing.md")})[0]
with patch.object(pathlib.Path, "read_bytes", side_effect=PermissionError("unreadable fixture")):
    ok, reason = sweep_owed.resolvable(row1004)
    assert not ok and reason
print("sweep-unblock AC1 present/absent/missing/unreadable ok")

runner1004 = FakeRunner()
lines1004 = sweep_owed.run(runner=runner1004, now=NOW)
q1004 = records_1004("owed-quarantined")
assert len(q1004) == 1 and q1004[0]["criterion"] == "AC2"
assert q1004[0]["spec_sha256"] == hashlib.sha256(sp1004.read_bytes()).hexdigest()
assert len(q1004[0]["reason"]) <= 300
manifest1004 = next(CONTENT.glob("L-sweep-*.md"))
assert [r["criterion"] for r in rows_of(manifest1004)] == ["AC1", "AC3"]
assert len(runner1004.calls) == 2
assert "kind: plain\n" in manifest1004.read_text()
raw1004 = [json.loads(l) for l in sweep_ledger.read_text().splitlines()]
assert all("actor" not in e for e in raw1004)
runs1004 = records_1004("sweep-run")
assert len(runs1004) == 1 and runs1004[0]["outcome"] == "batched"
assert runs1004[0]["rows"] == 2 and runs1004[0]["quarantined"] == 1
fresh1004 = fold.fold(fold.read_events())[0]
reread1004 = {r["criterion"]: r for r in owed.checks(fresh1004[sp1004.stem]["evs"], NOW)}
assert reread1004["AC2"]["status"] == "quarantined"
assert reread1004["AC2"]["quarantine_reason"] == q1004[0]["reason"]
print("sweep-unblock AC2 AC11 store round trip; AC10 batched; AC13 writer ok")

with patch.object(sweep_owed, "resolvable", wraps=sweep_owed.resolvable) as resolve1004:
    sweep_owed.run(runner=FakeRunner(), now=NOW)
    assert all(c.args[0]["criterion"] != "AC2" for c in resolve1004.call_args_list)
assert len(records_1004("owed-quarantined")) == 1
sp1004.write_text(sp1004.read_text() + "\nchanged but AC2 still absent\n")
sweep_owed.run(runner=FakeRunner(), now=NOW)
assert len(records_1004("owed-quarantined")) == 2
sp1004.write_text(sp1004.read_text() + "AC2 [backend]: repaired\n")
with patch.object(dispatch, "emit", wraps=dispatch.emit) as emit1004:
    emit1004.side_effect = lambda dst, base, type_, **kw: (
        "fixture refusal" if type_ == "owed-routed" else emit1004._mock_wraps(dst, base, type_, **kw))
    refused1004 = sweep_owed.run(runner=FakeRunner(), now=NOW)
assert any("fixture refusal" in line for line in refused1004)
assert not records_1004("owed-routed")
before_recheck1004 = {p.name: p.read_bytes() for p in EV.iterdir()}
sweep_owed.run(dry_run=True, runner=FakeRunner(), now=NOW)
assert before_recheck1004 == {p.name: p.read_bytes() for p in EV.iterdir()}
sweep_owed.run(runner=FakeRunner(), now=NOW)
assert len(records_1004("owed-routed")) == 1
fresh1004 = fold.fold(fold.read_events())[0]
assert next(r for r in owed.checks(fresh1004[sp1004.stem]["evs"], NOW)
            if r["criterion"] == "AC2")["status"] in ("due", "waiting")
print("sweep-unblock AC3 dedupe/hash change; AC4 recheck/refusal ok")

fixture_1004(("AC2",))
solo1004 = sweep_owed.run(runner=FakeRunner(), now=NOW)
assert len(records_1004("owed-quarantined")) == 1
assert any("nothing" in line for line in solo1004)
assert records_1004("sweep-run")[0]["outcome"] == "nothing-due"
sweep_owed.run(runner=FakeRunner(), now=NOW)
assert len(records_1004("owed-quarantined")) == 1
print("sweep-unblock AC2 single row; AC10 nothing-due ok")

state1004 = TMP / "state"
state1004.mkdir(exist_ok=True)
legacy1004 = state1004 / "sweep-owed-orphans.json"
retired1004 = state1004 / "sweep-owed-orphans.retired.json"
sp1004 = fixture_1004()
legacy1004.write_text(json.dumps([f"{sp1004.stem}/AC2", f"{sp1004.stem}/AC1", "L-spec-no-events/AC1"]))
before1004 = {p.name: p.read_bytes() for p in EV.iterdir()}
state_before1004 = {p.name: p.read_bytes() for p in state1004.iterdir()}
dry1004 = sweep_owed.run(dry_run=True, runner=FakeRunner(), now=NOW)
assert "sweep-owed: resolvable: 2 resolved, 1 unresolved" in dry1004
assert sum("DRY RUN would quarantine" in line for line in dry1004) == 1
assert not any("AC2" in line for line in dry1004 if "would batch" in line)
assert before1004 == {p.name: p.read_bytes() for p in EV.iterdir()}
assert state_before1004 == {p.name: p.read_bytes() for p in state1004.iterdir()}
assert not retired1004.exists()
with patch.object(sweep_owed, "resolvable", wraps=sweep_owed.resolvable) as resolve1004:
    sweep_owed.run(runner=FakeRunner(), now=NOW)
    keys1004 = [(c.args[0]["spec"], c.args[0]["criterion"]) for c in resolve1004.call_args_list]
    assert keys1004.count((sp1004.stem, "AC2")) == 1
assert not legacy1004.exists() and retired1004.exists()
assert len(records_1004("owed-quarantined")) == 1
print("sweep-unblock AC5 root isolation/retirement; AC8 counts; AC15 byte-identical dry run ok")

fixture_1004(("AC1",))
for fail1004 in (0, 1):
    sweep_owed.run(runner=FakeRunner(fail_at=fail1004), now=NOW)
    assert records_1004("sweep-run")[-1]["outcome"] == "failed"
    assert records_1004("sweep-run")[-1]["reason"]
write("L-sweep-1004-open.jsonl", {"type": "spawn-started", "subject": "L-sweep-open",
      "role": "owed-sweeper", "spawn": "L-owed-sweeper-open"})
sweep_owed.run(runner=FakeRunner(), now=NOW)
assert records_1004("sweep-run")[-1]["outcome"] == "open"
assert len(records_1004("sweep-run")) == 3
print("sweep-unblock AC10 packet/dispatch failure and open ok")

candidates1004 = [{"spec": "a", "criterion": f"AC{i}", "project": "A", "due_at": NOW - timedelta(hours=i)}
                  for i in range(12)]
candidates1004 += [{"spec": "b", "criterion": "AC1", "project": "B", "due_at": NOW}]
start1004 = {"type": "spawn-started", "role": "owed-sweeper", "project": "A", "ts": iso(hours=-3)}
assert sweep_owed._batch_for(candidates1004, [start1004])[0] == "B"
older1004 = {**start1004, "project": "B", "ts": iso(hours=-5)}
assert sweep_owed._batch_for(candidates1004, [start1004, older1004])[0] == "B"
project1004, batch1004 = sweep_owed._batch_for(candidates1004, [])
assert project1004 == "A" and len(batch1004) == sweep_owed.MAX_BATCH
assert [r["criterion"] for r in batch1004][:2] == ["AC11", "AC10"]
assert sweep_owed._batch_for(candidates1004[:-1], [start1004])[0] == "A"
print("sweep-unblock AC12 fairness, ordering and batch cap ok")

fixture_1004(("AC4", "AC7"))
write("L-sweep-1004-answer.jsonl", {
    "ts": iso(hours=-50), "type": "escalation-blocking", "subject": "L-spec-1004-fixture",
    "kind": "owed-expired", "criteria": json.dumps(["AC4"]), "evidence": json.dumps([""]),
    "default": sweep_owed.EXPIRED_DEFAULT, "revert": sweep_owed.EXPIRED_REVERT, "deadline": iso(hours=22)})
write("L-operator-1004-answer.jsonl", {"ts": iso(hours=-40), "type": "decision",
      "subject": "L-spec-1004-fixture", "why": "answered AC4"})
_, answer_specs1004 = specs_now()
assert {r["criterion"] for r in sweep_owed.due_candidates(answer_specs1004, NOW)} == {"AC4", "AC7"}
write("L-sweep-1004-expired.jsonl", {"ts": iso(hours=-1), "type": "owed-unobservable",
      "subject": "L-spec-1004-fixture", "criterion": "AC4", "capability": "unknown", "why": "unavailable"})
_, answer_specs1004 = specs_now()
assert next(r for r in owed.checks(answer_specs1004["L-spec-1004-fixture"]["evs"], NOW)
            if r["criterion"] == "AC4")["status"] == "expired"
assert not sweep_owed.sweep_expired(answer_specs1004, NOW)
print("sweep-unblock AC9 answered AC4 and unnamed AC7 due; expired AC4 deduped ok")

clear()
assert "sweep-owed: resolvable: 0 resolved, 0 unresolved" in sweep_owed.run(dry_run=True, now=NOW)
if HOME_ORPHANS_BEFORE is not None:
    assert (HOME_ORPHANS.read_bytes(), HOME_ORPHANS.stat().st_mtime_ns) == HOME_ORPHANS_BEFORE
    print("sweep-unblock home orphans bytes/mtime unchanged")
else:
    assert not HOME_ORPHANS.exists()
    print("sweep-unblock home orphans check skipped: absent")
# L-spec-1005 AC5/6: failure escalation is step 1b even with an open batch;
# the production run is inert in dry-run and survives a refused append.
clear()
write("L-spec-writer-failures.jsonl",
      {"ts": EPOCH, "type": "spec-written", "subject": "L-spec-9905"},
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-9905", "criterion": "AC1",
       "wake_at": iso(hours=-20)})
write("L-executor-failures.jsonl",
      {"ts": EPOCH, "type": "shipped", "subject": "L-spec-9905"},
      {"ts": iso(hours=-2), "type": "owed-failed", "subject": "L-spec-9905",
       "criterion": "AC1", "kind": "unmet", "evidence": "fixture failed"})
write("L-dispatch-failures.jsonl", {"type": "spawn-started", "subject": "L-sweep-9905",
                                  "role": "owed-sweeper", "spawn": "L-spawn-9905"})
before_failures = {p.name: p.read_bytes() for p in EV.glob("*.jsonl")}
failure_lines = sweep_owed.run(dry_run=True, now=NOW, runner=FakeRunner())
assert "DRY RUN would escalate L-spec-9905 kind=owed-failed" in failure_lines[0]
assert any("already open" in line for line in failure_lines)
assert before_failures == {p.name: p.read_bytes() for p in EV.glob("*.jsonl")}
with patch.object(dispatch, "emit", return_value="fixture refusal"):
    failure_lines = sweep_owed.run(now=NOW, runner=FakeRunner())
assert failure_lines[0] == "sweep-owed: escalation for L-spec-9905 refused: fixture refusal"
assert any("already open" in line for line in failure_lines)
# Pin the call ordering itself, including the path where batching is allowed.
for busy in (True, False):
    order = []
    with patch.object(sweep_owed, "sweep_expired", side_effect=lambda *a, **kw: order.append("expired") or []), \
         patch.object(sweep_owed.owed_failures, "escalate_failures",
                      side_effect=lambda *a, **kw: order.append("failures") or []), \
         patch.object(sweep_owed, "close_unverifiable", side_effect=lambda *a, **kw: order.append("close") or []), \
         patch.object(sweep_owed, "open_batch", side_effect=lambda *a: order.append("batch") or busy):
        sweep_owed.run(dry_run=True, now=NOW, runner=FakeRunner())
    assert order == ["expired", "failures", "close", *(["batch"] * (2 if busy else 3))]
clear()
write("L-spec-writer-expired.jsonl",
      {"ts": EPOCH, "type": "owed-ac", "subject": "L-spec-9905", "criterion": "AC1",
       "wake_at": iso(hours=-20)})
write("L-executor-expired.jsonl",
      {"ts": EPOCH, "type": "shipped", "subject": "L-spec-9905"},
      {"ts": iso(hours=-2), "type": "owed-unobservable", "subject": "L-spec-9905",
       "criterion": "AC1", "capability": "unknown", "why": "unavailable"})
_, expired_specs = specs_now()
sweep_owed.sweep_expired(expired_specs, NOW)
assert not any(e.get("kind") == "owed-expired" for e in fold.read_events()), \
    "0682: a check waiting on an operator act must not expire silently"
assert [(r["spec"], r["criterion"]) for r in __import__("owed_asks").queue(fold.read_events(), NOW)] == [
    ("L-spec-9905", "AC1")], "0682: the waiting check remains an explicit ask"
assert sweep_owed.EXPIRED_REVERT == "an owed-met (waive) or an Executor re-date puts a check back"
print("L1005 AC5 AC6 ok")

# L1113: real no-runner closes, cap, and fresh-process fold admission.
def backlog_fixture(sid="L-spec-9913", days=15, capability="operator-action", status="due"):
    wake = iso(days=-days) if days is not None else None
    write(f"L-spec-writer-{sid.rsplit('-', 1)[-1]}.jsonl",
          {"type": "spec-written", "subject": sid, "ts": EPOCH, "project": PROJ_A},
          {"type": "owed-ac", "subject": sid, "ts": EPOCH,
           "criterion": "AC1", "wake_at": wake})
    write(f"L-executor-{sid.rsplit('-', 1)[-1]}.jsonl", {"type": "shipped", "subject": sid, "ts": EPOCH})
    extras = []
    if status in ("blocked", "expired"):
        extras.append({"type": "owed-unobservable", "capability": capability, "why": "fixture"})
    else:
        extras.append({"type": "owed-routed", "route": "relabel", "capability": capability})
    if status == "quarantined":
        write(f"L-sweep-{sid.rsplit('-', 1)[-1]}.jsonl",
              {"type": "owed-quarantined", "subject": sid, "criterion": "AC1",
               "ts": iso(minutes=-30), "reason": "fixture", "spec_sha256": "fixture"})
    elif status in ("failed", "met", "unverifiable"):
        extras.append({"type": f"owed-{status}", "evidence": "fixture", "reason": "fixture"})
    write(f"L-operator-{sid.rsplit('-', 1)[-1]}.jsonl", *({"subject": sid, "criterion": "AC1", "ts": iso(hours=-1), **e}
                                     for e in extras))


def seed_closes(n, unverifiable=0):
    write("L-operator-month.jsonl", *[
        {"type": "owed-unverifiable" if i < unverifiable else ("owed-met" if i % 2 else "owed-waived"),
         "subject": "L-spec-9990", "criterion": f"AC{i}", "reason": "fixture", "evidence": "fixture"}
        for i in range(n)])


clear()
seed_closes(300)
for i, (days, status, cap, want) in enumerate([
        (16, "due", "operator-action", True), (15, "blocked", "operator-action", True),
        (15, "expired", "ro-dsn-grant", True), (13, "due", "operator-action", False),
        (14, "due", "operator-action", False), (15, "due", "other", False),
        (15, "quarantined", "operator-action", False), (15, "failed", "operator-action", False),
        (15, "met", "operator-action", False), (15, "unverifiable", "operator-action", False),
        (None, "due", "operator-action", False), (None, "due", "operator-action", False)]):
    sid = f"L-spec-{9910+i}"
    backlog_fixture(sid, days, cap, status)
    print("L1113 AC2", sid, days, status, "close" if want else "skip")
# An unshipped row is also undated and cannot close.
write("L-executor-9920.jsonl")
all_ev, fixture_specs = specs_now()
assert owed.checks(fixture_specs["L-spec-9920"]["evs"], NOW)[0]["due_at"] is None
assert owed.checks(fixture_specs["L-spec-9921"]["evs"], NOW)[0]["due_at"] is None
before = {p.name: p.read_bytes() for p in EV.glob("*.jsonl")}
preview = sweep_owed.close_unverifiable(all_ev, NOW, dry_run=True)
assert len([line for line in preview if "DRY RUN would close" in line]) == 3
assert before == {p.name: p.read_bytes() for p in EV.glob("*.jsonl")}
with patch.dict(os.environ, {"DOIT_LEDGER_FILE": str(EV / "L-operator-trap.jsonl")}):
    closed_lines = sweep_owed.close_unverifiable(all_ev, NOW)
closes = [e for e in fold.read_events() if e["type"] == "owed-unverifiable" and e["actor"] == "prod-runner"]
assert [e["subject"] for e in closes] == ["L-spec-9910", "L-spec-9911", "L-spec-9912"]
assert all("L-operator-0001" in e["reason"] and ("needs operator" in e["reason"] or "ro-dsn-grant" in e["reason"])
           and "due " in e["reason"] for e in closes)
assert not (EV / "L-operator-trap.jsonl").exists()
assert sweep_owed.close_unverifiable(fold.read_events(), NOW) == []
# Copy the same close to an unauthorized actor; a fresh interpreter reads both files.
raw_close = json.loads((EV / "L-prod-runner-local.jsonl").read_text().splitlines()[0])
assert "actor" not in raw_close
write("L-builder-forbidden.jsonl", raw_close)
reader = """
import json,sys
sys.path.insert(0,sys.argv[1])
import fold,owed
specs,_,ignored,_=fold.fold(fold.read_events())
row=owed.checks(specs['L-spec-9910']['evs'],fold.ts(sys.argv[2]))[0]
e=next(e for e in specs['L-spec-9910']['evs'] if e['type']=='owed-unverifiable')
print(json.dumps(dict(status=row['status'], actor=e['actor'], source=e['_src'],
                     dropped=sum(e['type']=='owed-unverifiable' for e in ignored))))
"""
readback = json.loads(subprocess.check_output([sys.executable, "-c", reader, str(REPO_ROOT / "src"), NOW.isoformat()],
                                             env={**os.environ, "DOIT_ROOT": str(TMP)}, text=True))
assert readback["status"] == "unverifiable" and readback["actor"] == "prod-runner"
assert "L-prod-runner-local.jsonl" in readback["source"] and readback["dropped"] == 1
print("L1113 AC4 fresh process read-back", readback)

for other_closes, uv, expected in [(0, 0, 0), (9, 0, 1), (10, 0, 1), (9, 1, 0)]:
    clear()
    seed_closes(other_closes, uv)
    backlog_fixture("L-spec-9913", 15)
    backlog_fixture("L-spec-9914", 16)
    output = sweep_owed.close_unverifiable(fold.read_events(), NOW)
    closes = [e for e in fold.read_events() if e["type"] == "owed-unverifiable" and e["actor"] == "prod-runner"]
    assert len(closes) == expected
    if closes:
        assert closes[0]["subject"] == "L-spec-9914"
    assert any(f"{2-expected} rows past 14 days held" in line and "cap" in line for line in output)
    counts = sweep_owed.owed_failures.month_counts(fold.read_events(), NOW)
    assert counts["unverifiable"] * 10 <= counts["closed"] or expected == 0
    print("L1113 AC3", other_closes, uv, output)

# No-runner expiry is excluded; runner expiry remains an escalation.
clear()
backlog_fixture("L-spec-9913", 15, "ro-dsn-grant", "expired")
backlog_fixture("L-spec-9914", 15, "unknown", "expired")
expiry = sweep_owed.sweep_expired(specs_now()[1], NOW)
assert len(expiry) == 1 and "L-spec-9914" in expiry[0]
for variant in ("relabel", "redate"):
    clear()
    backlog_fixture("L-spec-9913", 1)
    backlog_fixture("L-spec-9914", 1, "elapsed")
    backlog_fixture("L-spec-9915", 1, "operator-action", "blocked")
    if variant == "redate":
        write("L-operator-9913.jsonl",
              {"subject": "L-spec-9913", "type": "owed-unobservable", "criterion": "AC1",
               "capability": "operator-action", "why": "fixture", "ts": iso(hours=-3)},
              {"subject": "L-spec-9913", "type": "owed-ac", "criterion": "AC1",
               "wake_at": iso(hours=-1), "ts": iso(hours=-2)})
    fake = FakeRunner()
    output = sweep_owed.run(now=NOW, runner=fake)
    manifests = list(CONTENT.glob("L-sweep-*.md"))
    assert len(fake.calls) == 2 and len(manifests) == 1
    assert [r["spec"] for r in rows_of(manifests[0])] == ["L-spec-9914"]
    assert not any("L-spec-9913" in line and "dispatch" in line for line in output)
    assert any("needs operator" in line and "2" in line for line in output)
    print("L1113 AC5 no-runner excluded from manifest", variant)

# L-spec-1111: script observations and a fixed hand-off channel govern the batch.
import prod_runner

clear()
sid = "L-spec-9997"
write("L-spec-writer-partition.jsonl",
      {"type": "spec-written", "subject": sid, "project": "proj-a", "ts": EPOCH},
      *({"type": "owed-ac", "subject": sid, "criterion": f"AC{i}", "wake_at": iso(hours=-1),
         "line": f"fixture {i}", "ts": EPOCH} for i in range(1, 14)))
write("L-executor-partition.jsonl", {"type": "shipped", "subject": sid, "ts": EPOCH})
write("L-operator-partition.jsonl",
      {"type": "owed-routed", "subject": sid, "criterion": "AC8", "route": "relabel",
       "capability": "ro-dsn-grant", "ts": iso(hours=-2)},
      {"type": "owed-failed", "subject": sid, "criterion": "AC13", "kind": "unmet",
       "evidence": "old failure", "valid_until": iso(hours=-1), "ts": iso(hours=-2)})
records = {
    "AC1": ("build", ["repo"]), "AC2": ("build", ["repo"]),
    "AC3": ("prod", ["ro-prod-sql"]), "AC4": ("prod", ["droplet-read"]),
    "AC5": ("prod", ["deployed-site"]), "AC6": ("prod-browser", ["browser-review-account"]),
    "AC7": ("judgment", ["operator"]), "AC8": ("judgment", ["repo"]),
    "AC9": ("judgment", ["repo"]), "AC10": ("prod", ["ro-prod-sql"]),
    "AC11": ("build", ["repo"]), "AC12": ("prod", ["ro-prod-sql"]), "AC13": ("build", ["repo"])}


def fixture_record(row):
    kind, needs = records[row["criterion"]]
    return {"runner": kind, "needs": needs, "validity_h": 24,
            "commands": [] if row["criterion"] == "AC10" else ["fixture"]}


returned = {"lines": ["prod-runner fixture"], "bundle_rows": {(sid, "AC1"): "/evidence/failure.json",
            (sid, "AC11"): "/evidence/held.json", (sid, "AC13"): "/evidence/stale-failure.json"},
            "held": {(sid, "AC11"), (sid, "AC2")},
            "to_judgment": {(sid, "AC10")}}
order = []
real_expired = sweep_owed.sweep_expired


def expired_first(*args, **kw):
    order.append("expiry")
    return real_expired(*args, **kw)


def observe_pass(now, *, dry_run=False):
    assert order == ["expiry"]
    order.append("runner")
    if not dry_run:
        path = EV / prod_runner.LEDGER_NAME
        assert dispatch.emit(path, {"subject": sid}, "owed-met", criterion="AC12", evidence="fixture") is None
    return returned


def tree_bytes():
    return {str(p.relative_to(TMP)): p.read_bytes() for p in TMP.rglob("*") if p.is_file() and p.relative_to(TMP).parts[0] != "cache"}


with patch.object(prod_runner, "record_for", side_effect=fixture_record), \
        patch.object(prod_runner, "run", side_effect=observe_pass) as run_stub, \
        patch.object(sweep_owed, "sweep_expired", side_effect=expired_first), \
        patch.object(subprocess, "Popen", side_effect=AssertionError("real process")), \
        patch.object(subprocess, "run", side_effect=AssertionError("real process")):
    rows = sweep_owed.due_candidates(specs_now()[1], NOW)
    browser = prod_runner.batch_for_browser(rows)
    assert browser == [r for r in rows if r["criterion"] == "AC6"]
    # Full dry run: every file byte remains identical and the flag propagates.
    before = tree_bytes()
    sweep_owed.run(now=NOW, dry_run=True, runner=FakeRunner())
    dry_run_unchanged = tree_bytes() == before
    assert dry_run_unchanged
    assert run_stub.call_count == 1 and run_stub.call_args.kwargs["dry_run"] is True
    print("AC7 dry-run tree hash", hashlib.sha256(repr(sorted(before.items())).encode()).hexdigest())
    order.clear()
    run_stub.reset_mock()
    fake = FakeRunner()
    sweep_owed.run(now=NOW, runner=fake)
    run_stub.assert_called_once_with(NOW, dry_run=False)
    assert order == ["expiry", "runner"] and len(fake.calls) == 2
    manifest = rows_of(next(CONTENT.glob("L-sweep-*.md")))
    included = {r["criterion"] for r in manifest}
    assert included == {"AC1", "AC9", "AC10"}, manifest
    assert "/evidence/failure.json" in next(r["line"] for r in manifest if r["criterion"] == "AC1")
    reasons = {"AC1": "failing bundle", "AC2": "held undetermined build", "AC3": "script SQL",
               "AC4": "script droplet", "AC5": "script HTTP", "AC6": "browser hand-off",
               "AC7": "operator need", "AC8": "grant required", "AC9": "judgment",
               "AC10": "production no commands", "AC11": "held takes precedence", "AC12": "met on refold",
               "AC13": "failed row excluded despite build bundle hand-off"}
    for criterion, reason in reasons.items():
        print("AC7", criterion, "included" if criterion in included else "excluded", reason)
print("AC6 ok")
print("AC7 ok")

contract = (REPO_ROOT / "agents/owed-sweeper.md").read_text()
for phrase in ("evidence bundle", "read it, do not re-run", "never execute checks", "never receive a `browser`",
               "`operator-action`", "`ro-dsn-grant`", "`permission denied` from `ro-sql`", "never a verdict you record as failed",
               "## Output → events", "45-minute"):
    assert phrase in contract, phrase
assert next(line for line in contract.splitlines() if line.startswith("tools:")) == \
    "tools: Read, Glob, Grep, Bash, StructuredOutput, Write, mcp__chrome-devtools"
print("AC9 ok")
for criterion, body in (("AC7", {"included": sorted(included), "excluded": sorted(set(reasons) - included),
                                 "dry_run_unchanged": dry_run_unchanged}),
                        ("AC9", {"phrases_verified": True, "tools_unchanged": True})):
    print(json.dumps({"url": f"cli://sweep-owed/{criterion}", "status": 0,
        "body_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(), "body_excerpt": body}))


# L-spec-1112: browser manifests, real packet round-trip, legacy retries and one slot.
import prod_browser
import socket


def browser_signature(criterion, body):
    data = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='fixture://sweep-owed/' + criterion, status=0,
                         body_sha256=hashlib.sha256(data.encode()).hexdigest(), body_excerpt=body)))
    print(criterion + ' ok')


clear()
sid = 'L-spec-9998'
write('L-spec-writer-browser.jsonl',
      dict(type='spec-written', subject=sid, project='proj-a', ts=EPOCH),
      *(dict(type='owed-ac', subject=sid, criterion=f'AC{i}', line='browser' if i > 3 else 'fixture',
             wake_at=iso(hours=-14), ts=EPOCH) for i in range(1, 11)))
write('L-executor-browser.jsonl', dict(type='shipped', subject=sid, ts=EPOCH))
write('L-operator-browser.jsonl', dict(type='owed-unobservable', subject=sid, criterion='AC10',
                                    capability='browser', why='fixture', ts=iso(hours=-13)))
rows = owed.checks(specs_now()[1][sid]['evs'], NOW)
assert rows[-1]['status'] == 'blocked'
handed = sweep_owed.browser_input(specs_now()[1], sweep_owed.due_candidates(specs_now()[1], NOW), NOW)
assert len(handed) == 10 and len({r['criterion'] for r in handed}) == 10
for capability, age, expected in [('browser', -2, True), ('browser', -13, True),
                                  ('browser-review-account', -13, False), ('node_modules', -13, False)]:
    events = [e for e in specs_now()[1][sid]['evs'] if e['type'] != 'owed-unobservable']
    events.append(dict(type='owed-unobservable', subject=sid, criterion='AC10',
                       capability=capability, why='fixture', ts=iso(hours=age)))
    status = owed.checks(events, NOW)[-1]['status']
    candidate = sweep_owed.browser_input({sid: {'evs': events}}, [], NOW)
    assert bool(candidate) is expected
    assert status == ('blocked' if capability == 'browser' else 'expired')
    print('browser retry', capability, age, status)
browser_signature('AC14', {'browser': 'blocked with 12-hour retry',
                           'browser-review-account': 'expired', 'node_modules': 'expired'})

plain_text = sweep_owed._manifest_text('L-sweep-9998', 'proj-a', str(TMP), rows[:1])
browser_text = sweep_owed._manifest_text('L-sweep-9998', 'proj-a', str(TMP), rows[:1], kind='browser')
assert browser_text == plain_text.replace('kind: plain', 'kind: browser')
manifest = CONTENT / 'L-sweep-9998.md'
for kind, text in [('plain', plain_text), ('browser', browser_text), ('plain', plain_text.replace('kind: plain\n', ''))]:
    manifest.write_text(text)
    parsed = sweep_owed.packet.parse_sweep_manifest(manifest)
    assert parsed['kind'] == kind and parsed['rows'] == rows_of(manifest)
    process = subprocess.run([str(REPO_ROOT / 'doit'), 'packet', 'owed-sweeper', manifest.stem],
                             capture_output=True, text=True, env=dict(os.environ, DOIT_ROOT=str(TMP)))
    assert process.returncode == 0, process.stderr
    assert pathlib.Path(process.stdout.strip()).is_file()
manifest.write_text(plain_text.replace('kind: plain', 'kind: other'))
try:
    sweep_owed.packet.parse_sweep_manifest(manifest)
    raise AssertionError('unknown manifest kind accepted')
except SystemExit:
    pass
manifest.unlink()
browser_signature('AC13', {'real_packet_browser': 0, 'real_packet_plain': 0, 'unknown_kind_refused': True})

for kind in ('plain', 'browser'):
    fake = FakeRunner()
    before = set(CONTENT.glob('L-sweep-*.md'))
    lines = sweep_owed.dispatch_batch('proj-a', rows[:1], fake, kind=kind)
    assert len(fake.calls) == 2
    argv = fake.calls[-1]
    assert argv.count('--mcp-config') == int(kind == 'browser')
    assert '--detach' in argv and '--cwd' in argv and '--project' in argv
    if kind == 'browser':
        assert argv[-1] == str(REPO_ROOT / 'mcp/chrome-devtools.json')
    new = (set(CONTENT.glob('L-sweep-*.md')) - before).pop()
    assert sweep_owed.packet.parse_sweep_manifest(new)['kind'] == kind
    new.unlink()

empty_observations = dict(lines=[], bundle_rows={}, held=set(), to_judgment=set())
seen = []


def classify(row):
    return dict(runner='prod-browser' if int(row['criterion'][2:]) > 3 else 'judgment',
                needs=['browser-review-account'] if int(row['criterion'][2:]) > 3 else ['repo'],
                validity_h=24, commands=[])


def partition(rows):
    seen.append([r['criterion'] for r in rows])
    return ([r for r in rows if r['criterion'] in ('AC4', 'AC5', 'AC6')],
            [r for r in rows if r['criterion'] not in ('AC4', 'AC5', 'AC6')])


with patch.object(prod_runner, 'run', return_value=empty_observations), \
        patch.object(prod_runner, 'record_for', side_effect=classify), \
        patch.object(prod_browser, 'split', side_effect=partition), \
        patch.object(socket, 'socket', side_effect=AssertionError('network')), \
        patch.object(os, 'system', side_effect=AssertionError('shell')), \
        patch.object(subprocess, 'run', side_effect=AssertionError('process')), \
        patch.object(subprocess, 'Popen', side_effect=AssertionError('process')):
    fake = FakeRunner()
    sweep_owed.run(now=NOW, runner=fake)
    manifests = list(CONTENT.glob('L-sweep-*.md'))
    assert len(manifests) == 1 and len(fake.calls) == 2
    assert {r['criterion'] for r in rows_of(manifests[0])} == {'AC1', 'AC2', 'AC3'}
    assert set(seen[-1]) == {f'AC{i}' for i in range(4, 11)}
    manifests[0].unlink()
    # With no plain project competing, one browser batch owns the slot.
    with patch.object(sweep_owed, 'judgment_rows', return_value=([], [])):
        fake = FakeRunner()
        sweep_owed.run(now=NOW, runner=fake)
        manifests = list(CONTENT.glob('L-sweep-*.md'))
        assert len(manifests) == 1 and len(fake.calls) == 2
        assert {r['criterion'] for r in rows_of(manifests[0])} == {'AC7', 'AC8', 'AC9', 'AC10'}
        assert sweep_owed.packet.parse_sweep_manifest(manifests[0])['kind'] == 'browser'
        runs = [e for e in fold.read_events() if e['type'] == 'sweep-run']
        assert [e['kind'] for e in runs[-2:]] == ['plain', 'browser']
        write('L-owed-sweeper-browser.jsonl', dict(type='spawn-started', subject=manifests[0].stem,
             role='owed-sweeper', spawn='L-owed-sweeper-9998', project='proj-a'))
        fake = FakeRunner()
        sweep_owed.run(now=NOW, runner=fake)
        assert not fake.calls
browser_signature('AC7', {'plain_and_browser_dispatch': True, 'one_open_batch': True})
browser_signature('AC8', {'plain': 3, 'e2e': 3, 'seat': 4, 'legacy_browser_routed': True})

# The existing runner append path consumes the browser batch exactly once.
clear()
write('L-spec-writer-seam.jsonl', dict(type='spec-written', subject=sid, project='proj-a', ts=EPOCH),
      dict(type='owed-ac', subject=sid, criterion='AC1', line='browser', wake_at=iso(hours=-1), ts=EPOCH))
write('L-executor-seam.jsonl', dict(type='shipped', subject=sid, ts=EPOCH))
with patch.object(prod_browser, 'run_batch', return_value=[dict(spec=sid, criterion='AC1', outcome='met',
        observed_at=NOW.isoformat(), valid_until=iso(hours=24), evidence='clone=/fixture/scratch spec passed')]) as seam:
    prod_runner.run(NOW)
    assert seam.call_count == 1 and seam.call_args.args[0][0]['criterion'] == 'AC1'
    assert owed.checks(specs_now()[1][sid]['evs'], NOW)[0]['status'] == 'met'

# Reverse exactly the three granted edits; all other bytes match the baseline.
actual = (REPO_ROOT / 'agents/owed-sweeper.md').read_text()
normalized = re.sub(r'\n### Browser batches\n.*?(?=\n## Blind)', '', actual, flags=re.S)
normalized = normalized.replace('tools: Read, Glob, Grep, Bash, StructuredOutput, Write, mcp__chrome-devtools\n',
                                'tools: Read, Glob, Grep, Bash, StructuredOutput, Write\n')
normalized = normalized.replace('`deploy.sh`, a production wrapper, or a browser yourself, except in a\n`kind: browser` batch, see [Browser batches](#browser-batches).',
                                '`deploy.sh`, a production wrapper, or a browser yourself.')
assert hashlib.sha256(normalized.replace("elapsed | repo | not-live | ro-dsn-grant", "elapsed | other").encode()).hexdigest() == '16c522320824ff6899b5fa539b82331226e532cdc54ae0403281dbcb80f798e0'
assert actual.rsplit('\n## ', 1)[-1].startswith('Lessons (operator rule, 2026-09-24)')
browser_signature('AC9', {'tools_granted': True, 'untouched_sections_identical': True, 'lessons_last': True})

# W38: exercise the real prod-runner/no-command judgment path, not just the
# due-only helper. Expired failures retry only after 12h without an owned fix.
for case, age, validity, terminal, expected, should_batch in (
        ('recent undated failure', 1, None, None, 'failed', False),
        ('recent expired evidence', 2, -1, None, 'failed', False),
        ('recent valid evidence', 1, 1, None, 'failed', False),
        ('old failure', 13, None, None, 'failed', False),
        ('old expired evidence', 13, -12, None, 'failed', True),
        ('retry boundary', 12, -1, None, 'failed', True),
        ('validity boundary', 13, 0, None, 'failed', False),
        ('standing corrective', 13, -1, 'owed-corrective', 'correcting', False),
        ('standing corrective before failure', 13, -1, 'standing', 'failed', False),
        ('corrective before governing event', 13, -1, 'historical', 'failed', True),
        ('waived', 13, -1, 'owed-waived', 'waived', False),
        ('met', 13, -1, 'owed-met', 'met', False),
        ('explicit recheck', 1, None, 'owed-routed', 'due', True),
        ('genuinely due', None, None, None, 'due', True)):
    clear()
    sid = 'L-spec-9938'
    write('L-spec-writer-w38.jsonl',
          dict(type='spec-written', subject=sid, project='proj-a', ts=EPOCH),
          dict(type='owed-ac', subject=sid, criterion='AC1', line='fixture',
               wake_at=iso(hours=-24), ts=EPOCH))
    write('L-executor-w38.jsonl', dict(type='shipped', subject=sid, ts=EPOCH))
    lifecycle = []
    if age is not None:
        failure = dict(type='owed-failed', subject=sid, criterion='AC1',
                       kind='unmet', evidence='known miss', ts=iso(hours=-age))
        if validity is not None:
            failure['valid_until'] = iso(hours=validity)
        lifecycle.append(failure)
    if terminal:
        terminal_type = 'owed-corrective' if terminal in ('standing', 'historical') else terminal
        terminal_ts = (iso(hours=-14) if terminal == 'standing' else
                       (NOW - timedelta(days=3651)).isoformat() if terminal == 'historical' else iso(minutes=-30))
        lifecycle.append(dict(type=terminal_type, subject=sid, criterion='AC1',
                              ts=terminal_ts, corrective='L-spec-9939',
                              reason='fixture waiver', evidence='fixture met', route='recheck'))
    write('L-operator-w38.jsonl', *lifecycle)
    specs = specs_now()[1]
    assert owed.checks(specs[sid]['evs'], NOW)[0]['status'] == expected, case
    assert bool(prod_runner._selected(specs, NOW)) == should_batch, case
    with patch.object(prod_runner, 'record_for', return_value=dict(
            runner='prod', needs=['deployed-site'], commands=[], validity_h=24)), \
            patch.object(prod_runner, 'observe', side_effect=AssertionError('unexpected observation')), \
            patch.object(prod_runner, '_build', side_effect=AssertionError('unexpected build')), \
            patch.object(subprocess, 'run', side_effect=AssertionError('real process')), \
            patch.object(subprocess, 'Popen', side_effect=AssertionError('real process')):
        preview = sweep_owed.run(now=NOW, dry_run=True, runner=FakeRunner())
        assert any(f'would observe {sid}/AC1' in line for line in preview) == should_batch, case
        # Dry-run holds active prod observations; the real no-command pass below
        # must hand due/retry-ready rows on to judgment and create their manifest.
        assert not any('would batch' in line for line in preview), case
        for forced_handoff in (False, True):
            # Inject even ineligible hand-offs to exercise the routed-row guard
            # independently of the runner's upstream selection.
            with ExitStack() as handoff:
                if forced_handoff:
                    handoff.enter_context(patch.object(prod_runner, 'run', return_value=dict(
                        lines=[], held=set(), bundle_rows={}, to_judgment={(sid, 'AC1')})))
                fake = FakeRunner()
                output = sweep_owed.run(now=NOW, runner=fake)
                manifests = list(CONTENT.glob('L-sweep-*.md'))
                assert len(fake.calls) == (2 if should_batch else 0), (case, forced_handoff, output)
                assert len(manifests) == int(should_batch), (case, forced_handoff)
                if should_batch:
                    assert [(r['spec'], r['criterion']) for r in rows_of(manifests[0])] == [(sid, 'AC1')]
                    manifests[0].unlink()
    print('W38 lifecycle:', case, expected, 'batched' if should_batch else 'excluded')


# L-spec-1368: production clone helper, real git commands, isolated ledger.
def test_repo_batches():
    import shutil
    import build_runner

    def git(repo, *args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True,
            env=dict(os.environ, GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.test",
                     GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.test")).strip()

    def source_snapshot(repo):
        return (git(repo, "for-each-ref"), git(repo, "worktree", "list", "--porcelain"),
                (repo / ".git/config").read_bytes())

    sources = {}
    with patch.dict(os.environ):
        os.environ.pop("DOIT_MAIN", None)
        for branch in ("master", "main"):
            project = "repo-fixture-" + branch
            repo = TMP / "repos" / project
            repo.mkdir(parents=True)
            git(repo, "init", "-q", "-b", branch)
            (repo / ".gitignore").write_text(".env\n")
            git(repo, "add", ".gitignore")
            git(repo, "commit", "-qm", "base")
            base = git(repo, "rev-parse", "HEAD")
            (repo / "shipped.txt").write_text("shipped\n")
            git(repo, "add", "shipped.txt")
            git(repo, "commit", "-qm", "ship")
            sha = git(repo, "rev-parse", "HEAD")
            if branch == "main":
                (repo / "later.txt").write_text("later\n")
                git(repo, "add", "later.txt")
                git(repo, "commit", "-qm", "later")
                # Both common remote names must be pinned if present.
                git(repo, "branch", "master")
            before = source_snapshot(repo)
            with patch.object(dispatch, "provision_worktree_env", wraps=dispatch.provision_worktree_env) as provision:
                tree = sweep_owed.make_repo_cwd(project, sha, base, "L-sweep-970" + str(len(sources)))
                provision.assert_called_once_with(tree, repo)
            assert tree.parent.stat().st_mode & 0o777 == 0o700
            assert git(tree, "rev-parse", "HEAD") == sha
            assert git(tree, "merge-base", "HEAD", "origin/" + branch) == base
            assert git(tree, "diff", "--name-only", base) == "shipped.txt"
            assert git(tree, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") == "origin/" + branch
            assert not (tree / ".env").exists()
            assert source_snapshot(repo) == before
            sources[branch] = (project, repo, sha, base)
            for ref in (("main", "master") if branch == "main" else ("master",)):
                verification = f'BASE=$(git merge-base HEAD origin/{ref}) && git diff --name-only "$BASE"'
                observed = subprocess.run(["bash", "-c", verification], cwd=tree, capture_output=True, text=True)
                assert observed.returncode == 0 and observed.stdout.strip() == "shipped.txt"
                outside = subprocess.run(["bash", "-c", verification], cwd=TMP, capture_output=True, text=True)
                assert outside.returncode != 0 and not outside.stdout.strip()
            print("repo-batch AC2", branch, "clone/base/diff/source unchanged; AC7 real Verification passed")
        project, repo, sha, base = sources["main"]
        for bad_sha, bad_base in (("0" * 40, base), (sha, "0" * 40)):
            try:
                sweep_owed.make_repo_cwd(project, bad_sha, bad_base, "L-sweep-9799")
                raise AssertionError("unresolvable revision accepted")
            except build_runner.Undetermined:
                assert not (SCRATCH_ROOT / "sweeps/L-sweep-9799").exists()
        with patch.dict(os.environ, DOIT_MAIN="missing"):
            try:
                sweep_owed.make_repo_cwd(project, sha, base, "L-sweep-9799")
                raise AssertionError("unresolvable main branch accepted")
            except build_runner.Undetermined:
                assert not (SCRATCH_ROOT / "sweeps/L-sweep-9799").exists()

        # Three terminal trees and one open tree; count before removal is four.
        events, trees = [], []
        for i, terminal in enumerate(("spawn-done", "spawn-failed", "spawn-stale", None)):
            subject, spawn = f"L-sweep-978{i}", f"L-owed-sweeper-978{i}"
            tree = SCRATCH_ROOT / "sweeps" / subject / "tree"
            tree.mkdir(parents=True)
            trees.append(str(tree))
            events.append(dict(type="spawn-started", role="owed-sweeper", subject=subject, spawn=spawn))
            if terminal:
                events.append(dict(type=terminal, spawn=spawn))
        assert sum(pathlib.Path(t).is_dir() for t in trees) == 4
        assert sweep_owed.reap_repo_cwds(events) == trees[:3]
        assert pathlib.Path(trees[-1]).is_dir() and sweep_owed.reap_repo_cwds(events) == []
        print("repo-batch AC3 counted 4 trees; reaped 3 terminal trees; open tree preserved; idempotent")

        def reset():
            clear()
            shutil.rmtree(SCRATCH_ROOT / "sweeps")
            (SCRATCH_ROOT / "sweeps").mkdir()

        empty_observations = dict(lines=[], held=set(), bundle_rows={}, to_judgment=set())
        judgment = dict(runner="judgment", needs=["repo"], commands=[], validity_h=24)
        def seed(suffix, project=project, shipped=sha, capability="repo", criterion="AC1", blocked=False, routed=False):
            sid = "L-spec-1368-" + suffix
            suffix = suffix.replace("-", "")  # Keep filename-derived ledger actors canonical.
            write(f"L-spec-writer-{suffix}.jsonl",
                  dict(type="spec-written", subject=sid, project=project, ts=EPOCH),
                  *([] if routed else [dict(type="owed-ac", subject=sid, criterion=criterion,
                       line="fixture", wake_at=iso(hours=-24), ts=EPOCH)]))
            dispatch.spec_path(sid).write_text("## Acceptance criteria\nAC1 [backend]: fixture\n"
                                                "## Verification\n```bash\ngit diff --name-only $BASE\n```\n")
            write(f"L-executor-{suffix}.jsonl", dict(type="shipped", subject=sid, sha=shipped, ts=EPOCH))
            if routed:
                write(f"L-grader-{suffix}.jsonl", dict(type="criterion-routed", subject=sid, criterion=criterion,
                      capability="git", to="owed", ts=iso(hours=-23)))
            if not routed or capability != "repo":
                write(f"L-operator-{suffix}.jsonl", dict(type="owed-routed", subject=sid, criterion=criterion,
                      capability=capability, route="relabel", ts=iso(hours=-22)))
            if blocked:
                write(f"L-owed-sweeper-{suffix}.jsonl", dict(type="owed-unobservable", subject=sid,
                      criterion=criterion, capability=capability, why="fixture", ts=iso(hours=-20)))
            return sid

        def drive(*, dry_run=False):
            fake = FakeRunner()
            with patch.object(prod_runner, "run", return_value=empty_observations), \
                    patch.object(prod_runner, "record_for", return_value=judgment), \
                    patch.object(sweep_owed, "reap_repo_cwds", wraps=sweep_owed.reap_repo_cwds) as reaper, \
                    patch.object(sweep_owed, "make_repo_cwd", wraps=sweep_owed.make_repo_cwd) as maker:
                lines = sweep_owed.run(now=NOW, runner=fake, dry_run=dry_run)
                assert reaper.call_count == int(not dry_run)
            return fake, lines, maker.call_count

        reset()
        sids = [seed("a1"), seed("a2"), seed("a3", shipped=base),
                seed("b1", project=sources["master"][0], shipped=sources["master"][2])]
        fake, lines, calls = drive(dry_run=True)
        assert not fake.calls and calls == 0 and not list(CONTENT.glob("L-sweep-*.md"))
        fake, lines, calls = drive()
        manifests = list(CONTENT.glob("L-sweep-*.md"))
        assert any("1 repo row(s) deferred: one shipped sha" in line for line in lines)
        assert len(manifests) == 1 and calls == 1 and len(fake.calls) == 2
        parsed = sweep_owed.packet.parse_sweep_manifest(manifests[0])
        assert parsed["kind"] == "repo"
        assert {r["spec"] for r in parsed["rows"]} == set(sids[:2])
        assert {r["shipped_sha"] for r in parsed["rows"]} == {sha}
        assert {r["base_sha"] for r in parsed["rows"]} == {base}
        assert {r["clone"] for r in parsed["rows"]} == {parsed["cwd"]}
        assert fake.calls[-1][fake.calls[-1].index("--cwd") + 1] == parsed["cwd"]
        assert git(pathlib.Path(parsed["cwd"]), "rev-parse", "HEAD") == sha
        records = [e for e in fold.read_events() if e["type"] == "sweep-run"]
        assert len(records) == 1 and records[0]["kind"] == "repo" and records[0]["outcome"] == "batched"

        for open_kind, expected in (("plain", "batched"), ("browser", "batched"), ("repo", "open")):
            reset()
            seed("open-" + open_kind)
            open_path = CONTENT / "L-sweep-9777.md"
            open_path.write_text(f"# L-sweep-9777\nkind: {open_kind}\n")
            write("L-owed-sweeper-open.jsonl", dict(type="spawn-started", subject=open_path.stem,
                  spawn="L-owed-sweeper-9777", role="owed-sweeper", project=project))
            ev = fold.read_events()
            assert sweep_owed.open_batch(ev, NOW)
            assert sweep_owed.open_batch(ev, NOW, open_kind)
            fake, lines, calls = drive()
            records = [e for e in fold.read_events() if e["type"] == "sweep-run"]
            assert len(records) == 1 and records[0]["outcome"] == expected
            assert bool(fake.calls) == (expected == "batched")

        for capability, criterion, blocked, shipped, routed, expected in (
                ("repo", "verify", False, sha, True, "repo"),
                ("other", "AC1", False, sha, False, "repo"),
                ("repo", "AC1", True, sha, False, "repo"),
                ("repo", "AC1", False, None, False, None),
                ("elapsed", "verify", False, sha, True, "plain"),
                ("elapsed", "AC1", False, sha, False, "plain"),
                ("unknown-capability", "AC1", False, sha, False, "plain")):
            reset()
            seed("route", capability=capability, criterion=criterion, blocked=blocked, shipped=shipped, routed=routed)
            fake, lines, calls = drive()
            manifests = list(CONTENT.glob("L-sweep-*.md"))
            assert bool(manifests) == bool(expected), (capability, criterion, lines)
            if expected:
                assert sweep_owed.packet.parse_sweep_manifest(manifests[0])["kind"] == expected
            assert calls == int(expected == "repo")

        reset()
        seed("priority-repo")
        plain_sid = seed("priority-plain", capability="elapsed")
        fake, lines, calls = drive()
        assert calls == 0
        assert [r["spec"] for r in rows_of(next(CONTENT.glob("L-sweep-*.md")))] == [plain_sid]

        for failure in (build_runner.Undetermined("fixture"), OSError("fixture"), subprocess.SubprocessError("fixture")):
            reset()
            seed("failure")
            with patch.object(sweep_owed, "make_repo_cwd", side_effect=failure):
                fake, lines, calls = drive()
            assert not fake.calls and any("failed" in line for line in lines)
            assert not list(CONTENT.glob("L-sweep-*.md"))
            assert not list((SCRATCH_ROOT / "sweeps").glob("*/tree"))
            records = [e for e in fold.read_events() if e["type"] == "sweep-run"]
            assert len(records) == 1 and records[0]["outcome"] == "failed"
        print("repo-batch AC5 grouping, dispatch cwd, kind slots, legacy/verify routes, missing sha, blocked and failures passed")
        # The source checkout remains untouched after all dispatches too.
        assert git(repo, "rev-parse", "HEAD") != sha
        for tree in (SCRATCH_ROOT / "sweeps").glob("*/tree"):
            shutil.rmtree(tree)


test_repo_batches()


def test_capability_readmission():
    from test_dispatch import check_capability_readmission
    check_capability_readmission(fixture_root=TMP, now=NOW, epoch=EPOCH, iso=iso, clear=clear,
                                write=write, specs_now=specs_now, runner_type=FakeRunner)
test_capability_readmission()

ROOT_PATCHES.close()
