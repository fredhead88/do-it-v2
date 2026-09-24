#!/usr/bin/env python3
"""problems — the ledger folds into a queryable register of recurring problems.

  problems.py [--open] [--json] [--needs-statement] [--health] [--config PATH]

L-charter-0034: "collect problems over answers; record every interruption to
speed, efficiency or parallel work, with why." Elegance is the constraint —
no new service, database or dashboard. This is a fold over the SAME ledger
fold.py already reads: `lesson`, `problem-minted`, `problem-grouped` and (once
wave 2's `tick.py` lands) `problem-occurred` events fold into one `Problem`
per distinct slug (register()), queryable here.

A `problem=` value is a free-text slug, used only as a dict key and as text —
never as a filesystem path or shell argument — so `^[a-z0-9-]{3,60}$` is a
display concern (register-digest, wave 3), not enforced here (Assumptions).
"""
import collections, difflib, json, os, pathlib, re, sys, tomllib

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fold  # noqa: E402 — read_events()/EMITS/ts(): the same fold this register sits over
import harvest  # noqa: E402 — L-spec-0280/SD19: classifies spawn-failed/build-deviation/
                 # rejected-criterion/correction into occurrences this register folds in.
                 # harvest.py never imports fold or problems, so no import cycle.

# Repo root, next to `doit` — resolved relative to THIS file, never $DOIT_ROOT
# (Assumptions): these are versioned code constants (rank divisor, delay
# thresholds, harvest table) that ship with the code, unlike a per-installation
# file such as models.toml.
DEFAULT_TOML = pathlib.Path(__file__).resolve().parent.parent / "problems.toml"

FIX_SPEC_RE = re.compile(r"^L-spec-\d+$")


def _emits_filter(events):
    """R1: the same actor-authorization test `fold.fold()` applies at
    fold.py:1135-1137, run here too — so an unauthorized `fix-shipped` (or,
    once wave 2 lands, `problem-occurred`) can never reach a `Problem`,
    whether it arrived via `append()`/`check_append()` or was fed to
    `register()` directly from a raw ledger file. A type absent from
    `fold.EMITS` (`lesson`, `problem-minted`, `problem-grouped`) is never
    filtered — `EMITS.get(type)` returning `None` already means open to any
    actor (fold.py's own documented rule)."""
    out = []
    for e in events:
        allowed = fold.EMITS.get(e.get("type"))
        if allowed is not None and e.get("actor") not in allowed:
            continue
        out.append(e)
    return out


def _slug_of(e):
    """A `lesson`/`problem-grouped` event's resolved slug: the literal
    `problem=` value, or `"unassigned"` when the key is absent entirely — a
    missing key never becomes its own real slug (R1)."""
    return e.get("problem") or "unassigned"


def _load_rank_config(toml_path=None):
    p = pathlib.Path(toml_path) if toml_path else DEFAULT_TOML
    if not p.is_file():
        return {"cost_divisor_min": 60.0}
    d = tomllib.loads(p.read_text())
    rank = d.get("rank") or {}
    try:
        return {"cost_divisor_min": float(rank.get("cost_divisor_min", 60.0))}
    except (TypeError, ValueError):
        return {"cost_divisor_min": 60.0}


def occurrence_count(events, slug):
    """The literal count `fold.append()`'s mint hook needs: every surviving
    `lesson`/`problem-grouped` event whose resolved slug equals `slug` — R1's
    own definition of "occurrences", applied to ANY slug (including
    `"unassigned"`) independent of whether that slug ever becomes a `Problem`
    in `register()`'s output."""
    filtered = _emits_filter(events)
    return sum(1 for e in filtered if e.get("type") in ("lesson", "problem-grouped")
               and _slug_of(e) == slug)


def register(events, toml_path=None):
    """R1/R5 — fold surviving `lesson`, `problem-minted`, `problem-grouped` and
    `fix-shipped` events into one `Problem` per distinct slug (never
    `"unassigned"`, which is excluded up front). Returns the list ranked
    descending by `occurrences * (1 + (cost_min or 0) / rank.cost_divisor_min)`
    (the divisor read from `toml_path`, or the repo's own `problems.toml`).
    Never mutates `events`."""
    filtered = _emits_filter(events)

    groups = collections.defaultdict(list)        # slug -> [lesson|problem-grouped events]
    minted = {}                                    # slug -> statement, from problem-minted
    fix_events = collections.defaultdict(list)     # slug -> [fix-shipped events]
    shipped_by_subject = collections.defaultdict(list)

    for e in filtered:
        t = e.get("type")
        if t in ("lesson", "problem-grouped"):
            slug = _slug_of(e)
            if slug != "unassigned":
                groups[slug].append(e)
        elif t == "problem-minted":
            # Its own field is `slug=`, not `problem=` — it mints an identity
            # directly rather than tagging an occurrence's group.
            slug = e.get("slug")
            if slug and slug != "unassigned" and "statement" in e and slug not in minted:
                minted[slug] = e["statement"]
        elif t == "fix-shipped":
            # `fix-shipped <slug> ref=...` — the slug is the event's SUBJECT
            # (the CLI's positional argument), not a `problem=` field.
            slug = e.get("subject")
            if slug and slug != "unassigned":
                fix_events[slug].append(e)
                groups[slug]  # ensure the slug exists even with zero occurrences (AC17)
        elif t == "shipped" and e.get("subject"):
            shipped_by_subject[e["subject"]].append(e)

    # R5(a): a contributing lesson's `fix` matching ^L-spec-\d+$ links to the
    # EARLIEST surviving `shipped` event for that subject, if any survives at
    # all — no match, no fixes[] entry (not a placeholder).
    ref_shipped_at = collections.defaultdict(dict)   # slug -> {ref: shipped_at}
    for slug, occ in groups.items():
        for e in occ:
            if e.get("type") != "lesson":
                continue
            fix = e.get("fix")
            if fix and FIX_SPEC_RE.match(fix):
                shipped = shipped_by_subject.get(fix)
                if shipped:
                    earliest = min(shipped, key=lambda s: str(s.get("ts", "")))
                    ref_shipped_at[slug].setdefault(fix, earliest.get("ts"))
    # R5(b): every surviving fix-shipped event adds its own {ref, ts} directly.
    for slug, evs in fix_events.items():
        for e in evs:
            ref = e.get("ref")
            if ref:
                ref_shipped_at[slug][ref] = e.get("ts")

    def held(slug, shipped_at):
        shipped_dt = fold.ts(shipped_at)
        return not any(fold.ts(e.get("ts")) > shipped_dt for e in groups.get(slug, []))

    out = []
    for slug, occ in groups.items():
        occurrences = len(occ)
        if occ:
            first_seen = min(occ, key=lambda e: str(e.get("ts", "")))["ts"]
            last_seen = max(occ, key=lambda e: str(e.get("ts", "")))["ts"]
        else:
            first_seen = last_seen = None

        cost_min_vals = [e["cost_min"] for e in occ
                         if isinstance(e.get("cost_min"), (int, float)) and not isinstance(e.get("cost_min"), bool)]
        cost_min = sum(cost_min_vals) if cost_min_vals else None
        cost_tokens_vals = [e["cost_tokens"] for e in occ
                            if isinstance(e.get("cost_tokens"), (int, float)) and not isinstance(e.get("cost_tokens"), bool)]
        cost_tokens = sum(cost_tokens_vals) if cost_tokens_vals else None
        cost_unmeasured = sum(1 for e in occ if "cost_min" not in e)

        if slug in minted:
            statement, statement_needed = minted[slug], False
        else:
            lessons = [e for e in occ if e.get("type") == "lesson"]
            if lessons:
                earliest = min(lessons, key=lambda e: str(e.get("ts", "")))
                if "statement" in earliest:
                    statement, statement_needed = earliest["statement"], False
                else:
                    statement, statement_needed = earliest.get("text"), True
            else:
                statement, statement_needed = None, True

        fixes = [{"ref": ref, "shipped_at": at, "held": held(slug, at)}
                 for ref, at in ref_shipped_at.get(slug, {}).items()]
        recurred_after_fix = any(not f["held"] for f in fixes)

        out.append({"slug": slug, "statement": statement, "statement_needed": statement_needed,
                    "occurrences": occurrences, "first_seen": first_seen, "last_seen": last_seen,
                    "cost_min": cost_min, "cost_tokens": cost_tokens, "cost_unmeasured": cost_unmeasured,
                    "fixes": fixes, "recurred_after_fix": recurred_after_fix})

    # L-spec-0280/SD19 — fold harvest.occurrences() (spawn-failed/build-deviation/
    # rejected-criterion/correction, EMITS-authority-filtered exactly like every
    # other type this register reads) into the Problem list `out` built above:
    # join by slug, or mint a new Problem with statement_needed=True (unless an
    # author's problem-minted event already names this slug, in which case that
    # statement is used and statement_needed is False, same as the lesson path
    # above). Additive only — every assertion above this block is unchanged.
    by_slug = {p["slug"]: p for p in out}
    join_targets = [{"slug": p["slug"], "statement": p["statement"]} for p in out]
    for row in harvest.occurrences(filtered, join_targets, toml_path):
        slug = row["slug"]
        p = by_slug.get(slug)
        if p is not None:
            p["occurrences"] += 1
            ts = row["ts"]
            if ts is not None:
                if p["first_seen"] is None or str(ts) < str(p["first_seen"]):
                    p["first_seen"] = ts
                if p["last_seen"] is None or str(ts) > str(p["last_seen"]):
                    p["last_seen"] = ts
            if row["cost_min"] is not None:
                p["cost_min"] = (p["cost_min"] or 0) + row["cost_min"]
            else:
                p["cost_unmeasured"] += 1
            if row["cost_tokens"] is not None:
                p["cost_tokens"] = (p["cost_tokens"] or 0) + row["cost_tokens"]
        else:
            if slug in minted:
                statement, statement_needed = minted[slug], False
            else:
                text = row.get("text") or ""
                statement = text[:200] if text else f"(harvested {row['source_type']}, no free text)"
                statement_needed = True
            new_p = {"slug": slug, "statement": statement, "statement_needed": statement_needed,
                     "occurrences": 1, "first_seen": row["ts"], "last_seen": row["ts"],
                     "cost_min": row["cost_min"], "cost_tokens": row["cost_tokens"],
                     "cost_unmeasured": 1 if row["cost_min"] is None else 0,
                     "fixes": [], "recurred_after_fix": False}
            out.append(new_p)
            by_slug[slug] = new_p

    cfg = _load_rank_config(toml_path)
    divisor = cfg["cost_divisor_min"] or 1.0

    def score(p):
        return p["occurrences"] * (1 + (p["cost_min"] or 0) / divisor)

    return sorted(out, key=score, reverse=True)


def unassigned_summary(events):
    """finding-6: every surviving `lesson` whose resolved `problem` is
    `"unassigned"` (explicit, or the key absent entirely) — MINUS any such
    lesson whose own `_src` equals the `lesson_src=` of a surviving
    `problem-grouped` event: a backfill "clears" that one lesson out of the
    tally (SD15a)."""
    filtered = _emits_filter(events)
    cleared = {e.get("lesson_src") for e in filtered
              if e.get("type") == "problem-grouped" and e.get("lesson_src")}
    unassigned = [e for e in filtered if e.get("type") == "lesson" and _slug_of(e) == "unassigned"]
    remaining = [e for e in unassigned if e.get("_src") not in cleared]
    oldest_ts = min((e.get("ts") for e in remaining), key=lambda t: str(t)) if remaining else None
    return {"count": len(remaining), "oldest_ts": oldest_ts}


def _raw_unassigned_count(events):
    """The count `unassigned_summary` starts from, BEFORE subtracting any
    `problem-grouped` clearing — `doit problems --health`'s own reference
    point for "has at least one backfill event cleared a lesson"."""
    filtered = _emits_filter(events)
    return sum(1 for e in filtered if e.get("type") == "lesson" and _slug_of(e) == "unassigned")


def suggest_slugs(slug, statement, problems):
    """R3/SD12 — rank existing (non-`unassigned`) `Problem`s by
    `difflib.SequenceMatcher` ratio against `f"{slug} {statement}"`,
    descending; return the top 3, or all of them when fewer exist. Ties keep
    `problems`' own order (stable sort), so the highest-ratio candidate is
    always first."""
    target = f"{slug} {statement or ''}"
    scored = [(difflib.SequenceMatcher(None, target, f"{p['slug']} {p.get('statement') or ''}").ratio(), i, p)
              for i, p in enumerate(problems)]
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [p["slug"] for _, _, p in scored[:3]]


def health(events, toml_path=None):
    """`doit problems --health` — SD15's three owed Thinker acts. The
    backfill-confirmation line drops once `unassigned_summary(events)["count"]`
    is lower than the raw (unfiltered-by-grouping) unassigned count — i.e. at
    least one `problem-grouped` event has cleared a lesson. The other two
    (the OUTSTANDING.md banner, the pointer-file swap) are not
    code-checkable and always list as owed."""
    raw = _raw_unassigned_count(events)
    cleared_count = unassigned_summary(events)["count"]
    backfill_owed = not (cleared_count < raw)
    return [{"act": "backfill-confirmation", "owed": backfill_owed},
            {"act": "OUTSTANDING.md banner", "owed": True},
            {"act": "pointer-file swap", "owed": True}]


def _is_open(p):
    """Assumptions: "open" = no `fixes[]` at all, or `recurred_after_fix==True`."""
    return not p["fixes"] or p["recurred_after_fix"]


def main(argv):
    args = list(argv)
    config_path = None
    if "--config" in args:
        i = args.index("--config")
        config_path = args[i + 1]
        del args[i:i + 2]
    as_json = "--json" in args
    open_only = "--open" in args
    needs_statement = "--needs-statement" in args
    show_health = "--health" in args

    ev = fold.read_events()

    if show_health:
        rows = health(ev, config_path)
        if as_json:
            print(json.dumps({"health": rows}, sort_keys=True))
        else:
            for r in rows:
                print(f"{'OWED' if r['owed'] else 'done'}  {r['act']}")
        return 0

    problems = register(ev, config_path)
    if open_only:
        problems = [p for p in problems if _is_open(p)]
    if needs_statement:
        problems = [p for p in problems if p["statement_needed"]]

    if as_json:
        print(json.dumps({"problems": problems, "unassigned": unassigned_summary(ev)}, sort_keys=True))
    else:
        for p in problems:
            print(f"{p['slug']}\toccurrences={p['occurrences']}\tcost_min={p['cost_min']}\t"
                  f"statement_needed={p['statement_needed']}\t{p['statement'] or ''}")
        u = unassigned_summary(ev)
        print(f"unassigned: {u['count']} (oldest {u['oldest_ts']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
