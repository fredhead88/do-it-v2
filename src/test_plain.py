#!/usr/bin/env python3
"""One runnable check on plain.py. Run: python3 test_plain.py"""
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fold  # noqa: E402
import plain  # noqa: E402

TMP = pathlib.Path(tempfile.mkdtemp())
CONTENT = TMP / "content"
CONTENT.mkdir(parents=True)


def write(item_id, text):
    (CONTENT / f"{item_id}.md").write_text(text)


def write_sidecar(item_id, text):
    (CONTENT / plain.SIDECAR.format(item_id)).write_text(text)


# ── AC1 · goal/charter Intent extraction, four shapes ───────────────────────

# (i) a charter with a one-paragraph Intent, first sentence under 40 words.
write("L-charter-9601", (
    "# Test Charter One\n\n"
    "## 1. Intent\n"
    "This is a short intent sentence under forty words entirely. More text "
    "follows that must not appear.\n\n"
    "## 2. Requirements\n"
    "- R1 - something.\n"
))
res = plain.line("L-charter-9601", TMP)
assert res == {
    "text": "Test Charter One — This is a short intent sentence under forty words entirely.",
    "source": "intent",
}, res
print("AC1-i ok")

# (ii) a goal with an Intent whose first sentence spans >40 words.
words45 = [f"tok{i}" for i in range(1, 46)]
sentence45 = " ".join(words45) + "."
write("L-goal-9604", (
    "# Test Goal Long\n\n"
    f"## 1. Intent\n{sentence45}\n\n"
    "## 2. Something\nignored\n"
))
res = plain.line("L-goal-9604", TMP)
expected_sentence = " ".join(words45[:40]) + "…"
assert res == {"text": f"Test Goal Long — {expected_sentence}", "source": "intent"}, res
assert len(expected_sentence.rstrip("…").split()) == 40
print("AC1-ii ok")

# (iii) an Intent paragraph with no ". "/".\n" before its blank line.
write("L-charter-9602", (
    "# Test Charter NoPeriod\n\n"
    "## 1. Intent\n"
    "This paragraph has no ending punctuation\n"
    "at all just words spanning two lines\n\n"
    "## 2. Next\nignored\n"
))
res = plain.line("L-charter-9602", TMP)
assert res == {
    "text": "Test Charter NoPeriod — This paragraph has no ending punctuation at all "
            "just words spanning two lines",
    "source": "intent",
}, res
assert "\n" not in res["text"]
print("AC1-iii ok")

# (iv) an Intent whose first sentence wraps a newline before its terminating
# ". " — L-charter-0047's own wording, verbatim.
write("L-charter-9603", (
    "# Test Charter Wrap\n\n"
    "## 1. Intent\n"
    "Today the state of a charter lives in ledger events, spec files and internal "
    "state names (L1-complete,\nshipped-owed-due, reviewing). The operator has to "
    "ask the Thinker to translate.\n\n"
    "## 2. Requirements\nignored\n"
))
res = plain.line("L-charter-9603", TMP)
assert res == {
    "text": "Test Charter Wrap — Today the state of a charter lives in ledger events, "
            "spec files and internal state names (L1-complete, shipped-owed-due, reviewing).",
    "source": "intent",
}, res
assert "\n" not in res["text"]
print("AC1-iv ok")

# ── AC2 · RESERVED stub and title-only ──────────────────────────────────────

reserved_line = ("RESERVED id — used before a hypothetical ledger loss. Its record is git "
                  "master. Do not allocate; do not treat as content.")
write("L-charter-9611", reserved_line + "\n")
res = plain.line("L-charter-9611", TMP)
assert res == {"text": reserved_line, "source": "missing"}, res
print("AC2-reserved ok")

write("L-charter-9612", "# Title Only Charter\n\n## 2. Requirements\nSomething.\n")
res = plain.line("L-charter-9612", TMP)
assert res == {"text": "Title Only Charter", "source": "missing"}, res
print("AC2-title-only ok")

# ── AC3 · PLAIN_RE precedence and the bare-label non-match ──────────────────

write("L-spec-9605", (
    "# Some Spec\n\n"
    "## Goal\n\nThis is the goal section text, different from the plain line.\n\n"
    "In plain English: This is the plain line sentence.\n\n"
    "## Requirements\nSomething.\n"
))
res = plain.line("L-spec-9605", TMP)
assert res == {"text": "This is the plain line sentence.", "source": "spec-line"}, res

fixture2_text = "# Spec Two\n\nSome prose.\nIn plain English:\nUnrelated next line prose.\n"
assert plain.PLAIN_RE.search(fixture2_text) is None
assert plain.PLAIN_RE.search("in plain english: lowercase does not match") is None
assert plain.PLAIN_RE.search("In plain English no colon here") is None
print("AC3 ok")

# ── AC4 · sidecar beats a Goal section; a zero-byte sidecar does not count ──

write("L-spec-9607", (
    "# Spec Four\n\n## Goal\n\nA goal-section sentence that should be overridden "
    "by the sidecar. More text.\n\n## Requirements\nX.\n"
))
write_sidecar("L-spec-9607", "Sidecar wins over the goal section.\n")
res = plain.line("L-spec-9607", TMP)
assert res == {"text": "Sidecar wins over the goal section.", "source": "sidecar"}, res

write("L-spec-9608", (
    "# Spec Four Zero\n\n## Goal\n\nA goal section sentence used because the "
    "sidecar is empty. More text.\n\n## Requirements\nX.\n"
))
write_sidecar("L-spec-9608", "")
res = plain.line("L-spec-9608", TMP)
assert res == {
    "text": "A goal section sentence used because the sidecar is empty. (from the spec's goal)",
    "source": "goal-section",
}, res
print("AC4 ok")

# ── AC5 · Goal-section fallback, and the fully-missing case ─────────────────

write("L-spec-9609", (
    "# Spec Five\n\n## Goal\n\nA goal only sentence for AC5 testing purposes. "
    "Extra ignored text.\n\n## Requirements\nY.\n"
))
res = plain.line("L-spec-9609", TMP)
assert res == {
    "text": "A goal only sentence for AC5 testing purposes. (from the spec's goal)",
    "source": "goal-section",
}, res

write("L-spec-9610", "# Spec Six\n\n## Requirements\nZ.\n")
res = plain.line("L-spec-9610", TMP)
assert res == {"text": "no plain-English line yet", "source": "missing"}, res
print("AC5 ok")

# ── AC6 · sidecar filename format ────────────────────────────────────────────

assert plain.SIDECAR.format("L-spec-0999") == "L-spec-0999.plain.txt"
print("AC6 ok")

# ── AC9 · plain.GOAL_STATES verbatim ────────────────────────────────────────

assert plain.GOAL_STATES == ("no charters yet", "being built", "being proven", "done", "stopped")
print("AC9 ok")

# ── AC10 · every state has a phrase: non-empty, <=6 words, no banned
# substring; plus a byte-for-byte match of item 5's whole table. Exactly 26
# named per-state lines (15+6+5, not 34) plus one further named line, 27 in
# all — each naming its own state so a padded/miscounted output can't pass
# by count alone. ─────────────────────────────────────────────────────────

BANNED = ("l1", "l2", "owed", "void", "fold")
named = 0
for kind, states in (("spec", fold.SPEC_STATES), ("charter", fold.CHARTER_STATES),
                     ("goal", plain.GOAL_STATES)):
    for state in states:
        ph = plain.PHRASE[kind][state]
        assert ph, f"{kind}:{state} has no phrase"
        assert 1 <= len(ph.split()) <= 6, f"{kind}:{state} phrase too long: {ph!r}"
        low = ph.lower()
        for bad in BANNED:
            assert bad not in low, f"{kind}:{state} phrase contains banned substring {bad!r}: {ph!r}"
        print(f"AC10 {kind}:{state} ok")
        named += 1
assert named == 26, named

EXPECTED_PHRASE = {
    "spec": {
        "unknown": "state unclear — needs a look",
        "written": "written, waiting to be built",
        "building": "being built",
        "graded": "being checked",
        "reviewing": "being checked",
        "shipped": "merged, waiting for its check",
        "shipped-owed-evidence": "live, waiting for proof",
        "shipped-owed-due": "live, waiting for proof",
        "shipped-owed-expired": "live, proof overdue",
        "accepted": "proven",
        "closed-shipped": "proven",
        "closed-unbuilt": "stopped",
        "dropped": "stopped",
        "killed": "stopped",
        "void": "never usable, set aside",
    },
    "charter": {
        "open": "being planned or built",
        "L1-complete": "being built",
        "proving": "built, waiting for proof",
        "reopened": "reopened: a check failed",
        "L2-complete": "proven and closed",
        "retracted": "withdrawn",
    },
    "goal": {s: s for s in plain.GOAL_STATES},
}
assert plain.PHRASE == EXPECTED_PHRASE, "PHRASE does not match item 5's table byte-for-byte"
print("AC10 byte-for-byte ok")

# ── AC11 · phrase() never raises, unknown fallback exact ────────────────────

assert plain.phrase("spec", "written") == "written, waiting to be built"
assert plain.phrase("bogus", "written") == "unknown state: written"
assert plain.phrase("spec", "bogus-state") == "unknown state: bogus-state"
assert plain.phrase("spec", "") == "unknown state: "
assert plain.phrase("spec", None) == "unknown state: None"
print("AC11 ok")

# ── AC12 · plain.NEXT: 15 keys, valid owners, terminal rows exact ──────────

assert set(plain.NEXT) == set(fold.SPEC_STATES), sorted(plain.NEXT)
assert len(plain.NEXT) == 15
ROLES = {"planner", "executor", "builder", "grader", "reviewer", "owed-sweeper",
         "operator", "thinker", "nobody"}
for state, (text, owner) in plain.NEXT.items():
    assert owner in ROLES, (state, owner)
TERMINAL = {"accepted", "closed-shipped", "closed-unbuilt", "dropped", "killed", "void"}
for state in TERMINAL:
    assert plain.NEXT[state] == ("nothing — done", "nobody"), (state, plain.NEXT[state])
print("AC12 ok")

# ── AC13 · goal_state()'s 5-branch priority ─────────────────────────────────

CASES = [
    ([], "no charters yet"),
    (["retracted", "retracted"], "stopped"),
    (["L2-complete", "retracted"], "done"),
    (["proving", "L2-complete"], "being proven"),
    (["reopened", "retracted"], "being proven"),
    (["open", "L2-complete"], "being built"),
    (["open"], "being built"),
]
for inp, expected in CASES:
    got = plain.goal_state(inp)
    assert got == expected, (inp, got, expected)
print("AC13 ok")

shutil.rmtree(TMP, ignore_errors=True)
print("plain-core: all checks passed")
