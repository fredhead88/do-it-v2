#!/usr/bin/env python3
"""harvest — turn the four "an interruption happened" ledger event types into
`problem` occurrences, without ever writing to the ledger (L-charter-0034,
"collect problems over answers").

`occurrences(events, problems, toml_path=None)` is pure: `events` in, one
occurrence row per harvestable event out, in input order. It never mutates
`events`, never appends anything, never opens anything but (optionally) a local
TOML file read-only. The fold that turns a row into a NEW or an EXTENDED
`Problem` — join by slug, mint with `statement_needed=True` — is
`problems.register()`'s job (SD19), not this module's: this module only
classifies.

Classification order, per event, is the spec's (SD10/SD16), not a re-invention:
  1. token priority  — `problem:<slug> — <text>` on the event's OWN free-text
     field (never `spawn-failed`, which is wrapper-emitted, not authored text)
  2. `spawn-failed` reason table   (`SPAWN_FAILED_REASON_MAP`)
  3. `spawn-failed` why-pattern table (`SPAWN_FAILED_WHY_PATTERNS`, first match)
  4. similarity join  — difflib ratio >= 0.6 against an existing `Problem`'s
     `statement` (the other three types, no token match)
  5. mint  — a slug derived from the first five normalized words of the text
  6. unclassified  — `unclassified-<type>`, reported, never dropped
"""
import difflib
import pathlib
import re

# The only four types this module ever looks at (AC10) — every other type
# passes through untouched, producing no row.
HARVESTED_TYPES = ("spawn-failed", "build-deviation", "rejected-criterion", "correction")

# The event's own free-text field, per type — `what` for a build-deviation's
# own card text, `why` for the other three (a spawn-failed's `why` is
# wrapper-authored, not a human's, but it is still the only free text it
# carries, and step 3 reads it).
TEXT_FIELD = {"build-deviation": "what", "rejected-criterion": "why",
              "correction": "why", "spawn-failed": "why"}

# Step 1's token. Whitespace is REQUIRED, not optional, on both sides of the
# separator (fix for a backtracking bug: `\s*` let the greedy slug group
# shrink past its own internal hyphens until it hit one that satisfied
# `[—-]`, so `"problem:seat-unserved: x"` silently captured `"seat"` and
# `"problem:foo-bar baz"` captured `"foo"` — neither has a real separator at
# all). Because `-` sits inside the slug's own allowed charset and a bare
# space does not, a well-formed token's slug is bounded by the first space
# regardless of how many internal hyphens it carries; the `\s+` requirement is
# what makes a MALFORMED token (no whitespace anywhere near a `-`/`—`) fail to
# match at all, rather than fall back to a truncated slug.
TOKEN_RE = re.compile(r"^problem:([a-z0-9-]{3,60})\s+[—-]\s+")

SIMILARITY_THRESHOLD = 0.6

# Step 2 default — wholly replaced (never merged) by `[harvest.spawn_failed_reason]`
# in `problems.toml`/`toml_path` when that section exists.
SPAWN_FAILED_REASON_MAP = {
    "unserved": "seat-unserved",
    "spec-shape": "spec-shape-invalid",
    "killed": "spawn-killed-before-spend",
    "killed-duplicate": "spawn-killed-duplicate",
}

# Step 3 default, in order — wholly replaced (never merged) by
# `[[harvest.spawn_failed_why_pattern]]` rows when present. The last two
# (spec-writer-open / missing --path) were added against a live measurement
# that brought `unclassified-spawn-failed` to 0 (see L-spec-0280's own
# base-sha measurement).
SPAWN_FAILED_WHY_PATTERNS = [
    (r"timeout after ", "role-timeout"),
    (r"is_error|api_error", "spawn-api-error"),
    (r"contamination", "spawn-contamination"),
    (r"null structured_output|structured_output violates", "spawn-output-invalid"),
    (r"identical packet and contract", "spawn-retry-blocked"),
    (r"nothing at ", "spawn-output-missing"),
    (r"repo (status undetermined|identity changed)", "spawn-repo-state"),
    (r"wrapper ended with sigterm", "spawn-sigterm"),
    (r"^stopped by", "spawn-stopped-by-relay"),
    (r"is killed —", "spawn-refused-killed-subject"),
    (r"carries an open spec-writer spawn", "spawn-blocked-open-spec-writer"),
    (r"a writing role needs --path", "spawn-missing-required-path"),
]

# Cost extraction (R3): the live shape ten real `spawn-failed` rows actually
# carry — top-level fields on the event itself, summed. `usage` (a nested
# dict) is a fallback no live event uses today, kept for forward
# compatibility only.
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read", "cache_creation", "subagent_tokens")

# A module-level constant, not recomputed inline, so a test can monkeypatch it
# to a nonexistent path and exercise the "file absent" branch without depending
# on whether a real repo-root `problems.toml` happens to exist (AC11).
DEFAULT_TOML_PATH = pathlib.Path(__file__).resolve().parent.parent / "problems.toml"


def _load_tables(toml_path):
    """(`reason_map`, `why_patterns`) — the built-in defaults, or the wholesale
    replacement `problems.toml`/`toml_path` supplies. `toml_path=None` resolves
    to `DEFAULT_TOML_PATH` HERE, at call time (never at import), so a
    monkeypatch of the module constant is honored by every call made after it.
    Absent file, any parse exception, or a section missing/malformed all
    degrade to the built-in default for that ONE table — never an exception,
    same idiom as `carry.trusted_authors`."""
    path = pathlib.Path(toml_path) if toml_path is not None else DEFAULT_TOML_PATH
    try:
        import tomllib
        with open(path, "rb") as f:
            doc = tomllib.load(f)
    except (OSError, ValueError, ImportError):
        return dict(SPAWN_FAILED_REASON_MAP), list(SPAWN_FAILED_WHY_PATTERNS)
    harvest_tbl = doc.get("harvest")
    if not isinstance(harvest_tbl, dict):
        return dict(SPAWN_FAILED_REASON_MAP), list(SPAWN_FAILED_WHY_PATTERNS)

    reason_map = harvest_tbl.get("spawn_failed_reason")
    if not isinstance(reason_map, dict):
        reason_map = dict(SPAWN_FAILED_REASON_MAP)

    why_rows = harvest_tbl.get("spawn_failed_why_pattern")
    if isinstance(why_rows, list):
        why_patterns = [(row["pattern"], row["slug"]) for row in why_rows
                        if isinstance(row, dict) and row.get("pattern") and row.get("slug")]
    else:
        why_patterns = list(SPAWN_FAILED_WHY_PATTERNS)
    return reason_map, why_patterns


def _normalize_words(text):
    """Step 5's normalize, as a word list: lowercase; collapse whitespace runs;
    REPLACE (never delete) every remaining character outside `[a-z0-9 ]` with a
    space; split. Replacing rather than deleting is the fix for a prior draft
    that deleted non-`[a-z0-9 ]` characters before splitting, fusing
    `"double-charged"` into one word `"doublecharged"` and shifting the
    five-word window; a `.split()` on the final string (not a literal
    `split(" ")`) absorbs any run of spaces the replacement step reintroduces,
    so no empty word ever enters the list."""
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return text.split()


def _cost_min(event):
    d = event.get("duration_ms")
    if isinstance(d, bool) or not isinstance(d, (int, float)):
        return None
    return round(d / 60000, 4)


def _cost_tokens(event):
    """Sum of the present `TOKEN_FIELDS` at top level when at least one is
    present (an absent member counts as 0); else the sum of `usage`'s numeric
    leaves when `usage` is a non-empty dict; else `None` — never `0` for an
    event nothing measured."""
    present = [k for k in TOKEN_FIELDS if event.get(k) is not None]
    if present:
        return sum(event[k] for k in present)
    usage = event.get("usage")
    if isinstance(usage, dict) and usage:
        leaves = [v for v in usage.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if leaves:
            return sum(leaves)
    return None


def _similarity_join(text, problems):
    """The existing `Problem`'s slug whose `statement` is the closest match to
    `text` at ratio >= `SIMILARITY_THRESHOLD`, or `None`. A strict `>` when
    tracking the best keeps ties on the FIRST entry reaching the max — stable
    insertion order, per the spec."""
    best_slug, best_ratio = None, -1.0
    for p in problems:
        statement = p["statement"] or ""
        ratio = difflib.SequenceMatcher(None, text, statement).ratio()
        if ratio > best_ratio:
            best_ratio, best_slug = ratio, p["slug"]
    return best_slug if best_ratio >= SIMILARITY_THRESHOLD else None


def _mint(event_type, text):
    """A minted slug from the first five normalized words of `text`, sliced to
    60 chars and THEN stripped of a trailing `-` (slicing first is what the
    spec pins) — or `None` when `text` normalizes to no words at all (the
    unclassified case, step 6)."""
    words = _normalize_words(text)
    if not words:
        return None
    slug = (f"{event_type}-" + "-".join(words[:5]))[:60]
    return slug.rstrip("-")


def _classify(event, problems, tables):
    """(`slug`, `text`) for one harvestable event. `text` is always the raw
    value of the event's own free-text field (never trimmed of a `problem:...`
    token prefix, even when no token matched) — the row's `text`, which
    `problems.py`'s wiring seeds a minted `Problem`'s statement from without
    re-deriving any of this."""
    etype = event["type"]
    text = str(event.get(TEXT_FIELD[etype]) or "")

    if etype != "spawn-failed":
        m = TOKEN_RE.match(text)
        if m:
            return m.group(1), text

    if etype == "spawn-failed":
        reason_map, why_patterns = tables
        reason = event.get("reason")
        if reason is not None and reason in reason_map:
            return reason_map[reason], text
        why = str(event.get("why") or "")
        for pattern, slug in why_patterns:
            if re.search(pattern, why, re.IGNORECASE):
                return slug, text
        return f"unclassified-{etype}", text

    joined = _similarity_join(text, problems)
    if joined is not None:
        return joined, text
    minted = _mint(etype, text)
    if minted is None:
        return f"unclassified-{etype}", text
    return minted, text


def occurrences(events, problems, toml_path=None):
    """One occurrence dict per event in `events` whose `type` is one of
    `HARVESTED_TYPES`, in input order — every other event produces no row
    (AC10). Pure: no ledger write, `events` and `problems` are read, never
    mutated. `occurrence = {"slug", "ts", "source_type", "source_ref", "text",
    "cost_min", "cost_tokens"}`; `source_ref` is the event's own `_src`
    (`fold.read_events()`'s handle), absent on a bare test fixture that never
    set one."""
    tables = _load_tables(toml_path)
    out = []
    for event in events:
        etype = event.get("type")
        if etype not in HARVESTED_TYPES:
            continue
        slug, text = _classify(event, problems or [], tables)
        out.append({
            "slug": slug,
            "ts": event.get("ts"),
            "source_type": etype,
            "source_ref": event.get("_src"),
            "text": text,
            "cost_min": _cost_min(event),
            "cost_tokens": _cost_tokens(event),
        })
    return out
