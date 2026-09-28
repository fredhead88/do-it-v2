#!/usr/bin/env python3
"""L-spec-0476/plain-core: every internal state a spec, charter or goal can be
in, each state's plain phrase and (for a spec) its next step and owner, plus
the one-sentence plain-English line for any goal, charter or spec — sourced
from content files, never hand-maintained prose.

Pure library, no CLI entry point, no route, no model call. `plain.line()`
reads `.md`/`.plain.txt` files under a caller-supplied `root` (always
`$DOIT_ROOT/content/` in production, a throwaway fixture root in tests). It
never raises on a missing or malformed file — a degraded result (`source ==
"missing"`) is the failure mode.
"""
import pathlib
import re

# ── R1 · the one-sentence plain-English line ────────────────────────────────

# The one compiled pattern matching an `In plain English: <sentence>` line
# anywhere in a spec's text. `[ \t]*`, not `\s*`: `\s` also matches a newline,
# and since `.+` under MULTILINE never crosses lines, a `\s*` gap would let a
# bare "In plain English:" line (nothing after the colon) capture the
# FOLLOWING line as group 1 instead of failing to match; `[ \t]*` confines the
# gap to same-line horizontal whitespace, so a bare label line simply does not
# match.
PLAIN_RE = re.compile(r"^In plain English:[ \t]*(.+)$", re.MULTILINE)

# The sidecar filename format string; `SIDECAR.format(item_id)` names the file
# read/written beside the spec at `root / "content" / SIDECAR.format(item_id)`.
SIDECAR = "{}.plain.txt"

_TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)


def _title(text):
    """The item's title: the first `# <title>` line, or (a RESERVED stub) the
    file's first non-blank line, verbatim."""
    m = _TITLE_RE.search(text)
    if m:
        return m.group(1).strip()
    for ln in text.splitlines():
        if ln.strip():
            return ln
    return ""


def _section(text, heading_re):
    """The body of the section headed by `heading_re` (a regex matching just
    the heading line, anchored `^`), bounded by the next `## ` heading or end
    of file. `None` if the heading is absent."""
    m = re.search(heading_re, text, re.MULTILINE)
    if not m:
        return None
    nl = text.find("\n", m.end())
    body_start = nl + 1 if nl != -1 else len(text)
    m2 = re.search(r"^## ", text[body_start:], re.MULTILINE)
    body_end = body_start + m2.start() if m2 else len(text)
    return text[body_start:body_end]


def _cap_words(sentence):
    words = sentence.split()
    if len(words) > 40:
        return " ".join(words[:40]) + "…"
    return sentence


def _first_sentence(body):
    """The section body's first sentence (SD5/item 4): the first paragraph
    (up to the first blank line), its internal whitespace — including
    embedded newlines from a wrapped source line — collapsed to single
    spaces FIRST, then everything up to and including the first `. ` in the
    collapsed text; if no such boundary exists, the whole collapsed first
    paragraph, no period appended. Word-capped at 40 words (`_cap_words`)."""
    body = body.strip("\n")
    first_para = re.split(r"\n[ \t]*\n", body, maxsplit=1)[0]
    collapsed = re.sub(r"\s+", " ", first_para).strip()
    idx = collapsed.find(". ")
    sentence = collapsed[: idx + 1] if idx != -1 else collapsed
    return _cap_words(sentence)


def _goal_or_charter_line(text):
    title = _title(text)
    intent = _section(text, r"^## 1\. Intent[ \t]*$")
    if intent is None:
        return {"text": title, "source": "missing"}
    sentence = _first_sentence(intent)
    return {"text": f"{title} — {sentence}", "source": "intent"}


def _spec_line(item_id, root, text):
    m = PLAIN_RE.search(text)
    if m:
        return {"text": m.group(1).strip(), "source": "spec-line"}
    sidecar_path = pathlib.Path(root) / "content" / SIDECAR.format(item_id)
    if sidecar_path.exists():
        content = sidecar_path.read_text().strip()
        if content:
            return {"text": content, "source": "sidecar"}
    goal = _section(text, r"^## Goal[ \t]*$")
    if goal is not None:
        sentence = _first_sentence(goal)
        return {"text": f"{sentence} (from the spec's goal)", "source": "goal-section"}
    return {"text": "no plain-English line yet", "source": "missing"}


def line(item_id, root):
    """The one-sentence plain-English line for `item_id` (an `L-goal-…` /
    `L-charter-…` / `L-spec-…` id), read from `root / "content" / f"{item_id}.md"`.
    `root` is a `pathlib.Path` to a DOIT root. Never raises: any other prefix,
    or a missing content file, returns the `"missing"` fallback below."""
    if item_id.startswith("L-spec-"):
        kind = "spec"
    elif item_id.startswith("L-charter-") or item_id.startswith("L-goal-"):
        kind = "goal-or-charter"
    else:
        return {"text": f"{item_id}: no content file", "source": "missing"}
    path = pathlib.Path(root) / "content" / f"{item_id}.md"
    if not path.exists():
        return {"text": f"{item_id}: no content file", "source": "missing"}
    text = path.read_text()
    if kind == "spec":
        return _spec_line(item_id, root, text)
    return _goal_or_charter_line(text)


# ── R2 · plain status words ──────────────────────────────────────────────────

GOAL_STATES = ("no charters yet", "being built", "being proven", "done", "stopped")

# kind -> state -> phrase, verbatim (charter's own R2 wording): a fixed
# vocabulary table in code, not ledger-derived, by design.
PHRASE = {
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
    "goal": {s: s for s in GOAL_STATES},
}


def phrase(kind, state):
    """`PHRASE[kind][state]`, or `"unknown state: {state}"` for an unknown
    `kind` or an unknown `state` under a known `kind`. Never raises: a
    non-string `state` (including `None`) is stringified first, for both the
    lookup and the fallback message."""
    state_key = state if isinstance(state, str) else str(state)
    found = PHRASE.get(kind, {}).get(state_key)
    if found is not None:
        return found
    return f"unknown state: {state_key}"


# spec state -> (next-step text, owner). Every one of SPEC_STATES' 15 entries
# (fold.py, this unit's own footprint owns the enumeration; this table is
# keyed by the same 15 literals). Terminal states all map to exactly
# ("nothing — done", "nobody"). The 9 non-terminal rows are this unit's own
# default (Assumptions): chosen to match each state's own meaning per
# `fold.spec_state`'s docstring and `agents/executor.md`'s lane table.
NEXT = {
    "unknown": ("find out why it has no lifecycle event yet", "operator"),
    "written": ("waiting to be picked up and built", "builder"),
    "building": ("being built right now", "builder"),
    "graded": ("waiting to be graded", "grader"),
    "reviewing": ("waiting to be reviewed", "reviewer"),
    "shipped": ("waiting for its acceptance check", "executor"),
    "shipped-owed-evidence": ("waiting for the owed-sweeper to check it", "owed-sweeper"),
    "shipped-owed-due": ("due for its owed check", "owed-sweeper"),
    "shipped-owed-expired": ("proof overdue — needs a decision", "executor"),
    "accepted": ("nothing — done", "nobody"),
    "closed-shipped": ("nothing — done", "nobody"),
    "closed-unbuilt": ("nothing — done", "nobody"),
    "dropped": ("nothing — done", "nobody"),
    "killed": ("nothing — done", "nobody"),
    "void": ("nothing — done", "nobody"),
}


def goal_state(charter_states):
    """SD4, first match wins: (1) empty list -> "no charters yet"; (2) every
    entry "retracted" -> "stopped"; (3) every non-"retracted" entry
    "L2-complete" -> "done"; (4) every non-"retracted" entry in {"proving",
    "reopened", "L2-complete"} -> "being proven"; (5) otherwise -> "being
    built"."""
    if not charter_states:
        return "no charters yet"
    live = [s for s in charter_states if s != "retracted"]
    if not live:
        return "stopped"
    if all(s == "L2-complete" for s in live):
        return "done"
    if all(s in ("proving", "reopened", "L2-complete") for s in live):
        return "being proven"
    return "being built"
