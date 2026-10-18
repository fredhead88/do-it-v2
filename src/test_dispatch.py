#!/usr/bin/env python3
"""Run dispatch regressions and the ordered historical fixture chunks."""
from pathlib import Path
import subprocess, sys

def test_dispatch_1134():
    import argparse, contextlib, hashlib, io, json, os, tempfile
    from unittest import mock
    with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ):
        os.environ["DOIT_ROOT"] = temporary
        os.environ["DOIT_NO_POKE"] = "1"
        os.environ.pop("DOIT_SEAT", None)
        sys.path.insert(0, str(Path(__file__).parent))
        import dispatch, fold, models
        root = Path(temporary)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "l-spec-9134"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "--allow-empty", "-qm", "fixture"], cwd=repo, check=True)
        pinned = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                                capture_output=True, text=True).stdout.strip()
        packet = root / "packet.md"
        packet.write_text("fixture\nPINNED_BASE_SHA: " + pinned + "\n")

        def signed(ac, body):
            print(json.dumps({"criterion": ac, "url": "cli://dispatch/" + ac, "status": 0,
                              "body_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                              "body_excerpt": body}))

        with mock.patch.multiple(dispatch, ROOT=root, EVENTS=root / "events", CONTENT=root / "content"), \
                mock.patch.multiple(fold, ROOT=root, EVENTS=root / "events"), \
                mock.patch.object(models, "resolve", return_value={"backend": "codex", "model": "fixture", "source": "fixture"}), \
                mock.patch.object(dispatch, "ensure_room", return_value=True), \
                mock.patch.object(dispatch, "run_codex", side_effect=RuntimeError("fixture backend stop")) as backend:
            def start(role, caller):
                if caller is None:
                    os.environ.pop("DOIT_LEDGER_FILE", None)
                else:
                    os.environ["DOIT_LEDGER_FILE"] = str(root / caller)
                a = argparse.Namespace(role=role, subject="L-spec-9134", packet=str(packet), path=None,
                                       cwd=str(repo), charter=None, project=None, mcp_config=None,
                                       timeout=None, max_usd=None, seat=False)
                before = backend.call_count
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    try:
                        dispatch.main(a)
                    except RuntimeError as exc:
                        assert str(exc) == "fixture backend stop"
                assert backend.call_count == before + 1
                ledger = max((root / "events").glob(f"L-{role}-[0-9]*.jsonl"))
                rows = [json.loads(line) for line in ledger.read_text().splitlines()]
                event = next(row for row in rows if row["type"] in ("spawn-started", "build-started"))
                assert "actor" not in event
                return {"type": event["type"], "issued_by": event["issued_by"]}

            row = start("spec-auditor", "L-executor-0001.jsonl")
            assert row == {"type": "spawn-started", "issued_by": "executor"}, row
            signed("AC1", row)
            print("issued_by AC1 ok")
            row = start("builder", "L-tick-local.jsonl")
            assert row == {"type": "build-started", "issued_by": "tick"}, row
            signed("AC2", row)
            print("issued_by AC2 ok")
            rows = [start(role, caller) for caller in (None, "L--0001.jsonl")
                    for role in ("spec-auditor", "builder")]
            assert all(row["issued_by"] == "unknown" for row in rows), rows
            for role in ("spec-auditor", "builder"):
                assert start(role, "foo.jsonl")["issued_by"] == "foo"
            signed("AC3", rows)
            print("issued_by AC3 ok")

        caller = str(root / "L-tick-local.jsonl")
        with mock.patch.dict(os.environ, {"DOIT_LEDGER_FILE": caller, "ANTHROPIC_API_KEY": "fixture"}):
            parent = dict(os.environ)
            for runner in (dispatch.run_claude, dispatch.run_codex_exec):
                with mock.patch.object(dispatch.subprocess, "run") as child:
                    runner(["fixture-agent"], "packet", str(repo), 12)
                env = child.call_args.kwargs["env"]
                assert env.get("DOIT_LEDGER_FILE") != caller
                assert "ANTHROPIC_API_KEY" not in env
                assert dict(os.environ) == parent
        signed("AC4", {"runners": 2, "caller_ledger_removed": True, "metered_key_removed": True,
                       "parent_unchanged": True})
        print("issued_by AC4 ok")

def test_dispatch_0898():
    _checks = Path(__file__).with_name("dispatch_checks_3.py")
    _new = _checks.read_text().split("# END L0898 REGRESSIONS\n", 1)[0]
    # Separate processes keep module-level ledger roots hermetic during collection.
    subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, " +
                    repr(str(_checks.parent)) + "); " + _new + "\ncheck_dispatch_0898()"], check=True)

_legacy = r"""
from pathlib import Path
_historical = Path(__file__).with_name("dispatch_checks_3.py").read_text().split("# END L0898 REGRESSIONS\n", 1)[1]
for _chunk in range(1, 4):
    _path = Path(__file__).with_name(f"dispatch_checks_{_chunk}.py")
    _source = _historical if _chunk == 3 else _path.read_text()
    # Historical fake builder checkouts lack per-subject branches; the new
    # regressions above prove the worktree guard using real checkouts.
    _source = _source.replace("import dispatch, fold, grader_view, harness  # noqa: E402",
        "import dispatch, fold, grader_view, harness\n"
        "dispatch.builder_worktree_preflight = lambda *args: None")
    _source = _source.replace("importlib.reload(dispatch)",
        "importlib.reload(dispatch)\ndispatch.builder_worktree_preflight = lambda *args: None")
    exec(compile(_source, str(_path), "exec"), globals())
"""
if __name__ == "__main__":
    test_dispatch_1134()
    test_dispatch_0898()
    subprocess.run([sys.executable, "-c", "__file__ = " + repr(__file__) + "\n" + _legacy], check=True)
