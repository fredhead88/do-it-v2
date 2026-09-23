#!/usr/bin/env python3
"""One runnable check on `notes`. Run: python3 test_notes.py

Everything is a fixture — `DOIT_ROOT` points under a fresh temp dir, and
`intake.subprocess` is replaced BEFORE any fixture calls `notes.run`/
`notes.note_close` (AC11), so no `gh` process and no network connection is
ever reached from this file.
"""
import json, os, pathlib, sys, tempfile, types

TMP = pathlib.Path(tempfile.mkdtemp(prefix="notes-test-"))
os.environ["DOIT_ROOT"] = str(TMP)
os.environ.pop("DOIT_PROJECT", None)
for d in ("events", "content"):
    (TMP / d).mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import carry, dispatch, fold, intake, notes, tick  # noqa: E402

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


# ── network guard (AC11) ─────────────────────────────────────────────────────
NET = [0]
import urllib.request  # noqa: E402


def no_network(*a, **k):
    NET[0] += 1
    raise AssertionError("notes must never open a network connection")


urllib.request.urlopen = no_network


def _ok(out=""):
    return types.SimpleNamespace(returncode=0, stdout=out, stderr="")


def _fail(err="boom"):
    return types.SimpleNamespace(returncode=1, stdout="", stderr=err)


CALLS = []


class Stub:
    """Stands in for `intake.subprocess` — the `test_intake.py` pattern
    (Constraints), recording argv and returning a canned result, never
    running anything real. Covers only the `gh` shapes `notes.note_close`'s
    own `intake.comment`/`intake.close` calls reach."""
    def __init__(self):
        self.comments = {}
        self.states = {}
        self.comment_fail = False
        self.close_fail = False

    def run(self, argv, capture_output=True, text=True, **kw):
        CALLS.append(list(argv))
        if argv[:3] == ["gh", "pr", "view"] and "comments" in argv:
            url = argv[3]
            return _ok(json.dumps({"comments": [{"body": b} for b in self.comments.get(url, [])]}))
        if argv[:3] == ["gh", "pr", "view"] and "state" in argv:
            url = argv[3]
            return _ok(json.dumps({"state": self.states.get(url, "OPEN")}))
        if argv[:3] == ["gh", "pr", "comment"]:
            if self.comment_fail:
                return _fail()
            url, body = argv[3], argv[argv.index("--body") + 1]
            self.comments.setdefault(url, []).append(body)
            return _ok("")
        if argv[:3] == ["gh", "pr", "close"]:
            if self.close_fail:
                return _fail()
            self.states[argv[3]] = "CLOSED"
            return _ok("")
        raise AssertionError(f"unexpected gh call in test_notes.py stub: {argv}")


STUB = Stub()
intake.subprocess = STUB


def reset():
    for p in (TMP / "events").glob("*.jsonl"):
        p.unlink()
    STUB.__init__()
    CALLS.clear()


def pr(url, author, title, created):
    return {"url": url, "number": 1, "author_login": author, "title": title,
           "created_at": created, "body": "unread", "files": []}


def append_line(name, event):
    p = TMP / "events" / name
    with open(p, "a") as fh:
        fh.write(json.dumps({"v": 1, **event}) + "\n")


T0 = "2026-09-01T00:00:00+00:00"
T1 = "2026-09-02T00:00:00+00:00"
T2 = "2026-09-03T00:00:00+00:00"
T3 = "2026-09-04T00:00:00+00:00"

# ══ AC1 · listed once, no dupe on an identical re-run, fresh on reopen ═══════
reset()
U1 = "https://github.com/fredhead88/albert-scott-platform/pull/101"
ev = fold.read_events()
out1 = notes.run(ev, [pr(U1, "alice", "First note", T0)])
check(out1 == {"listed": 1, "gone": 0}, f"AC1: first sighting lists once: {out1}")

ev = fold.read_events()
out2 = notes.run(ev, [pr(U1, "alice", "First note", T0)])
check(out2 == {"listed": 0, "gone": 0}, f"AC1: an identical re-run appends nothing new: {out2}")
listed1 = [e for e in fold.read_events() if e["type"] == "inbound-note-listed" and e["subject"] == U1]
check(len(listed1) == 1, f"AC1: exactly one inbound-note-listed after both calls: {listed1}")

# U1 goes away (another url present so open_note_prs is non-empty) -> gone
U2 = "https://github.com/fredhead88/albert-scott-platform/pull/102"
ev = fold.read_events()
out3 = notes.run(ev, [pr(U2, "bob", "Other note", T1)])
check(out3 == {"listed": 1, "gone": 1}, f"AC1 setup: U1 goes gone, U2 listed: {out3}")

# U1 reappears -> fresh inbound-note-listed
ev = fold.read_events()
out4 = notes.run(ev, [pr(U1, "alice", "First note", T2), pr(U2, "bob", "Other note", T1)])
check(out4 == {"listed": 1, "gone": 0}, f"AC1: a reopened url gets a fresh listing: {out4}")
listed1b = [e for e in fold.read_events() if e["type"] == "inbound-note-listed" and e["subject"] == U1]
check(len(listed1b) == 2, f"AC1: reopen appended a SECOND inbound-note-listed for U1: {listed1b}")

print("notes: AC1 checks pass")

# ══ AC2 · gone inferred only when open_note_prs is non-empty this call ═══════
reset()
Ua, Ub, Uc = ("https://github.com/fredhead88/albert-scott-platform/pull/201",
             "https://github.com/fredhead88/albert-scott-platform/pull/202",
             "https://github.com/fredhead88/albert-scott-platform/pull/203")
ev = fold.read_events()
notes.run(ev, [pr(Ua, "a", "A", T0), pr(Ub, "b", "B", T0)])
append_line("L-operator-local.jsonl", {"ts": T1, "type": "note-answered", "subject": Uc,
                                       "source": Uc, "project": "albert-scott"})
ev = fold.read_events()
out5 = notes.run(ev, [pr(Ua, "a", "A", T0)])          # Ub absent, non-empty call
check(out5["gone"] == 1, f"AC2: exactly the newly-closed url gets inbound-note-gone: {out5}")
gone5 = [e for e in fold.read_events() if e["type"] == "inbound-note-gone"]
check(len(gone5) == 1 and gone5[0]["subject"] == Ub, f"AC2: it names Ub, not Ua or Uc: {gone5}")

reset()
ev = fold.read_events()
notes.run(ev, [pr(Ua, "a", "A", T0), pr(Ub, "b", "B", T0)])
ev = fold.read_events()
out6 = notes.run(ev, [])                               # empty listing this call
check(out6["gone"] == 0, f"AC2: an EMPTY open_note_prs appends NO inbound-note-gone: {out6}")
gone6 = [e for e in fold.read_events() if e["type"] == "inbound-note-gone"]
check(len(gone6) == 0, f"AC2: zero gone events for either tracked url: {gone6}")

print("notes: AC2 checks pass")

# ══ AC5 · a malformed pr or a bad response_file refuses BEFORE any intake call ═
reset()
VALID_PR = "https://github.com/fredhead88/albert-scott-platform/pull/42"
good_file = TMP / "resp_good.txt"
good_file.write_text("Thanks for the note!")
missing_file = TMP / "does_not_exist.txt"
empty_file = TMP / "resp_empty.txt"
empty_file.write_text("   \n\t")

rc_a = notes.note_close(VALID_PR, str(missing_file))
check(rc_a == 1 and not CALLS, f"AC5a: missing response file refuses, zero calls: {rc_a} {CALLS}")
rc_b = notes.note_close(VALID_PR, str(empty_file))
check(rc_b == 1 and not CALLS, f"AC5b: empty response file refuses, zero calls: {rc_b} {CALLS}")
rc_c = notes.note_close("123", str(good_file))
check(rc_c == 1 and not CALLS, f"AC5c: a bare PR number refuses, zero calls: {rc_c} {CALLS}")

print("notes: AC5 checks pass")

# ══ AC6 · a fresh valid pair comments, closes, appends note-answered ════════
reset()
U6 = "https://github.com/fredhead88/albert-scott-platform/pull/601"
resp6 = TMP / "resp6.txt"
resp6.write_text("Answering your note: all good.")
rc6 = notes.note_close(U6, str(resp6))
check(rc6 == 0, f"AC6: a fresh valid pair returns 0: {rc6}")
check(STUB.comments.get(U6) == ["Answering your note: all good."],
     f"AC6: the recorded comment body equals the file's bytes decoded: {STUB.comments}")
check(STUB.states.get(U6) == "CLOSED", "AC6: intake.close was called")
answered6 = [e for e in fold.read_events() if e["type"] == "note-answered" and e["subject"] == U6]
check(len(answered6) == 1, f"AC6: exactly one note-answered: {answered6}")
a6 = answered6[0]
check(a6.get("source") == U6 and a6.get("project") == "albert-scott" and a6.get("response") == str(resp6),
     f"AC6: all four fields present: {a6}")
check(any(c[:3] == ["gh", "pr", "comment"] for c in CALLS) and any(c[:3] == ["gh", "pr", "close"] for c in CALLS),
     f"AC11: the recorded dump shows an intake.comment and an intake.close call: {CALLS}")

print("notes: AC6 checks pass")

# ══ AC7 · the false-success short-circuit: comment ok, close fails, then retry ═
reset()
U7 = "https://github.com/fredhead88/albert-scott-platform/pull/701"
resp7 = TMP / "resp7.txt"
resp7.write_text("Reply text for U7.")
STUB.close_fail = True
rc7a = notes.note_close(U7, str(resp7))
check(rc7a == 2, f"AC7 call1: comment ok, close False -> 2: {rc7a}")
answered7a = [e for e in fold.read_events() if e["type"] == "note-answered" and e["subject"] == U7]
check(len(answered7a) == 0, f"AC7 call1: a failed close appends nothing: {answered7a}")

STUB.close_fail = False
rc7b = notes.note_close(U7, str(resp7))
check(rc7b == 0, f"AC7 call2: close now succeeds -> 0: {rc7b}")
answered7b = [e for e in fold.read_events() if e["type"] == "note-answered" and e["subject"] == U7]
check(len(answered7b) == 1, f"AC7 call2: exactly one note-answered appends: {answered7b}")

rc7c = notes.note_close(U7, str(resp7))
check(rc7c == 0, f"AC7 call3: already answered -> still 0: {rc7c}")
answered7c = [e for e in fold.read_events() if e["type"] == "note-answered" and e["subject"] == U7]
check(len(answered7c) == 1, f"AC7 call3: no further append: {answered7c}")

print("notes: AC7 checks pass")

# ══ AC9 · fold.EMITS already carries the three SD1 entries (wave-1's own work) ═
check(fold.EMITS["inbound-note-listed"] == {"intake"},
     f"AC9: fold.EMITS['inbound-note-listed']: {fold.EMITS['inbound-note-listed']}")
check(fold.EMITS["inbound-note-gone"] == {"intake"},
     f"AC9: fold.EMITS['inbound-note-gone']: {fold.EMITS['inbound-note-gone']}")
check(fold.EMITS["note-answered"] == {"thinker", "operator"},
     f"AC9: fold.EMITS['note-answered']: {fold.EMITS['note-answered']}")

reset()
U9 = "https://github.com/fredhead88/albert-scott-platform/pull/901"
append_line("L-thinker-local.jsonl", {"ts": T0, "type": "inbound-note-listed", "subject": U9,
                                      "source": U9, "project": "albert-scott"})
append_line("L-thinker-local.jsonl", {"ts": T1, "type": "inbound-note-gone", "subject": U9,
                                      "source": U9, "project": "albert-scott"})
append_line("L-executor-local.jsonl", {"ts": T2, "type": "note-answered", "subject": U9,
                                       "source": U9, "project": "albert-scott"})
_, _, ignored9, by_subject9 = fold.fold(fold.read_events())
mis9 = {(e["type"], e["actor"]) for e in ignored9 if e.get("subject") == U9}
check(mis9 == {("inbound-note-listed", "thinker"), ("inbound-note-gone", "thinker"),
              ("note-answered", "executor")},
     f"AC9: all three mis-actored events land in ignored: {mis9}")
check(not any(e.get("subject") == U9 for e in by_subject9.get(U9, [])),
     f"AC9: none counted toward by_subject: {by_subject9.get(U9)}")

print("notes: AC9 checks pass")

# ══ AC10 · neither notes.py nor this spec's tick.py diff names ingest_inbound_spec
NOTES_SRC = (pathlib.Path(__file__).resolve().parent / "notes.py").read_text()
check("ingest_inbound_spec" not in NOTES_SRC, "AC10: src/notes.py never names ingest_inbound_spec")

print("notes: AC10 check passes")

# ══ AC11 · the stub was installed before any call above, and no real network fired
check(intake.subprocess is STUB, "AC11: intake.subprocess is the recording stub throughout")
check(NET[0] == 0, "AC11: no fixture opened a real network connection")

print("notes: AC11 checks pass")

# ══ AC13 · src/notes.py stays at or under 400 physical lines ════════════════
check(len(NOTES_SRC.splitlines()) <= 400, f"AC13: src/notes.py exceeds 400 lines")

print("notes: AC13 check passes")

# ══ AC8 · tick._record() wiring, proven entirely here — no line of
# src/test_tick.py written or read by this proof ═════════════════════════════
reset()
U8 = "https://github.com/fredhead88/albert-scott-platform/pull/801"
_real_intake_run, _real_uncarried, _real_sync_v4, _real_notes_run = (
    intake.run, carry.uncarried, carry.sync_v4, notes.run)


def fake_intake_run(events):
    return {"spec_prs": [], "note_prs": [pr(U8, "carol", "T8", T0)]}


try:
    intake.run = fake_intake_run
    carry.uncarried = lambda events: []
    carry.sync_v4 = lambda events: None
    rc8 = tick._record()
    check(rc8 is not None, "AC8: tick._record() ran (lock free)")
    listed8 = [e for e in fold.read_events() if e["type"] == "inbound-note-listed" and e["subject"] == U8]
    check(len(listed8) == 1, f"AC8: notes.run's effect landed via tick._record(): {listed8}")
    reread8 = [e for e in fold.read_events() if e["type"] == "inbound-note-listed" and e["subject"] == U8]
    check(len(reread8) == 1, "AC8: visible in the RE-READ ledger carry.uncarried receives")

    def raising_notes_run(events, open_note_prs):
        raise RuntimeError("notes boom")

    notes.run = raising_notes_run
    n0_8 = len([e for e in fold.read_events() if e["type"] == "tick"])
    rc8b = tick._record()
    check(rc8b is not None, "AC8: a raising notes.run never stops the tick's own heartbeat")
    ticks8 = [e for e in fold.read_events() if e["type"] == "tick"]
    check(len(ticks8) == n0_8 + 1, "AC8: exactly one new tick event lands")
    last8 = ticks8[-1]
    check("notes boom" in last8.get("intake_error", ""),
         f"AC8: the tick event names the caught exception on intake_error: {last8}")
    check("carry_error" not in last8, f"AC8: lane/carry_error otherwise unaffected: {last8}")
finally:
    intake.run, carry.uncarried, carry.sync_v4, notes.run = (
        _real_intake_run, _real_uncarried, _real_sync_v4, _real_notes_run)

print("notes: AC8 checks pass")

print(f"notes: {N} checks pass")
