"""Read-budget regressions for the sweep's shared ledger snapshot."""
import ast
from contextlib import contextmanager, ExitStack
import dataclasses
from datetime import datetime, timedelta, timezone
import inspect
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from unittest.mock import patch

import checks
import dispatch
import fold
import prod_browser
import prod_runner
import provision
import sweep_owed

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime.now(timezone.utc).replace(microsecond=0)
EPOCH = (NOW - timedelta(days=3650)).isoformat(timespec="seconds")

@contextmanager
def read_once_fixture(*, expired=False):
    with tempfile.TemporaryDirectory(prefix="sweep-read-once-") as temporary, ExitStack() as stack:
        root = pathlib.Path(temporary)
        for directory in ("events", "content", "home", "repos/demo"):
            (root / directory).mkdir(parents=True)
        stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(root), HOME=str(root / "home"),
            DOIT_LEDGER_FILE="L-operator-local.jsonl"))
        for module in (dispatch, fold, sweep_owed.packet):
            for attr in ("ROOT", "CONTENT", "EVENTS", "PACKETS"):
                if hasattr(module, attr):
                    stack.enter_context(patch.object(module, attr, root if attr == "ROOT" else root / attr.lower()))
        stack.enter_context(patch.object(fold, "PROJECT", None))
        stack.enter_context(patch.object(sweep_owed.verifier, "run", return_value=[]))
        stack.enter_context(patch.object(sweep_owed, "dispatch_batch", side_effect=AssertionError("paid dispatch")))
        # Hold a model batch open while allowing all real observation/setup paths.
        stack.enter_context(patch.object(sweep_owed, "open_batch", return_value=True))
        repo = root / "repos/demo"
        def git(*args):
            return subprocess.check_output(["git", "-C", str(repo), *args], text=True,
                                           stderr=subprocess.DEVNULL).strip()
        git("init", "-q")
        (repo / "fixture").write_text("scratch\n")
        git("add", ".")
        git("-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "fixture")
        sha = git("rev-parse", "HEAD")
        sid = "L-spec-9901"
        (root / "content" / (sid + ".md")).write_text("## Acceptance criteria\n" + "".join(
            f"AC{i} [backend]: fixture\n" for i in range(1, 6)))
        record = checks.CheckRecord(sid + ":AC1", sid, "AC1",
            [sys.executable, "-c", "print('scratch verdict')"], sha,
            "scratch", False, "repo-venv", "pre-merge", "script")
        ev = [dict(type="spec-written", project="demo"),
              dict(type="check-declared", **dataclasses.asdict(record))]
        ev.extend(dict(type="owed-ac", criterion=f"AC{i}", line="fixture",
            wake_at=(NOW - timedelta(hours=1)).isoformat(),
            **({"capability": "browser"} if i == 2 else {})) for i in range(1, 5))
        if expired:
            ev.append(dict(type="owed-ac", criterion="AC5", line="fixture",
                wake_at=(NOW - timedelta(days=5)).isoformat(),
                deadline=(NOW - timedelta(days=1)).isoformat()))
        (root / "events/L-spec-writer-fixture.jsonl").write_text("".join(json.dumps(
            dict(v=1, subject=sid, ts=EPOCH, **event)) + "\n" for event in ev))
        (root / "events/L-executor-fixture.jsonl").write_text("".join(json.dumps(
            dict(v=1, subject=sid, ts=EPOCH, **event)) + "\n" for event in
            [dict(type="shipped", sha=sha), dict(type="deploy-landed", target="demo", sha=sha)]))
        (root / "events/L-operator-fixture.jsonl").write_text("".join(json.dumps(dict(v=1,
            subject=sid, ts=(NOW - timedelta(hours=2)).isoformat(), **event)) + "\n" for event in
            [dict(type="owed-routed", criterion="AC2", route="relabel", capability="browser"),
             *([dict(type="owed-unobservable", criterion="AC5", capability="repo",
                     why="scratch expiry", evidence="scratch")] if expired else [])]))
        if expired:
            (root / "events/L-sweep-fixture.jsonl").write_text("".join(json.dumps(dict(v=1,
                subject=sid, ts=(NOW - timedelta(hours=3)).isoformat(), type="owed-routed",
                criterion="AC5", route="repo", epoch=EPOCH)) + "\n"
                for _ in range(sweep_owed.owed_routes.MAX_ATTEMPTS)))
        stack.enter_context(patch.object(prod_browser.grading_env, "_look_toml",
            return_value={"prod": [dict(project="demo", repo=str(repo))]}))
        yield root, sid
@contextmanager
def ledger_read_spy():
    original = fold.read_events
    calls, snapshots = [], []
    depth = 0
    def read(*args, **kwargs):
        nonlocal depth
        assert depth == 0, "nested ledger read"
        calls.append(pathlib.Path(inspect.currentframe().f_back.f_code.co_filename).name)
        depth += 1
        try:
            result = original(*args, **kwargs)
            snapshots.append(result)
            return result
        finally:
            depth -= 1
    with patch.object(fold, "read_events", new=read):
        yield calls, snapshots
def test_ac1_dry_run_reads_the_ledger_once():
    with read_once_fixture(), ledger_read_spy() as (calls, snapshots), \
            patch.object(prod_browser, "_target", wraps=prod_browser._target) as target:
        sweep_owed.run(dry_run=True, now=NOW)
        assert len(sweep_owed.due_candidates(fold.fold(snapshots[0])[0], NOW)) >= 3
        assert target.called, "browser deployment resolution must run"
        assert calls == ["sweep_owed.py"], calls
    print("L-spec-1531 AC1 ok")
def test_ac2_real_sweep_read_budget():
    for expired in (False, True):
        with read_once_fixture(expired=expired) as (root, sid), ledger_read_spy() as (calls, snapshots), \
                patch.object(provision, "_repo", wraps=provision._repo) as checkout:
            lines = sweep_owed.run(now=NOW)
            assert checkout.called, "real repo-venv checkout must run"
            assert any(f"{sid}/AC1: script met" in line for line in lines), lines
            assert len(sweep_owed.due_candidates(fold.fold(snapshots[0])[0], NOW)) >= 4
            assert not set(calls) & {"prod_runner.py", "provision.py", "prod_browser.py"}, calls
            assert calls.count("sweep_owed.py") <= (4 if expired else 3), calls
            print("AC2 reads:", "earlier append" if expired else "no earlier append", calls)
    print("L-spec-1531 AC2 ok")
def test_ac3_every_remaining_read_is_marked():
    for filename in ("sweep_owed.py", "prod_runner.py", "provision.py", "prod_browser.py"):
        source = (REPO_ROOT / "src" / filename).read_text()
        lines = source.splitlines()
        tree = ast.parse(source)
        rereads = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "read_events":
                continue
            nearby = "\n".join(lines[max(0, node.lineno - 2):node.lineno])
            kind = "events=None default" if "events=None default" in nearby else "re-read:"
            assert kind in nearby, (filename, node.lineno)
            if kind == "re-read:":
                assert re.search(r"re-read:\s*\S+", nearby)
                rereads += 1
            owner = next(n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and n.lineno <= node.lineno <= n.end_lineno)
            print(filename, owner, kind)
        if filename == "sweep_owed.py":
            assert rereads <= 4
    print("L-spec-1531 AC3 ok")
def test_ac5_prod_runner_sees_earlier_appends():
    for expired in (False, True):
        with read_once_fixture(expired=expired) as (_, sid), ledger_read_spy() as (calls, snapshots), \
                patch.object(prod_runner, "run", wraps=prod_runner.run) as run:
            sweep_owed.run(now=NOW)
            supplied = run.call_args.kwargs["events"]
            if expired:
                assert any(e.get("subject") == sid and e.get("type") == "escalation-blocking"
                           and e.get("kind") == "owed-expired" for e in supplied)
            else:
                assert supplied is snapshots[0]
    print("L-spec-1531 AC5 ok")


def main():
    test_ac1_dry_run_reads_the_ledger_once()
    test_ac2_real_sweep_read_budget()
    test_ac3_every_remaining_read_is_marked()
    test_ac5_prod_runner_sees_earlier_appends()


if __name__ == "__main__":
    main()
