#!/usr/bin/env python3
"""backfill_tokens.py — the one-time retro backfill (operator ask, 2026-09-15;
docs/handoffs/v2-pilot-retro-model-split.md Next Steps item 9): before
scripts/seat/stamp.sh learned to resolve the four-way split (src/usage.py),
every seat spawn-done event stamped `input_tokens` (and its three siblings) as
null. This script finds every one of those, resolves the split from the
spawn's own `session` field the same way stamp.sh now does going forward, and
either prints what it would write (default) or appends a `correction` event
(--apply) — the ledger is append-only (D111), so a repair is a new event that
names the old one, never an edit.

  backfill_tokens.py [--apply] [--root R]

Runs as the operator: a `correction` is operator-only (fold.EMITS), and
DOIT_LEDGER_FILE decides which file the correction lands in (default
L-operator-local.jsonl, the file the docs already use for this kind of repair).
--apply is the integrator's call, not this script's default, and NEVER against
a root you do not operate — point --root at a throwaway copy to rehearse.
"""
import json, os, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent.parent / "src"
sys.path.insert(0, str(SRC))


def candidates(events):
    """Seat spawn-done events with a session and no split yet. `spawn-failed`
    is excluded on purpose — a failed spawn's usage, when the CLI reported one,
    already rode the normal path; this backfill is for the ones that COMPLETED
    before the split existed."""
    return [e for e in events
            if e.get("type") == "spawn-done" and e.get("spawn_path") == "seat"
            and e.get("session") and e.get("input_tokens") is None]


def main(argv):
    apply = "--apply" in argv
    if "--root" in argv:
        os.environ["DOIT_ROOT"] = argv[argv.index("--root") + 1]
    import fold, usage  # noqa: E402  (after DOIT_ROOT is set)

    events = fold.read_events()
    cs = candidates(events)
    resolved, unresolved = 0, 0
    for e in cs:
        split = usage.resolve(e["session"])
        four = {k: split[k] for k in usage.FIELDS}
        if all(v is None for v in four.values()):
            unresolved += 1
            print(f"UNRESOLVED {e['spawn']} · session {e['session']!r} · subject {e.get('subject')} "
                  f"— no transcript under {usage.PROJECTS}")
            continue
        resolved += 1
        setfields = {"input_tokens": four["input_tokens"], "output_tokens": four["output_tokens"],
                     "cache_read": four["cache_read_input_tokens"],
                     "cache_creation": four["cache_creation_input_tokens"]}
        why = "seat-route backfill from the sub-agent transcript"
        print(f"{'APPLY' if apply else 'DRY  '} {e['spawn']} · ref {e['_src']} · {setfields}")
        if apply:
            fold.append(["correction", e["subject"], f"ref={e['_src']}",
                        f"set:={json.dumps(setfields)}", f"why={why}"])
    print(f"\n{resolved} resolved, {unresolved} unresolved, {len(cs)} candidate(s) total"
         + ("" if apply else " (dry run — pass --apply to write corrections)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
