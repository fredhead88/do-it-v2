#!/usr/bin/env python3
"""Hermetic production-runner regression checks; no production access."""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
import pathlib
import socket
import subprocess
import tempfile
from unittest.mock import patch

import prod_runner
import dispatch
import fold
import owed
import sweep_owed

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
SPEC = "L-spec-9998"
SHA = "a" * 40


def record(command='ro-sql project "select 1"', **kw):
    return {"runner": "prod", "needs": ["ro-prod-sql"], "validity_h": 24,
            "commands": [command] if command else [], **kw}


def process(code=0, stderr="", stdout="ok"):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def observation(outcome, now, capability="ro-dsn", validity=24):
    result = {"outcome": outcome, "observed_at": now.isoformat(),
              "valid_until": (now + timedelta(hours=validity)).isoformat(), "evidence": "fixture"}
    if outcome == "unobservable":
        result.update(capability=capability, why="fixture unavailable")
    return result


def tree_hash(root):
    return hashlib.sha256(b"".join(str(p.relative_to(root)).encode() + p.read_bytes()
        for p in sorted(root.rglob("*")) if p.is_file() and p.relative_to(root).parts[0] != "cache")).hexdigest()


@contextmanager
def fixture(n=1, *, build=False, sha=SHA):
    with tempfile.TemporaryDirectory(prefix="prod-runner-test-") as temporary, ExitStack() as stack:
        root = pathlib.Path(temporary)
        for name in ("events", "content", "home"):
            (root / name).mkdir()
        stack.enter_context(patch.dict(os.environ, {"DOIT_ROOT": str(root), "HOME": str(root / "home"),
            "DOIT_LEDGER_FILE": str(root / "events" / "L-operator-trap.jsonl")}))
        for module in (fold, dispatch):
            for attr in ("ROOT", "CONTENT", "EVENTS"):
                if hasattr(module, attr):
                    stack.enter_context(patch.object(module, attr, root if attr == "ROOT" else root / attr.lower()))
        stack.enter_context(patch.object(fold, "PROJECT", None))
        stack.enter_context(patch.object(prod_runner, "PASSES", []))
        stack.enter_context(patch.object(prod_runner, "record_for", return_value=
            record("python check.py", runner="build", needs=["repo"]) if build else record()))
        def write(name, events):
            (root / "events" / name).write_text("".join(json.dumps({"v": 1, "subject": SPEC,
                "ts": (NOW - timedelta(hours=3)).isoformat(), **e}) + "\n" for e in events))
        write("L-spec-writer-fixture.jsonl", [{"type": "spec-written", "project": "project"},
            *({"type": "owed-ac", "criterion": f"AC{i}", "wake_at": (NOW - timedelta(hours=4)).isoformat(),
                "line": "fixture"} for i in range(1, n + 1))])
        write("L-executor-fixture.jsonl", [{"type": "shipped", "sha": sha}])
        (root / "content" / f"{SPEC}.md").write_text("## Acceptance criteria\n" + "".join(
            f"AC{i} [backend]: fixture\n" for i in range(1, n + 1)))
        yield root, write


def run_at(now, outcome="failed", **kw):
    with patch.object(dispatch, "now", return_value=now.isoformat()), patch.object(
            prod_runner, "observe", return_value=observation(outcome, now, **kw)):
        return prod_runner.run(now)


def events(root):
    path = root / "events" / prod_runner.LEDGER_NAME
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_empty_commands():
    with patch.object(subprocess, "run") as process:
        result = prod_runner.observe({}, {"commands": [], "needs": [], "validity_h": 24})
    assert result["outcome"] == "unobservable"
    assert result["capability"] == "repo" and result["why"].startswith("no commands")
    process.assert_not_called()


def test_observe():
    for command in ('scripts/sweep/ro-sql project "select 1"', 'droplet-read project cat /etc/os-release',
                    'curl -sS -o /dev/null -w %{http_code} https://host/path'):
        for code, expected in ((0, "met"), (1, "failed"), (2, "unobservable"), (3, "unobservable")):
            # curl 1 is a transport error, not an HTTP failure.
            if command.startswith("curl") and code == 1:
                code = 22
            with patch.object(subprocess, "run", return_value=process(code)) as stub:
                result = prod_runner.observe({}, record(command, validity_h=6), now=NOW)
            assert result["outcome"] == expected, (command, result)
            assert datetime.fromisoformat(result["valid_until"]) - datetime.fromisoformat(
                result["observed_at"]) == timedelta(hours=6)
            assert isinstance(stub.call_args.args[0], list)
            assert not stub.call_args.kwargs.get("shell")
            assert stub.call_args.kwargs["timeout"] == 60
            if expected == "unobservable":
                assert result["capability"] == "ro-dsn" and result["why"]
    for exc in (subprocess.TimeoutExpired(["stub"], 60), OSError("unavailable")):
        with patch.object(subprocess, "run", side_effect=exc):
            assert prod_runner.observe({}, record())["outcome"] == "unobservable"
    with fixture() as (root, _), patch.object(prod_runner, "record_for", return_value=record(None)), \
            patch.object(subprocess, "run") as stub:
        result = prod_runner.run(NOW)
        assert result["to_judgment"] == {(SPEC, "AC1")} and not events(root)
        stub.assert_not_called()
    print("AC1 ok")


def test_allowlist():
    refusals = ["-X POST url", "-XPOST url", "--request POST url", "-d a=b url",
                "--data-binary @f url", "--json '{}' url", "-F f=@x url", "-T f url", "-K cfg url",
                "--config cfg url", "-o /tmp/x url", "-O url", "file:///etc/passwd", "ftp://h/f",
                "-w @f url", "-H @f https://h", "-X GET url", "--output /dev/null https://h",
                "--data=x https://h", "-o/dev/null https://h", "-w '%output{x}' https://h"]
    commands = ["curl " + args for args in refusals] + [f'ro-sql project "{sql}"' for sql in (
        "insert into t values (1)", "update t set a=1", "delete from t", "drop table t",
        "select 1; drop table t")] + [head + " x" for head in ("rm", "ssh", "portal-e2e", "keepa-fetch")]
    for command in commands:
        with patch.object(subprocess, "run") as stub:
            result = prod_runner.observe({}, record(command))
        assert result["outcome"] == "unobservable" and result["capability"] == "repo"
        stub.assert_not_called()
        print("refused:", command)
    for command in [f'ro-sql project "{sql}"' for sql in (
            "select 1", "with x as (select 1) select * from x", "show server_version", "explain select 1")] + [
            "curl -sS -o /dev/null -w %{http_code} https://host/p",
            "curl -sSfL -I -H 'Accept: x' --max-time 20 https://host/p"]:
        with patch.object(subprocess, "run", return_value=process()) as stub:
            assert prod_runner.observe({}, record(command))["outcome"] == "met"
        argv = stub.call_args.args[0]
        if command.startswith("curl"):
            assert argv[1] == "-q" and argv[2:4] == ["--proto", "=http,https"]
        else:
            assert argv[0] == str(prod_runner.SOURCE / "scripts/sweep/ro-sql")
    # Validate the entire row before starting even its first valid command.
    with patch.object(subprocess, "run") as stub:
        assert prod_runner.observe({}, record(commands=['ro-sql project "select 1"', "rm x"]))[
            "capability"] == "repo"
        stub.assert_not_called()
    print("AC2 ok")


def test_permissions():
    for message in ("ERROR: permission denied for table x", "PERMISSION DENIED FOR TABLE X", "connection refused"):
        with patch.object(subprocess, "run", return_value=process(1, message)):
            result = prod_runner.observe({}, record())
        assert result["outcome"] == "unobservable"
        assert result["capability"] == ("ro-dsn" if "connection" in message else "ro-dsn-grant")
    print("AC3 ok")


def test_strikes():
    with fixture() as (root, _):
        run_at(NOW)
        state = json.loads((root / "state/prod-runner.json").read_text())
        assert not events(root) and state[f"{SPEC}/AC1"]["observed_at"] == NOW.isoformat()
        print("AC4 (a) first failure retained")
        run_at(NOW + timedelta(minutes=4))
        assert not events(root) and json.loads((root / "state/prod-runner.json").read_text()) == state
        print("AC4 (b) four minutes held")
        run_at(NOW + timedelta(minutes=5))
        assert [e["type"] for e in events(root)] == ["owed-failed"]
        assert events(root)[0]["observations"] == [NOW.isoformat(), (NOW + timedelta(minutes=5)).isoformat()]
        print("AC4 (c) two failures confirmed")
    with fixture() as (root, _):
        run_at(NOW)
        run_at(NOW + timedelta(minutes=5), "met")
        assert events(root)[0]["type"] == "owed-met"
        assert json.loads((root / "state/prod-runner.json").read_text()) == {}
        print("AC4 (d) met clears state")
    with fixture() as (root, _):
        run_at(NOW, validity=0.05)
        run_at(NOW + timedelta(minutes=5))
        assert not events(root)
        assert json.loads((root / "state/prod-runner.json").read_text())[f"{SPEC}/AC1"][
            "observed_at"] == (NOW + timedelta(minutes=5)).isoformat()
        print("AC4 (e) expired strike discarded")
    for bad_state in ("corrupt", "[]", '{"x": null}', '{"x":{"observed_at":"bad"}}'):
        with fixture() as (root, _):
            (root / "state").mkdir()
            (root / "state/prod-runner.json").write_text(bad_state)
            run_at(NOW)
            assert not events(root)
    print("AC4 (f) corrupt state safe")
    with fixture() as (root, _):
        run_at(NOW)
        run_at(NOW + timedelta(minutes=5), "unobservable")
        assert not events(root)
        assert json.loads((root / "state/prod-runner.json").read_text())[f"{SPEC}/AC1"]["outcome"] == "failed"
        print("AC4 (g) unobservable preserves failure")
        run_at(NOW + timedelta(minutes=10), "unobservable")
        assert events(root)[0]["type"] == "owed-unobservable"
        assert json.loads((root / "state/prod-runner.json").read_text())[f"{SPEC}/AC1"]["outcome"] == "failed"
    with fixture() as (root, _):
        for offset, response in ((0, process(1, "connection refused")), (5, subprocess.TimeoutExpired([], 60))):
            clock = NOW + timedelta(minutes=offset)
            with patch.object(dispatch, "now", return_value=clock.isoformat()), patch.object(subprocess, "run",
                    **({"side_effect": response} if isinstance(response, Exception) else {"return_value": response})):
                prod_runner.run(clock)
            assert len(events(root)) == (0 if offset == 0 else 1)
            if offset == 0:
                specs = fold.fold(fold.read_events())[0]
                assert len(sweep_owed.due_candidates(specs, clock)) == 1
        assert events(root)[0]["type"] == "owed-unobservable"
    with fixture() as (root, _):
        run_at(NOW, "unobservable", capability="ro-dsn-grant")
        assert events(root)[0]["capability"] == "ro-dsn-grant"
    for intervening in ("met", "failed"):
        with fixture() as (root, _):
            run_at(NOW, "unobservable")
            run_at(NOW + timedelta(minutes=1), intervening)
            assert not json.loads((root / "state/prod-runner.json").read_text()) or intervening == "failed"
    print("AC4 (h) transient two-strike; deterministic immediate")
    with fixture(n=4) as (_, write):
        write("L-operator-failures.jsonl", [
            {"type": "owed-failed", "criterion": f"AC{i}", "kind": "unmet", "evidence": "fixture",
             "ts": (NOW - timedelta(hours=1)).isoformat(), **fields}
            for i, fields in ((1, {"valid_until": (NOW - timedelta(seconds=1)).isoformat()}),
                              (2, {"valid_until": (NOW + timedelta(hours=1)).isoformat()}), (3, {}))])
        specs = fold.fold(fold.read_events())[0]
        for _ in range(2):
            selected = prod_runner._selected(specs, NOW)
            assert {r["criterion"] for r in selected} == {"AC4"}
        print("AC4 (i) recent failures held regardless of evidence validity")
        assert {r["criterion"] for r in sweep_owed.due_candidates(specs, NOW)} == {"AC4"}
        print("AC4 (j) no failed rows added inside the retry window")
    print("AC4 ok")


def test_stale_failure_selection():
    for case, age, validity, corrective_age, selected in (
            ("missing validity", 13, None, None, False),
            ("invalid validity", 13, "invalid", None, False),
            ("recent expired failure", 2, -1, None, False),
            ("expired 13h failure", 13, -1, None, True),
            ("standing corrective before failure", 13, -1, 14, False),
            ("correcting", 13, -1, 12, False),
            ("corrective before governing event", 13, -1, 49, True),
            ("retry boundary", 12, -1, None, True),
            ("inside retry boundary", 11.99, -1, None, False),
            ("validity boundary", 13, 0, None, False),
            ("valid evidence", 13, 1, None, False)):
        with fixture() as (root, write):
            earlier = (NOW - timedelta(hours=48)).isoformat()
            write("L-spec-writer-fixture.jsonl", [
                dict(type="spec-written", project="project", ts=earlier),
                dict(type="owed-ac", criterion="AC1", wake_at=earlier, line="fixture", ts=earlier)])
            write("L-executor-fixture.jsonl", [dict(type="shipped", sha=SHA, ts=earlier)])
            failure = dict(type="owed-failed", criterion="AC1", kind="unmet", evidence="known miss",
                           ts=(NOW - timedelta(hours=age)).isoformat())
            if validity is not None:
                failure["valid_until"] = ((NOW + timedelta(hours=validity)).isoformat()
                                           if isinstance(validity, (int, float)) else validity)
            # An older expired failure must not supply a missing validity
            # window for the latest failure.
            lifecycle = [{**failure, "ts": (NOW - timedelta(hours=age + 1)).isoformat(),
                          "valid_until": (NOW - timedelta(hours=2)).isoformat()}, failure]
            if corrective_age is not None:
                lifecycle.append(dict(type="owed-corrective", criterion="AC1", corrective="L-spec-9999",
                                      ts=(NOW - timedelta(hours=corrective_age)).isoformat()))
            write("L-operator-failures.jsonl", lifecycle)
            specs = fold.fold(fold.read_events())[0]
            assert bool(prod_runner._selected(specs, NOW)) == selected, case
            with patch.object(subprocess, "run", side_effect=AssertionError("real process")):
                run_at(NOW, "met")
            assert [e["type"] for e in events(root)] == (["owed-met"] if selected else []), case
            print("W38 stale observation:", case, "re-observed" if selected else "excluded")


def test_events():
    for stale in (False, True):
        with fixture() as (root, write):
            if stale:
                write("L-operator-oldmet.jsonl", [{"type": "owed-met", "criterion": "AC1", "evidence": "old",
                    "ts": (NOW - timedelta(days=1)).isoformat()}])
            # Assemble synthetic sensitive input without putting it in logs.
            sensitive = "postgres" + "://" + "u:secret@h/db"
            for offset in (0, 5):
                clock = NOW + timedelta(minutes=offset)
                with patch.object(subprocess, "run", return_value=process(1, sensitive + " x" * 1000)), \
                        patch.object(dispatch, "now", return_value=clock.isoformat()):
                    prod_runner.run(clock)
            event = events(root)[0]
            assert event["kind"] == ("stale" if stale else "unmet")
            assert event["subject"] == SPEC and event["criterion"] == "AC1"
            assert len(event["evidence"]) <= 600 and sensitive not in event["evidence"]
            assert "secret" not in (root / "events" / prod_runner.LEDGER_NAME).read_text()
            assert not fold.required_reason(event)
            assert all(datetime.fromisoformat(event[key]) for key in ("observed_at", "valid_until"))
    assert "userinfo" not in prod_runner.redact("https://userinfo@host/p")
    print("AC5 ok")


def bundle_file(root, *, code=0):
    path = root / "evidence.json"
    path.write_text(json.dumps({"spec": SPEC, "sha": SHA, "base_sha": "b" * 40,
        "runner_version": "1", "commands": [{"criterion": "AC1", "exit": code}], "base": [],
        "comparison": {"new": [], "pre_existing": [], "undetermined": []}, "judgment_only": []}))
    return path


def test_build():
    for code in (0, 1):
        with fixture(build=True) as (root, _):
            bundle = bundle_file(root, code=code)
            with patch.object(subprocess, "run", return_value=process(stdout=str(bundle))) as stub:
                result = prod_runner.run(NOW)
            assert stub.call_args.args[0] == [str(prod_runner.SOURCE / "doit"), "run-checks", SPEC,
                "--sha", SHA, "--criteria", "AC1"]
            assert stub.call_args.kwargs["timeout"] == 2100
            if code:
                assert not events(root) and result["bundle_rows"] == {(SPEC, "AC1"): str(bundle)}
            else:
                assert events(root)[0]["type"] == "owed-met" and str(bundle) in events(root)[0]["evidence"]
    for response in (process(2), subprocess.TimeoutExpired([], 2100)):
        with fixture(build=True) as (root, _), patch.object(subprocess, "run",
                **({"side_effect": response} if isinstance(response, Exception) else {"return_value": response})):
            result = prod_runner.run(NOW)
            assert result["held"] == {(SPEC, "AC1")} and not events(root)
    with fixture(build=True, sha=None) as (root, _), patch.object(subprocess, "run") as stub:
        assert prod_runner.run(NOW)["held"] == {(SPEC, "AC1")}
        stub.assert_not_called()
    with fixture(n=7, build=True) as (_, _), patch.object(prod_runner, "_build", return_value=(None, None)) as stub:
        result = prod_runner.run(NOW)
        assert stub.call_count == 5 and {(SPEC, "AC6"), (SPEC, "AC7")} <= result["held"]
    with fixture(build=True), patch.object(prod_runner.time, "monotonic", side_effect=[0, 1500]), \
            patch.object(subprocess, "run") as stub:
        assert prod_runner.run(NOW)["held"] == {(SPEC, "AC1")}
        stub.assert_not_called()
    for build in (False, True):
        with fixture(build=build) as (root, _), patch.object(subprocess, "run") as stub:
            (root / "state").mkdir()
            with (root / "state/prod-runner.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                before = tree_hash(root)
                assert prod_runner.run(NOW)["held"] == {(SPEC, "AC1")}
                assert tree_hash(root) == before and not events(root)
            stub.assert_not_called()
    print("AC6 ok")


def test_ledger_and_hooks():
    assert prod_runner.PASSES == []
    with fixture() as (root, _):
        def hook(now, dry_run, emit):
            assert dry_run or events(root)[0]["type"] == "owed-met"
            assert emit("owed-met", SPEC, criterion="AC2", evidence="hook") is None
            return ["hook complete"]
        def broken(*args):
            raise ValueError("fixture")
        prod_runner.PASSES.extend([broken, hook])
        with patch.object(prod_runner, "observe", return_value=observation("met", NOW)):
            result = prod_runner.run(NOW)
        assert any("broken" in line for line in result["lines"])
        raw = events(root)
        assert len(raw) == 2 and all("actor" not in e for e in raw)
        assert not (root / "events/L-operator-trap.jsonl").exists()
        specs, _, ignored, _ = fold.fold(fold.read_events())
        assert not ignored, ignored
        assert all(e["actor"] == "prod-runner" for e in specs[SPEC]["evs"] if e["type"] == "owed-met")
        assert owed.checks(specs[SPEC]["evs"], NOW)[0]["status"] == "met"
        before = tree_hash(root)
        prod_runner.run(NOW, dry_run=True)
        assert tree_hash(root) == before
        print("AC8 tree hash", before)
    print("AC8 ok")


def test_observed_data_build_refusal_is_unobservable():
    with fixture(build=True) as (root, _), patch.object(subprocess, "run", return_value=process(
            2, stderr="run-checks: " + prod_runner.build_runner.PROD_DATA_UNAVAILABLE)):
        for minutes in (0, 5):
            clock = NOW + timedelta(minutes=minutes)
            with patch.object(dispatch, "now", return_value=clock.isoformat()):
                result = prod_runner.run(clock)
            assert not result["bundle_rows"] and not result["to_judgment"]
        event, = events(root)
        assert event["type"] == "owed-unobservable" and event["capability"] == "ro-dsn", event
        assert event["env"] == {"pg": "none", "prod_dsns": "unset", "required_pg": "prod_ro"}
        assert not fold.required_reason(event)
    print("W55 revised: missing production data becomes owed-unobservable, never failing evidence")


def test_hermetic():
    with fixture() as (root, _), patch.object(socket, "socket", side_effect=AssertionError("network")), \
            patch.object(subprocess, "Popen", side_effect=AssertionError("real process")), \
            patch.object(subprocess, "run", return_value=process()) as stub:
        prod_runner.run(NOW)
        assert stub.call_count == 1
        assert pathlib.Path(stub.call_args.args[0][0]).name == "ro-sql"
        assert (root / "events" / prod_runner.LEDGER_NAME).is_file()
    with fixture(n=8) as (root, _):
        bundle = bundle_file(root)
        data = json.loads(bundle.read_text())
        data["commands"][0]["criterion"] = "AC4"
        bundle.write_text(json.dumps(data))
        records = [record(), record("droplet-read project cat /etc/os-release", needs=["droplet-read"]),
                   record("curl -sSf https://host/path", needs=["deployed-site"]),
                   record("python check.py", runner="build", needs=["repo"]),
                   record(None, runner="prod-browser", needs=["browser-review-account"]),
                   record(None, runner="judgment", needs=["repo"]), record(None),
                   record(None, runner="judgment", needs=["operator"])]
        calls = []
        def execute(argv, **kw):
            calls.append(argv)
            return process(stdout=str(bundle) if argv[1] == "run-checks" else "ok")
        with patch.object(prod_runner, "record_for", side_effect=lambda r: records[int(r["criterion"][2:])-1]), \
                patch.object(socket, "socket", side_effect=AssertionError("network")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("real process")), \
                patch.object(subprocess, "run", side_effect=execute):
            prod_runner.record_for.side_effect = lambda r, events=None: records[int(r["criterion"][2:])-1]
            result = prod_runner.run(NOW)
        assert {pathlib.Path(argv[0]).name for argv in calls} == {"ro-sql", "droplet-read", "curl", "doit"}
        assert len(calls) == 4 and result["to_judgment"] == {(SPEC, "AC7")}
        assert len(events(root)) == 4
    footprint = ["prod_runner.py", "test_prod_runner.py", "sweep_owed.py", "test_sweep_owed.py",
                 "owed_routes.py", "test_owed_routes.py"]
    paths = [prod_runner.SOURCE / "src" / name for name in footprint] + [prod_runner.SOURCE / "agents/owed-sweeper.md"]
    forbidden = ["claude" + " -p", "ANTHROPIC" + "_API_KEY"]
    assert all(token not in p.read_text() for p in paths for token in forbidden)
    assert all(len((prod_runner.SOURCE / "src" / name).read_text().splitlines()) < 1500
               for name in ("prod_runner.py", "sweep_owed.py", "owed_routes.py"))
    print("AC10 ok")


def test_ac4_events_passed_down_runner():
    import dataclasses
    import checks
    original_record_for = prod_runner.record_for
    with fixture(n=3) as (_, write), patch.object(prod_runner, "record_for", new=original_record_for):
        declaration = checks.CheckRecord(SPEC + ":AC1", SPEC, "AC1", ["/usr/bin/true"], SHA,
            "scratch", False, "repo-venv", "pre-merge", "script")
        write("L-spec-writer-fixture.jsonl", [dict(type="spec-written", project="project"),
            dict(type="check-declared", **dataclasses.asdict(declaration)),
            *(dict(type="owed-ac", criterion=f"AC{i}", wake_at=(NOW - timedelta(hours=4)).isoformat(),
                line="fixture", **({"capability": "browser"} if i == 3 else {})) for i in range(1, 4))])
        ev = fold.read_events()
        rows = [dict(spec=SPEC, criterion=f"AC{i}", **({"capability": "browser"} if i == 3 else {}))
                for i in range(1, 4)]
        expected_records = [prod_runner.record_for(row) for row in rows]
        expected = prod_runner.run(NOW, dry_run=True)
        with patch.object(fold, "read_events", side_effect=AssertionError("implicit read")):
            assert [prod_runner.record_for(row, events=ev) for row in rows] == expected_records
            assert prod_runner.run(NOW, dry_run=True, events=ev) == expected
            assert prod_runner.run(NOW, dry_run=True, events=[]) == dict(
                lines=[], bundle_rows={}, held=set(), to_judgment=set())
        assert [record["runner"] for record in expected_records] == ["script", "judgment", "prod-browser"]
    print("L-spec-1531 AC4 prod_runner ok")


if __name__ == "__main__":
    test_ac4_events_passed_down_runner()
    for test in (test_empty_commands, test_observe, test_allowlist, test_permissions, test_strikes,
                 test_stale_failure_selection, test_events, test_build, test_ledger_and_hooks,
                 test_observed_data_build_refusal_is_unobservable, test_hermetic):
        test()
    for criterion in (1, 2, 3, 4, 5, 6, 8, 10):
        body = {"criterion": f"AC{criterion}", "result": "ok", "suite": "test_prod_runner.py"}
        print(json.dumps({"url": f"cli://prod-runner/AC{criterion}", "status": 0,
            "body_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(), "body_excerpt": body}))
