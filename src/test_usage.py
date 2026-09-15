#!/usr/bin/env python3
"""One runnable check on the sub-agent transcript resolver. Run: python3 test_usage.py"""
import json, pathlib, sys, tempfile, time

TMP = pathlib.Path(tempfile.mkdtemp())
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import usage  # noqa: E402


def write_transcript(dirpath, name, lines):
    p = dirpath / "subagents"
    p.mkdir(parents=True, exist_ok=True)
    f = p / f"agent-{name}.jsonl"
    f.write_text("".join(json.dumps(l) + "\n" for l in lines))
    return f


def assistant(mid, usage_obj, model="claude-sonnet-5"):
    return {"type": "assistant", "message": {"id": mid, "model": model, "usage": usage_obj}}


PROJECTS = TMP / "projects"

# find_transcript: bare id or "agent-<id>" both resolve; a session with no
# match anywhere returns None, never raises.
slug1 = PROJECTS / "-opt-albert-scott" / "session-a"
f1 = write_transcript(slug1, "abc123", [assistant("m1", {"input_tokens": 1, "output_tokens": 2,
                                                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})])
assert usage.find_transcript("agent-abc123", PROJECTS) == f1
assert usage.find_transcript("abc123", PROJECTS) == f1
assert usage.find_transcript("agent-nope", PROJECTS) is None
assert usage.find_transcript(None, PROJECTS) is None
assert usage.find_transcript("", PROJECTS) is None

# newest match wins when the same agent id exists under two project slugs
slug2 = PROJECTS / "-home-albert-do-it" / "session-b"
time.sleep(0.05)
f2 = write_transcript(slug2, "abc123", [assistant("m1", {"input_tokens": 9, "output_tokens": 9,
                                                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})])
assert usage.find_transcript("agent-abc123", PROJECTS) == f2, "the newer file (by mtime) wins"

# split_from_transcript: a streamed message repeats its usage under the SAME id
# as the response grows — dedupe by id, LAST occurrence wins, then sum across ids.
slug3 = PROJECTS / "-opt-albert-scott" / "session-c"
f3 = write_transcript(slug3, "grow1", [
    assistant("m1", {"input_tokens": 2, "output_tokens": 10, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50}),
    assistant("m1", {"input_tokens": 2, "output_tokens": 40, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50}),  # same id, later, grown
    {"type": "attachment", "attachment": {"type": "environment"}},          # non-assistant lines are skipped
    assistant("m2", {"input_tokens": 2, "output_tokens": 5, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 0}),
])
got = usage.split_from_transcript(f3)
assert got == {"input_tokens": 4, "output_tokens": 45, "cache_read_input_tokens": 300,
              "cache_creation_input_tokens": 50, "model_observed": "claude-sonnet-5"}, got

# a transcript with no assistant usage at all resolves to None (the caller
# decides how to render that; the function itself never invents zero)
slug4 = PROJECTS / "-opt-albert-scott" / "session-d"
f4 = write_transcript(slug4, "empty1", [{"type": "attachment"}, {"type": "user", "message": {"role": "user"}}])
assert usage.split_from_transcript(f4) is None

# resolve(): the public entry point. A real session resolves every field; an
# unresolvable one returns every field None (unmeasured is never a silent zero).
r = usage.resolve("agent-grow1", PROJECTS)
assert r["input_tokens"] == 4 and r["output_tokens"] == 45 and r["model_observed"] == "claude-sonnet-5" \
    and r["transcript"] == str(f3), r
r_none = usage.resolve("agent-does-not-exist", PROJECTS)
assert all(r_none[f] is None for f in usage.FIELDS) and r_none["model_observed"] is None \
    and r_none["transcript"] is None, r_none
r_empty = usage.resolve("agent-empty1", PROJECTS)
assert all(r_empty[f] is None for f in usage.FIELDS) and r_empty["transcript"] == str(f4), \
    "a resolved-but-empty transcript still names itself, distinct from no-match-at-all"

# a session with no id at all (an old event, or a claude-p/codex terminal that
# never had one) resolves to the same all-None shape without touching the fs
assert usage.resolve(None, PROJECTS) == {**{f: None for f in usage.FIELDS}, "model_observed": None, "transcript": None}

# stamp(): the meta.json the pane writes. subagent_tokens (hand-supplied) is
# kept ALONGSIDE the resolved split, never replaced by it.
ROOT = TMP / "root"
meta, resolved = usage.stamp("L-grader-0099", "claude-sonnet-5", "agent-grow1", "3", "5000", "12345",
                             root=str(ROOT), projects=PROJECTS)
assert resolved is True
assert meta["usage"] == {"subagent_tokens": 12345, "input_tokens": 4, "output_tokens": 45,
                         "cache_read_input_tokens": 300, "cache_creation_input_tokens": 50}, meta["usage"]
assert meta["model"] == "claude-sonnet-5" and meta["model_observed"] == "claude-sonnet-5" \
    and meta["session"] == "agent-grow1" and meta["turns"] == 3 and meta["duration_ms"] == 5000
on_disk = json.loads((ROOT / "seat" / "L-grader-0099.meta.json").read_text())
assert on_disk == meta, "stamp() must not claim the write landed without re-reading it"

# no transcript resolves: the four fields (and model_observed) stay null, the
# blended figure is still written, and stamp() says unresolved so the CLI can warn
meta2, resolved2 = usage.stamp("L-grader-0100", "claude-opus-5", "agent-ghost", "1", "1000", "999",
                               root=str(ROOT), projects=PROJECTS)
assert resolved2 is False
assert meta2["usage"]["subagent_tokens"] == 999 and meta2["usage"]["input_tokens"] is None
assert meta2["model_observed"] is None and meta2["model"] == "claude-opus-5", \
    "unresolved: model stays what the pane typed; model_observed stays null, not a guess"

print(f"OK ({__file__})")
