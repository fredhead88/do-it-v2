#!/usr/bin/env python3
"""Run dispatch regressions and the ordered historical fixture chunks."""
from pathlib import Path
import json, os, subprocess, sys, tempfile


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
    _isolated_run([sys.executable, "-c", "import sys; sys.path.insert(0, " +
                   repr(str(_checks.parent)) + "); " + _new + "\ncheck_dispatch_0898()"])

_legacy = r"""
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).parent))
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
    # These historical graders exercise legacy no-bundle capability and
    # verdict handling. The real bundle gate is covered in check_evidence1109
    # above, in its own process; it now refuses these old fixture packets.
    with patch("grader_packet.gate", return_value=None):
        exec(compile(_source, str(_path), "exec"), globals())
"""
if __name__ == "__main__":
    if sys.argv[1:] == ["--alloc-cases"]:
        _check_alloc_suffix()
    elif sys.argv[1:] == ["--issued-by-cases"]:
        test_dispatch_1134()
    else:
        test_alloc_suffix()
        _isolated_run([sys.executable, __file__, "--issued-by-cases"])
        test_dispatch_0898()
        _isolated_run([sys.executable, "-c", "__file__ = " + repr(__file__) + "\n" + _legacy])


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
    def record(row):
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
