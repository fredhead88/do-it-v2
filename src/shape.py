#!/usr/bin/env python3
"""shape — the spec-shape gate as one callable check plus two mechanical repairs.

  shape.check(spec_text) -> {"block": [...], "warn": [...]}
  shape.mechanical_fix(spec_text) -> (repaired_text, repairs)
  doit shape FILE [--fix]

L-spec-0385 (charter L-charter-0038): `validate.spec_shape`/`spec_shape_warnings`
are the only two things this module ever CALLS to decide pass/fail — no rule of
theirs is reimplemented or changed here (`src/validate.py` is read-only for this
unit). `check()` is a thin wrapper. `mechanical_fix()` performs exactly the two
purely-mechanical repairs the charter names (`writes-annotation`, `ac-bullet`)
and nothing else — a Verification chain is never auto-repaired, because
rewriting a command changes what the grader runs.
"""
import pathlib, re, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import merge_gate  # noqa: E402
import validate  # noqa: E402

# The label-line test, verbatim from `merge_gate.grant_from_text`'s own match —
# `writes-annotation` locates the SAME line the merge gate itself would read as
# the Writes grant's label.
WRITES_LABEL_RE = re.compile(r"^\s*[|>*_\-\s]*writes[*_\s]*[:|]\s*", re.I)

# A bulleted/numbered AC line whose id is either bare or tightly bold-wrapped
# (the bold closing IMMEDIATELY after the digits) — never the whole-line-bold
# shape, which this simply never matches (it has no bullet prefix at all, or
# its bold does not close right after the id).
AC_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:AC(\d+)|\*\*AC(\d+)\*\*)")

# A value line's held-fixed prefix in block form: leading whitespace, plus one
# `-`/`*`/`+` bullet marker and the single space after it, if present.
_BLOCK_HELD_RE = re.compile(r"^[ \t]*(?:[-*+]\s+)?")


def check(spec_text):
    """`{"block": validate.spec_shape(spec_text), "warn": validate.spec_shape_warnings(spec_text)}`,
    verbatim — R5/SD9."""
    return {"block": validate.spec_shape(spec_text), "warn": validate.spec_shape_warnings(spec_text)}


def _token_passes(tok):
    """The identical per-token test `merge_gate.check_grant` itself applies."""
    return bool(merge_gate.PATH_TOKEN_RE.fullmatch(tok)) and any(c in tok for c in "/.*?")


def _fix_tokens(rest):
    """Drop the first failing whitespace-separated token in `rest` and
    everything after it on the same physical line, trimming the trailing
    whitespace before it. Returns `(new_rest, changed)`; `changed=False` (and
    `new_rest is rest`) when every token already passes — the line is left
    byte-identical."""
    for m in re.finditer(r"\S+", rest):
        if not _token_passes(m.group(0)):
            return rest[:m.start()].rstrip(), True
    return rest, False


def _writes_fix_ops(parts):
    """`(idx, new_line)` pairs for every writes-grant line `writes-annotation`
    actually changes, in line order. `parts` is the spec text split on `\n`,
    read but never mutated here."""
    label_idx, label_match = None, None
    for i, line in enumerate(parts):
        m = WRITES_LABEL_RE.match(line)
        if m:
            label_idx, label_match = i, m
            break
    if label_idx is None:
        return []

    prefix = parts[label_idx][:label_match.end()]
    remainder = parts[label_idx][label_match.end():]
    # Swallow one immediately-following `**` — the closing half of a bold
    # label (`**Writes:**`) — into the held-fixed prefix, mirroring
    # `merge_gate.grant_from_text`'s own odd-`**`-count test.
    if prefix.count("**") % 2 == 1 and remainder.startswith("**"):
        prefix += "**"
        remainder = remainder[2:]

    stripped = remainder.strip()
    is_inline = bool(stripped) and not re.fullmatch(r"\(.*\)", stripped, re.S)

    ops = []
    if is_inline:
        new_rest, changed = _fix_tokens(remainder)
        if changed:
            ops.append((label_idx, prefix + new_rest))
        return ops

    # Block form: every following non-blank, non-fence, non-heading line up to
    # the first blank line or heading is a grant-value line — the identical
    # scan `grant_from_text`'s own block-list branch performs.
    started = False
    for j in range(label_idx + 1, len(parts)):
        t = parts[j].strip()
        if t.startswith("```"):
            if started:
                break
            continue
        if not t:
            if started:
                break
            continue
        if t.startswith("#"):
            break
        started = True
        held_m = _BLOCK_HELD_RE.match(parts[j])
        held = held_m.group(0)
        rest = parts[j][len(held):]
        new_rest, changed = _fix_tokens(rest)
        if changed:
            ops.append((j, held + new_rest))
    return ops


def _ac_bullet_ops(parts):
    """`(idx, new_line)` pairs for every `ac-bullet` repair, in line order."""
    ops = []
    for i, line in enumerate(parts):
        m = AC_BULLET_RE.match(line)
        if not m:
            continue
        num = m.group(1) or m.group(2)
        ops.append((i, f"AC{num}{line[m.end():]}"))
    return ops


def mechanical_fix(spec_text):
    """Exactly the two repairs named in §2 — `writes-annotation`,
    `ac-bullet` — and nothing else; a Verification chain is never touched.
    Returns `(text, repairs)`: `repairs` names each repaired physical line, in
    the order the lines appear in the text; `[]` and the byte-identical input
    text when nothing needed repair."""
    parts = spec_text.split("\n")
    ops = ([(i, nl, "writes-annotation") for i, nl in _writes_fix_ops(parts)]
           + [(i, nl, "ac-bullet") for i, nl in _ac_bullet_ops(parts)])
    if not ops:
        return spec_text, []
    ops.sort(key=lambda t: t[0])
    out = list(parts)
    repairs = []
    for idx, newline, label in ops:
        out[idx] = newline
        repairs.append(label)
    return "\n".join(out), repairs


def main(argv):
    fix = "--fix" in argv
    paths = [a for a in argv if a != "--fix"]
    if len(paths) != 1:
        sys.exit("usage: doit shape FILE [--fix]")
    path = paths[0]
    try:
        text = pathlib.Path(path).read_text()
    except OSError as e:
        sys.stderr.write(f"shape: {path}: {e}\n")
        sys.exit(2)

    if fix:
        new_text, repairs = mechanical_fix(text)
        if repairs:
            pathlib.Path(path).write_text(new_text)
            text = new_text

    result = check(text)
    for b in result["block"]:
        print(f"BLOCK: {b}")
    for w in result["warn"]:
        print(f"WARN: {w}")
    sys.exit(1 if result["block"] else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
