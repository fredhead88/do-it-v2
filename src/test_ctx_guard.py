#!/usr/bin/env python3
"""Context hook delivery, persistence and fail-silent input checks."""
import datetime as dt
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
NOW = dt.datetime.now(dt.timezone.utc)
SENTENCES = {
    "planner": "Context is at {n}%. Finish the step you are on, write your handover file, then run `doit pane-end <subject> --handover <file> --reason ctx`.",
    "thinker": "Context is at {n}%. Finish the step you are on, write your handover file, then run `doit pane-end <subject> --handover <file> --reason ctx`.",
    "executor": "Context is at {n}%. Finish the step you are on, write your handover file, then run `doit pane-end --executor --handover <file> --reason ctx`.",
    "relay": "Context is at {n}%. Finish the step you are on, then run `doit pane-end --relay --reason ctx`.",
}


def run(root, payload, role="planner"):
    return subprocess.run(["/usr/bin/python3", str(HERE / "ctx_guard.py"), "--role", role],
                          input=payload, text=True, capture_output=True,
                          env={**os.environ, "DOIT_ROOT": str(root)}, timeout=10)


def check_delivery():
    with tempfile.TemporaryDirectory(prefix="1131-ctx-") as tmp:
        root = Path(tmp)
        state_dir = root / "state" / "pane-ctx"
        state_dir.mkdir(parents=True)
        state = {"used_percentage": 50, "observed_at": NOW.isoformat(), "cwd": str(HERE.parent)}
        for role, sentence in SENTENCES.items():
            for event in ("UserPromptSubmit", "PostToolUse"):
                sid = f"{role}_{event}"
                (state_dir / f"{sid}.json").write_text(json.dumps(state))
                payload = json.dumps({"session_id": sid, "hook_event_name": event})
                first, second = run(root, payload, role), run(root, payload, role)
                assert first.returncode == second.returncode == 0
                assert not first.stderr and not second.stderr
                expected = sentence.format(n=50)
                if event == "PostToolUse":
                    assert json.loads(first.stdout) == {"hookSpecificOutput": {
                        "hookEventName": event, "additionalContext": expected}}
                else:
                    assert first.stdout == expected + "\n"
                assert second.stdout == "" and (state_dir / f"{sid}.ctx-guard").is_file()
                print(f"1131-AC1 {role} {event} first=printed second=silent")
        (state_dir / "fraction.json").write_text(json.dumps({**state, "used_percentage": 62.9}))
        p = run(root, json.dumps({"session_id": "fraction", "hook_event_name": "UserPromptSubmit"}))
        assert p.stdout == SENTENCES["planner"].format(n=62) + "\n"

        # Concurrent processes must reserve the same marker exactly once.
        (state_dir / "race.json").write_text(json.dumps(state))
        racers = [subprocess.Popen(["/usr/bin/python3", str(HERE / "ctx_guard.py"), "--role", "planner"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env={**os.environ, "DOIT_ROOT": str(root)}) for _ in range(8)]
        results = [p.communicate(json.dumps({"session_id": "race", "hook_event_name": "UserPromptSubmit"}),
                                 timeout=10) for p in racers]
        assert sum(bool(out) for out, err in results) == 1
        assert all(p.returncode == 0 for p in racers) and all(not err for out, err in results)

        # Each invalid input gets a separate directory; none may spend a marker.
        good_input = {"session_id": "S", "hook_event_name": "UserPromptSubmit"}
        cases = [("no-state", None, good_input), ("bad-state-json", "{", good_input)]
        for key, values in {
            "used_percentage": [None, "50", True, float("nan"), -1, 49.9, float("inf")],
            "observed_at": [None, "garbled", (NOW - dt.timedelta(minutes=11)).isoformat(),
                            (NOW + dt.timedelta(minutes=2)).isoformat()],
        }.items():
            for i, value in enumerate(values):
                changed = dict(state)
                if value is None:
                    changed.pop(key)
                else:
                    changed[key] = value
                cases.append((f"{key}-{i}", json.dumps(changed), good_input))
        for label, payload in [("empty-stdin", ""), ("bad-stdin", "not json"), ("stdin-list", "[]"),
                               ("no-session", {"hook_event_name": "UserPromptSubmit"}),
                               ("no-event", {"session_id": "S"})]:
            cases.append((label, json.dumps(state), payload))
        for i, event in enumerate(["Stop", "SessionStart", "other", None, [], 1]):
            cases.append((f"bad-event-{i}", json.dumps(state), {**good_input, "hook_event_name": event}))
        for i, sid in enumerate(["../x", "a/b", "", "a" * 129, None, 1, []]):
            cases.append((f"bad-session-{i}", json.dumps(state), {**good_input, "session_id": sid}))
        for label, contents, payload in cases:
            case_root = root / label
            case_dir = case_root / "state" / "pane-ctx"
            case_dir.mkdir(parents=True)
            if contents is not None:
                (case_dir / "S.json").write_text(contents)
            p = run(case_root, payload if isinstance(payload, str) else json.dumps(payload))
            assert (p.returncode, p.stdout, p.stderr) == (0, "", ""), (label, p)
            assert not list(case_root.rglob("*.ctx-guard")), label
            print(f"1131-AC2 {label} silent")
        assert not [p for label, _, _ in cases for p in (root / label).rglob("*.ctx-guard")]
        print("1131-AC2 invalid-case markers=[]")


def check_hooks(argv, role):
    """Validate the actual command and Claude's hook matcher envelope."""
    assert argv.count("--settings") == 1
    index = argv.index("--settings")
    assert index < len(argv) - 1
    hooks = json.loads(argv[index + 1])["hooks"]
    assert set(hooks) == {"PostToolUse", "UserPromptSubmit"}
    for event, matchers in hooks.items():
        assert len(matchers) == 1 and len(matchers[0]["hooks"]) == 1
        hook = matchers[0]["hooks"][0]
        assert set(hook) == {"type", "command"} and hook["type"] == "command"
        assert shlex.split(hook["command"]) == ["/usr/bin/python3", str(HERE / "ctx_guard.py"), "--role", role]
    assert not ({"-p", "--json-schema", "--output-format"} & set(argv))
    flags = ["-n", "--agent", "--model", "--disallowedTools", "--dangerously-skip-permissions"]
    positions = [argv.index(flag) for flag in flags if flag in argv]
    assert positions == sorted(positions)
    print(f"1131-AC3 {role} hooks=2")


if __name__ == "__main__":
    check_delivery()
    base_ref = subprocess.run(["git", "merge-base", "HEAD", "origin/main"], cwd=HERE.parent,
                              capture_output=True, text=True, check=True).stdout.strip()
    for role in SENTENCES:
        path = f"agents/{role}.md"
        data = (HERE.parent / path).read_bytes()
        base = subprocess.run(["git", "show", f"{base_ref}:{path}"], cwd=HERE.parent,
                              capture_output=True, check=True).stdout
        assert data.split(b"---", 2)[:2] == base.split(b"---", 2)[:2], role
        lines = [line for line in data.decode().splitlines() if line.startswith("Context rule:")]
        assert len(lines) == 1 and all(word in lines[0] for word in ["50%", "doit pane-end", "--reason ctx"])
        if role in ("executor", "relay"):
            assert f"--{role}" in lines[0]
        if role == "planner":
            assert "overrides the 60% budget line" in lines[0]
        print(f"1131-AC6 {role} one-line")
