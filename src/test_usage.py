#!/usr/bin/env python3
"""One runnable check on the sub-agent transcript resolver. Run: python3 test_usage.py"""
import json, os, pathlib, sys, tempfile, time

_TEMP = tempfile.TemporaryDirectory(prefix="usage-test-")
TMP = pathlib.Path(_TEMP.name)
# R8.2: HOME/DOIT_ROOT isolation, set before any project module is imported —
# a direct `python3 test_usage.py` must never resolve fold.ROOT to ~/.do-it.
os.environ["HOME"] = str(TMP / "home")
os.environ["DOIT_ROOT"] = str(TMP / "root")
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

def legacy():
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

    # count_turns: the distinct-assistant-id count, independent of split_from_transcript,
    # and None-safe (★ count_turns is the distinct-id count, and None-safe).
    got_turns = usage.count_turns(f3)
    assert got_turns == 2, got_turns  # m1 (streamed twice, same id) + m2 -> 2 distinct ids
    assert usage.count_turns(f4) is None, "no assistant usage at all -> None, never 0"
    assert usage.count_turns(None) is None, "no path -> None"

    # stamp(): the meta.json the pane writes. subagent_tokens (hand-supplied) is
    # kept ALONGSIDE the resolved split, never replaced by it. `turns`, unlike
    # duration_ms/subagent_tokens, is now cross-checked against the transcript's
    # own derived count (★ only turns is corrected past 10%; duration_ms and
    # subagent_tokens are never touched, no matter the gap).
    ROOT = TMP / "root"
    meta, resolved = usage.stamp("L-grader-0099", "claude-sonnet-5", "agent-grow1", "3", "5000", "12345",
                                 root=str(ROOT), projects=PROJECTS)
    assert resolved is True
    assert meta["usage"] == {"subagent_tokens": 12345, "input_tokens": 4, "output_tokens": 45,
                             "cache_read_input_tokens": 300, "cache_creation_input_tokens": 50}, meta["usage"]
    assert meta["model"] == "claude-sonnet-5" and meta["model_observed"] == "claude-sonnet-5" \
        and meta["session"] == "agent-grow1" and meta["turns"] == 2 and meta["duration_ms"] == 5000, meta
    assert meta["turns_supplied"] == 3 and meta["stamp_corrected"] == "turns", meta
    on_disk = json.loads((ROOT / "seat" / "L-grader-0099.meta.json").read_text())
    assert on_disk == meta, "stamp() must not claim the write landed without re-reading it"

    # omitted turns ("-"): derived from the transcript, or None when nothing
    # resolves — never invented as 0 (★ omitted turns is the transcript's own
    # derived count, or None when nothing resolves).
    meta_omitted, _ = usage.stamp("L-grader-0101", "claude-sonnet-5", "agent-grow1", "-", "5000", "1",
                                  root=str(ROOT), projects=PROJECTS)
    assert meta_omitted["turns"] == 2, meta_omitted
    assert "turns_supplied" not in meta_omitted and "stamp_corrected" not in meta_omitted, meta_omitted

    meta_omitted_ghost, _ = usage.stamp("L-grader-0102", "claude-opus-5", "agent-ghost", "-", "1000", "999",
                                        root=str(ROOT), projects=PROJECTS)
    assert meta_omitted_ghost["turns"] is None, meta_omitted_ghost
    assert "turns_supplied" not in meta_omitted_ghost and "stamp_corrected" not in meta_omitted_ghost, \
        meta_omitted_ghost

    # a supplied turns within 10% of the derived value is kept, uncorrected
    # (★ a supplied turns value within 10% of the derived one is kept, uncorrected).
    meta_close, _ = usage.stamp("L-grader-0103", "claude-sonnet-5", "agent-grow1", "2", "5000", "399",
                                root=str(ROOT), projects=PROJECTS)
    assert meta_close["turns"] == 2, meta_close
    assert "turns_supplied" not in meta_close and "stamp_corrected" not in meta_close, meta_close

    # the CLI path (stamp_main) derives turns via the literal "-" sentinel
    # identically to the Python API (★ the CLI path derives turns via the literal
    # - sentinel, identically to the Python API). stamp_main's internal resolve()
    # call reads usage.PROJECTS directly (no --projects flag exists), so the
    # fixture root is monkeypatched in and restored immediately after.
    _real_projects = usage.PROJECTS
    usage.PROJECTS = PROJECTS
    try:
        rc = usage.stamp_main(["L-x-0004", "claude-sonnet-5", "agent-grow1", "-", "5000", "1",
                               "--root", str(ROOT)])
    finally:
        usage.PROJECTS = _real_projects
    assert rc == 0, rc
    cli_meta = json.loads((ROOT / "seat" / "L-x-0004.meta.json").read_text())
    assert cli_meta["turns"] == 2, cli_meta

    # no transcript resolves: the four fields (and model_observed) stay null, the
    # blended figure is still written, and stamp() says unresolved so the CLI can warn
    meta2, resolved2 = usage.stamp("L-grader-0100", "claude-opus-5", "agent-ghost", "1", "1000", "999",
                                   root=str(ROOT), projects=PROJECTS)
    assert resolved2 is False
    assert meta2["usage"]["subagent_tokens"] == 999 and meta2["usage"]["input_tokens"] is None
    assert meta2["model_observed"] is None and meta2["model"] == "claude-opus-5", \
        "unresolved: model stays what the pane typed; model_observed stays null, not a guess"


# Captured verbatim from seat/L-probe-0033.codex.jsonl.
CODEX_LINE = '{"type":"turn.completed","usage":{"input_tokens":4497404,"cached_input_tokens":4350464,"cache_write_input_tokens":0,"output_tokens":26606,"reasoning_output_tokens":5866}}'
CODEX_TOKENS = dict(vendor="codex", input=4497404, cached_input=4350464, output=26606, reasoning=5866)
REPO = pathlib.Path(__file__).resolve().parent.parent


def seatfile(root, body, backend="codex", spawn="L-probe-0033"):
    seat = root / "seat"
    seat.mkdir(parents=True, exist_ok=True)
    path = seat / (spawn + (".codex.jsonl" if backend == "codex" else ".meta.json"))
    path.write_text(body)
    return path


def codex_tokens():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        path = seatfile(root, CODEX_LINE + "\n")
        assert usage.tokens_for_spawn("L-probe-0033", root, "codex") == CODEX_TOKENS
        path.write_text(CODEX_LINE + "\n" + CODEX_LINE + "\n")
        assert usage.tokens_for_spawn("L-probe-0033", root, "codex") == {
            key: value if key == "vendor" else 2 * value for key, value in CODEX_TOKENS.items()}


def seat_tokens():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        split = dict(input_tokens=5, cache_read_input_tokens=20, cache_creation_input_tokens=3, output_tokens=7)
        path = seatfile(root, json.dumps(dict(usage=split)), "seat")
        assert usage.tokens_for_spawn("L-probe-0033", root, "seat") == dict(
            vendor="claude", input=28, cached_input=20, output=7, reasoning=None)
        split["output_tokens"] = None
        path.write_text(json.dumps(dict(usage=split)))
        assert usage.tokens_for_spawn("L-probe-0033", root, "seat")["output"] is None
        split["cache_read_input_tokens"] = None
        path.write_text(json.dumps(dict(usage=split)))
        result = usage.tokens_for_spawn("L-probe-0033", root, "seat")
        assert result["input"] is None and result["cached_input"] is None
        path.write_text('{"usage":{"subagent_tokens":284290,"input_tokens":null,"output_tokens":null,"cache_read_input_tokens":null,"cache_creation_input_tokens":null}}')
        assert usage.tokens_for_spawn("L-probe-0033", root, "seat") is None


def undetermined():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        for backend in ("codex", "seat", "unknown"):
            assert usage.tokens_for_spawn("absent", root, backend) is None
        for spawn in ("../escape", "a/b", "", "x\n", None):
            assert usage.tokens_for_spawn(spawn, root, "codex") is None
        for backend in ("codex", "seat"):
            for body in ("", "{", "[]", "null", "true"):
                seatfile(root, body, backend)
                assert usage.tokens_for_spawn("L-probe-0033", root, backend) is None
            for bad in (True, -1, "5", float("inf"), float("nan"), 1.5):
                fields = dict(json.loads(CODEX_LINE)["usage"]) if backend == "codex" else dict(
                    input_tokens=5, cache_read_input_tokens=20, cache_creation_input_tokens=3, output_tokens=7)
                for field in fields:
                    if field == "cache_write_input_tokens":
                        continue
                    invalid = {**fields, field: bad}
                    row = dict(type="turn.completed", usage=invalid) if backend == "codex" else dict(usage=invalid)
                    seatfile(root, json.dumps(row), backend)
                    assert usage.tokens_for_spawn("L-probe-0033", root, backend) is None, (backend, field, bad)
            for bad in (True, -1, "5", float("inf")):
                row = dict(type="turn.completed", usage=bad) if backend == "codex" else dict(usage=bad)
                seatfile(root, json.dumps(row), backend)
                assert usage.tokens_for_spawn("L-probe-0033", root, backend) is None
        seatfile(root, '\n'.join(json.dumps(dict(type=kind)) for kind in
                                 ("thread.started", "turn.started", "error", "turn.failed")))
        assert usage.tokens_for_spawn("L-probe-0033", root, "codex") is None


def dispatch_fixture(mode):
    """Same real main/emit/fold round trip as dispatch_checks, isolated before imports."""
    import argparse, contextlib, io, subprocess
    from unittest.mock import patch
    import dispatch, fold, models, grading_env
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-qb", "fixture"], cwd=repo, check=True)
        packet = root / "packet.md"
        packet.write_text("unique fixture " + mode)
        output = root / "content" / "L-research-fixture.md"
        result = dict(answered="yes", path="content/L-research-fixture.md", summary="fixture", contamination=False)
        flat = dict(input_tokens=11, output_tokens=7, cache_read_input_tokens=3,
                    cache_creation_input_tokens=2, subagent_tokens=99)
        def backend(spawn, *args, **kwargs):
            if mode != "no_usage":
                seatfile(root, CODEX_LINE, spawn=spawn)
            if mode in ("no_json", "no_usage"):
                return subprocess.CompletedProcess([], 1, "bad", "fixture failed")
            if mode == "failed":
                return subprocess.CompletedProcess([], 0, json.dumps(dict(is_error=True, result="fixture failed", usage=flat)), "")
            if mode == "fallback":
                return subprocess.CompletedProcess([], 0, json.dumps(dict(is_error=True, result="fixture refusal")), "")
            output.parent.mkdir(exist_ok=True)
            output.write_text("fixture result")
            return subprocess.CompletedProcess([], 0, json.dumps(dict(is_error=False, structured_output=result, usage=flat)), "")
        def seat_backend(spawn, *args, **kwargs):
            seatfile(root, json.dumps(dict(usage=flat)), "seat", spawn)
            output.parent.mkdir(exist_ok=True)
            output.write_text("fixture result")
            return subprocess.CompletedProcess([], 0, json.dumps(dict(is_error=False, structured_output=result, usage=flat)), "")
        route = dict(backend="codex", model="fixture", source="fixture",
                     fallback=dict(backend="seat", model="claude-sonnet-5-5") if mode == "fallback" else None)
        with patch.dict(os.environ, DOIT_ROOT=str(root), DOIT_NO_POKE="1"), \
                patch.multiple(dispatch, ROOT=root, EVENTS=root / "events", CONTENT=root / "content"), \
                patch.multiple(fold, ROOT=root, EVENTS=root / "events"), \
                patch.object(models, "resolve", return_value=route), \
                patch.object(dispatch, "ensure_room", return_value=True), \
                patch.object(dispatch, "relay_serving", return_value=True), \
                patch.object(dispatch, "run_codex", side_effect=backend) as ran, \
                patch.object(dispatch, "run_seat", side_effect=seat_backend), \
                patch.object(grading_env, "failure_kind", return_value="work"), \
                patch.object(grading_env, "preflight", return_value=[("git", "fixture absent")] if mode == "preflight" else []):
            os.environ.pop("DOIT_SEAT", None)
            os.environ.pop("DOIT_LEDGER_FILE", None)
            # reviewer reaches the direct capability preflight refusal without a grader bundle.
            role = "reviewer" if mode == "preflight" else "research"
            args = argparse.Namespace(role=role, subject="L-spec-fixture", packet=str(packet), path=str(output),
                                      cwd=str(repo), charter=None, project=None, mcp_config=None,
                                      timeout=None, max_usd=None, seat=False)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    dispatch.main(args)
                except SystemExit as exc:
                    assert exc.code == 1, exc.code
            rows = fold.read_events(use_cache=False)
            terminal = [row for row in rows if row["type"] in ("spawn-done", "spawn-failed")]
            assert len(terminal) == 1, rows
            return terminal[0], ran.call_count


def terminal_roundtrip():
    done, _ = dispatch_fixture("done")
    assert done["type"] == "spawn-done" and done["tokens"] == CODEX_TOKENS, done
    assert done["backend_ran"] is True
    expected = dict(input_tokens=11, output_tokens=7, cache_read=3, cache_creation=2, subagent_tokens=99)
    assert {k: done[k] for k in expected} == expected
    failed, _ = dispatch_fixture("failed")
    assert failed["tokens"] == CODEX_TOKENS and failed["type"] == "spawn-failed"
    assert {k: failed[k] for k in expected} == expected
    for mode in ("no_json", "no_usage"):
        failed, _ = dispatch_fixture(mode)
        assert failed["type"] == "spawn-failed" and failed["backend_ran"] is True
        assert failed["tokens"] == (CODEX_TOKENS if mode == "no_json" else None)
    refused, count = dispatch_fixture("preflight")
    assert not count and refused["tokens"] is None and "backend_ran" not in refused, refused
    assert refused["reason"] == "preflight:git"


def fallback_and_spend():
    import copy, fold
    done, _ = dispatch_fixture("fallback")
    assert done["backend_fallback"] is True and done["tokens"] == dict(
        vendor="claude", input=16, cached_input=3, output=7, reasoning=None), done
    old = {k: v for k, v in done.items() if k not in ("tokens", "backend_ran")}
    def without_additions(value):
        if isinstance(value, dict):
            return {k: without_additions(v) for k, v in value.items() if k not in ("tokens", "backend_ran")}
        if isinstance(value, (list, tuple)):
            return [without_additions(v) for v in value]
        return value
    assert without_additions(fold.fold([copy.deepcopy(done)])) == without_additions(fold.fold([copy.deepcopy(old)]))
    assert fold.spend_by_model([done]) == fold.spend_by_model([old])


def vendor_routing():
    import models, re
    source = REPO / "machine/models.toml"
    routes = models.load(source)
    roles = {p.stem for p in (REPO / "agents").glob("*.md")}
    for role in ("builder", "spec-writer", "research", "probe"):
        assert role in roles and routes[role]["backend"] == "codex"
    for role in ("grader", "reviewer", "plan-auditor", "spec-auditor", "charter-reviewer"):
        assert role in roles and routes[role]["backend"] == "seat" and routes[role]["model"].startswith("claude-")
    assert "executor" in roles and routes["executor"]["model"] == "claude-sonnet-5-5"
    assert all(route["backend"] != "claude-p" for name, route in routes.items() if not name.startswith("_"))
    assert not re.search(r"(?m)^\s*(key|token)\s*=", source.read_text())


def contract_text():
    executor = (REPO / "agents/executor.md").read_text()
    assert executor.count("## Sonnet trial") == 1
    trial = executor.split("## Sonnet trial", 1)[1].split("\n## ", 1)[0]
    for marker in ("claude-sonnet-5-5", "undetermined:", "merge ruling", "question", "claude-opus-5-5",
                   "lesson", "axis=K", "doit health --json", "tokens_per_live_spec.claude", "14 days", "rise", "ends"):
        assert marker in trial, marker
    doc = (REPO / "docs/HOW-IT-WORKS.md").read_text()
    for marker in ("tokens", "vendor", "cached_input", "reasoning", "codex", "claude", "doit health", "unknown"):
        assert marker in doc, marker
    import howto
    assert doc == howto.render(str(REPO))


def run_groups():
    import argparse, hashlib
    parser = argparse.ArgumentParser()
    parser.add_argument("-k")
    args = parser.parse_args()
    groups = {name: globals()[name] for name in ("legacy", "codex_tokens", "seat_tokens", "undetermined",
              "terminal_roundtrip", "fallback_and_spend", "vendor_routing", "contract_text")}
    selected = [name for name in groups if args.k is None or args.k in name]
    if not selected:
        parser.exit(1, "no group matches: " + args.k + "\n")
    for name in selected:
        groups[name]()
        body = name + ": passed"
        print(json.dumps(dict(url="cli://usage/" + name, status=0,
                             body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))


if __name__ == "__main__":
    run_groups()
