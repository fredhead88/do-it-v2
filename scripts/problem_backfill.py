#!/usr/bin/env python3
"""problem_backfill — read-only, one-time proposal of problem-slug groupings
over the ledger's historic `lesson` events (L-spec-0278).

Reads every `lesson` event via `fold.read_events()` (unmodified; corrections
and the project filter are already applied there) and proposes a `slug` +
full-text `statement` grouping for each: a pre-assigned `problem` value wins
first, then a shared non-`open` `fix` value, then Jaccard >= 0.35 over
normalised token sets against each group's founding tokens. Writes one
markdown proposal file for the Thinker to review and confirm by hand. Never
appends to the ledger, never writes under `$DOIT_ROOT/events/`.

    scripts/problem_backfill.py [--out PATH]
"""
import argparse
import json
import os
import pathlib
import re
import sys

# `fold.ROOT`/`EVENTS`/`PROJECT` bind at import time from os.environ, so
# DOIT_ROOT must already be set in the environment before this import runs
# (the caller's env, or a subprocess env in tests) -- never mutated after.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import fold  # noqa: E402

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "when", "while",
    "of", "to", "in", "on", "for", "with", "as", "by", "at", "be", "been",
    "being", "is", "are", "was", "were", "from", "not", "no", "this", "that",
    "these", "those", "it", "its", "i", "we", "you", "they", "he", "she",
    "them", "his", "her", "their", "our", "your", "s", "t", "don", "can",
    "will", "just", "should", "now", "here", "there", "about", "into", "over",
    "under", "again", "further", "once", "more", "most", "other", "some",
    "such", "only", "own", "same", "so", "than", "too", "very", "all", "each",
    "few", "both",
}

SLUG_RE = re.compile(r"^[a-z0-9-]{3,60}$")
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def statement_text(e):
    """Section 6.1: `text` if non-empty string, else `subject` (always present)."""
    t = e.get("text")
    if isinstance(t, str) and t:
        return t
    return e["subject"]


def normalized_tokens(text):
    """Section 6.2: lowercase, non-alnum runs -> space, strip, collapse, split,
    drop pure-digit tokens, drop stopwords. Returns a set."""
    lowered = text.lower()
    collapsed = _TOKEN_SPLIT_RE.sub(" ", lowered).strip()
    if not collapsed:
        return set()
    words = collapsed.split()
    return {w for w in words if not w.isdigit() and w not in STOPWORDS}


def ordered_normalized_words(text):
    """Same normalization as `normalized_tokens`, but preserving occurrence
    order and duplicates, for slug generation (§6.3 takes the first five
    tokens in the order they occur in the normalized text)."""
    lowered = text.lower()
    collapsed = _TOKEN_SPLIT_RE.sub(" ", lowered).strip()
    if not collapsed:
        return []
    words = collapsed.split()
    return [w for w in words if not w.isdigit() and w not in STOPWORDS]


def jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


class Group:
    __slots__ = ("slug", "founding_tokens", "fix_key", "statement", "members")

    def __init__(self, slug, founding_tokens, fix_key, statement):
        self.slug = slug
        self.founding_tokens = founding_tokens
        self.fix_key = fix_key
        self.statement = statement
        self.members = []


def make_slug(statement, existing_slugs, ordinal):
    """Section 6.3: first five ordered normalized tokens, joined by '-',
    truncated to 60 chars, trailing '-' stripped; empty/short -> problem-<n>;
    collision -> append -2, -3, ... (first unused)."""
    words = ordered_normalized_words(statement)[:5]
    candidate = "-".join(words)[:60].rstrip("-")
    if len(candidate) < 3:
        candidate = f"problem-{ordinal}"
    base = candidate
    if base in existing_slugs:
        suffix = 2
        while f"{base}-{suffix}" in existing_slugs:
            suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def build_groups(lessons):
    """Section 6: process lessons in the order given (already oldest-first,
    tie-broken by `_src` per `fold.read_events()`). Returns the ordered list
    of Group objects."""
    groups = []
    slug_index = {}   # slug -> Group
    fix_index = {}    # fix value -> Group
    ordinal = 0        # 1-based ordinal of groups founded so far, any path

    for e in lessons:
        src = e["_src"]
        stmt = statement_text(e)

        # Case 1: pre-assigned slug.
        pre = e.get("problem")
        if isinstance(pre, str) and pre and SLUG_RE.match(pre):
            g = slug_index.get(pre)
            if g is None:
                ordinal += 1
                g = Group(pre, normalized_tokens(stmt), None, stmt)
                groups.append(g)
                slug_index[pre] = g
            g.members.append(src)
            continue

        # Case 2: fix-sharing.
        fix = e.get("fix")
        if isinstance(fix, str) and fix and fix != "open":
            g = fix_index.get(fix)
            if g is None:
                ordinal += 1
                slug = make_slug(stmt, slug_index.keys(), ordinal)
                g = Group(slug, normalized_tokens(stmt), fix, stmt)
                groups.append(g)
                fix_index[fix] = g
                slug_index[slug] = g
            g.members.append(src)
            continue

        # Case 3: cluster by Jaccard against every existing group's founding
        # token set (pre-assigned, fix-linked, or auto-founded -- all eligible).
        tokens = normalized_tokens(stmt)
        best_group, best_ratio = None, -1.0
        for g in groups:
            ratio = jaccard(tokens, g.founding_tokens)
            if ratio > best_ratio:
                best_ratio, best_group = ratio, g
        if best_group is not None and best_ratio >= 0.35:
            best_group.members.append(src)
            continue

        ordinal += 1
        slug = make_slug(stmt, slug_index.keys(), ordinal)
        g = Group(slug, tokens, None, stmt)
        groups.append(g)
        slug_index[slug] = g
        g.members.append(src)

    return groups


def render(groups, n):
    g_count = len(groups)
    ratio = g_count / n if n else 0.0
    ratio_str = f"{ratio:.3f}"
    lines = [f"# problem-backfill-proposal — {n} lesson events, {g_count} groups, ratio {ratio_str}", ""]
    for g in groups:
        body = json.dumps({"slug": g.slug, "statement": g.statement, "members": g.members})
        lines.append(f"## {g.slug}")
        lines.append("```json")
        lines.append(body)
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n", g_count, ratio_str


def main(argv=None):
    parser = argparse.ArgumentParser(description="Propose problem-slug groupings over ledger lesson events.")
    parser.add_argument("--out", default=None, help="Output path (default: $DOIT_ROOT/content/problem-backfill-proposal.md)")
    args = parser.parse_args(argv)

    if not fold.EVENTS.is_dir():
        print(f"error: {fold.EVENTS} does not exist", file=sys.stderr)
        return 1

    lessons = [e for e in fold.read_events() if e.get("type") == "lesson"]
    n = len(lessons)
    groups = build_groups(lessons)
    text, g_count, ratio_str = render(groups, n)

    out_path = pathlib.Path(args.out) if args.out else (fold.ROOT / "content" / "problem-backfill-proposal.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)

    # AC8 / R1b: the group:lesson ratio is reported here, never gated on --
    # this proposal is a draft the Thinker confirms by hand, and the grouping
    # rule stays exactly as specified in §6 (Jaccard >= 0.35 + fix-sharing +
    # problem-pinning) regardless of the resulting ratio.
    print(f"groups: {g_count} lessons: {n} ratio: {ratio_str}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
