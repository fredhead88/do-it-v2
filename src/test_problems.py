#!/usr/bin/env python3
"""One runnable check on the problem-register rules (L-spec-0279). Run: python3 test_problems.py"""
import contextlib, io, itertools, json, os, pathlib, shutil, sys, tempfile
from datetime import datetime, timedelta, timezone

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"] = str(TMP)
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fold, problems  # noqa: E402

T = datetime(2026, 9, 24, tzinfo=timezone.utc)


def iso(minutes):
    return (T + timedelta(minutes=minutes)).isoformat(timespec="seconds")


_n = itertools.count()


def ev(type_, actor="operator", src=None, **kw):
    """A hand-built, already-`read_events()`-shaped event: `actor`/`_src` set
    directly (the same pattern test_fold.py's AC21 uses), so most of this file
    exercises `register()`/`unassigned_summary()` directly without touching disk."""
    e = {"v": 1, "type": type_, "actor": actor, "_src": src or f"fixture:{next(_n)}"}
    e.update(kw)
    return e


def write_ledger(files):
    """The one place this file DOES touch disk — for AC19, which needs a real
    filename to prove D90's actor-from-filename stamping feeds register()'s own
    EMITS filter, independent of append()/check_append()."""
    events_dir = TMP / "events"
    shutil.rmtree(events_dir, ignore_errors=True)
    events_dir.mkdir(parents=True)
    for name, evs in files.items():
        (events_dir / name).write_text("".join(json.dumps(e) + "\n" for e in evs))
    return fold.read_events()


# ── AC1 · 3 lesson problem=X (distinct ts) + 1 problem-grouped problem=X, all
# actors open -> exactly one Problem, slug=="X", occurrences==4
e1 = ev("lesson", problem="X", ts=iso(0))
e2 = ev("lesson", problem="X", ts=iso(1))
e3 = ev("lesson", problem="X", ts=iso(2))
e4 = ev("problem-grouped", problem="X", ts=iso(3), lesson_src="elsewhere:1")
r1 = problems.register([e1, e2, e3, e4])
assert len(r1) == 1 and r1[0]["slug"] == "X" and r1[0]["occurrences"] == 4, r1
print("AC1 ok")

# ── AC2 · two near-identical-statement slugs never merge; each occurrences==1
d1 = ev("lesson", problem="dup-a", statement="the deploy silently skipped rows", ts=iso(0))
d2 = ev("lesson", problem="dup-b", statement="the deploy silently skipped rows too", ts=iso(1))
r2 = problems.register([d1, d2])
by2 = {p["slug"]: p for p in r2}
assert len(r2) == 2 and by2["dup-a"]["occurrences"] == 1 and by2["dup-b"]["occurrences"] == 1, r2
print("AC2 ok")

# ── AC3 · a lesson for a never-seen slug still mints (not a rejection);
# first_seen==last_seen==that event's ts
f3 = ev("lesson", problem="fresh-slug", ts=iso(5))
r3 = problems.register([f3])
assert r3[0]["slug"] == "fresh-slug" and r3[0]["first_seen"] == r3[0]["last_seen"] == iso(5), r3
print("AC3 ok")

# ── AC4 · 3 occurrences at distinct ts, fed OUT of ts order: first_seen/
# last_seen are the exact min/max, not append order
y1 = ev("lesson", problem="Y", ts=iso(10))
y2 = ev("lesson", problem="Y", ts=iso(2))
y3 = ev("lesson", problem="Y", ts=iso(7))
r4 = problems.register([y1, y2, y3])
assert r4[0]["first_seen"] == iso(2) and r4[0]["last_seen"] == iso(10), r4
print("AC4 ok")

# ── AC5 · two different problems.toml (differing only rank.cost_divisor_min)
# produce different orderings for a fixture with differing cost_min
lo1 = ev("lesson", problem="lo-cost", ts=iso(0), cost_min=1)
lo2 = ev("lesson", problem="lo-cost", ts=iso(1), cost_min=1)
lo3 = ev("lesson", problem="lo-cost", ts=iso(2), cost_min=1)
hi1 = ev("lesson", problem="hi-cost", ts=iso(3), cost_min=1000)
fixture5 = [lo1, lo2, lo3, hi1]
toml_a, toml_b = TMP / "a.toml", TMP / "b.toml"
toml_a.write_text("[rank]\ncost_divisor_min = 1.0\n")
toml_b.write_text("[rank]\ncost_divisor_min = 100000.0\n")
order_a = [p["slug"] for p in problems.register(fixture5, toml_path=str(toml_a))]
order_b = [p["slug"] for p in problems.register(fixture5, toml_path=str(toml_b))]
assert order_a == ["hi-cost", "lo-cost"], order_a
assert order_b == ["lo-cost", "hi-cost"], order_b
assert order_a != order_b, "the divisor must come from the given config, not be hardcoded"
print("AC5 ok")

# ── AC6 · cost_min sums across occurrences that carry it; cost_unmeasured
# counts the ones that don't; a slug with none of them sums to None
p1a = ev("lesson", problem="p1", ts=iso(0), cost_min=5)
p1b = ev("lesson", problem="p1", ts=iso(1), cost_min=7)
p1c = ev("lesson", problem="p1", ts=iso(2))
p2a = ev("lesson", problem="p2", ts=iso(0))
p2b = ev("lesson", problem="p2", ts=iso(1))
r6 = problems.register([p1a, p1b, p1c, p2a, p2b])
by6 = {p["slug"]: p for p in r6}
assert by6["p1"]["cost_min"] == 12 and by6["p1"]["cost_unmeasured"] == 1, by6["p1"]
assert by6["p2"]["cost_min"] is None and by6["p2"]["cost_unmeasured"] == 2, by6["p2"]
print("AC6 ok")

# ── AC7 · --health lists all three SD15 acts as owed with zero problem-grouped
# events; the backfill-confirmation act alone drops once one clears a lesson
u7 = ev("lesson", ts=iso(0))              # no problem= at all -> unassigned
rows7a = problems.health([u7])
assert len(rows7a) == 3 and all(r["owed"] for r in rows7a), rows7a
g7 = ev("problem-grouped", problem="p9", ts=iso(1), lesson_src=u7["_src"])
rows7b = problems.health([u7, g7])
acts7 = {r["act"]: r["owed"] for r in rows7b}
assert acts7["backfill-confirmation"] is False, acts7
assert acts7["OUTSTANDING.md banner"] is True and acts7["pointer-file swap"] is True, acts7
print("AC7 ok")

# ── AC8 · a mint with no statement=, no problem-minted anywhere for the slug:
# statement_needed==True, statement==the lesson's own text
l8 = ev("lesson", problem="mint-a", text="thing broke", ts=iso(0))
r8 = problems.register([l8])
assert r8[0]["statement_needed"] is True and r8[0]["statement"] == "thing broke", r8[0]
print("AC8 ok")

# ── AC9 · same shape, but the lesson carries statement= directly: that wins,
# never the text= fallback
l9 = ev("lesson", problem="mint-b", statement="the real statement", text="raw text field", ts=iso(0))
r9 = problems.register([l9])
assert r9[0]["statement_needed"] is False and r9[0]["statement"] == "the real statement", r9[0]
print("AC9 ok")

# ── AC10 · unassigned/keyless lessons never become a Problem; unassigned_summary
# counts them, minus any problem-grouped-cleared one, and re-derives oldest_ts
un1 = ev("lesson", problem="unassigned", ts=iso(0))
un2 = ev("lesson", problem="unassigned", ts=iso(1))
un3 = ev("lesson", ts=iso(2))                          # no problem key at all
pp1 = ev("lesson", problem="p1", ts=iso(3))
fixture10 = [un1, un2, un3, pp1]
r10 = problems.register(fixture10)
assert len(r10) == 1 and r10[0]["slug"] == "p1", r10
s10a = problems.unassigned_summary(fixture10)
assert s10a == {"count": 3, "oldest_ts": iso(0)}, s10a
g10 = ev("problem-grouped", problem="p9", ts=iso(4), lesson_src=un1["_src"])  # clears the MIN-ts one
s10b = problems.unassigned_summary(fixture10 + [g10])
assert s10b == {"count": 2, "oldest_ts": iso(1)}, s10b
print("AC10 ok")

# ── AC12 · fold.append()'s mint hook: exactly 3 of 4 existing slugs named on a
# mint, all 2 when only 2 exist, nothing on a non-mint re-append. suggest_slugs'
# OWN ordering guarantee (independent of the CLI): the highest-ratio candidate
# comes first.
os.environ.pop("DOIT_LEDGER_FILE", None)
shutil.rmtree(TMP / "events", ignore_errors=True)
(TMP / "events").mkdir(parents=True)
for i, slug in enumerate(["reg-a", "reg-b", "reg-c", "reg-d"]):
    fold.append(["lesson", f"S12-{i}", f"problem={slug}", "text=x"])
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    fold.append(["lesson", "S12-mint", "problem=reg-new", "text=y"])
named = sum(1 for slug in ["reg-a", "reg-b", "reg-c", "reg-d"] if slug in buf.getvalue())
assert named == 3, buf.getvalue()

shutil.rmtree(TMP / "events", ignore_errors=True)
(TMP / "events").mkdir(parents=True)
for i, slug in enumerate(["only-a", "only-b"]):
    fold.append(["lesson", f"S12b-{i}", f"problem={slug}", "text=x"])
buf2 = io.StringIO()
with contextlib.redirect_stderr(buf2):
    fold.append(["lesson", "S12b-mint", "problem=only-new", "text=y"])
assert "only-a" in buf2.getvalue() and "only-b" in buf2.getvalue(), buf2.getvalue()

buf3 = io.StringIO()
with contextlib.redirect_stderr(buf3):
    fold.append(["lesson", "S12b-again", "problem=only-a", "text=z"])   # already-seen slug: non-mint
assert buf3.getvalue() == "", repr(buf3.getvalue())

cands12 = [{"slug": "far-off", "statement": "totally unrelated"},
           {"slug": "close-match", "statement": "the deploy silently skipped rows"},
           {"slug": "medium", "statement": "the deploy skipped some rows"}]
top12 = problems.suggest_slugs("new-slug", "the deploy silently skipped rows exactly", cands12)
assert top12[0] == "close-match", top12
print("AC12 ok")

# ── AC13/AC11/AC14 · regression coverage for the rest of R3's CLI-level ACs
# (their own review_path is a live manual run, not a `./doit test` marker —
# kept here anyway as the regression layer for the hook/--inbox code this spec
# adds to fold.append()).
shutil.rmtree(TMP / "events", ignore_errors=True)
(TMP / "events").mkdir(parents=True)
fold.append(["lesson", "S13", "problem=new-slug-xyz", "text=oops"])   # AC13: mint, no statement=
rows13 = problems.register(fold.read_events())
p13 = next(p for p in rows13 if p["slug"] == "new-slug-xyz")
assert p13["statement_needed"] is True, p13

fold.append(["lesson", "S11", "text=foo"])                             # AC11: no problem= -> unassigned
found11 = [e for e in fold.read_events() if e.get("subject") == "S11"]
assert found11 and found11[-1]["problem"] == "unassigned", found11

os.environ["DOIT_LEDGER_FILE"] = "L-other-actor.jsonl"                 # AC14: --inbox overrides it
try:
    fold.append(["lesson", "S14", "problem=x", "--inbox"])
    inbox_text = (TMP / "events" / "L-inbox-local.jsonl").read_text()
    other_path = TMP / "events" / "L-other-actor.jsonl"
    other_text = other_path.read_text() if other_path.exists() else ""
    assert "S14" in inbox_text and "S14" not in other_text, (inbox_text, other_text)
finally:
    os.environ.pop("DOIT_LEDGER_FILE", None)

# ── AC15 · earliest shipped wins (not append order or latest); a lesson.fix
# whose spec never ships contributes no fixes[] entry at all
T0, T0b, T1, T3 = iso(0), iso(1), iso(2), iso(3)
ls15 = ev("lesson", subject="S", problem="p1", fix="L-spec-0001", ts=T0)
ls15b = ev("lesson", subject="S3", problem="p1", fix="L-spec-0002", ts=T0b)   # never ships
sh15_late = ev("shipped", subject="L-spec-0001", ts=T3, actor="executor")
sh15_early = ev("shipped", subject="L-spec-0001", ts=T1, actor="executor")
fixture15 = [ls15, ls15b, sh15_late, sh15_early]                              # appended out of ts order
r15 = problems.register(fixture15)
p1_15 = next(p for p in r15 if p["slug"] == "p1")
assert p1_15["fixes"] == [{"ref": "L-spec-0001", "shipped_at": T1, "held": True}], p1_15["fixes"]
assert p1_15["recurred_after_fix"] is False, p1_15
assert all(f["ref"] != "L-spec-0002" for f in p1_15["fixes"]), p1_15
print("AC15 ok")

# ── AC16 · a new occurrence after the shipped_at flips held to False
ls16 = ev("lesson", subject="S2", problem="p1", ts=iso(2.5))                  # T1 < iso(2.5) < T3
r16 = problems.register(fixture15 + [ls16])
p1_16 = next(p for p in r16 if p["slug"] == "p1")
assert p1_16["fixes"][0]["held"] is False, p1_16["fixes"]
assert p1_16["recurred_after_fix"] is True, p1_16
assert p1_16["occurrences"] == 3, p1_16
print("AC16 ok")

# ── AC17 · a manual fix-shipped with no matching lesson.fix still adds a fix,
# held via the same rule (vacuously True: zero occurrences to exceed it)
fix17 = ev("fix-shipped", subject="p2", ref="abc123", ts=T1, actor="thinker")
r17 = problems.register([fix17])
p2_17 = next(p for p in r17 if p["slug"] == "p2")
assert p2_17["fixes"] == [{"ref": "abc123", "shipped_at": T1, "held": True}], p2_17
print("AC17 ok")

# ── AC19 · an unauthorized fix-shipped (actor `builder`, via a real
# `L-builder-0007.jsonl` fixture file — D90's filename-derived actor) is
# dropped by register()'s OWN EMITS filter, independent of append()/
# check_append(); the slug's other (authorized-type) occurrence still counts.
fix19 = {"v": 1, "type": "fix-shipped", "subject": "p2", "ref": "xyz", "ts": T1}
lesson19 = {"v": 1, "type": "lesson", "subject": "S19", "problem": "p2", "ts": T0}
raw19 = write_ledger({"L-builder-0007.jsonl": [fix19], "L-operator-local.jsonl": [lesson19]})
r19 = problems.register(raw19)
p2_19 = next(p for p in r19 if p["slug"] == "p2")
assert p2_19["fixes"] == [], p2_19
assert p2_19["occurrences"] == 1, p2_19
print("AC19 ok")
