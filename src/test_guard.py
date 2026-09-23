#!/usr/bin/env python3
"""Checks on the destructive-delete guard and its registration: AC4-AC9 of
L-spec-0183. Run: python3 test_guard.py

Every settings file this exercises is a scratch file under `harness.py`'s
sandbox — never the real `~/.claude/settings.json`. Every `canonical_command()`
resolution here runs against a synthetic `doit`-stub-plus-script fixture on a
temporarily prepended `PATH`, never the ambient box state, so this file's
result does not depend on whether this spec has been merged into the
canonical `do-it-v2` checkout yet (AC9's own point: a `worktrees`-shaped
checkout, or one simply missing the script, must resolve to `None`, and a
build running inside a worktree IS exactly that shape).
"""
import contextlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import guard  # noqa: E402
import harness  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "hooks" / "destructive-delete-guard.py"
GUARD_PY = pathlib.Path(__file__).resolve().parent / "guard.py"

n = 0


def ok(cond, why):
    global n
    assert cond, why
    n += 1


@contextlib.contextmanager
def path_prepended(dirpath):
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{dirpath}{os.pathsep}{old}" if old else str(dirpath)
    try:
        yield
    finally:
        if old:
            os.environ["PATH"] = old
        else:
            os.environ.pop("PATH", None)


def canonical_fixture():
    """A synthetic canonical checkout: a `doit` stub directly on a directory,
    and this repo's real, just-ported script alongside it at the same
    relative offset the real `doit` uses — everything `canonical_command()`
    needs, none of it depending on the ambient box or a merge having
    happened yet."""
    root = harness.test_root("guard-canonical")
    hooks_dir = root / "scripts" / "hooks"
    hooks_dir.mkdir(parents=True)
    shutil.copyfile(SCRIPT, hooks_dir / "destructive-delete-guard.py")
    stub = root / "doit"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return root


def run_hook(cmd, env_overrides=None):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run([sys.executable, str(SCRIPT)], input=payload,
                           capture_output=True, text=True, env=env)


# ── AC4: six payloads the canonical script must deny ─────────────────────────
DENY_CASES = [
    'rm -rf "$HOME"',
    'rm -rf "$DOIT_ROOT"',
    'rm -rf "$SOME_UNKNOWN_VAR"',
    'rm -rf /home',
    'rm -rf "$HOME/do-it-v2"',
    'rm -rf "$HOME/.claude"',
]
for cmd in DENY_CASES:
    p = run_hook(cmd)
    ok(p.returncode == 0, f"{cmd!r}: exit code must be 0, got {p.returncode} stderr={p.stderr!r}")
    try:
        data = json.loads(p.stdout)
    except Exception as e:
        raise AssertionError(f"{cmd!r}: stdout must be valid JSON, got {p.stdout!r} ({e})")
    ok(data.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
       f"{cmd!r}: must deny, got {data}")

# ── AC5: four payloads that must produce no deny at all ──────────────────────
harmless_pwd = harness.test_root("guard-ac5-pwd")
ALLOW_CASES = [
    'rm -rf /tmp/some-harmless-unrelated-dir',
    'rm somefile.txt',
    'ls -la',
    'rm -rf "$PWD/some-subdir"',
]
for cmd in ALLOW_CASES:
    p = run_hook(cmd, env_overrides={"PWD": str(harmless_pwd)})
    ok(p.returncode == 0, f"{cmd!r}: exit code must be 0, got {p.returncode} stderr={p.stderr!r}")
    ok(p.stdout == "", f"{cmd!r}: stdout must be empty, got {p.stdout!r}")
harness.cleanup(harmless_pwd)

# ── AC9 sub-case 1: canonical_command() under a normal, resolvable PATH ──────
fixture = canonical_fixture()
with path_prepended(str(fixture)):
    which_result = shutil.which("doit")
    ok(which_result is not None, "fixture setup: the stub must be found on PATH")
    expected_dir = pathlib.Path(which_result).resolve().parent
    cmd = guard.canonical_command()
    ok(cmd is not None, f"canonical_command() must resolve against the fixture, got {cmd!r}")
    ok(cmd.endswith("scripts/hooks/destructive-delete-guard.py"),
       f"canonical_command() must end in the script path: {cmd!r}")
    resolved_script = pathlib.Path(cmd.split(" ", 1)[1])
    ok(resolved_script.resolve().parent.parent.parent == expected_dir,
       f"the resolved script must sit under the SAME directory shutil.which('doit') "
       f"resolves to independently: {resolved_script} vs {expected_dir}")

# ── AC9 sub-case 2: PATH with no `doit` on it at all ─────────────────────────
empty_path_dir = harness.test_root("guard-ac9-empty")
old_path = os.environ.get("PATH", "")
os.environ["PATH"] = str(empty_path_dir)
try:
    ok(shutil.which("doit") is None, "fixture setup: an empty PATH dir must not resolve doit")
    ok(guard.canonical_command() is None, "canonical_command() must be None with no doit on PATH")
    scratch9b = harness.test_root("guard-ac9b-settings") / "settings.json"
    ok(guard.install(scratch9b) is False, "install() must refuse when canonical_command() is None")
    ok(not scratch9b.exists(), "install() must not create a file when it refuses")
finally:
    os.environ["PATH"] = old_path

# ── AC9 sub-case 3: doit resolves, but the only script sits under `worktrees` ─
worktree_shaped = harness.test_root("guard-ac9-worktrees")
stub2 = worktree_shaped / "doit"
stub2.write_text("#!/bin/sh\nexit 0\n")
stub2.chmod(0o755)
nested_hooks = worktree_shaped / "worktrees" / "scripts" / "hooks"
nested_hooks.mkdir(parents=True)
shutil.copyfile(SCRIPT, nested_hooks / "destructive-delete-guard.py")
with path_prepended(str(worktree_shaped)):
    ok("worktrees" not in worktree_shaped.parts,
       "fixture precondition: the PATH-target dir itself carries no worktrees component")
    result = guard.canonical_command()
    ok(result is None, f"canonical_command() must be None when the real script only "
                        f"sits under a nested worktrees copy, got {result!r}")
    scratch9c = harness.test_root("guard-ac9c-settings") / "settings.json"
    ok(guard.install(scratch9c) is False, "install() must refuse, script under worktrees-only")
    ok(not scratch9c.exists(), "install() must not write anything on that refusal")

# ── AC6/AC7/AC8 run under the same resolvable canonical fixture ─────────────
with path_prepended(str(fixture)):
    CMD = guard.canonical_command()
    ok(CMD is not None, "the fixture must resolve a canonical command for the rest of this file")

    # AC6 — additive, idempotent install against a scratch file with unrelated content
    scratch6 = harness.test_root("guard-ac6") / "settings.json"
    original = {
        "theme": "dark",
        "hooks": {"PreToolUse": [{"matcher": "Read",
                                   "hooks": [{"type": "command", "command": "other-hook"}]}]},
    }
    scratch6.write_text(json.dumps(original))

    ok(guard.registered(scratch6) is False, "registered() must be False before install()")
    ok(guard.install(scratch6) is True, "install() must succeed")
    ok(guard.registered(scratch6) is True, "registered() must be True right after install()")

    data6 = json.loads(scratch6.read_text())
    ok(data6.get("theme") == "dark", "the unrelated theme key must survive untouched")
    read_entries = data6["hooks"]["PreToolUse"]
    ok(any(e.get("matcher") == "Read" and e.get("hooks") == [{"type": "command", "command": "other-hook"}]
           for e in read_entries),
       f"the unrelated Read-matcher entry must survive byte-for-byte: {read_entries}")
    count_before = len(read_entries)

    ok(guard.install(scratch6) is True, "a second install() must still return True")
    data6b = json.loads(scratch6.read_text())
    entries_b = data6b["hooks"]["PreToolUse"]
    matching = [e for e in entries_b for h in e.get("hooks", []) if h.get("command") == CMD]
    ok(len(matching) == 1, f"exactly one entry naming canonical_command(), got {len(matching)}: {matching}")
    ok(len(entries_b) == count_before, f"a second install() must not add a second top-level entry: {entries_b}")

    # AC7 — malformed JSON is a refusal, byte-unchanged; an absent file is created cleanly
    bad = harness.test_root("guard-ac7-bad") / "settings.json"
    bad.write_bytes(b"{not json")
    before_bytes = bad.read_bytes()
    ok(guard.install(bad) is False, "install() must refuse malformed JSON")
    ok(bad.read_bytes() == before_bytes, "install() must leave malformed JSON byte-for-byte unchanged")

    absent_dir = harness.test_root("guard-ac7-absent")
    absent = absent_dir / "settings.json"
    ok(not absent.exists(), "fixture precondition: the absent path must not exist yet")
    ok(guard.install(absent) is True, "install() on an absent file must succeed")
    absent_data = json.loads(absent.read_text())
    absent_entries = absent_data["hooks"]["PreToolUse"]
    ok(len(absent_entries) == 1, f"exactly one entry on a freshly created file: {absent_entries}")

    # AC8 — stale_panes: exclude fresh, exempt, and non-claude; include only the stale one
    scratch8 = harness.test_root("guard-ac8") / "settings.json"
    guard.install(scratch8)
    mtime = scratch8.stat().st_mtime
    before_t, after_t = mtime - 100, mtime + 100

    procs = [
        {"pid": 1, "argv": ["claude", "-n", "L-executor-0009", "--agent", "executor"], "start_epoch": before_t},
        {"pid": 2, "argv": ["claude", "-n", "L-planner-0003", "--agent", "planner"], "start_epoch": after_t},
        {"pid": 3, "argv": ["claude", "-n", "L-relay-0002", "--agent", "relay"], "start_epoch": before_t},
        {"pid": 4, "argv": ["bash", "-c", "sleep 1"], "start_epoch": before_t},
    ]
    got = guard.stale_panes(scratch8, procs=procs)
    ok([r["pid"] for r in got] == [1], f"exactly pid 1 must be returned, got {got}")

    procs_file = harness.test_root("guard-ac8-procs") / "procs.json"
    procs_file.write_text(json.dumps(procs))
    env = dict(os.environ)
    env["PATH"] = os.environ["PATH"]
    env["DOIT_CLAUDE_SETTINGS_PATH"] = str(scratch8)
    env["DOIT_GUARD_PROCS"] = str(procs_file)
    p_status = subprocess.run([sys.executable, str(GUARD_PY), "status"],
                               capture_output=True, text=True, env=env)
    ok(p_status.returncode != 0, f"status must exit non-zero with a stale pane present: {p_status.stdout}")
    ok("pid=1" in p_status.stdout, f"status must name pid 1: {p_status.stdout!r}")

    procs_file.write_text(json.dumps([r for r in procs if r["pid"] != 1]))
    p_status2 = subprocess.run([sys.executable, str(GUARD_PY), "status"],
                                capture_output=True, text=True, env=env)
    ok(p_status2.returncode == 0, f"status must exit 0 once pid 1 is gone: {p_status2.stdout}")
    ok(p_status2.stdout.strip() != "" and "pid=" not in p_status2.stdout,
       f"status must name nothing once pid 1 is gone: {p_status2.stdout!r}")

print(f"guard: {n} checks pass")
