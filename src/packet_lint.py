#!/usr/bin/env python3
"""packet_lint — one append-only, versioned, severity-graded checklist that
`validate.spec_shape` and `packet.py`'s packet build both run, replacing what
used to be a fixed set of hardcoded lint buckets that had to be hand-patched
every time a new recurring packet defect was found (L-spec-0273 /
L-charter-0033 SD9/SD15/SD21).

Each `lint/packet-checklist.toml` row is `{id, since, applies_to, check,
message, severity}`. `applies_to` is exactly `"spec"` or `"packet:<role>"` —
one value, never a list. `severity` is `"warn"` or `"block"`. A row's
EFFECTIVE severity is `"block"` when a `[[promotion]]` row `{id, promoted_at,
reason}` — an append-only, separate table — names its id, else the row's own
`severity`. PL-001..PL-009 all ship `severity = "warn"`, no promotions exist,
so their effective severity is `warn` today (AC2).

Adding a tenth defect class costs one toml row plus one check function here;
promoting an existing one to blocking costs one `[[promotion]]` row.
`run()`'s dispatch loop does not change shape for either.

security_path: every check function is a pure string/regex scan (plus one
`tomllib.loads` and one call into `merge_gate.grant_from_text`, itself pure)
returning data — never `exec()`/`eval()` of `text` or the toml, and never a
shell command built from either.
"""
import pathlib, re, sys, tomllib

HERE = pathlib.Path(__file__).resolve().parent
CHECKLIST = HERE.parent / "lint" / "packet-checklist.toml"

sys.path.insert(0, str(HERE))
import merge_gate, packet  # noqa: E402 — local, same convention as validate.py


def load(checklist_path):
    """The parsed toml — `{"entry": [...], "promotion": [...]}`, both present
    (possibly empty) regardless of whether the file itself declares either."""
    data = tomllib.loads(pathlib.Path(checklist_path).read_text())
    data.setdefault("entry", [])
    data.setdefault("promotion", [])
    return data


def run(text, checklist_path, applies_to):
    """Every checklist entry whose `applies_to` equals `applies_to`, run against
    `text`, in checklist order. Returns one `{"id", "severity", "text"}` dict
    per firing entry — `text` always matches `^PL-\\d{3}: `. A check function
    returns `None` for no finding, or a (possibly empty) string detail."""
    data = load(checklist_path)
    promoted = {p["id"] for p in data["promotion"]}
    out = []
    for entry in data["entry"]:
        if entry["applies_to"] != applies_to:
            continue
        fn = getattr(sys.modules[__name__], entry["check"])
        detail = fn(text)
        if detail is None:
            continue
        sev = "block" if entry["id"] in promoted else entry["severity"]
        msg = f"{entry['id']}: {entry['message']}"
        if detail:
            msg += f" — {detail}"
        out.append({"id": entry["id"], "severity": sev, "text": msg})
    return out


def split(findings):
    """`(block_texts, warn_texts)` — the two buckets a caller folds separately."""
    block = [f["text"] for f in findings if f["severity"] == "block"]
    warn = [f["text"] for f in findings if f["severity"] == "warn"]
    return block, warn


# ── PL-001..009 check functions — each: text -> str | None ────────────────────

_BASE_SHA_HEAD = re.compile(r"(?:base_sha|BASE)\s*:?\s*HEAD\b")


def check_base_sha_head(text):
    """PL-001: `base_sha`/`BASE` immediately followed by the literal `HEAD` —
    case-sensitive, so a real (lowercase) hex sha never matches."""
    m = _BASE_SHA_HEAD.search(text)
    return m.group(0) if m else None


_AC_MARKER = re.compile(r"^\**AC\d+\s*\[")


def check_ac_truncated(text):
    """PL-002: group the `## Acceptance` section into per-criterion blocks at
    each `^\\**AC\\d+\\s*\\[` line; the LAST block fires when it has no
    `review_path` substring AND its last non-blank line does not end a
    sentence. A section absent entirely returns no finding (bucket 2's job)."""
    sec = packet.section(text, "Acceptance")
    if not sec:
        return None
    lines = sec.splitlines()
    starts = [i for i, l in enumerate(lines) if _AC_MARKER.match(l)]
    if not starts:
        return None
    block = lines[starts[-1]:]
    has_review_path = any("review_path" in l.lower() for l in block)
    nonblank = [l for l in block if l.strip()]
    ends_properly = bool(nonblank) and nonblank[-1].rstrip()[-1:] in ".!?"
    if has_review_path or ends_properly:
        return None
    m = re.search(r"AC\d+", lines[starts[-1]])
    return m.group(0) if m else lines[starts[-1]].strip()[:40]


def _fenced_block(text, section_name):
    sec = packet.section(text, section_name)
    if not sec:
        return None
    m = re.search(r"```[a-z]*\n(.*?)```", sec, re.S)
    return m.group(1) if m else None


def check_cd_prefix(text):
    """PL-003: the Verification fenced block shells into a relative `cd
    /opt/albert-scott` instead of using absolute interpreter paths. Literal
    scope, per SD9's own wording — not a pattern, not case-insensitive."""
    block = _fenced_block(text, "Verification")
    if block and "cd /opt/albert-scott" in block:
        return "cd /opt/albert-scott"
    return None


_NODE_TOOLS = {"npm", "npx", "node"}


def check_bare_node_tools(text):
    """PL-004: within the Verification fenced block, a segment whose first
    non-assignment, non-`env` token's basename is exactly npm/npx/node and is
    not `/`-prefixed resolves through PATH, which a worktree with no
    activated venv does not carry the same way twice (same rule as
    `packet._interp_violations`, independently scoped to these three names)."""
    block = _fenced_block(text, "Verification")
    if not block:
        return None
    segs, _bad = packet._quote_aware_scan(block)
    hits = []
    for seg in segs:
        toks = seg.split()
        while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
            toks.pop(0)
        if toks and pathlib.PurePosixPath(toks[0]).name == "env":
            toks = toks[1:]
        if not toks:
            continue
        cmd = toks[0]
        if pathlib.PurePosixPath(cmd).name in _NODE_TOOLS and not cmd.startswith("/"):
            hits.append(cmd)
    return ", ".join(hits) if hits else None


_OWN_ID = re.compile(r"^#\s+(L-spec-\d+|L-charter-\d+)", re.M)
_ANY_ID = re.compile(r"L-spec-\d+|L-charter-\d+")


def check_sibling_heading(text):
    """PL-005: the spec's own id is its first `^#\\s+(L-spec-\\d+|L-charter-
    \\d+)` line; any `^##\\s` heading naming a DIFFERENT such id fires, naming
    the offending id. No parseable own-id H1 -> never fires."""
    m = _OWN_ID.search(text)
    if not m:
        return None
    own = m.group(1)
    for line in text.splitlines():
        if re.match(r"^##\s", line):
            for mm in _ANY_ID.finditer(line):
                if mm.group(0) != own:
                    return mm.group(0)
    return None


_QUOTED_SPAN = re.compile(r"`[^`]*`|\"[^\"]*\"|'[^']*'")
_PL006_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"diff against main", r"diff against master",
    r"compare to master", r"compare to origin", r"git merge-base"))


def check_review_path_base_comparison(text):
    """PL-006: a `^\\s*review_path:\\s*(.*)$` line, with matching-quote spans
    stripped from its value first, whose REMAINING text names the base-drift
    anti-pattern. A line that only CITES the pattern inside quotes, or a
    prose paragraph merely describing it, never fires (the quote-strip and
    the line-start anchor respectively)."""
    for line in text.splitlines():
        m = re.match(r"^\s*review_path:\s*(.*)$", line)
        if not m:
            continue
        remaining = _QUOTED_SPAN.sub("", m.group(1))
        for pat in _PL006_PATTERNS:
            if pat.search(remaining):
                return line.strip()[:80]
    return None


_EXIT_CODE = re.compile(r"exits?\s*([01])\b", re.I)


def check_ac_contradiction(text):
    """PL-007: within the Acceptance section, a backtick-quoted identifier
    followed within 150 characters on the SAME line by an exit-code assertion
    records that code for the identifier; one recorded with both 0 and 1
    fires, naming it."""
    sec = packet.section(text, "Acceptance")
    if not sec:
        return None
    codes = {}
    for line in sec.splitlines():
        for m in re.finditer(r"`([^`]+)`", line):
            window = line[m.end():m.end() + 150]
            em = _EXIT_CODE.search(window)
            if em:
                codes.setdefault(m.group(1), set()).add(em.group(1))
    for ident, cs in codes.items():
        if "0" in cs and "1" in cs:
            return ident
    return None


def check_bracket_path_rejected(text):
    """PL-008(b): a standing regression guard, independent of bucket 3 — if
    `merge_gate.grant_from_text(text)` raises `Undetermined` whose message
    names any of `( ) [ ]`, the path-token regex has regressed."""
    try:
        merge_gate.grant_from_text(text)
    except merge_gate.Undetermined as e:
        msg = str(e)
        if any(c in msg for c in "()[]"):
            return msg[:80]
    return None


_STOP_MARKERS = ("DSN missing in a worktree", "fills /tmp", "leaves master red")


def check_missing_stop_section(text):
    """PL-009 (`applies_to = "packet:builder"` only): `p_builder`'s own output
    is supposed to always carry a `## STOP` heading naming the three
    local-box footguns; this is the standing guard that fires if it ever
    stops doing so."""
    if re.search(r"^## STOP\b", text, re.M) and all(m in text for m in _STOP_MARKERS):
        return None
    return "the ## STOP section (local-box footguns) is missing from the builder packet"
