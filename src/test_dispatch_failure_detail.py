#!/usr/bin/env python3
"""L-spec-1732: bounded, redacted diagnostics on dispatch's machine holds."""
import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch


def signed(criterion, body):
    encoded = json.dumps(body, sort_keys=True).encode()
    print(json.dumps(dict(criterion=criterion, url="cli://dispatch-failure-detail/" + criterion,
                         status=0, body_sha256=hashlib.sha256(encoded).hexdigest(),
                         body_excerpt=body)))


def helper_checks(dispatch, prod_runner):
    tail = "RuntimeError: boom"
    traceback = "Traceback:\n" + "  fixture frame\n" * 100 + tail
    assert dispatch._failure_detail(traceback) == traceback[-600:]
    assert len(dispatch._failure_detail(traceback)) <= 600
    assert dispatch._failure_detail(traceback).endswith(tail)
    database_url = "postgresql://u:p@h/db"
    boundary = "prefix " + database_url + " " + "x" * 590
    assert "u:p@" in boundary[-600:]  # Truncating first would leak this fragment.
    assert dispatch._failure_detail(boundary) == ("prefix [redacted database URL] " + "x" * 590)[-600:]
    assert "u:p@" not in dispatch._failure_detail(boundary)
    assert database_url not in dispatch._failure_detail(boundary)
    assert dispatch._failure_detail("token=abc123") == "token=[redacted]"
    fixtures = [None, "", "plain failure", database_url, "postgres://u:p@h/db",
                "HTTPS://user:pass@example.invalid/path", "password: fixture",
                "token=abc123", "secret=fixture; api_key=fixture, api-key:fixture",
                "AUTHORIZATION:fixture", "before " + database_url + " after"]
    for text in fixtures:
        assert dispatch._failure_detail(text) == prod_runner.redact(text)
    assert dispatch._failure_detail(None) == dispatch._failure_detail("") == ""
    signed("AC1", dict(bounded=True, keeps_tail=True, redacts_before_truncation=True,
                       parity_fixtures=len(fixtures), empty=True))
    print("L-spec-1732 AC1 ok")


def dispatch_checks(dispatch, fold, models, grading_env):
    observations = []
    for mode in ("unknown", "preflight", "unserved", "exhausted"):
        clock = datetime(2026, 10, 23, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo").mkdir()
            packet = root / "packet"
            packet.write_text("fixture\nPINNED_BASE_SHA: " + "a" * 40 + "\n")
            mapping = dict(backend="codex", model="fixture", source="fixture")
            response = subprocess.CompletedProcess(
                [], 1, json.dumps(dict(is_error=True, result="unclassified refusal")), "")
            with patch.multiple(dispatch, ROOT=root, EVENTS=root / "events", CONTENT=root / "content"), \
                 patch.multiple(fold, ROOT=root, EVENTS=root / "events"), \
                 patch.object(models, "resolve", return_value=mapping), \
                 patch.object(dispatch, "ensure_room", return_value=True), \
                 patch.object(dispatch, "builder_worktree_preflight"), \
                 patch.object(dispatch, "provision_worktree_env", return_value="absent"), \
                 patch.object(dispatch.spawns, "gate", return_value=None), \
                 patch.object(dispatch.atexit, "register"), \
                 patch.object(dispatch, "now", side_effect=lambda: clock.isoformat()), \
                 patch.object(dispatch, "run_codex", return_value=response) as backend, \
                 patch.object(dispatch, "run_seat", side_effect=AssertionError("unexpected seat")) as seat:
                def run(role="builder"):
                    args = argparse.Namespace(role=role, subject="L-spec-9149", packet=str(packet),
                        path=None, cwd=str(root / "repo"), charter=None, project="fixture",
                        mcp_config=None, timeout=None, max_usd=None, seat=False)
                    stderr = io.StringIO()
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                        try:
                            dispatch.main(args)
                        except SystemExit as exc:
                            return exc.code, stderr.getvalue()
                    raise AssertionError("failed backend returned success")

                if mode == "preflight":
                    failure = grading_env.InfraFailure("no-space")
                    with patch.object(grading_env, "required", return_value=["db"]), \
                         patch.object(grading_env, "preflight", side_effect=failure):
                        code, stderr = run("reviewer")
                    text, role = str(failure), "reviewer"
                    assert backend.call_count == seat.call_count == 0
                else:
                    role = "builder"
                    text = "is_error: unclassified refusal"
                    if mode == "unserved":
                        failure = dispatch.Unserved("fixture", root / "missing.claimed", 1)
                        backend.side_effect = failure
                        text = "unserved: " + str(failure)
                    elif mode == "exhausted":
                        response.stdout = json.dumps(dict(is_error=True, result="at capacity"))
                        text = "is_error: at capacity"
                        for minutes in (5, 15, 45, 120):
                            assert run()[0] == 2
                            clock += timedelta(minutes=minutes)
                    code, stderr = run()
                    assert backend.call_count == (5 if mode == "exhausted" else 1)
                    assert seat.call_count == 0

                assert code == 2
                rows = fold.read_events(use_cache=False)
                row = rows[-1]
                detail = dispatch._failure_detail(text)
                assert row["detail"] == detail
                assert 0 < len(detail) <= 600
                classification = "infra" if mode in ("preflight", "exhausted") else "unknown"
                assert f"dispatch: {classification} hold {row['spawn']} {detail}" in stderr
                expected = dict(v=1, ts=clock.isoformat(timespec="seconds"), subject="L-spec-9149",
                    project="fixture", spawn=row["spawn"], role=role, backend="codex",
                    attempt=row["spawn"], detail=detail, actor=role,
                    _src=f"{row['spawn']}.jsonl:{1 if mode == 'preflight' else 2}")
                if mode == "preflight":
                    expected.update(type="infra-failure", **{"class": "no-space"},
                        retry_at=(clock + timedelta(minutes=5)).isoformat(timespec="seconds"))
                    assert len(rows) == 1
                else:
                    exhausted = mode == "exhausted"
                    expected.update(type="escalation-blocking", owner="executor",
                        kind="infra-exhausted" if exhausted else "failure-unknown",
                        why="infra: retry ladder exhausted" if exhausted else "unknown: failure could not be classified",
                        default="hold; Executor investigates before retrying" if exhausted else "hold; Executor classifies the failure",
                        deadline=(clock + timedelta(hours=24)).isoformat(),
                        revert="Executor records a decision or unblocked")
                    assert not any(e["type"] in ("spawn-failed", "rejected-criterion") for e in rows)
                    assert sum(e["type"] == "escalation-blocking" for e in rows) == 1
                    infra = [e for e in rows if e["type"] == "infra-failure"]
                    assert len(infra) == (4 if exhausted else 0)
                    assert all(e["detail"] == detail for e in infra)
                    calls = backend.call_count
                    assert run()[0] == 2 and backend.call_count == calls
                assert row == expected, (row, expected)
                observations.append(dict(mode=mode, exit_code=code, event=row,
                                         stderr=stderr.strip(), other_fields_match=True))
    signed("AC2", observations)
    print("L-spec-1732 AC2 ok")


def main():
    # Set the scratch root before any project import; stub the import-time CLI probe.
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, clear=False):
        os.environ.update(DOIT_ROOT=tmp, DOIT_NO_POKE="1")
        os.environ.pop("DOIT_LEDGER_FILE", None)
        os.environ.pop("DOIT_SEAT", None)
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "fixture", "")):
            import dispatch
            import fold
            import grading_env
            import models
            import prod_runner
        with patch.object(subprocess, "run", side_effect=AssertionError("unexpected subprocess")), \
             patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected process")):
            helper_checks(dispatch, prod_runner)
            dispatch_checks(dispatch, fold, models, grading_env)


if __name__ == "__main__":
    main()
