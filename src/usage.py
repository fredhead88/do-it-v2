#!/usr/bin/env python3
"""usage — the four-way token split for a seat spawn, read from its OWN sub-agent
transcript (retro step 9, docs/handoffs/v2-pilot-retro-model-split.md Next Steps
item 9). On the seat route the pane serves a spawn as an Agent-tool sub-agent;
that sub-agent has its own transcript at

  ~/.claude/projects/<project-slug>/<pane-session-uuid>/subagents/agent-<id>.jsonl

(root overridable via DOIT_CLAUDE_PROJECTS, so a test never touches the real
one). Every assistant line in it carries a `usage` object and a `model` field.
A streamed response repeats the same usage object across several JSONL lines
under one message `id` as it grows (tool calls extend one turn) — dedupe by
id, LAST occurrence wins (the settled totals), THEN sum across ids. That sum
is the cumulative BILLED usage across every API call in the run; it is not
the same number as the pane's blended `subagent_tokens` (see stamp.sh and
docs/handoffs — the two measure different things and neither replaces the
other, which is why stamp.sh keeps both).

Used by scripts/seat/stamp.sh (the completion stamp, S13) and
scripts/seat/backfill_tokens.py (the one-time retro backfill). Unmeasured is
never zero: no resolvable transcript -> every field None, never 0.
"""
import json, os, pathlib

PROJECTS = pathlib.Path(os.environ.get("DOIT_CLAUDE_PROJECTS", pathlib.Path.home() / ".claude" / "projects"))
FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def find_transcript(session, projects=None):
    """<projects>/*/*/subagents/agent-<id>.jsonl for a session naming `agent-<id>`
    (bare `<id>` also accepted). More than one project slug can hold a
    same-named id across roots or repos over time; the NEWEST file by mtime
    wins — the id is only unique enough to matter within one pane's run."""
    if not session:
        return None
    root = pathlib.Path(projects or PROJECTS)
    agent_id = session[len("agent-"):] if str(session).startswith("agent-") else str(session)
    matches = sorted(root.glob(f"*/*/subagents/agent-{agent_id}.jsonl"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def split_from_transcript(path):
    """The four-way split (deduped by message id, last occurrence wins) and the
    model the transcript's own assistant lines ran on — read off the SAME
    winning line as its usage, never a separate scan. None when the file names
    no assistant usage at all (a killed spawn, an empty transcript)."""
    by_id, order = {}, []
    for line in pathlib.Path(path).read_text().splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") or {}
        mid, usage = msg.get("id"), msg.get("usage")
        if not mid or not usage:
            continue
        if mid not in by_id:
            order.append(mid)
        by_id[mid] = (usage, msg.get("model"))
    if not order:
        return None
    totals = {f: 0 for f in FIELDS}
    model = None
    for mid in order:
        u, m = by_id[mid]
        for f in FIELDS:
            totals[f] += u.get(f) or 0
        model = m or model
    return {**totals, "model_observed": model}


def count_turns(path):
    """The count of distinct assistant message ids in the transcript at `path` —
    the same per-line filter and dedup-by-id set split_from_transcript already
    applies, read as an independent single pass. None when `path` is falsy,
    unreadable, or no line satisfies the filter — unmeasured is never zero."""
    if not path:
        return None
    try:
        lines = pathlib.Path(path).read_text().splitlines()
    except OSError:
        return None
    ids = set()
    for line in lines:
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") or {}
        mid, usage_obj = msg.get("id"), msg.get("usage")
        if not mid or not usage_obj:
            continue
        ids.add(mid)
    return len(ids) if ids else None


def resolve(session, projects=None):
    """session -> {input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens, model_observed, transcript}. All four token
    fields and model_observed are None when no transcript resolves, or a
    resolved transcript carries no assistant usage — unmeasured is never a
    silent zero."""
    empty = {**{f: None for f in FIELDS}, "model_observed": None, "transcript": None}
    path = find_transcript(session, projects)
    if path is None:
        return empty
    got = split_from_transcript(path)
    if got is None:
        return {**empty, "transcript": str(path)}
    return {**got, "transcript": str(path)}


def main(argv):
    """usage.py <session>            print the resolved split as JSON
       usage.py stamp <spawn> <model> <session> <turns> <duration_ms> <subagent_tokens> [--root R]
                                      resolve + write <root>/seat/<spawn>.meta.json (stamp.sh's helper)
    """
    import sys
    if argv[:1] == ["stamp"]:
        return stamp_main(argv[1:])
    if len(argv) != 1:
        sys.exit("usage: usage.py <session-id> | usage.py stamp <spawn> <model> <session> "
                 "<turns> <duration_ms> <subagent_tokens> [--root R]")
    print(json.dumps(resolve(argv[0])))
    return 0


def stamp(spawn, model, session, turns, duration_ms, subagent_tokens, root=None, projects=None):
    """Resolve `session`'s transcript and write `<root>/seat/<spawn>.meta.json` —
    the pane's completion stamp (S13). `model:` stays what the pane operator
    typed (the requested model); the four-way split and `model_observed` (the
    model the transcript's own assistant lines actually ran on — real evidence,
    unlike the operator's typed claim) are added alongside the still
    hand-supplied blended `subagent_tokens`, never replacing it. `duration_ms`
    and `subagent_tokens` are always taken as supplied, never derived or
    corrected. `turns` may be `None` or the literal string "-" to mean
    "omitted": it is then set to the transcript's own derived turn count
    (count_turns), itself possibly None when nothing resolves — never invented
    as 0. A supplied `turns` that differs from the derived count by more than
    10% (of the derived value) is overridden with the derived value, and the
    override is recorded as `turns_supplied`/`stamp_corrected: "turns"`; a
    supplied value within 10% (or with no derived value to compare against) is
    kept as supplied, uncorrected. Returns the meta dict written, and whether a
    transcript resolved (for stamp.sh's warning — a missing transcript must not
    block the completion signal)."""
    r = pathlib.Path(root) if root else pathlib.Path(os.environ.get("DOIT_ROOT", pathlib.Path.home() / ".do-it"))
    split = resolve(session, projects)
    derived = count_turns(split["transcript"])
    omitted = turns is None or turns == "-"
    corrected = {}
    if omitted:
        turns_value = derived
    else:
        supplied = int(turns)
        if derived is not None and abs(supplied - derived) > 0.10 * max(abs(derived), 1):
            turns_value = derived
            corrected = {"turns_supplied": supplied, "stamp_corrected": "turns"}
        else:
            turns_value = supplied
    meta = {
        "model": model, "session": session, "turns": turns_value, "duration_ms": int(duration_ms),
        "usage": {
            "subagent_tokens": int(subagent_tokens),
            "input_tokens": split["input_tokens"], "output_tokens": split["output_tokens"],
            "cache_read_input_tokens": split["cache_read_input_tokens"],
            "cache_creation_input_tokens": split["cache_creation_input_tokens"],
        },
        "model_observed": split["model_observed"],
        "served_as": "general-purpose+contract",
        **corrected,
    }
    seat_dir = r / "seat"
    seat_dir.mkdir(parents=True, exist_ok=True)
    p = seat_dir / f"{spawn}.meta.json"
    p.write_text(json.dumps(meta) + "\n")
    assert json.loads(p.read_text()) == meta, f"write to {p} did not land"
    return meta, split["input_tokens"] is not None


def stamp_main(argv):
    import sys
    root = None
    if "--root" in argv:
        i = argv.index("--root")
        root = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if len(argv) != 6:
        sys.exit("usage: usage.py stamp <spawn> <model> <session> <turns> <duration_ms> "
                 "<subagent_tokens> [--root R]")
    spawn, model, session, turns, duration_ms, subagent_tokens = argv
    meta, resolved = stamp(spawn, model, session, turns, duration_ms, subagent_tokens, root=root)
    print(json.dumps(meta))
    if not resolved:
        print(f"WARNING: no sub-agent transcript resolved for session {session!r} under "
              f"{pathlib.Path(os.environ.get('DOIT_CLAUDE_PROJECTS') or PROJECTS)} — the four-way "
              "split stays null (unmeasured, never zero)", file=__import__("sys").stderr)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
