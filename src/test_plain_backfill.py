#!/usr/bin/env python3
"""L-spec-0479/plain-backfill: AC1-AC5, AC9. Run: python3 test_plain_backfill.py

AC1 is pure — no fixture, no fold, no plain import. AC2 exercises `split_batches`
in-process against a real folded ledger (an isolated `DOIT_ROOT` per section,
via `fold.ROOT`/`fold.EVENTS` reassignment — test_fold.py's own convention).
AC3/AC4/AC5/AC9 shell out to `plain_backfill.py` as a REAL subprocess (the
production write path), each with its own isolated `DOIT_ROOT` and the
subprocess-isolation env (fix-7): absolute script path, `DOIT_ROOT` pinned,
`DOIT_PROJECT` stripped so it cannot silently filter the seeded fixture events
out of the child's `fold.read_events()`.
"""
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import plain_backfill  # noqa: E402

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


def run(root, *argv):
    """fix-7: subprocess isolation."""
    env = os.environ.copy()
    env["DOIT_ROOT"] = str(root)
    env.pop("DOIT_PROJECT", None)
    return subprocess.run(
        [sys.executable, str(HERE / "plain_backfill.py"), *argv],
        capture_output=True, text=True, env=env)


def write_events(root, actor, *events):
    p = pathlib.Path(root) / "events" / f"{actor}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        for i, e in enumerate(events):
            fh.write(json.dumps({"v": 1, "ts": f"2026-09-08T10:{i:02d}:00+00:00", **e}) + "\n")


def write_spec(root, spec_id, goal="Ships a thing."):
    d = pathlib.Path(root) / "content"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{spec_id}.md").write_text(f"# {spec_id}: a spec\n\n## Goal\n\n{goal}\n", encoding="utf-8")


def rejected_lines(root):
    p = pathlib.Path(root) / "content" / plain_backfill.REJECTED_NAME
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def hash_events(root):
    out = {}
    for f in sorted((pathlib.Path(root) / "events").glob("*.jsonl")):
        out[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


# ── AC1 · state_ok / source_ok — pure, no fixture, no fold, no plain import ────
check(plain_backfill.state_ok("void") is False, "AC1: void excluded")
check(plain_backfill.state_ok("killed") is False, "AC1: killed excluded")
check(plain_backfill.state_ok("dropped") is False, "AC1: dropped excluded")
check(plain_backfill.state_ok("closed-unbuilt") is False, "AC1: closed-unbuilt excluded")
check(plain_backfill.state_ok("written") is True, "AC1: written ok")
check(plain_backfill.state_ok("accepted") is True, "AC1: accepted ok")
check(plain_backfill.state_ok("shipped-owed-due") is True, "AC1: shipped-owed-due ok")
check(plain_backfill.source_ok("goal-section") is True, "AC1: goal-section ok")
check(plain_backfill.source_ok("missing") is True, "AC1: missing ok")
check(plain_backfill.source_ok("spec-line") is False, "AC1: spec-line excluded")
check(plain_backfill.source_ok("sidecar") is False, "AC1: sidecar excluded")
check(plain_backfill.source_ok("intent") is False, "AC1: intent excluded")
print("0479-AC1 ok")

import fold  # noqa: E402  (only the in-process ACs below need it)

# ── AC2 · split_batches: state exclusion, source exclusion, no-content-file ────
# exclusion, then the rejection cap moving a spec from eligible to hand_line.
AC2_ROOT = pathlib.Path(tempfile.mkdtemp())
fold.ROOT, fold.EVENTS = AC2_ROOT, AC2_ROOT / "events"

write_spec(AC2_ROOT, "L-spec-9101")
write_events(AC2_ROOT, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-9101", "charter": "L-charter-x"})

write_spec(AC2_ROOT, "L-spec-9102")
write_events(AC2_ROOT, "L-spec-writer-0001",
             {"type": "spawn-started", "subject": "L-spec-9102"},
             {"type": "spawn-failed", "subject": "L-spec-9102"})

(AC2_ROOT / "content" / "L-spec-9103.md").write_text(
    "# L-spec-9103: a spec\n\nIn plain English: It already has a real spec line here, thanks.\n\n"
    "## Goal\n\nShips a thing.\n", encoding="utf-8")
write_events(AC2_ROOT, "L-executor-0001",
             {"type": "spec-written", "subject": "L-spec-9103", "charter": "L-charter-x"},
             {"type": "build-done", "subject": "L-spec-9103", "branch": "b", "ready_sha": "abc1234"},
             {"type": "shipped", "subject": "L-spec-9103", "charter": "L-charter-x"})
write_events(AC2_ROOT, "L-grader-0001", {"type": "verdict", "subject": "L-spec-9103", "confirmed": True})
write_events(AC2_ROOT, "L-reviewer-0001", {"type": "review", "subject": "L-spec-9103", "depth": "gates-only"})

# L-spec-9106: shaped subject, eligible state ("written"), NO content file at all.
write_events(AC2_ROOT, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-9106", "charter": "L-charter-x"})

specs2, *_ = fold.fold(fold.read_events())
check(specs2["L-spec-9102"]["state"] == "void", f"AC2: 9102 derives void: {specs2['L-spec-9102']['state']}")
check(specs2["L-spec-9103"]["state"] == "accepted", f"AC2: 9103 derives accepted: {specs2['L-spec-9103']['state']}")

eligible, hand_line = plain_backfill.split_batches(specs2, AC2_ROOT)
check(eligible == ["L-spec-9101"], f"AC2: eligible before rejects: {eligible}")
check(hand_line == [], f"AC2: hand_line before rejects: {hand_line}")

rejected2 = AC2_ROOT / "content" / plain_backfill.REJECTED_NAME
rejected2.parent.mkdir(parents=True, exist_ok=True)
with open(rejected2, "a", encoding="utf-8") as fh:
    for _ in range(2):
        fh.write(json.dumps({"spec": "L-spec-9101", "line": "x", "reason": "test seed"}) + "\n")

eligible2, hand_line2 = plain_backfill.split_batches(specs2, AC2_ROOT)
check(eligible2 == [], f"AC2: eligible after two rejects: {eligible2}")
check(hand_line2 == ["L-spec-9101"], f"AC2: hand_line after two rejects: {hand_line2}")
check("L-spec-9106" not in eligible2 and "L-spec-9106" not in hand_line2, "AC2: 9106 absent from both")
print("0479-AC2 ok")

# ── AC3 · manifest, real subprocess, batch chunking ────────────────────────────
AC3_ROOT = pathlib.Path(tempfile.mkdtemp())
for i in range(17):
    sid = f"L-spec-91{50 + i}"
    write_spec(AC3_ROOT, sid)
    write_events(AC3_ROOT, "L-executor-0001", {"type": "spec-written", "subject": sid, "charter": "L-charter-x"})

p1 = run(AC3_ROOT, "manifest", "--batch", "15")
check(p1.returncode == 0, f"AC3: manifest --batch 15 exit 0: {p1.stderr}")
check(p1.stdout.strip() == "2 batches, 17 eligible, 0 needing a hand line.",
      f"AC3: summary line: {p1.stdout!r}")
manifests = list((AC3_ROOT / "content").glob("plain-backfill-manifest-*.md"))
check(len(manifests) == 1, f"AC3: exactly one manifest file: {manifests}")
today = manifests[0].stem.split("plain-backfill-manifest-", 1)[1]
text = manifests[0].read_text(encoding="utf-8")
check(text.startswith(f"# Plain-English backfill manifest — {today}"), f"AC3: manifest header: {text[:80]!r}")
batch1 = text.split("## Batch 1", 1)[1].split("## Batch 2", 1)[0]
batch2 = text.split("## Batch 2", 1)[1]
check(len([l for l in batch1.splitlines() if l.startswith("- L-spec-")]) == 15, "AC3: batch 1 has 15")
check(len([l for l in batch2.splitlines() if l.startswith("- L-spec-")]) == 2, "AC3: batch 2 has 2")

manifests[0].unlink()
p2 = run(AC3_ROOT, "manifest", "--batch", "5")
check(p2.returncode == 0, f"AC3: manifest --batch 5 exit 0: {p2.stderr}")
check(p2.stdout.strip() == "4 batches, 17 eligible, 0 needing a hand line.",
      f"AC3: --batch 5 summary: {p2.stdout!r}")
manifests2 = list((AC3_ROOT / "content").glob("plain-backfill-manifest-*.md"))
text2 = manifests2[0].read_text(encoding="utf-8")
sizes = []
for i in range(1, 5):
    chunk = text2.split(f"## Batch {i}", 1)[1]
    if i < 4:
        chunk = chunk.split(f"## Batch {i + 1}", 1)[0]
    sizes.append(len([l for l in chunk.splitlines() if l.startswith("- L-spec-")]))
check(sizes == [5, 5, 5, 2], f"AC3: batch sizes 5/5/5/2: {sizes}")
print("0479-AC3 ok")

# ── AC4 · apply, store round trip, production write path, events/ untouched ───
AC4_ROOT = pathlib.Path(tempfile.mkdtemp())
write_spec(AC4_ROOT, "L-spec-9104")
write_events(AC4_ROOT, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-9104", "charter": "L-charter-x"})

packet4 = AC4_ROOT / "packets" / "L-charter-0047-research-1.md"
packet4.parent.mkdir(parents=True, exist_ok=True)
packet4.write_text("kind: plain-english-backfill-batch\nspecs:\n- L-spec-9104\n", encoding="utf-8")

jsonl4 = AC4_ROOT / "content" / "plain-backfill-1.jsonl"
jsonl4.write_text(json.dumps({
    "spec": "L-spec-9104",
    "line": "It ships the weekly digest email to every active client automatically."}) + "\n",
    encoding="utf-8")

before4 = hash_events(AC4_ROOT)
r1 = run(AC4_ROOT, "apply", str(jsonl4), str(packet4))
check(r1.returncode == 0, f"AC4: apply #1 exit 0: {r1.stderr}")
check(r1.stdout.strip() == "written=1 skipped=0 rejected=0", f"AC4: apply #1 output: {r1.stdout!r}")

sidecar4 = AC4_ROOT / "content" / "L-spec-9104.plain.txt"
read_code = f"import pathlib\nprint(pathlib.Path({str(sidecar4)!r}).read_text(encoding='utf-8'), end='')\n"
rr = subprocess.run([sys.executable, "-c", read_code], capture_output=True, text=True)
check(rr.returncode == 0, f"AC4: sidecar read exit 0: {rr.stderr}")
check(rr.stdout == "It ships the weekly digest email to every active client automatically.\n",
      f"AC4: sidecar contents: {rr.stdout!r}")

r2 = run(AC4_ROOT, "apply", str(jsonl4), str(packet4))
check(r2.returncode == 0, f"AC4: apply #2 exit 0: {r2.stderr}")
check(r2.stdout.strip() == "written=0 skipped=1 rejected=0", f"AC4: apply #2 output: {r2.stdout!r}")

rr2 = subprocess.run([sys.executable, "-c", read_code], capture_output=True, text=True)
check(rr2.stdout == "It ships the weekly digest email to every active client automatically.\n",
      "AC4: sidecar unchanged after second apply")

after4 = hash_events(AC4_ROOT)
check(before4 == after4, f"AC4: events/ hash set unchanged: {before4} vs {after4}")
print("0479-AC4 ok")

# ── AC5 · decide_row's reject taxonomy, in order, and the cap it feeds ────────
AC5_ROOT = pathlib.Path(tempfile.mkdtemp())
write_spec(AC5_ROOT, "L-spec-9105")
write_events(AC5_ROOT, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-9105", "charter": "L-charter-x"})

packet5 = AC5_ROOT / "packets" / "L-charter-0047-research-1.md"
packet5.parent.mkdir(parents=True, exist_ok=True)
packet5.write_text("kind: plain-english-backfill-batch\nspecs:\n- L-spec-9105\n", encoding="utf-8")

rows5 = [
    {"spec": "L-spec-9199", "line": "A perfectly fine eight to thirty word sentence that ends properly."},
    {"spec": "L-spec-9105", "line": "Too short."},
    {"spec": "L-spec-9105",
     "line": "This line names L-spec-0001 directly which eight or more words alone cannot excuse here."},
    {"spec": "L-spec-9105",
     "line": "This sentence is fine length-wise but still uses the word fold somewhere in it."},
]
jsonl5 = AC5_ROOT / "content" / "plain-backfill-1.jsonl"
jsonl5.parent.mkdir(parents=True, exist_ok=True)
jsonl5.write_text("\n".join(json.dumps(r) for r in rows5) + "\nnot json\n", encoding="utf-8")

p5 = run(AC5_ROOT, "apply", str(jsonl5), str(packet5))
check(p5.returncode == 0, f"AC5: apply exit 0: {p5.stderr}")
lines5 = p5.stdout.strip("\n").splitlines()
check(lines5[0] == "written=0 skipped=0 rejected=5", f"AC5: summary: {lines5[0]!r}")
reasons5 = lines5[1:]
check(len(reasons5) == 5, f"AC5: five reject lines: {reasons5}")
check(reasons5[0] == "?: malformed JSON line", f"AC5: malformed first: {reasons5[0]!r}")
check(reasons5[1] == "L-spec-9199: unknown spec subject", f"AC5: unknown subject: {reasons5[1]!r}")
check(reasons5[2] == "L-spec-9105: not 8-30 words ending in a period", f"AC5: too short: {reasons5[2]!r}")
check(reasons5[3] == "L-spec-9105: names a spec or charter id", f"AC5: names an id: {reasons5[3]!r}")
check(reasons5[4] == "L-spec-9105: uses jargon word 'fold'", f"AC5: jargon: {reasons5[4]!r}")

rl5 = rejected_lines(AC5_ROOT)
check(len(rl5) == 5, f"AC5: rejected.jsonl gains exactly 5 lines: {rl5}")
counts5 = plain_backfill.rejection_counts(AC5_ROOT)
check(counts5.get("L-spec-9105") == 3, f"AC5: rejection_counts()[L-spec-9105] == 3: {counts5}")

fold.ROOT, fold.EVENTS = AC5_ROOT, AC5_ROOT / "events"
specs5, *_ = fold.fold(fold.read_events())
eligible5, hand_line5 = plain_backfill.split_batches(specs5, AC5_ROOT)
check("L-spec-9105" in hand_line5 and "L-spec-9105" not in eligible5,
      f"AC5: L-spec-9105 now in hand_line: eligible={eligible5} hand_line={hand_line5}")
print("0479-AC5 ok")

# ── AC9 · omission path — partial omission, and a fully-missing jsonl ─────────
AC9_ROOT = pathlib.Path(tempfile.mkdtemp())
write_spec(AC9_ROOT, "L-spec-9107")
write_spec(AC9_ROOT, "L-spec-9108")
write_events(AC9_ROOT, "L-executor-0001",
             {"type": "spec-written", "subject": "L-spec-9107", "charter": "L-charter-x"},
             {"type": "spec-written", "subject": "L-spec-9108", "charter": "L-charter-x"})

p1_9 = run(AC9_ROOT, "packet", "--batch", "15")
check(p1_9.returncode == 0, f"AC9: packet #1 exit 0: {p1_9.stderr}")
check(p1_9.stdout.strip() == "packet 1: 2 specs -> packets/L-charter-0047-research-1.md",
      f"AC9: packet #1 output: {p1_9.stdout!r}")
packet1_9 = AC9_ROOT / "packets" / "L-charter-0047-research-1.md"
ptext1 = packet1_9.read_text(encoding="utf-8")
check("kind: plain-english-backfill-batch" in ptext1 and "specs:" in ptext1
      and "- L-spec-9107" in ptext1 and "- L-spec-9108" in ptext1, f"AC9: packet 1 shape: {ptext1!r}")

jsonl_a = AC9_ROOT / "content" / "plain-backfill-1.jsonl"
jsonl_a.parent.mkdir(parents=True, exist_ok=True)
jsonl_a.write_text(json.dumps({
    "spec": "L-spec-9107",
    "line": "This one automatically ships a fresh weekly report to the client."}) + "\n", encoding="utf-8")
pa = run(AC9_ROOT, "apply", str(jsonl_a), str(packet1_9))
check(pa.returncode == 0, f"AC9: case A apply exit 0: {pa.stderr}")
lines_a = pa.stdout.strip("\n").splitlines()
check(lines_a[0] == "written=1 skipped=0 rejected=1", f"AC9: case A summary: {lines_a[0]!r}")
check(lines_a[1] == "L-spec-9108: omitted by research", f"AC9: case A omission line: {lines_a[1]!r}")

fold.ROOT, fold.EVENTS = AC9_ROOT, AC9_ROOT / "events"
counts_a = plain_backfill.rejection_counts(AC9_ROOT)
check(counts_a.get("L-spec-9108") == 1, f"AC9: rejection_counts()[L-spec-9108] == 1: {counts_a}")

# Case B: missing jsonl (a timed-out dispatch) — a fresh pair, a fresh packet name.
#
# problem:spec-formula-contradiction — the spec's AC9 text says this `packet
# --batch 15` call yields "2 specs" (just the fresh 9109/9110 pair). That is
# false given the spec's OWN `split_batches` rule and this AC's own Case A
# fixture: `L-spec-9108` was omitted only ONCE there (rejection_counts()==1,
# strictly < 2), so `split_batches` — verbatim as specified — legitimately
# still counts it `eligible`, and it resurfaces in this batch alongside the
# fresh pair: 3 specs, not 2 (confirmed live against the real subprocess
# before writing this assertion). The design intent AC9 is actually testing
# — an omission counts toward SD8's two-rejection cap and the loop terminates
# — holds regardless; asserted below against the real, correct numbers.
write_spec(AC9_ROOT, "L-spec-9109")
write_spec(AC9_ROOT, "L-spec-9110")
write_events(AC9_ROOT, "L-executor-0001",
             {"type": "spec-written", "subject": "L-spec-9109", "charter": "L-charter-x"},
             {"type": "spec-written", "subject": "L-spec-9110", "charter": "L-charter-x"})
p2_9 = run(AC9_ROOT, "packet", "--batch", "15")
check(p2_9.returncode == 0, f"AC9: packet #2 exit 0: {p2_9.stderr}")
check(p2_9.stdout.strip() == "packet 2: 3 specs -> packets/L-charter-0047-research-2.md",
      f"AC9: packet #2 output (fresh name, never reusing 1; 3 specs incl. the once-omitted 9108): {p2_9.stdout!r}")
packet2_9 = AC9_ROOT / "packets" / "L-charter-0047-research-2.md"
missing_jsonl = AC9_ROOT / "content" / "plain-backfill-2.jsonl"
check(not missing_jsonl.exists(), "AC9: jsonl never created — the timed-out case")
pb = run(AC9_ROOT, "apply", str(missing_jsonl), str(packet2_9))
check(pb.returncode == 0, f"AC9: case B apply exit 0 (no longer a usage error): {pb.stderr}")
lines_b = pb.stdout.strip("\n").splitlines()
check(lines_b[0] == "written=0 skipped=0 rejected=3", f"AC9: case B summary: {lines_b[0]!r}")
check(set(lines_b[1:]) == {"L-spec-9108: omitted by research", "L-spec-9109: omitted by research",
                            "L-spec-9110: omitted by research"},
      f"AC9: case B all three omitted: {lines_b[1:]}")

# L-spec-9108 already reached its SECOND omission in case B above (count 1 -> 2)
# and so already hit the cap; one further missing-jsonl apply round naming only
# L-spec-9109 brings ITS rejection count to 2 as well.
packet3_9 = AC9_ROOT / "packets" / "L-charter-0047-research-3.md"
packet3_9.write_text("kind: plain-english-backfill-batch\nspecs:\n- L-spec-9109\n", encoding="utf-8")
pc = run(AC9_ROOT, "apply", str(AC9_ROOT / "content" / "plain-backfill-3.jsonl"), str(packet3_9))
check(pc.returncode == 0, f"AC9: third missing-jsonl apply exit 0: {pc.stderr}")

specs9, *_ = fold.fold(fold.read_events())
eligible9, hand_line9 = plain_backfill.split_batches(specs9, AC9_ROOT)
check("L-spec-9108" in hand_line9 and "L-spec-9108" not in eligible9,
      f"AC9: L-spec-9108 already at the cap from case B: eligible={eligible9} hand_line={hand_line9}")
check("L-spec-9109" in hand_line9 and "L-spec-9109" not in eligible9,
      f"AC9: L-spec-9109 reaches the cap, terminating the loop: eligible={eligible9} hand_line={hand_line9}")
check("L-spec-9110" in eligible9, f"AC9: L-spec-9110 (one omission only) stays eligible: {eligible9}")
print("0479-AC9 ok")

print(f"plain_backfill: {N} checks pass")
