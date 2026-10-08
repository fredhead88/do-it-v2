#!/usr/bin/env python3
"""L-spec-0755 R4 (AC8) and R3 (AC5, AC6). Run: python3 test_amends.py

Hermetic: a throwaway DOIT_ROOT, never the real ~/.do-it; no paid call."""
import json, os, pathlib, re, subprocess, sys, tempfile, time, datetime, argparse

TMP = pathlib.Path(tempfile.mkdtemp(prefix="amends-test-"))
os.environ["DOIT_ROOT"], os.environ["DOIT_NO_POKE"] = str(TMP), "1"
os.environ["HOME"] = str(TMP / "home")
os.environ.pop("DOIT_PROJECT", None)
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import amends, dispatch, fold, packet, spawns, validate  # noqa: E402

(TMP / "content").mkdir(parents=True)
(TMP / "events").mkdir(parents=True)
EV = TMP / "events"


def put(actor, type_, subject, ts, **kv):
    """Append one event to the actor's file with an explicit ts (fixtures need control of time)."""
    e = {"v": 1, "ts": ts, "type": type_, "subject": subject, "project": "t", **kv}
    with open(EV / f"L-{actor}-0001.jsonl", "a") as fh:
        fh.write(json.dumps(e) + "\n")


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat(timespec="seconds")


SPEC = TMP / "content" / "L-spec-0001.md"
SPEC.write_text("""# L-spec-0001
## Goal
Spend per project is a rendered column.
## Acceptance criteria
AC1 [backend]: the column renders.
  review_path: run `doit` / worked if a SPEND line appears / failed if not.
AC2 [backend]: the total is unfiltered.
  review_path: run `doit` / worked if the total is the full sum / failed if not.
AC3 [backend]: nothing else moved.
  review_path: run the tests / worked if all pass / failed if not.
""")
T0 = int(time.time()) - 1000
os.utime(SPEC, (T0, T0))
put("spec-writer", "spec-written", "L-spec-0001", iso(T0 - 5), spec="L-spec-0001", path=str(SPEC), footprint=[])

# ── unit: stale_spec_reason ──────────────────────────────────────────────────
def reason(extra_events=()):
    evs = [e for e in fold.read_events() if e.get("subject") == "L-spec-0001"]
    return amends.stale_spec_reason(evs + list(extra_events), "L-spec-0001", SPEC)

assert reason() is None, "no decision at all"
put("operator", "decision", "L-spec-0001", iso(T0 + 10), why="some ruling with no amends")
assert reason() is None, "a decision without amends= never blocks"
put("thinker", "decision", "L-spec-0001", iso(T0 + 20), why="free text never shown", amends="AC3,AC1")
r = reason()
assert r and "AC1" in r and "AC3" in r and iso(T0 + 20) in r and "doit shape" in r and "rebuild the packet" in r, r
assert "free text never shown" not in r, "a decision's why is never surfaced"
os.utime(SPEC, (T0 + 30, T0 + 30))                    # the spec file was edited after the decision
assert reason() is None
os.utime(SPEC, (T0, T0))                              # back to stale; now a later spec-written clears it
assert reason() is not None
put("spec-writer", "spec-written", "L-spec-0001", iso(T0 + 25), spec="L-spec-0001", path=str(SPEC), footprint=[])
assert reason() is None
put("thinker", "decision", "L-spec-0001", iso(T0 + 40), amends="AC2")
assert "AC2" in reason()
assert amends.stale_spec_reason([{"type": "decision", "subject": "S", "amends": "AC1", "ts": iso(T0)}], "S",
                                TMP / "no-such-spec.md"), "an unreadable mtime refuses, never allows"
assert amends.amended_ids({"amends": "AC1, AC12,bogus"}) == ["AC1", "AC12"]
assert amends.amended_ids({"amends": "bogus"}) == ["bogus"]
# the decision's ts is compared to the mtime floored to the second: same second is not stale
os.utime(SPEC, (T0 + 40.9, T0 + 40.9))
assert reason() is None
print("L-spec-0755 amends.stale_spec_reason ok")

# ── AC8 · through `doit packet` ──────────────────────────────────────────────
os.utime(SPEC, (T0, T0))
CARD = TMP / "content" / "card-0001.md"
CARD.write_text("L-spec-0001 · DONE · built by L-builder-0001\nverify exit 0\n"
                "AC1 [backend] done · command-output · check: run · evidence: ok\n")
put("builder", "build-done", "L-spec-0001", iso(T0 + 1), status="DONE", card=str(CARD), branch="l-spec-0001",
    base_sha="1" * 40, ready_sha="2" * 40, verify_exit=0, tests_added=True)
WT = TMP / "wt"
WT.mkdir()
n_before = len(list((TMP / "packets").glob("*.md"))) if (TMP / "packets").exists() else 0


def doit_packet(role="grader"):
    return subprocess.run([str(HERE.parent / "doit"), "packet", role, "L-spec-0001", "--worktree", str(WT), "--project", "t"],
                          capture_output=True, text=True, env={**os.environ})

r = doit_packet()
assert r.returncode != 0 and "AC2" in r.stderr and iso(T0 + 40) in r.stderr and "doit shape" in r.stderr, (r.stdout, r.stderr)
assert (len(list((TMP / "packets").glob("*.md"))) if (TMP / "packets").exists() else 0) == n_before, "no packet is written"
os.utime(SPEC, (T0 + 100, T0 + 100))                   # `touch` after the decision
r = doit_packet()
assert r.returncode == 0, (r.stdout, r.stderr)
print("L-spec-0755 AC8 ok (an amending decision blocks the packet until the spec file is edited)")

# ── AC5 · a re-grade clears every standing rejection it now judges met ───────
put("grader", "rejected-criterion", "L-spec-0001", iso(T0 + 200), criterion="AC5 [backend]", why="old reason alpha")
put("grader", "rejected-criterion", "L-spec-0001", iso(T0 + 200), criterion="AC2", why="old reason beta")
put("builder", "rejected-criterion", "L-spec-0001", iso(T0 + 200), criterion="COMMIT-SHAPE", why="3 commits above base")
A = argparse.Namespace(subject="L-spec-0001", cwd=str(WT), pinned_base_sha=None)
BASE = {"subject": "L-spec-0001", "project": "t", "spawn": "L-grader-0100"}


def out(verdicts, cleared=None, **kw):
    o = {"verdicts": verdicts, "matches_intent": "yes", "card_ok": "yes", "checkers": [], "could_not_run": False,
         "contamination": False, "declarations": []}
    if cleared is not None:
        o["cleared"] = cleared
    return {**o, **kw}


def verdict(ac, v, reason="r"):
    return {"ac": ac, "verdict": v, "reason": reason}


def clears(o):
    return [(e[1]["criterion"], e[1]["evidence"]) for e in dispatch.events_for("grader", o, A, BASE)
            if e[0] == "criterion-cleared"]

c = clears(out([verdict("AC5", "met", "reproduced it"), verdict("AC2", "unmet", "still wrong")]))
assert c == [("AC5 [backend]", "reproduced it")], c       # the standing string is the event's criterion
# a `cleared` entry clears it too, and wins its own evidence
c = clears(out([verdict("AC5", "met"), verdict("AC2", "met")], cleared=[{"ac": "ac2", "evidence": "ran the check"}]))
assert sorted(x[0] for x in c) == ["AC2", "AC5 [backend]"] and ("AC2", "r") in c, c
# a cleared entry whose verdict in the same Output is not met fails the spawn
for bad in ("unmet", "cannot-assess"):
    try:
        dispatch.events_for("grader", out([verdict("AC2", bad)], cleared=[{"ac": "AC2", "evidence": "x"}]), A, BASE)
    except spawns.ClearedNotMet as e:
        assert "AC2" in str(e)
    else:
        raise AssertionError("cleared-not-met must raise")
# COMMIT-SHAPE is never cleared by a grader, not even on a confirmed verdict or a cleared entry
c = clears(out([verdict("AC5", "met"), verdict("AC2", "met")], cleared=[{"ac": "COMMIT-SHAPE", "evidence": "x"}]))
assert "COMMIT-SHAPE" not in [x[0] for x in c] and len(c) == 2, c
assert spawns.norm_ac("AC5 [backend]") == spawns.norm_ac("ac5") == "AC5"
assert spawns.norm_ac("DONE-COND") == "done-cond"
print("L-spec-0755 AC5 ok (normalised id match, cleared entries, cleared-not-met, COMMIT-SHAPE never cleared)")

# ── AC6 · the packet and the contract carry the new items ────────────────────
c_ = packet.Ctx(packet.argparse.Namespace(subject="L-spec-0001", charter=None, project="t", worktree=str(WT),
                                          role="grader", base_sha=None))
lines = packet.p_grader(c_)
item8 = [l for l in lines if l.startswith("8. Standing rejections to re-judge")]
assert len(item8) == 1 and "AC5 [backend]" in item8[0] and "AC2" in item8[0], lines
assert "COMMIT-SHAPE" not in item8[0]
text = "\n".join(lines)
assert "old reason alpha" not in text and "old reason beta" not in text, "no old rejection reason may reach a grader"
assert "3 commits above base" not in text
GRADER_OUT = out([verdict("AC5", "met")])
sch = HERE.parent / "agents" / "grader.schema.json"
import jsonschema
jsonschema.validate(GRADER_OUT, json.loads(sch.read_text()))                                   # without `cleared`
jsonschema.validate(out([verdict("AC5", "met")], cleared=[{"ac": "AC5", "evidence": "e"}]), json.loads(sch.read_text()))
for badout in (out([verdict("AC5", "met")], cleared=[{"ac": "AC5"}]), out([verdict("AC5", "met")], cleared="AC5")):
    try:
        jsonschema.validate(badout, json.loads(sch.read_text()))
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("a malformed cleared must be rejected")
md = (HERE.parent / "agents" / "grader.md").read_text()
assert "`cleared`" in md and "git merge-base HEAD main" in md and "Standing rejections to re-judge" in md
assert "never against the `base_sha` that `build-started` recorded" in md
for tmp_out, expect in ((TMP / "o1.json", True), (TMP / "o2.json", True)):
    tmp_out.write_text(json.dumps(GRADER_OUT if tmp_out.name == "o1.json"
                                  else out([verdict("AC5", "met")], cleared=[{"ac": "AC5", "evidence": "e"}])))
    r = subprocess.run([str(HERE.parent / "doit"), "validate", "grader", str(tmp_out)], capture_output=True, text=True)
    assert r.returncode == 0 and "VALID" in r.stdout, (r.stdout, r.stderr)
print("L-spec-0755 AC6 ok (standing ids only in the packet, no old reasons, cleared optional in the schema, md names it)")
print("ALL OK")
