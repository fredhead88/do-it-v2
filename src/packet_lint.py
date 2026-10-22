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
`severity`. PL-001..PL-009 retain `severity = "warn"`; the checklist
promotes PL-003, and PL-010 blocks fixed working directories.

Adding a tenth defect class costs one toml row plus one check function here;
promoting an existing one to blocking costs one `[[promotion]]` row.
`run()`'s dispatch loop does not change shape for either.

security_path: every check function is a pure string/regex scan (plus one
`tomllib.loads` and one call into `merge_gate.grant_from_text`, itself pure)
returning data — never `exec()`/`eval()` of `text` or the toml, and never a
shell command built from either.
"""
import pathlib, posixpath, re, shlex, sys, tomllib

HERE = pathlib.Path(__file__).resolve().parent
CHECKLIST = HERE.parent / "lint" / "packet-checklist.toml"

sys.path.insert(0, str(HERE))
import merge_gate, packet  # noqa: E402 — local, same convention as validate.py
import verification


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


def check_cd_prefix(text):
    """PL-003: the Verification fenced block shells into a relative `cd
    /opt/albert-scott` instead of using absolute interpreter paths. Literal
    scope, per SD9's own wording — not a pattern, not case-insensitive."""
    block = verification.block(text)
    if block and "cd /opt/albert-scott" in block:
        return "cd /opt/albert-scott"
    return None


def _fixed_cwd(target):
    """Classify text only: never expand variables or inspect the named path."""
    if target.startswith("/"):
        path = posixpath.normpath(target)
        return not any(path == root or path.startswith(root + "/")
                       for root in ("/tmp", "/var/tmp"))
    return target.startswith("~") or (target.startswith("$") and
                                      bool(re.search(r"/repos(?:/|$)", target)))


def _shell_cwd(command):
    """Quote-aware command positions, plus shell -c command strings."""
    segments, _ = packet._quote_aware_scan(command)
    for segment in segments:
        lexer = shlex.shlex(segment, posix=True, punctuation_chars=";&|()\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = []
        # Keep a malformed trailing quote from hiding an earlier complete target.
        try:
            tokens.extend(lexer)
        except ValueError:
            pass
        cmd, words = None, []
        for token in tokens + ["\n"]:
            if token and all(c in ";&|()\n" for c in token):
                cmd, words = None, []
                continue
            if cmd is None:
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
                    continue
                if token in ("env", "command", "exec", "do"):
                    continue
                cmd = pathlib.PurePosixPath(token).name
                continue
            target = None
            if cmd in ("cd", "pushd") and not words and not token.startswith("-"):
                target = token
            if words and (words[-1] == "--root" or
                          (words[-1] == "-C" and cmd in ("git", "make", "tar"))):
                target = token
            if cmd in ("git", "make", "tar") and token.startswith("-C") and len(token) > 2:
                target = token[2:]
            if token.startswith("--root=") and cmd not in ("echo", "printf"):
                target = token[len("--root="):]
            if target and _fixed_cwd(target):
                return target
            if cmd in ("sh", "bash", "dash", "zsh", "ksh") and words and re.fullmatch(r"-[a-z]*c[a-z]*", words[-1]):
                found = _shell_cwd(token)
                if found:
                    return found
            if cmd not in ("cd", "pushd") or not token.startswith("-"):
                words.append(token)
    return None


def check_live_checkout_cwd(text):
    """PL-010: fixed cwd targets in Verification or criterion review paths.

    Criterion anchors follow packet.criterion_block; headings end a block.
    Review prose can introduce a command with `do`, or quote it in backticks.
    The Verification block is scanned first, then review paths in file order.
    """
    found = _shell_cwd(verification.block(text) or "")
    if found:
        return found
    active, review, lines = False, False, []
    for line in text.splitlines() + ["# end"]:
        anchor = re.match(r"^\s*(?:[-*|]\s*)*(?:Acceptance:\s*)?\**AC\d+[a-z]?\**\s*\[", line)
        if anchor or re.match(r"^#+\s", line):
            if lines:
                # Backticks delimit shell examples; prose's `do` starts a command.
                for span in re.split(r"`+", "\n".join(lines)):
                    span = re.sub(r"(?:^| / )do\s+", "\n", span)
                    found = _shell_cwd(span)
                    if found:
                        return found
            active, review, lines = bool(anchor), False, []
        marker = re.search(r"\breview_path\b\s*:?", line, re.I) if active else None
        if marker and not review:
            review = True
            line = line[marker.end():].lstrip()
        if review:
            lines.append(line)
    return None


_NODE_TOOLS = {"npm", "npx", "node"}


def check_bare_node_tools(text):
    """PL-004: within the Verification fenced block, a segment whose first
    non-assignment, non-`env` token's basename is exactly npm/npx/node and is
    not `/`-prefixed resolves through PATH, which a worktree with no
    activated venv does not carry the same way twice (same rule as
    `packet._interp_violations`, independently scoped to these three names)."""
    block = verification.block(text)
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


def check_verification_trailing_prose(text):
    """PL-012: only blank lines may follow the Verification closing fence."""
    trailing = verification.trailing_text(text).strip()
    return trailing.splitlines()[0][:80] if trailing else None


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


# ── PL-011 (L-spec-0682 R5): an owed check must name an environment that exists ─

_OWED_MARK = re.compile(r"\bOWED\b")
_OWED_ENVS = tuple(re.compile(p, re.I) for p in (
    r"\bprod(?:uction)?\b[^.\n]{0,30}?\bread\b|\bro-sql\b|\bdroplet-read\b",
    r"verifier portal|portal session|verifier account|verify@albertscott\.com|prod(?:uction)? portal",
    r"local stack|private-pg|scratch postgres|local postgres|local uvicorn",
    r"\bCI\b|GitHub Actions"))
_LOCAL_DEV = re.compile(r"local dev(?:elopment)? server", re.I)
_SCREENSHOT = re.compile(r"screenshot", re.I)


def check_owed_environment(text):
    """PL-011: within the Acceptance section, a criterion block whose header line
    carries the word OWED must name at least one environment an owed check can
    actually be observed in — a prod read, the verifier portal session, the local
    stack, or CI — and must not be written against a "local dev server" with
    screenshots (the verifier has no such environment; write it against the prod
    portal with the verifier account, `verify@albertscott.com`, by name only).
    Returns the first offending criterion id with the reason, else `None`."""
    sec = packet.section(text, "Acceptance")
    if not sec:
        return None
    lines = sec.splitlines()
    starts = [i for i, l in enumerate(lines) if _AC_MARKER.match(l)]
    for n, i in enumerate(starts):
        block = "\n".join(lines[i:starts[n + 1] if n + 1 < len(starts) else len(lines)])
        if not _OWED_MARK.search(lines[i]):
            continue
        cid = (re.search(r"AC\d+[a-z]?", lines[i]) or re.search(r".{0,20}", lines[i])).group(0)
        if _LOCAL_DEV.search(block) and _SCREENSHOT.search(block):
            return (f"{cid}: a local dev server with screenshots does not exist for the verifier; write it "
                    "against the prod portal with the verifier account (verify@albertscott.com)")
        if not any(p.search(block) for p in _OWED_ENVS):
            return (f"{cid}: names none of prod read, verifier portal session, local stack, CI")
    return None
