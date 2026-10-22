"""Pure, shared extraction of the first fenced block in Verification.

Only a complete fenced block supplies commands. Section prose, later code
blocks and subsequent headings never become part of the executable text.
"""
from dataclasses import dataclass
import re

_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+")
_VERIFY = re.compile(r"^ {0,3}#{1,6}[ \t]+(?:\d+[a-z]?\.[ \t]*)?Verification\b", re.I)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)[\r\n]*$")


@dataclass
class _Section:
    code: str | None
    raw: str
    trailing: str


def _parse(text):
    lines = (text or "").splitlines(keepends=True)
    start = opening = closing = None
    fence = None
    end = len(lines)
    for i, line in enumerate(lines):
        if fence is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0])
                            + "{" + str(len(fence)) + r",}[ \t]*[\r\n]*", line):
                if opening is not None and closing is None:
                    closing = i
                fence = None
            continue
        match = _FENCE.match(line)
        if match and not (match[1][0] == "`" and "`" in match[2]):
            fence = match[1]
            if start is not None and opening is None:
                opening = i
            continue
        if _HEADING.match(line):
            if start is not None:
                end = i
                break
            if _VERIFY.match(line):
                start = i
    if start is None:
        return None
    if closing is None:
        return _Section(None, "".join(lines[start:end]), "")
    code = "".join(lines[opening + 1:closing])
    trailing = "".join(lines[closing + 1:end])
    # Keep the existing packet display's whitespace, while excluding prose.
    blanks = re.match(r"\s*", trailing)[0]
    return _Section(code, "".join(lines[start:closing + 1]) + blanks, trailing)


def block(text):
    """Verbatim code inside the first closed Verification fence, else None."""
    found = _parse(text)
    return found.code if found else None


def section(text):
    """Verification heading and fenced block for display; None if absent."""
    found = _parse(text)
    return found.raw if found else None


def trailing_text(text):
    """Text after the closing fence, before the next Markdown heading."""
    found = _parse(text)
    return found.trailing if found else ""
