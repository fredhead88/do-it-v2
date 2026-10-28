#!/usr/bin/env python3
"""Run dispatch regressions and the ordered historical fixture chunks."""
from pathlib import Path
import atexit, json, os, subprocess, sys, tempfile


if __name__ == "__main__":
    _root = tempfile.TemporaryDirectory(prefix="dispatch-parent-test-")
    atexit.register(_root.cleanup)
    os.environ["DOIT_ROOT"] = _root.name
    os.environ.pop("DOIT_LEDGER_FILE", None)
    os.environ["DOIT_NO_POKE"] = "1"


def _isolated_run(args):
    # Historical dispatch checks also create HOME/doit-scratch; isolate every
    # child before imports, including when this script runs without the launcher.
    with tempfile.TemporaryDirectory(prefix="dispatch-test-") as tmp:
        for name in ("home", "root", "xdg"):
            (Path(tmp) / name).mkdir()
        env = {**os.environ, "HOME": tmp + "/home", "DOIT_ROOT": tmp + "/root",
               "XDG_CONFIG_HOME": tmp + "/xdg"}
        env.pop("DOIT_LEDGER_FILE", None)
        subprocess.run(args, env=env, check=True)


def test_alloc_suffix():
    _isolated_run([sys.executable, __file__, "--alloc-cases"])


def _check_alloc_suffix():
    import dispatch

    def case(names, expected, prefix="L-charter-", suffix=".md"):
        with tempfile.TemporaryDirectory(dir=dispatch.ROOT) as tmp:
            d = Path(tmp)
            originals = {name: ("original: " + name).encode() for name in names}
            for name, data in originals.items():
                (d / name).write_bytes(data)
            claimed = dispatch.alloc(d, prefix, suffix)
            assert claimed == d / expected and claimed.is_file(), claimed
            assert all((d / name).read_bytes() == data for name, data in originals.items())
            print(f"alloc {', '.join(map(repr, names))} -> {claimed.name}; originals unchanged")

    case(["L-charter-0040.md", "L-charter-0041.md", "L-charter-0041-t2.md"],
         "L-charter-0042.md")
    case(["L-charter-0041-t2.md"], "L-charter-0001.md")
    for name in ("L-spec-0100-t2.md", "L-spec-0100.audited-0616.md",
                 "L-spec-0100.v2.md", "L-spec-0100x.md", "L-spec-0100-.md",
                 "L-spec-0100\u0661.md", "L-spec-0100\n.md"):
        case(["L-spec-0007.md", name], "L-spec-0008.md", "L-spec-")
    case(["L-spec-0007.md", "L-spec-0100.md"], "L-spec-0101.md", "L-spec-")
    case(["L-builder-0100-t2.jsonl"], "L-builder-0001.jsonl", "L-builder-", ".jsonl")
    with tempfile.TemporaryDirectory(dir=dispatch.ROOT) as tmp:
        d = Path(tmp)
        (d / "L-probe-0003").mkdir()
        (d / "L-probe-0003-t2").write_bytes(b"original")
        assert dispatch.alloc(d, "L-probe-", "") == d / "L-probe-0004"
        assert (d / "L-probe-0003").is_dir()
        assert (d / "L-probe-0003-t2").read_bytes() == b"original"
        print("alloc empty suffix: directory 0003 + file 0003-t2 -> L-probe-0004")
    case(["L-charter-8000.md", "L-charter-9102.md"], "L-charter-0001.md")
    dispatch.EVENTS.mkdir()
    (dispatch.EVENTS / "L-planner-0001.jsonl").write_text(json.dumps(
        {"v": 1, "type": "spec-written", "subject": "L-spec-0007"}) + "\n")
    with tempfile.TemporaryDirectory(dir=dispatch.ROOT) as tmp:
        d = Path(tmp)
        claims = [dispatch.alloc(d, "L-spec-", ".md") for _ in range(2)]
        assert [p.name for p in claims] == ["L-spec-0008.md", "L-spec-0009.md"]
        listing = sorted(os.listdir(d))
        assert listing == [p.name for p in claims] and all(p.is_file() for p in claims)
        print(f"alloc ledger-only 0007, distinct O_EXCL claims, fresh os.listdir: {listing}")
    print("alloc-suffix AC1/AC2/AC3 pass (temporary HOME and DOIT_ROOT)")

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
                mock.patch.object(dispatch, "run_codex", side_effect=RuntimeError("AssertionError: fixture backend stop")) as backend:
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
                        assert str(exc) == "AssertionError: fixture backend stop"
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
    # Load the consumed seams only AFTER the fixture sets its ledger root.
    _new = _new.replace("        import dispatch, fold, models", "        import dispatch, fold, models\n" +
                        "\n".join("        " + line for line in _historical_work_seams.splitlines()))
    _isolated_run([sys.executable, "-c", "import sys; sys.path.insert(0, " +
                   repr(str(_checks.parent)) + "); " + _new + "\ncheck_dispatch_0898()"])

# These fixtures exercise the existing WORK path. Supply the new consumed
# seams explicitly so their contract/output assertions still test that path.
# Infra, unknown and unserved-relay cases run separately with the real classifier.
_historical_work_seams = """
from unittest.mock import Mock
import nextstep, relay
nextstep.classify_failure = Mock(return_value="work")
relay.serving = Mock(return_value=True)
"""

def _seat_timing_checks(dispatch, root, repo):
    from unittest.mock import patch
    # Replace the historical thread/join timing checks with the same boundaries
    # driven synchronously. Nothing here waits for wall time or a relay.
    for name, window, cap, claimed in (("stale", 60, 60, False),
            ("window", 3, 60, False), ("default", None, 6, False),
            ("claimed", 3, 4, True)):
        clock = [0.0]
        ledger = root / f"L-research-clock-{name}.jsonl"
        spawn = "L-research-clock-" + name
        def sleep(seconds):
            clock[0] += seconds
            if claimed and clock[0] == 2:
                (dispatch.SEAT / f"{spawn}.claimed").touch()
            if claimed and clock[0] == 6:
                (dispatch.SEAT / f"{spawn}.result.json").write_text("{}")
        with patch.dict(os.environ, DOIT_SEAT_CLAIM_SEC="1"):
            try:
                result = dispatch.run_seat(spawn, ["fixture"], "packet", str(repo), cap,
                    window=window, ledger=ledger, base=dict(subject="fixture", spawn=spawn),
                    role="research", clock=lambda: clock[0], sleep=sleep)
            except dispatch.Unserved:
                assert not claimed and clock[0] == (cap if window is None else window) // 2 * 2 + 2
            else:
                assert claimed and isinstance(result, dispatch.SeatResult) and clock[0] == 7
        rows = [json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []
        assert [row["type"] for row in rows] == (["seat-queued"] if claimed else ["seat-queued", "seat-stale"]), rows
        assert rows[0]["spawn"] == spawn, rows[0]
        if not claimed:
            assert rows[1]["age_s"] == 2, rows[1]
        for path in dispatch.SEAT.glob(f"{spawn}.*"):
            path.unlink()
    return 4


def _fast_seat_poll(dispatch):
    import time
    run = dispatch.run_seat
    def fast(*args, **kwargs):
        kwargs.setdefault("sleep", lambda seconds: time.sleep(min(seconds, .01)))
        return run(*args, **kwargs)
    dispatch.run_seat = fast


_legacy = "_historical_work_seams = " + repr(_historical_work_seams) + "\n" + r"""
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).parent))
from test_dispatch import _fast_seat_poll, _seat_timing_checks
_historical = Path(__file__).with_name("dispatch_checks_3.py").read_text().split("# END L0898 REGRESSIONS\n", 1)[1]
for _chunk in range(1, 4):
    _path = Path(__file__).with_name(f"dispatch_checks_{_chunk}.py")
    _source = _historical if _chunk == 3 else _path.read_text()
    if _chunk == 1:
        start = _source.index("# ── L-spec-0269 AC1 (rewrite")
        end = _source.index("def _seat_driven(", start)
        _source = _source[:start] + "N += _seat_timing_checks(dispatch, TMP, REPO)\n\n" + _source[end:]
        _source = _source.replace("time.sleep(3.5)", "time.sleep(.05)")
    # Historical fake builder checkouts lack per-subject branches; the new
    # regressions above prove the worktree guard using real checkouts.
    _source = _source.replace("import dispatch, fold, grader_view, harness  # noqa: E402",
        "import dispatch, fold, grader_view, harness\n" + _historical_work_seams +
        "dispatch.builder_worktree_preflight = lambda *args: None\n_fast_seat_poll(dispatch)")
    _source = _source.replace("importlib.reload(dispatch)",
        "importlib.reload(dispatch)\ndispatch.builder_worktree_preflight = lambda *args: None\n_fast_seat_poll(dispatch)")
    # These historical graders exercise legacy no-bundle capability and
    # verdict handling. The real bundle gate is covered in check_evidence1109
    # above, in its own process; it now refuses these old fixture packets.
    with patch("grader_packet.gate", return_value=None):
        exec(compile(_source, str(_path), "exec"), globals())
"""
def _infra_response(ac, rows):
    import hashlib
    body = json.dumps(rows, sort_keys=True).encode()
    print(json.dumps(dict(criterion=ac, url="cli://infra-retry/" + ac, status=0,
                         body_sha256=hashlib.sha256(body).hexdigest(), body_excerpt=rows)))


def infra_dispatch_checks(mode):
    import argparse, contextlib, io
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch
    import dispatch, fold, models, relay
    clock = datetime(2026, 10, 23, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as tmp, \
         patch.dict(os.environ, {"DOIT_ROOT": tmp, "DOIT_NO_POKE": "1"}):
        root = Path(tmp)
        (root / "repo").mkdir()
        packet = root / "packet"
        packet.write_text("fixture\nPINNED_BASE_SHA: " + "a" * 40 + "\n")
        mapping = dict(backend="codex", model="fixture", source="fixture",
                       fallback=dict(backend="seat", model="fixture"))
        response = subprocess.CompletedProcess([], 1, json.dumps(dict(is_error=True, result="at capacity")), "")
        with patch.multiple(dispatch, ROOT=root, EVENTS=root / "events", CONTENT=root / "content"), \
             patch.multiple(fold, ROOT=root, EVENTS=root / "events"), \
             patch.object(models, "resolve", return_value=mapping), \
             patch.object(dispatch, "ensure_room", return_value=True), \
             patch.object(dispatch, "builder_worktree_preflight"), \
             patch.object(dispatch.spawns, "gate", return_value=None), \
             patch.object(dispatch, "now", side_effect=lambda: clock.isoformat()), \
             patch.object(dispatch, "run_codex", return_value=response) as backend, \
             patch.object(dispatch, "run_seat", return_value=response) as seat:
            def events():
                return fold.read_events(use_cache=False)
            def run(role="builder"):
                a = argparse.Namespace(role=role, subject="L-spec-9149", packet=str(packet), path=None,
                    cwd=str(root / "repo"), charter=None, project="fixture", mcp_config=None,
                    timeout=None, max_usd=None, seat=False)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    try:
                        dispatch.main(a)
                    except SystemExit as exc:
                        return exc.code
                raise AssertionError("a failed backend returned success")
            if mode == "schema":
                response.stdout = json.dumps(dict(is_error=True, result=(
                    "Invalid schema for response_format 'x': In context=('properties','ac_types'), "
                    "'uniqueItems' is not permitted.")))
                with patch.object(relay, "serving", return_value=False, create=True):
                    assert run() == 2
                rows = events()  # Fresh fold.read_events, reading dispatch.emit's ledger.
                failures = [e for e in rows if e["type"] == "infra-failure"]
                assert len(failures) == 1, rows
                row = failures[0]
                assert row["class"] == "schema-rejected" and row["backend"] == "codex"
                assert row["attempt"] == row["spawn"] and row["role"] == "builder"
                assert row["retry_at"] == (clock + timedelta(minutes=5)).isoformat(timespec="seconds")
                assert backend.call_count == 1 and seat.call_count == 0
                assert not any(e["type"] in ("escalation-blocking", "spawn-failed", "backend-fallback")
                               for e in rows), rows
                _infra_response("AC6", rows)
                print("codex-strict AC6 status=0")
            elif mode == "ladder":
                with patch.object(relay, "serving", return_value=False, create=True):
                    for index, minutes in enumerate((5, 15, 45, 120)):
                        before = backend.call_count
                        assert run() == 2
                        infra = [e for e in events() if e["type"] == "infra-failure"]
                        assert len(infra) == index + 1 and backend.call_count == before + 1
                        row = infra[-1]
                        assert row["backend"] == "codex" and row["class"] == "capacity"
                        assert row["attempt"] == row["spawn"] and row["role"] == "builder"
                        allowed = clock + timedelta(minutes=minutes)
                        assert row["retry_at"] == allowed.isoformat(timespec="seconds")
                        clock = allowed - timedelta(seconds=1)
                        assert run() == 2 and backend.call_count == before + 1
                        assert dispatch.infra_gate(events(), "L-spec-9149", "builder", clock) == "wait"
                        clock = allowed
                    assert run() == 2 and backend.call_count == 5
                    assert run() == 2 and backend.call_count == 5
                rows = events()
                assert not any(e["type"] in ("spawn-failed", "rejected-criterion", "backend-fallback") for e in rows)
                holds = [e for e in rows if e["type"] == "escalation-blocking"]
                assert len(holds) == 1 and holds[0]["kind"] == "infra-exhausted" and holds[0]["owner"] == "executor"
                assert dispatch.infra_gate(rows, "L-spec-9149", "builder", clock) == "hold"
                reset = rows + [dict(type="spawn-done", role="builder", subject="L-spec-9149", ts=clock.isoformat())]
                assert dispatch.infra_gate(reset, "L-spec-9149", "builder", clock) == "run"
                _infra_response("AC1", rows)
            elif mode == "fallback":
                for serving in (False, RuntimeError("relay unavailable"), None, True):
                    for path in (root / "events").glob("*.jsonl"):
                        path.unlink()
                    backend.reset_mock(); seat.reset_mock()
                    from contextlib import nullcontext
                    cm = (nullcontext() if serving is None else
                          patch.object(relay, "serving", side_effect=serving, create=True)
                          if isinstance(serving, Exception) else patch.object(relay, "serving", return_value=serving, create=True))
                    with cm:
                        assert run() == 2
                    rows = events()
                    assert seat.call_count == int(serving is True)
                    assert sum(e["type"] == "backend-fallback" for e in rows) == int(serving is True)
                    if serving is not True:
                        assert not (root / "seat").exists()
                        assert next(e for e in rows if e["type"] == "infra-failure")["class"] == "capacity"
                    _infra_response("AC2", rows)
                    if serving is True:
                        clock += timedelta(minutes=5)
                        with patch.object(relay, "serving", return_value=True, create=True):
                            assert run() == 2
                        assert backend.call_count == 1 and seat.call_count == 2
                        assert sum(e["type"] == "backend-fallback" for e in events()) == 1
                        assert [e for e in events() if e["type"] == "infra-failure"][-1]["backend"] == "seat"
                        _infra_response("AC2", events())
            elif mode == "preflight":
                import grading_env
                with patch.object(grading_env, "required", return_value=["db"]), \
                     patch.object(grading_env, "preflight", side_effect=grading_env.InfraFailure("no-space")):
                    assert run("reviewer") == 2
                rows = events()
                assert backend.call_count == 0 and seat.call_count == 0
                assert [e["type"] for e in rows] == ["infra-failure"]
                _infra_response("AC6", rows)
            elif mode == "unknown":
                mapping.pop("fallback")
                response.stdout = json.dumps(dict(is_error=True, result="unclassified refusal"))
                assert run() == 2
                rows = events()
                assert [e for e in rows if e["type"] == "escalation-blocking"][-1]["owner"] == "executor"
                assert not any(e["type"] in ("infra-failure", "spawn-failed", "rejected-criterion") for e in rows)
                before = backend.call_count
                assert run() == 2 and backend.call_count == before
                for path in (root / "events").glob("*.jsonl"):
                    path.unlink()
                response.stdout = json.dumps(dict(is_error=True, result="AssertionError: fixture"))
                assert run() == 1
                failed = [e for e in events() if e["type"] == "spawn-failed"]
                assert failed[-1]["why"] == "is_error: AssertionError: fixture"
                before = backend.call_count
                assert run() in (1, 2) and backend.call_count == before
                _infra_response("AC3", rows + failed)
                for path in (root / "events").glob("*.jsonl"):
                    path.unlink()
                backend.side_effect = dispatch.Unserved("fixture", root / "missing.claimed", 1)
                assert run() == 2
                rows = events()
                assert not any(e["type"] in ("spawn-failed", "infra-failure") for e in rows)
                assert rows[-1]["type"] == "escalation-blocking" and rows[-1]["kind"] == "failure-unknown"
                _infra_response("AC3", rows)


def infra_provision_checks():
    import time
    from unittest.mock import patch
    import build_runner, grading_env, fold, sweep_provision
    responses = []
    with tempfile.TemporaryDirectory() as tmp, \
         patch.dict(os.environ, {"DOIT_ROOT": tmp, "DOIT_NO_POKE": "1"}), \
         patch.multiple(grading_env, ROOT=Path(tmp), CONTENT=Path(tmp) / "content",
                        DEPLOY_LOCK=Path(tmp) / "deploy.lock"), \
         patch.multiple(fold, ROOT=Path(tmp), EVENTS=Path(tmp) / "events"):
        root = Path(tmp)
        (root / "tree").mkdir()
        (root / "content").mkdir()
        # A release that never recovers consumes virtual time, never 15 real minutes.
        clock, delays = [0.0], []
        def sleep(seconds):
            delays.append(seconds)
            clock[0] += seconds
        with patch.object(grading_env, "_look_toml", return_value={"prod": [dict(
                project="fixture", ssh_target="fixture", version_path="/version")]}), \
             patch.object(grading_env, "_run", return_value=subprocess.CompletedProcess([], 7, "", "")), \
             patch.object(grading_env, "release_in_progress", return_value=True):
            ok, why = grading_env._prove_deploy("fixture", root, clock=lambda: clock[0], sleep=sleep)
        assert not ok and sum(delays) == 900 and "waited_s=900" in why
        responses.append(dict(deploy_retry_seconds=sum(delays), real_sleep=False))
        (root / "content/L-spec-9149.md").write_text("fixture")
        record = root / "check.json"
        record.write_text(json.dumps(dict(subject="L-spec-9149", project="fixture")))
        with patch.multiple(fold, ROOT=root, EVENTS=root / "events"), \
             patch.object(grading_env, "required", return_value=["db"]), \
             patch.object(grading_env, "_spec_verify_text", return_value=("", "")), \
             patch.object(grading_env, "_project_row", return_value={}), \
             patch.object(grading_env, "_prove_db", return_value=(False, "No space left on device", None)), \
             patch.object(grading_env, "_write_state"):
            try:
                grading_env.preflight("grader", "L-spec-9149", root, "fixture", repo=root)
                raise AssertionError("infra provisioning returned a capability failure")
            except grading_env.InfraFailure as exc:
                assert exc.category == "no-space"
                responses.append(dict(preflight=str(exc)))
        # The actual private PG subprocess diagnostic is classified before sanitization.
        failure = subprocess.CompletedProcess([], 1, b"", b"")
        with patch.object(build_runner, "_process", return_value=(1, b"No space left on device")):
            try:
                build_runner._provision(root / "tree", root, {"db_setup": {"argv": ["fixture"]}}, {}, time.monotonic() + 30)
                raise AssertionError("provision accepted an unavailable PG")
            except build_runner.InfraFailure as exc:
                assert str(exc) == "infra:no-space"
        with patch.object(sweep_provision.build_runner, "_required", side_effect=build_runner.InfraFailure("no-space")), \
             patch.object(sweep_provision, "_stop_pg"), \
             patch.object(sweep_provision.tempfile, "mkdtemp", return_value=str(root / "pg")), \
             patch.object(sweep_provision, "_pg_path", side_effect=lambda p: p):
            code, output = sweep_provision.run_check(root / "tree", "fixture", "# pg_scratch\ntrue", record)
            assert code == 2 and output == b"infra:no-space\n"
        rows = fold.read_events(use_cache=False)
        assert sorted((row["type"], row["actor"], row.get("kind")) for row in rows) == [
            ("infra-failure", "owed-sweeper", None),
            ("resource-created", "operator", "scratch-db"),
            ("resource-created", "owed-sweeper", "scratch-db")]
        assert all(row["ref"] == str(root / "pg") for row in rows if row["type"] == "resource-created")
        responses.extend(rows)
        with patch.object(build_runner, "_process", return_value=(7, b"FAILED fixture assertion")):
            assert sweep_provision.run_check(root / "tree", "fixture", "exit 7", record) == (7, b"FAILED fixture assertion")
        # Exercise run's tip AND base provisioning holds and its own ledger seam.
        done = dict(type="build-done", project="fixture", ready_sha="a" * 40, base_sha="b" * 40)
        for fail_base in (False, True):
            emitted = []
            tip = ([], [], [], [], {})
            side_effect = [tip, build_runner.InfraFailure("capacity")] if fail_base else build_runner.InfraFailure("no-space")
            with patch.object(fold, "fold", return_value=({}, {}, {}, {"L-spec-9149": [done]})), \
                 patch.object(grading_env, "_project_checkout", return_value=root), \
                 patch.object(grading_env, "_project_row", return_value={}), \
                 patch.object(build_runner, "_resolve", side_effect=lambda repo, sha, *args: sha), \
                 patch.object(build_runner, "_commands", return_value=([("AC1", "true")], [])), \
                 patch.object(build_runner, "_run_tree", side_effect=side_effect), \
                 patch.object(build_runner, "_event", side_effect=lambda spec, project, kind, **kv: emitted.append(dict(type=kind, **kv))):
                try:
                    build_runner.run("L-spec-9149")
                    raise AssertionError("infra failure published an evidence bundle")
                except build_runner.Undetermined as exc:
                    assert str(exc).startswith("infra:")
            assert [e["type"] for e in emitted] == ["infra-failure", "evidence-bundle-failed"]
            assert emitted[-1]["reason"].startswith("infra:")
            responses.extend(emitted)
    _infra_response("AC6", responses)
    infra_dispatch_checks("preflight")


def infra_emits_checks():
    from contextlib import redirect_stderr
    from io import StringIO
    from unittest.mock import patch
    import dispatch, fold, fold_emits, tomllib
    roles = {"builder", "grader", "reviewer", "spec-writer", "research", "probe", "owed-sweeper", "executor", "build-runner"}
    assert fold_emits.EMITS["infra-failure"] == roles
    fields = dict(attempt="fixture", backend="codex", **{"class": "no-space"}, retry_at="2026-10-23T00:05:00+00:00", project="fixture")
    assert all(fold.admits(dict(type="infra-failure", **fields), role) is None for role in roles)
    with tempfile.TemporaryDirectory() as tmp, \
         patch.dict(os.environ, {"DOIT_ROOT": tmp}), \
         patch.multiple(dispatch, ROOT=Path(tmp), EVENTS=Path(tmp) / "events", CONTENT=Path(tmp) / "content"), \
         patch.multiple(fold, ROOT=Path(tmp), EVENTS=Path(tmp) / "events"), \
         redirect_stderr(StringIO()):
        path = Path(tmp) / "L-thinker-local.jsonl"
        dispatch.emit(path, dict(subject="fixture"), "infra-failure", **fields)
        row = json.loads(path.read_text())
        assert fold.admits(row, "thinker") and "actor" not in row
    retired = tomllib.loads((Path(__file__).parents[1] / "retired.d/infra-retry.toml").read_text())["retired"]
    assert len(retired) == 1 and retired[0]["name"] == "unconditional-seat-fallback"
    pattern = retired[0]["pattern"]
    assert retired[0]["kind"] == "code" and retired[0]["paths"] == ["src/dispatch.py"]
    top = Path(__file__).parents[1]
    paths = [top / "src/dispatch.py", top / "README.md"]
    paths += [p for directory in ("docs", "agents") for p in (top / directory).rglob("*") if p.is_file()]
    assert all(pattern not in p.read_text() for p in paths)
    _infra_response("AC7", dict(roles=sorted(roles), unauthorized="recorded-and-ignored", retired=retired))


def codex_strict_checks():
    import hashlib
    import jsonschema
    import dispatch

    schemas = Path(__file__).resolve().parents[1] / "agents"

    def walk(schema):
        if isinstance(schema, list):
            for value in schema:
                yield from walk(value)
        elif isinstance(schema, dict):
            assert not set(schema).intersection(dispatch._STRICT_DROP), schema
            for key, value in schema.items():
                if key in ("properties", "$defs", "definitions"):
                    for child in value.values():
                        yield from walk(child)
                else:
                    yield from walk(value)
            yield schema

    checked = []
    for path in sorted(schemas.glob("*.schema.json")):
        before = path.read_bytes()
        original = json.loads(before)
        unchanged = json.dumps(original, sort_keys=True)
        strict = dispatch.strict_schema(original)
        list(walk(strict))
        assert json.dumps(original, sort_keys=True) == unchanged
        assert path.read_bytes() == before
        assert set(strict.get("properties", {})) == set(original.get("properties", {}))
        checked.append(path.name)
        print("schema ok", path.name)
    assert checked
    # Keyword-like names in every schema name map must survive recursively.
    for name_map in ("properties", "$defs", "definitions"):
        original = {name_map: {name: {"type": "string", "default": "fixture"}
                               for name in dispatch._STRICT_DROP}}
        strict = dispatch.strict_schema(original)
        assert set(strict[name_map]) == set(dispatch._STRICT_DROP)
        list(walk(strict))
    print(f"schema count {len(checked)}")
    _infra_response("AC1", dict(schemas=checked, count=len(checked), property_names_preserved=True))
    print("codex-strict AC1 status=0")

    # OpenAI Structured Outputs subset, retrieved 2026-10-25:
    # https://developers.openai.com/api/docs/guides/structured-outputs#supported-schemas
    # Composition exclusions plus unsupported structural constraints and legacy
    # exclusions; supported scalar/array bounds must remain in the strict copy.
    refused = {"if", "then", "else", "not", "allOf", "dependentRequired", "dependentSchemas",
               "dependencies", "patternProperties", "default", "$comment", "examples",
               "unevaluatedProperties", "propertyNames", "contains", "minContains", "maxContains",
               "uniqueItems", "minProperties", "maxProperties", "unevaluatedItems"}
    supported = {"pattern", "format", "minLength", "maxLength", "minimum", "maximum",
                 "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minItems", "maxItems",
                 "const", "enum"}
    assert set(dispatch._STRICT_DROP) == refused
    assert len(dispatch._STRICT_DROP) == len(refused)
    assert refused.isdisjoint(supported)
    # Exercise removal even for keywords absent from today's role contracts.
    probe = {"items": {"type": "string", **{key: True for key in refused},
                       **{key: "fixture" for key in supported}}}
    strict = dispatch.strict_schema(probe)
    assert set(strict["items"]) == supported | {"type"}
    _infra_response("AC2", dict(refused=sorted(refused), supported=sorted(supported)))
    print("codex-strict AC2 status=0")

    path = schemas / "spec-writer.schema.json"
    before = path.read_bytes()
    original = json.loads(before)
    unchanged = json.dumps(original, sort_keys=True)
    strict = dispatch.strict_schema(original)
    card = dict(status="written", spec_id="L-spec-fixture", ac_count=2, ac_types=["ui", "backend"],
                footprint=[], requirement_ids=[], owed=0, unknowns=0, split=[], weak_dimensions=[],
                escalations=[], declarations=[], killed_by_check=None)
    clean = dispatch.unnull(card, original)
    jsonschema.validate(clean, original)
    duplicate = dict(clean, ac_types=["ui", "ui"])
    try:
        jsonschema.validate(duplicate, original)
    except jsonschema.ValidationError as exc:
        assert exc.validator == "uniqueItems" and list(exc.path) == ["ac_types"]
    else:
        raise AssertionError("original contract accepted duplicate ac_types")
    assert "uniqueItems" not in strict["properties"]["ac_types"]
    assert json.dumps(original, sort_keys=True) == unchanged and path.read_bytes() == before
    _infra_response("AC3", dict(duplicate_rejected=True, distinct_valid=True,
                               original_sha256=hashlib.sha256(before).hexdigest()))
    print("codex-strict AC3 status=0")


def test_codex_strict():
    _isolated_run([sys.executable, __file__, "--codex-strict-cases"])


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1].startswith("--infra-"):
        mode = sys.argv[1][len("--infra-"):]
        # Direct verification commands need the same pre-import isolation as
        # the suite launcher; patching fold alone misses other cached roots.
        _isolated_run([sys.executable, __file__, "--infra-cases", mode])
    elif sys.argv[1:2] == ["--infra-cases"]:
        mode = sys.argv[2]
        if mode == "emits":
            infra_emits_checks()
        elif mode == "provision":
            infra_provision_checks()
        else:
            infra_dispatch_checks(mode)
    elif sys.argv[1:] == ["--codex-strict"]:
        test_codex_strict()
    elif sys.argv[1:] == ["--codex-strict-cases"]:
        codex_strict_checks()
    elif sys.argv[1:] == ["--alloc-cases"]:
        _check_alloc_suffix()
    elif sys.argv[1:] == ["--issued-by-cases"]:
        test_dispatch_1134()
    else:
        test_codex_strict()
        test_alloc_suffix()
        _isolated_run([sys.executable, __file__, "--issued-by-cases"])
        test_dispatch_0898()
        _isolated_run([sys.executable, "-c", "__file__ = " + repr(__file__) +
                       "\nimport sys; sys.path.insert(0, " + repr(str(Path(__file__).parent)) + ");\n" + _legacy])


def test_emit_admission():
    from contextlib import redirect_stderr
    from io import StringIO
    import dispatch, fold
    with tempfile.TemporaryDirectory() as tmp:
        for name, kind, fields in (
                ('L-thinker-local.jsonl', 'owed-routed', dict(criterion='AC1', route='plain')),
                ('L-executor-local.jsonl', 'owed-quarantined', dict(criterion='AC1', reason='fixture', spec_sha256='fixture'))):
            path = Path(tmp) / name
            reason = dispatch.emit(path, {'subject': 'fixture'}, kind, strict=True, **fields)
            assert reason == fold.admits(dict(type=kind, **fields), fold.actor_of(name))
            assert reason and not path.exists()
            warning = StringIO()
            with redirect_stderr(warning):
                assert dispatch.emit(path, {'subject': 'fixture'}, kind, **fields) is None
            assert len(path.read_text().splitlines()) == 1
            assert f'fold: recorded-and-ignored {kind} by {fold.actor_of(name)}' in warning.getvalue()
        for strict in (False, True):
            path = Path(tmp) / 'L-executor-allowed.jsonl'
            assert dispatch.emit(path, {'subject': 'fixture'}, 'owed-met', strict=strict,
                                 criterion='AC1', evidence='fixture') is None
        assert len(path.read_text().splitlines()) == 2
    print('AC8 ok strict admission and compatible default')


if __name__ == '__main__' and not sys.argv[1:]:
    test_emit_admission()


def check_capability_readmission(*, fixture_root, now, epoch, iso, clear, write, specs_now, runner_type):
    """Capability dispatch checks sharing the sweeper driver's ledger fixtures."""
    import hashlib
    from datetime import timedelta
    from unittest.mock import patch
    import dispatch, fold, owed, owed_routes, prod_runner, sweep_owed
    TMP, NOW, EPOCH, FakeRunner = fixture_root, now, epoch, runner_type
    REPO_ROOT = Path(__file__).resolve().parent.parent
    EV = TMP / 'events'
    clear()
    epochs = {'deploy': iso(hours=-1), 'droplet-wrapper': 'new'}
    root = TMP / 'checkout'
    wrapper = root / 'scripts/sweep/droplet-read'
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(b'fixture wrapper')
    events = [dict(type='deploy-landed', target='p', ts=iso(hours=-2)),
              dict(type='deploy-landed', target='q', project='p', ts=iso(hours=-1))]
    events += [dict(type=t, target='p', ts=iso()) for t in ('shipped', 'deploy-started', 'deploy-failed')]
    with patch.object(sweep_owed, 'doit_bin', return_value=root / 'doit'):
        assert sweep_owed.route_epochs(events, 'p') == {
            'deploy': iso(hours=-2), 'droplet-wrapper': hashlib.sha256(wrapper.read_bytes()).hexdigest()}
        assert sweep_owed.route_epochs(events, 'missing')['deploy'] is None
        wrapper.unlink()
        assert 'droplet-wrapper' not in sweep_owed.route_epochs(events, 'p')
    print('AC1 ok deploy target and unreadable wrapper')

    def seed(cap, n=1, runner='judgment'):
        sid = f'L-spec-1369-{cap}-{n}'
        write(f'L-spec-writer-c{n}{cap.replace(chr(45), chr(95))}.jsonl',
              dict(type='spec-written', subject=sid, project='proj-a', ts=EPOCH),
              dict(type='owed-ac', subject=sid, criterion='AC1', line=runner, ts=EPOCH, wake_at=iso(hours=-30)))
        dispatch.spec_path(sid).write_text('## Acceptance criteria\nAC1 [backend]: fixture\n')
        write(f'L-executor-c{n}{cap.replace(chr(45), chr(95))}.jsonl', dict(type='shipped', subject=sid, sha='fixture', ts=EPOCH))
        write(f'L-owed-sweeper-c{n}{cap.replace(chr(45), chr(95))}.jsonl', dict(type='owed-unobservable', subject=sid,
              criterion='AC1', capability=cap, why='fixture', ts=iso(hours=-20)))
        return sid

    judgment = dict(runner='judgment', needs=[], commands=[])
    empty = dict(lines=[], held=set(), bundle_rows={}, to_judgment=set())
    caps = [*owed_routes.ROUTES, 'other']
    with patch.object(prod_runner, 'record_for', return_value=judgment):
        for cap in caps:
            seed(cap)
        selected = sweep_owed.readmitted(specs_now()[1], NOW, lambda p: epochs)
        assert {r['capability'] for r in selected} == set(caps) - {'operator-action', 'ro-dsn-grant'}
        assert {r['capability'] for r in sweep_owed.readmitted(specs_now()[1], NOW, lambda p: {})} == {'repo', 'browser', 'elapsed', 'other'}
        # Statuses outside blocked and operator-only commands cannot enter.
        with patch.object(owed, 'checks', return_value=[{**selected[0], 'status': s} for s in
                     ('due', 'expired', 'failed', 'quarantined', 'met')]):
            assert not sweep_owed.readmitted({'s': {'evs': []}}, NOW, lambda p: epochs)
        with patch.object(prod_runner, 'record_for', return_value={**judgment, 'needs': ['operator']}):
            assert not sweep_owed.readmitted(specs_now()[1], NOW, lambda p: epochs)
        assert sweep_owed.browser_input(specs_now()[1], [], NOW)
    print('AC2 ok nine capabilities and status/operator guards')

    schema = json.loads((REPO_ROOT / 'agents/owed-sweeper.schema.json').read_text())
    assert set(schema['properties']['results']['items']['properties']['capability']['enum']) == set(owed_routes.ROUTES)
    assert owed_routes.route_for('other') == 'repo'
    print('AC7 ok schema vocabulary and legacy mapping')

    clear()
    with patch.object(prod_runner, 'record_for', return_value=judgment), \
            patch.object(prod_runner, 'run', return_value=empty):
        sid = seed('other')
        # A successful production dispatch emits through the real strict writer.
        def dispatched(project, batch, runner, **kw):
            assert kw['kind'] == 'repo'
            return ['sweep-owed: batched 1 check(s) in project proj-a as L-sweep-1369 (fixture)']
        with patch.object(sweep_owed, 'dispatch_batch', side_effect=dispatched):
            sweep_owed.run(now=NOW, runner=FakeRunner(), dry_run=True)
            assert not [e for e in fold.read_events() if e['type'] == 'owed-routed']
            with patch.object(sweep_owed, 'dispatch_batch', return_value=['sweep-owed: dispatch failed for fixture']):
                sweep_owed.run(now=NOW, runner=FakeRunner())
            assert not [e for e in fold.read_events() if e['type'] == 'owed-routed']
            sweep_owed.run(now=NOW, runner=FakeRunner())
        reread = fold.read_events()
        attempts = [e for e in reread if e['type'] == 'owed-routed']
        assert len(attempts) == 1 and attempts[0]['route'] == attempts[0]['capability'] == 'repo'
        assert attempts[0]['epoch'] == '' and attempts[0]['batch'] == 'L-sweep-1369'
        assert fold.admits(attempts[0], 'sweep') is None
        row = owed.checks(fold.fold(reread)[0][sid]['evs'], NOW)[0]
        assert row['route_attempts'] == {'repo': 1} and row['last_route_epoch'] == ''
        assert not sweep_owed.readmitted(fold.fold(reread)[0], NOW, lambda p: epochs)
        print('AC9 ok fresh ledger read and fold preserve strict sweep attempt')
        with patch.object(dispatch, 'now', return_value=iso(hours=13)), \
                patch.object(sweep_owed, 'dispatch_batch', side_effect=dispatched):
            sweep_owed.run(now=NOW + timedelta(hours=13), runner=FakeRunner())
        evs = specs_now()[1][sid]['evs']
        row = owed.checks(evs, NOW + timedelta(hours=13))[0]
        assert row['status'] == 'expired' and row['route_attempts'] == {'repo': 2}
        assert sweep_owed.sweep_expired({sid: {'evs': evs}}, NOW + timedelta(hours=13)) == []
        assert not sweep_owed.readmitted({sid: {'evs': evs}}, NOW + timedelta(hours=26), lambda p: epochs)
        met = dict(type='owed-met', subject=sid, criterion='AC1', evidence='fixture', ts=iso(hours=14))
        assert owed.checks([*evs, met], NOW + timedelta(hours=14))[0]['status'] == 'met'
        assert not sweep_owed.sweep_expired({sid: {'evs': [*evs, met]}}, NOW + timedelta(hours=14))
        dispatch.emit(EV / 'L-owed-sweeper-result.jsonl', {'subject': sid}, 'owed-unobservable',
                      criterion='AC1', capability='other', why='fixture')
        # Time-based release is independent of the final result's timestamp.
        assert len(sweep_owed.sweep_expired(specs_now()[1], NOW + timedelta(hours=26))) == 1
        assert not sweep_owed.sweep_expired(specs_now()[1], NOW + timedelta(hours=26))
    print('AC3 ok batch attempts, dry-run, failure, legacy counts')
    print('AC6 ok exhaustion, in-flight skip, met and one escalation')

    clear()
    with patch.object(prod_runner, 'record_for', return_value=judgment):
        for n in range(40):
            seed('elapsed', n)
        for n in range(3):
            sid = seed('elapsed', n + 40)
            (EV / f'L-owed-sweeper-c{n+40}elapsed.jsonl').unlink()
        assert len(sweep_owed.readmitted(specs_now()[1], NOW, lambda p: epochs)) == sweep_owed.READMIT_CAP
        seen = []
        def batch(project, rows, runner, **kw):
            seen.extend(rows)
            return ['sweep-owed: batched 8 check(s) in project proj-a as L-sweep-1370 (fixture)']
        with patch.object(prod_runner, 'run', return_value=empty), patch.object(sweep_owed, 'dispatch_batch', side_effect=batch):
            sweep_owed.run(now=NOW, runner=FakeRunner())
        assert [r['status'] for r in seen[:3]] == ['due'] * 3
        assert len([r for r in fold.read_events() if r['type'] == 'owed-routed']) == 5
        # Fairness may choose a different project only among due projects.
        due = dict(seen[0], project='due-project')
        blocked = dict(seen[-1], project='unseen', due_at=NOW - timedelta(days=50))
        assert sweep_owed._batch_for([blocked, due], [dict(type='spawn-started', role='owed-sweeper',
               project='due-project', ts=iso())])[0] == 'due-project'
    print('AC4 ok capped readmission and due rows first')

    clear()
    for n in range(40):
        seed('repo', n, runner='build')
    def record(row, events=None):
        return dict(judgment, runner='build')
    with patch.object(prod_runner, 'record_for', side_effect=record):
        original = specs_now()[1]
        first = sweep_owed.readmitted(original, NOW, lambda p: epochs)
        held = {(r['spec'], r['criterion']) for r in first[:8]}
        refill = sweep_owed.readmitted(original, NOW, lambda p: epochs, exclude=held)
        assert len(refill) == 16 and not held.intersection((r['spec'], r['criterion']) for r in refill)
        assert len(sweep_owed.browser_input(original, [], NOW)) == 16
        for hour in (0, 13, 26, 39, 52, 65):
            current = NOW + timedelta(hours=hour)
            with patch.object(dispatch, 'now', return_value=current.isoformat()), \
                    patch.object(prod_runner, 'run', return_value=empty):
                sweep_owed.run(now=current, runner=FakeRunner())
        assert not sweep_owed.readmitted(specs_now()[1], NOW + timedelta(hours=78), lambda p: epochs)
        attempts = [e for e in fold.read_events() if e['type'] == 'owed-routed']
        assert len(attempts) == 80 and all('batch' not in e for e in attempts)
    print('AC5 ok runner input, cap refill and finite model-free attempts')


def test_infra_retry():
    for mode in ("schema", "ladder", "fallback", "unknown", "provision", "emits"):
        _isolated_run([sys.executable, __file__, "--infra-cases", mode])


if __name__ == "__main__" and not sys.argv[1:]:
    test_infra_retry()


import unittest
from unittest.mock import patch


class AuditGateTests(unittest.TestCase):
    def setUp(self):
        import dispatch, fold
        self.dispatch, self.fold = dispatch, fold
        self.tmp = tempfile.TemporaryDirectory(prefix='audit-gate-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'events').mkdir()
        (self.root / 'content').mkdir()
        for module, attrs in ((dispatch, dict(ROOT=self.root, EVENTS=self.root / 'events', CONTENT=self.root / 'content')),
                              (fold, dict(ROOT=self.root, EVENTS=self.root / 'events'))):
            swap = patch.multiple(module, **attrs)
            swap.start()
            self.addCleanup(swap.stop)

    def emit(self, actor, kind, subject='L-spec-9673', **kw):
        path = self.root / 'events' / ('L-' + actor + '-9673.jsonl')
        stamp = kw.pop('ts', '2026-10-28T12:00:00+00:00')
        with patch.object(self.dispatch, 'now', return_value=stamp):
            self.assertIsNone(self.dispatch.emit(path, {}, kind, subject=subject, **kw))

    def read(self, subject='L-spec-9673'):
        return [e for e in self.fold.read_events(use_cache=False) if e.get('subject') == subject]

    def signed(self, ac, body):
        import hashlib
        payload = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url='cli://audit-gate/' + ac, status=0,
            body_sha256=hashlib.sha256(payload.encode()).hexdigest(), body_excerpt=payload)))

    def test_ac1_audit_origin_and_outcome_matrix(self):
        cases = {
            'a': [('spec-writer', 'spec-written', {})],
            'b': [('spec-writer', 'spec-written', {}), ('spec-auditor', 'spawn-done', {'bad_cut': False})],
            'c': [('spec-writer', 'spec-written', {}), ('operator', 'spec-carried', {'source': 'fixture', 'tier': 'fixture', 'audited_at': '2026-10-27'})],
            'd-thinker': [('thinker', 'spec-written', {})],
            'd-operator': [('operator', 'spec-written', {})],
            'e': [('spec-writer', 'spec-written', {}), ('builder', 'build-started', {})],
            'f': [('spec-writer', 'spec-written', {}), ('spec-auditor', 'spawn-done', {'bad_cut': False}), ('spec-writer', 'spec-written', {})],
            'g': [('spec-writer', 'spec-written', {}), ('spec-auditor', 'spawn-failed', {'reason': 'fixture'})],
            'h': [('spec-writer', 'spec-written', {}), ('spec-auditor', 'spawn-done', {'subject': 'L-spec-9999', 'bad_cut': False})],
            'i': [('spec-writer', 'spec-written', {})],
            'j': [],
            'k': [('spec-writer', 'spec-written', {}), ('spec-auditor', 'spawn-done', {'bad_cut': True})],
            'l': [('spec-writer', 'spec-written', {}), ('spec-auditor', 'spawn-done', {'bad_cut': True}), ('spec-writer', 'spec-written', {})],
            'm': [('spec-writer', 'spec-written', {}), ('spec-auditor', 'spawn-done', {'bad_cut': True}), ('operator', 'decision', {})],
            'n': [('spec-writer', 'spec-written', {}), ('spec-auditor', 'spawn-done', {})],
        }
        from datetime import datetime, timedelta, timezone
        start = datetime(2026, 10, 27, tzinfo=timezone.utc)
        for label, rows in cases.items():
            for path in (self.root / 'events').glob('*.jsonl'):
                path.unlink()
            for n, (actor, kind, extra) in enumerate(rows):
                self.emit(actor, kind, ts=(start + timedelta(minutes=n)).isoformat(), **extra)
            events = self.read()
            if label == 'i':
                events[0].pop('actor')
            result = self.dispatch.unaudited(events)
            self.assertEqual(result is not None, label in {'a', 'g', 'h', 'i', 'k', 'n'}, label)
            if result:
                self.assertTrue(result.startswith('unaudited:'))
            if label == 'i':
                self.assertTrue(result.startswith('unaudited: undetermined'))
            if label in {'k', 'n'}:
                self.assertIn('bad_cut', result)
            self.signed('AC1-' + label, dict(events=events, result=result))

    def test_ac5_wrapper_refuses_before_backend(self):
        import argparse, contextlib, io, models
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'l-spec-9673'], cwd=repo, check=True)
        subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '--allow-empty', '-qm', 'fixture'], cwd=repo, check=True)
        packet = self.root / 'packet.md'
        packet.write_text('fixture\nPINNED_BASE_SHA: X0000000\n')
        self.emit('spec-writer', 'spec-written')
        with patch.object(models, 'resolve', return_value=dict(backend='codex', model='fixture', source='fixture')), \
             patch.object(self.dispatch, 'ensure_room', return_value=True), \
             patch.dict(os.environ, {'DOIT_NO_POKE': '1'}), \
             patch.object(self.dispatch, 'run_codex', side_effect=RuntimeError('AssertionError: fixture backend stop')) as backend:
            os.environ.pop('DOIT_SEAT', None)
            def start(role='builder'):
                args = argparse.Namespace(role=role, subject='L-spec-9673', packet=str(packet), path=None,
                    cwd=str(repo), charter=None, project='repo', mcp_config=None, timeout=None, max_usd=None, seat=False)
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    try:
                        self.dispatch.main(args)
                    except SystemExit as exc:
                        return exc.code
                    except RuntimeError as exc:
                        self.assertIn('fixture backend stop', str(exc))
                        return 'backend'
            self.assertNotEqual(start(), 0)
            backend.assert_not_called()
            events = self.read()
            failed = [e for e in events if e['type'] == 'spawn-failed']
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]['reason'], 'unaudited')
            self.assertIn('L-spec-9673', failed[0]['why'])
            self.assertFalse(any(e['type'] == 'build-started' for e in events))
            self.signed('AC5-refused', dict(backend_calls=0, events=events))
            # Existing deterministic refusals keep precedence on the same unaudited spec.
            packet.write_text('fixture without pin\n')
            self.assertNotEqual(start(), 0)
            self.assertTrue(any(e.get('reason') == 'no-pinned-base' for e in self.read() if e['type'] == 'spawn-failed'))
            packet.write_text('fixture\nPINNED_BASE_SHA: X0000000\n')
            for module, name, value, expected in (
                    (self.fold, 'subject_project', 'other-project', 'wrong-project'),
                    (self.dispatch, 'open_spec_writer_spawn', True, 'rework-open'),
                    (self.fold, 'spec_shape_pending', True, 'spec-shape')):
                with patch.object(module, name, return_value=value):
                    before = {e.get('spawn') for e in self.read() if e['type'] == 'spawn-failed'}
                    self.assertNotEqual(start(), 0)
                    new = [e for e in self.read() if e['type'] == 'spawn-failed' and e.get('spawn') not in before]
                    self.assertEqual(new[0]['reason'], expected)
            self.emit('spec-writer', 'spec-written', charter='L-charter-9673', footprint=['fixture.py'])
            with patch.object(self.dispatch, 'spec_wave', return_value=2), \
                 patch.object(self.dispatch, 'wave_blocker', return_value='L-spec-9672'):
                self.assertNotEqual(start(), 0)
                self.assertTrue(any(e.get('reason') == 'wave-order' for e in self.read()))
            backend.assert_not_called()
            import grading_env
            with patch.object(self.dispatch, 'unaudited', wraps=self.dispatch.unaudited) as predicate, \
                 patch.object(grading_env, 'preflight', return_value=[]):
                for role in ('grader', 'reviewer', 'spec-auditor', 'spec-writer'):
                    start(role)
                predicate.assert_not_called()
            self.signed('AC5-other-roles', dict(roles=['grader', 'reviewer', 'spec-auditor', 'spec-writer'], gate_calls=0))
            self.signed('AC5-guard-precedence', [dict(reason=e.get('reason'), why=e.get('why'))
                        for e in self.read() if e['type'] == 'spawn-failed'])
            self.emit('spec-auditor', 'spawn-done', bad_cut=False)
            self.assertEqual(start(), 'backend', self.read())
            self.assertTrue(any(e['type'] == 'build-started' for e in self.read()))
            # A pre-deploy build remains exempt even without its audit.
            (self.root / 'events' / 'L-spec-auditor-9673.jsonl').unlink()
            self.assertEqual(start(), 'backend', self.read())
            self.signed('AC5-audited-rebuild', dict(backend_calls=backend.call_count, events=self.read()))


if __name__ == '__main__' and not sys.argv[1:]:
    unittest.main(argv=[sys.argv[0]])
