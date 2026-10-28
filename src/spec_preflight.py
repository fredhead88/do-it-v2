#!/usr/bin/env python3
"""Read-only spec checks, plus a bounded tick that records their evidence."""
import argparse
import ast
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time

import fold
import merge_gate
import packet
import scratch
import verification

TEST_CAP_S = 600
TICK_MAX = 4
DOIT = Path(__file__).resolve().parent.parent / "doit"
HEX = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
TEST_TOKEN = re.compile(r"(?<![\w/])(?:[\w./-]+/)?test_[\w.-]+\.py(?:::[\w.\[\]/:-]+)?")
SAFE_TEST = re.compile(r"(?:[A-Za-z0-9_-]+/)*test_[A-Za-z0-9_.-]+\.py(?:::[A-Za-z0-9_.\[\]/:-]+)?\Z")


def _process(argv, *, timeout=30, **kwargs):
    """Every subprocess is bounded; descendants cannot retain a timed-out pipe."""
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, start_new_session=True,
                          **kwargs) as child:
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except BaseException:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate()
            raise
        return subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)


def _events(root):
    # Use the canonical parser, correction order and authorization door, without
    # updating the reader cache (run itself must leave the ledger root untouched).
    saved = fold.ROOT, fold.EVENTS, fold.PROJECT
    try:
        fold.ROOT, fold.EVENTS, fold.PROJECT = root, root / "events", ""
        if not fold.EVENTS.is_dir():
            raise OSError("events directory unavailable")
        events = fold.read_events(use_cache=False)
        ignored = {id(e) for e in fold.fold(events)[2]}
        return [e for e in events if id(e) not in ignored]
    finally:
        fold.ROOT, fold.EVENTS, fold.PROJECT = saved


def pending(events):
    unmatched = {}
    for index, event in enumerate(events):
        sid = event.get("subject")
        if event.get("type") == "spec-written":
            unmatched[sid] = index
        elif event.get("type") == "spec-preflight":
            unmatched.pop(sid, None)
    return sorted(unmatched, key=unmatched.get)


def _environment(allocation):
    # An allowlist avoids inheriting any production path, database or service
    # override. Each standard directory is separate, including the scratch root.
    env = {k: v for k, v in os.environ.items() if k in
           {"PATH", "LANG", "LANGUAGE", "LC_ALL", "TZ", "SYSTEMROOT"}}
    for name in ("HOME", "DOIT_ROOT", "DOIT_SCRATCH", "TMPDIR", "XDG_CONFIG_HOME",
                 "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        path = allocation / name.lower()
        path.mkdir(mode=0o700)
        env[name] = str(path)
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", PYTHONNOUSERSITE="1")
    return env


def _ac_blocks(text):
    lines = text.split("\n")
    return [(cid, "\n".join(lines[start:end]))
            for cid, start, end in packet._criterion_spans(text)]


def _reason(body, n, reason):
    value = f"check {n}: {reason}"
    if value not in body["undetermined"]:
        body["undetermined"].append(value)


def _finding(body, n, identifier, line):
    row = dict(check=n, id=identifier, line=line)
    if row not in body["findings"]:
        body["findings"].append(row)


def _imports(code):
    tree = ast.parse(code)
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
            modules.extend((node.module + "." if node.module else "") + a.name for a in node.names)
    return modules


def _check_interpreter(body, text, project, checkout):
    if project != "albert-scott":
        return
    block = verification.block(text)
    if block is None:
        _reason(body, 1, "no fenced Verification block")
        return
    for line in block.splitlines():
        if re.search(r"\$PWD/\.venv|\.\./\.venv|(?<![\w/.-])python3?\b", line):
            _finding(body, 1, "interpreter", line)
            continue
        match = re.search(r"(?:^|\s)(/[^\s]*python[\w.]*)\b", line)
        if not match or match[1] == "/opt/albert-scott/.venv/bin/python":
            continue
        try:
            args = shlex.split(line[match.start(1):])
            if "pytest" in args or any(re.match(r"(?:\./)?tests/", t) for t in args[1:]):
                _finding(body, 1, "interpreter", line)
                continue
            if "-c" in args:
                code = args[args.index("-c") + 1]
            else:
                script = next((t for t in args[1:] if t.endswith(".py")), None)
                if script is None or checkout is None:
                    raise ValueError("cannot classify interpreter command")
                path = (checkout / script).resolve()
                if not path.is_relative_to(checkout.resolve()):
                    raise ValueError("script outside checkout")
                code = path.read_text()
            if any(name.split(".")[0] not in sys.stdlib_module_names for name in _imports(code)):
                _finding(body, 1, "interpreter", line)
        except (OSError, SyntaxError, ValueError, IndexError):
            _reason(body, 1, "cannot classify imports: " + line)


def _check_shas(body, text, checkout, env):
    for cid, block in _ac_blocks(text):
        for line in block.splitlines():
            for candidate in HEX.findall(line):
                if checkout is None:
                    _reason(body, 2, "repository or main/master ref unavailable")
                    continue
                result = _process(["git", "rev-parse", "--verify", candidate + "^{commit}"],
                                  cwd=checkout, env=env)
                if result.returncode == 0:
                    _finding(body, 2, cid, line)


def _check_reach(body, writes, checkout):
    for path in writes:
        if not path.endswith(".py"):
            _reason(body, 3, "unsupported Writes file " + path)
    if checkout is None:
        _reason(body, 3, "repository checkout unavailable")
        return
    expanded = {str(p.relative_to(checkout)) for grant in writes for p in checkout.glob(grant)}
    for grant in writes:
        if not grant.endswith(".py"):
            continue
        for target in checkout.glob(grant):
            if not target.is_file():
                continue
            relative = str(target.relative_to(checkout))
            module = relative[:-3].replace("/", ".")
            for test in sorted(checkout.rglob("test_*.py")):
                test_id = str(test.relative_to(checkout))
                if test_id in expanded or not test.resolve().is_relative_to(checkout.resolve()):
                    continue
                try:
                    text = test.read_text()
                    imports = _imports(text)
                except (OSError, SyntaxError, UnicodeError):
                    _reason(body, 3, "cannot inspect " + test_id)
                    continue
                if relative in text or any(m == module or m == target.stem or
                                           module.endswith("." + m) for m in imports):
                    _finding(body, 3, test_id, "reaches Writes file " + relative)


def _test_ids(body, text):
    blocks = [verification.block(text) or ""] + [b for _, b in _ac_blocks(text)]
    ids = []
    for block in blocks:
        for line in block.splitlines():
            # Keep unsupported runners visible, while still executing supported
            # ids elsewhere in the same spec.
            runners = re.findall(r"(?<![\w/.-])(vitest|node)\b", line)
            unsupported_files = re.findall(r"[\w./-]+\.test\.[tj]sx?\b", line)
            for runner in dict.fromkeys(runners or unsupported_files):
                _reason(body, 4, "unsupported runner " + runner)
            if "test_" not in line or ".py" not in line:
                continue
            if (";" in line or "$(" in line or re.search(r"`[^`]+`/", line) or
                    re.search(r"(?:^|\s)(?:--rootdir\S*|-k)(?:\s|$)", line)):
                _reason(body, 4, "unsafe test id " + line.strip())
                continue
            try:
                tokens = shlex.split(line.replace("`", ""))
            except ValueError:
                _reason(body, 4, "unsafe test id " + line.strip())
                continue
            spaced = [t for t in tokens if "test_" in t and ".py" in t and re.search(r"\s", t)]
            if spaced:
                for token in spaced:
                    _reason(body, 4, "unsafe test id " + token)
                continue
            candidates = [token.rstrip('.,)') for token in tokens if 'test_' in token and '.py' in token]
            if not candidates:
                _reason(body, 4, "unsafe test id " + line.strip())
            for token in candidates:
                if not SAFE_TEST.fullmatch(token):
                    _reason(body, 4, "unsafe test id " + token)
                elif token not in ids:
                    ids.append(token)
    return ids


def _check_tests(body, text, project, checkout, env, writes):
    ids = _test_ids(body, text)
    if checkout is None:
        _reason(body, 4, "repository checkout unavailable")
        return
    if project not in ("albert-scott", "do-it-v2"):
        _reason(body, 4, "no runner for project " + project)
        return
    remaining = TEST_CAP_S
    for identifier in ids:
        file = identifier.split("::", 1)[0]
        path = (checkout / file).resolve()
        if not path.is_relative_to(checkout.resolve()):
            _reason(body, 4, "unsafe test id " + identifier + " (outside checkout)")
        elif "::" in identifier and project == "do-it-v2":
            _reason(body, 4, identifier + " not runnable by the script harness")
        elif not path.is_file():
            if any(Path(file).match(grant) for grant in writes):
                _finding(body, 4, identifier, "new on master")
            else:
                _reason(body, 4, "missing test on master " + identifier)
        elif remaining <= 0:
            _reason(body, 4, "timeout: total test budget exhausted before " + identifier)
        else:
            argv = (["/usr/bin/python3", file] if project == "do-it-v2" else
                    ["/opt/albert-scott/.venv/bin/python", "-m", "pytest", "-q", "-p", "no:cacheprovider", identifier])
            start = time.monotonic()
            try:
                result = _process(argv, cwd=checkout, env=env, timeout=remaining)
                if result.returncode:
                    body["red_on_master"].append(identifier)
            except subprocess.TimeoutExpired:
                _reason(body, 4, "timeout running " + identifier)
            except OSError:
                _reason(body, 4, "runner unavailable for " + identifier)
            finally:
                remaining -= time.monotonic() - start


def _validate(body):
    assert set(body) == {"spec", "findings", "red_on_master", "undetermined"}
    assert isinstance(body["spec"], str)
    for row in body["findings"]:
        assert set(row) == {"check", "id", "line"} and type(row["check"]) is int and 1 <= row["check"] <= 4
        assert all(isinstance(row[k], str) for k in ("id", "line"))
    for key in ("red_on_master", "undetermined"):
        assert isinstance(body[key], list) and all(isinstance(v, str) for v in body[key])
    return body


def run(spec_id, *, root=None, now=None):
    root = Path(root) if root is not None else fold.ROOT
    body = dict(spec=spec_id, findings=[], red_on_master=[], undetermined=[])
    try:
        events = _events(root)
        written = next((e for e in reversed(events) if e.get("type") == "spec-written"
                        and e.get("subject") == spec_id), None)
        if written is None:
            body["undetermined"].append("spec: no spec-written")
            return _validate(body)
        path = Path(written.get("path", ""))
        candidates = (path, root / path, root / "content" / path.name)
        source = next(p for p in candidates if p.is_file())
        text = source.read_text()
        project = written.get("project") or ""
        if not re.fullmatch(r"[A-Za-z0-9_-]+", project):
            raise ValueError("missing or unsafe project")
        try:
            writes = merge_gate.grant_from_text(text)
        except merge_gate.Undetermined as exc:
            writes = []
            _reason(body, 3, str(exc))
        with tempfile.TemporaryDirectory(prefix="spec-", dir=scratch.sub("spec-preflight")) as temp:
            allocation = Path(temp)
            env = _environment(allocation)
            repo, checkout = root / "repos" / project, allocation / "checkout"
            try:
                branch = next((name for name in ("main", "master") if _process(
                    ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", "refs/heads/" + name],
                    env=env).returncode == 0), None)
                if branch is None:
                    raise ValueError("main/master ref unavailable")
                selected = _process(["git", "-C", str(repo), "rev-parse", "refs/heads/" + branch], env=env)
                cloned = _process(["git", "-c", "core.hooksPath=" + os.devnull, "clone", "--local", "--no-hardlinks",
                                   "--no-checkout", str(repo), str(checkout)], timeout=120, env=env)
                if selected.returncode or cloned.returncode:
                    raise ValueError("cannot clone project repository")
                detached = _process(["git", "-c", "core.hooksPath=" + os.devnull, "checkout", "--detach", selected.stdout.strip()],
                                    cwd=checkout, env=env, timeout=120)
                if detached.returncode:
                    raise ValueError("cannot detach master checkout")
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                checkout = None
                _reason(body, 4, str(exc))
            for number, check in enumerate((
                    lambda: _check_interpreter(body, text, project, checkout),
                    lambda: _check_shas(body, text, checkout, env),
                    lambda: _check_reach(body, writes, checkout),
                    lambda: _check_tests(body, text, project, checkout, env, writes)), 1):
                try:
                    check()
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    _reason(body, number, str(exc))
    except (OSError, ValueError, StopIteration, scratch.ScratchRootError) as exc:
        body["undetermined"].append("spec: " + (str(exc) or "spec file unavailable"))
    return _validate(body)


def _eligible(sid, events, now):
    indexed = [(i, e) for i, e in enumerate(events) if e.get("subject") == sid]
    written = [(i, e) for i, e in indexed if e.get("type") == "spec-written"]
    if not written:
        return False
    index, newest = written[-1]
    try:
        stamp = datetime.fromisoformat(newest["ts"].replace("Z", "+00:00"))
        if not timedelta(0) <= now - stamp < timedelta(days=3):
            return False
    except (KeyError, ValueError, TypeError):
        return False
    return not any(e.get("type") in {"shipped", "spec-closed", "spec-killed"} or
                   (i > index and e.get("type") == "build-started") for i, e in indexed)


def _append(root, body):
    written = next((e for e in reversed(_events(root)) if e.get("type") == "spec-written"
                    and e.get("subject") == body["spec"]), {})
    project = written.get("project")
    if not isinstance(project, str) or not project:
        raise ValueError("preflight append has no project")
    argv = [str(DOIT), "append", "spec-preflight", body["spec"], "project=" + project]
    argv.extend(key + ":=" + json.dumps(value) for key, value in body.items())
    result = _process(argv, env={**os.environ, "DOIT_ROOT": str(root),
                               "DOIT_LEDGER_FILE": "L-spec-preflight-local.jsonl"}, timeout=30)
    if result.returncode:
        raise ValueError("preflight append failed")


def tick(*, root=None, now=None):
    root = Path(root) if root is not None else fold.ROOT
    now = now or datetime.now(timezone.utc)
    try:
        events = _events(root)
    except (OSError, ValueError) as exc:
        print("preflight ledger unavailable: " + str(exc), file=sys.stderr)
        return 2
    waiting = pending(events)
    eligible = [sid for sid in waiting if _eligible(sid, events, now)]
    for sid in eligible[:TICK_MAX]:
        try:
            _append(root, run(sid, root=root, now=now))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print("preflight append: " + str(exc), file=sys.stderr)
            return 1
    print(f"spec-preflight: processed={min(len(eligible), TICK_MAX)} "
          f"deferred={max(0, len(eligible)-TICK_MAX)} (tick cap {TICK_MAX}); "
          f"skipped={len(waiting)-len(eligible)} "
          "(requires a valid timestamp under 3 days old, no later build-started, and no closed state)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["tick"])
    parser.parse_args()
    sys.exit(tick())
