#!/usr/bin/env python3
"""Scratch acceptance groups for preflight screening and admission."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import shlex
from unittest import mock

from test_spec_preflight import Fixture, NOW, sp


def body():
    return dict(spec="L-spec-9001", findings=[], red_on_master=[],
                undetermined=[], refused=[], cpu_s=0.0)


def rows(result, check):
    return [r for r in result["refused"] if r["check"] == check]


def report(case, result):
    sp._validate(result)
    print(f"{case}: refused={result['refused']} undetermined={result['undetermined']} "
          f"child_cpu_s={result['cpu_s']:.6f}", flush=True)


def AC1():
    import autodispatch as ad
    import dispatch
    import fold
    import lane_rules
    import nextstep as ns

    f = Fixture()
    sid = "L-spec-9001"
    now = datetime.now(timezone.utc)
    def event(kind, minute, **fields):
        actor = "spec-preflight" if kind == "spec-preflight" else "operator"
        return dict(type=kind, subject=sid, actor=actor,
                    ts=(now + timedelta(minutes=minute)).isoformat(),
                    _src=f"L-{actor}-fixture.jsonl:{minute + 200}") | fields
    written = event("spec-written", -60, project="do-it-v2")
    audit = event("spawn-done", -59, role="spec-auditor", bad_cut=False)
    clean = event("spec-preflight", -58, **body(), spec_written_src=written["_src"])
    rewrite = event("spec-written", -30, project="do-it-v2")
    refusal = dict(check="verification-red", id="src/test_red.py", reason="exit 1", seconds=0.1)
    samples = [
        ("no preflight", [], "held: awaiting spec-preflight"),
        ("old schema", [{k: v for k, v in clean.items() if k not in {"refused", "cpu_s"}}],
         "held: awaiting spec-preflight"),
        ("preceding clean revision", [dict(clean, ts=event("x", -61)["ts"])],
         "held: awaiting spec-preflight"),
        ("current empty refused", [clean], None),
        ("current refused", [dict(clean, refused=[refusal])],
         "held: spec-preflight refused (verification-red: src/test_red.py)"),
        ("current undetermined", [dict(clean, undetermined=["runner unavailable"])],
         "held: spec-preflight undetermined (runner unavailable)"),
        ("later rewrite", [clean, rewrite], "held: awaiting spec-preflight"),
        ("older captured revision", [clean, rewrite, dict(clean, ts=event("x", -29)["ts"])],
         "held: awaiting spec-preflight"),
        ("later clean recheck", [clean, rewrite, dict(clean, ts=event("x", -29)["ts"],
                                                   spec_written_src=rewrite["_src"])], None),
        ("later refused recheck", [clean, dict(clean, ts=event("x", -20)["ts"], refused=[refusal])],
         "held: spec-preflight refused (verification-red: src/test_red.py)"),
        ("later clean after refusal", [dict(clean, refused=[refusal]),
                                       dict(clean, ts=event("x", -20)["ts"])], None),
        ("forged preflight", [dict(clean, actor="builder", _src="L-builder-fixture.jsonl:1")],
         "held: awaiting spec-preflight"),
        ("wrong subject", [dict(clean, subject="L-spec-9002")], "held: awaiting spec-preflight"),
    ]
    records = []
    started = time.monotonic()
    with mock.patch.object(dispatch, "CONTENT", f.root / "content"), \
         mock.patch.object(fold, "ROOT", f.root), \
         mock.patch.object(ad.shape, "check", return_value={"block": []}), \
         mock.patch.object(ad, "_lane_limit", return_value=30), \
         mock.patch.object(ad, "capacity", return_value=4), \
         mock.patch.object(ad, "_own_terminal_event", return_value=False):
        (f.root / "content" / (sid + ".md")).write_text("# scratch spec\n")
        def check(name, evs, expected):
            evs = ns._snapshot(evs, {})[0]
            own = [e for e in evs if e.get("subject") == sid]
            assert ad._automatic_hold(own, evs) == expected, name
            specs = {sid: dict(id=sid, state="written", evs=own)}
            row = ad.candidates(evs, specs, now)[0]
            assert (row["status"] == "dispatchable") == (expected is None), (name, row)
            step = ns.step_for(evs, specs, sid, now, lane_rules.load())
            assert (step.action == "build") == (expected is None), (name, step)
            if expected and "spec-preflight" in expected:
                assert step.reason == expected, (name, step)
                assert row["reason"] == expected
            stale = event("spawn-stale", -1, role="builder", spawn="L-builder-9001")
            dead = ad.dead_spawns(evs + [stale], now)
            assert bool(dead) == (expected is None), (name, dead)
            failure = event("spawn-failed", -2, role="builder", reason="stale-base",
                            spawn="L-builder-9002")
            recovery = ns.step_for(evs + [failure], specs, sid, now, lane_rules.load())
            assert (recovery.action == "build") == (expected is None), (name, recovery)
            records.append(dict(case=name, hold=expected, step=asdict(step), recovery=asdict(recovery)))
        for name, extra, expected in samples:
            check(name, [written, audit, *extra], expected)
        dependency = event("dependency-hold", -10, waits_for="L-spec-9002")
        check("dependency hold", [written, audit, clean, dependency],
              ad._automatic_hold([written, audit, clean, dependency], [dependency]))
        path = f.root / "content" / (sid + ".md")
        path.write_text("Dependency-hold: waits_for=L-spec-9002\n")
        check("text hold", [written, audit, clean],
              ad._automatic_hold([written, audit, clean], []))
        path.write_text("# scratch spec\n")
        ruling = event("decision", -10, action="hold", waits_for="L-spec-9002")
        check("standing ruling", [written, audit, clean, ruling],
              ad._automatic_hold([written, audit, clean, ruling], []))
        # Equal timestamps retain same-source append order, as fold does.
        tied_written = dict(written, _src="L-operator-fixture.jsonl:1")
        tied_clean = dict(clean, ts=written["ts"], actor="spec-preflight",
                          _src="L-spec-preflight-fixture.jsonl:1",
                          spec_written_src=tied_written["_src"])
        # Across sources the canonical content key puts preflight before written.
        check("equal timestamp preceding evidence", [tied_written, audit, tied_clean],
              "held: awaiting spec-preflight")
    payload = json.dumps(dict(cases=records, elapsed_s=time.monotonic() - started), sort_keys=True)
    print(json.dumps(dict(url="scratch://preflight-gate/AC1", status=0,
                         body_sha256=hashlib.sha256(payload.encode()).hexdigest(), body_excerpt=payload)))


def AC2():
    f = Fixture()
    for name, code in (("green", "pass"), ("red", "raise SystemExit(3)"),
                       ("other", "raise SystemExit(4)")):
        f.file(f"src/test_{name}.py", code)
    f.commit()
    f.git("checkout", "-qb", "builder")
    f.file("src/test_red.py", "pass")
    f.commit()
    command = "\n".join("/usr/bin/python3 src/test_" + n + ".py"
                        for n in ("green", "red", "other", "new", "missing"))
    f.spec(command, writes=("src/test_new.py",))
    result = f.run()
    assert result["red_on_master"] == ["src/test_red.py", "src/test_other.py"]
    assert {r["id"] for r in rows(result, "verification-red")} == set(result["red_on_master"])
    assert all(r["seconds"] > 0 and "exit" in r["reason"] for r in result["refused"])
    assert any(r["id"] == "src/test_new.py" for r in result["findings"])
    assert any("missing test on master src/test_missing.py" in r for r in result["undetermined"])
    report("master red despite green builder; two red, green, granted/ungranted missing", result)

    events = sp._events(f.root)
    written = next(e for e in reversed(events) if e["type"] == "spec-written")
    events.append(dict(type="writes-granted", subject="L-spec-9001",
                       spec_written_src=written["_src"], tests=[dict(test="src/test_missing.py")]))
    result = sp.run("L-spec-9001", root=f.root, events=events)
    assert any(r["id"] == "src/test_missing.py" for r in result["findings"])
    assert not any("missing test on master" in r for r in result["undetermined"])
    report("effective-grant exemption", result)
    for command in ("/usr/bin/python3 ../test_escape.py", "node src/test_green.py"):
        f.spec(command)
        with mock.patch.object(sp, "_check_chain") as chain:
            result = f.run()
        assert result["undetermined"]
        assert not chain.called
        report("unsafe identifier or unsupported runner", result)
    result = body()
    with mock.patch.object(sp, "_process", side_effect=FileNotFoundError):
        sp._check_tests(result, "## Verification\n```bash\npython src/test_green.py\n```",
                        "do-it-v2", f.repo, {}, [])
    assert any("runner unavailable" in x for x in result["undetermined"])
    report("unavailable runner", result)
    f.file("src/test_sleep.py", "import time; time.sleep(20)")
    f.commit()
    f.spec("/usr/bin/python3 src/test_sleep.py")
    # The test lives on the builder branch only; first make main contain it.
    f.git("branch", "-f", "main", "HEAD")
    with mock.patch.object(sp, "TEST_CAP_S", 0.05), mock.patch.object(sp, "CHAIN_LIMIT_S", 0.05):
        result = f.run()
    assert any("timeout running src/test_sleep.py" in x for x in result["undetermined"])
    report("actual per-test timeout", result)


def AC3():
    assert sp.CHAIN_LIMIT_S == 120
    f = Fixture()
    code = ("import os; assert os.getcwd() != " + repr(str(f.repo))
            + "; assert not os.environ.get('PREFLIGHT_SECRET_SENTINEL')")
    f.spec("/usr/bin/python3 -c " + shlex.quote(code))
    actual = sp._process
    calls = []
    def capture(argv, **kw):
        if argv[:2] == ["/bin/bash", "-c"]:
            calls.append((argv, kw))
            assert not (Path(kw["cwd"]) / ".git/HEAD").read_text().startswith("ref:")
            assert kw["env"]["HOME"] != os.environ["HOME"]
        result = actual(argv, **kw)
        if argv[:2] == ["/bin/bash", "-c"]:
            assert result.returncode == 0, result.stderr
        return result
    with mock.patch.dict(os.environ, PREFLIGHT_SECRET_SENTINEL="synthetic"), \
            mock.patch.object(sp, "_process", side_effect=capture):
        result = f.run()
    assert len(calls) == 1 and not result["refused"] and not result["undetermined"]
    report("one actual short detached chain with isolated environment", result)
    for elapsed, overtime in ((119.999, False), (120.001, True)):
        result = body()
        with mock.patch.object(sp.time, "monotonic", side_effect=[0, elapsed]), \
                mock.patch.object(sp, "_process", return_value=subprocess.CompletedProcess([], 7, "", "")) as process:
            sp._check_chain(result, "exit 7", f.repo, {})
        assert process.call_count == 1
        assert bool(rows(result, "verification-overtime")) == overtime
        assert not result["red_on_master"] and not rows(result, "verification-red")
        report(f"mock boundary {elapsed}s, nonzero exit supplies no red verdict", result)
    for code, checkout in ((None, f.repo), ("true", None)):
        result = body()
        sp._check_chain(result, code, checkout, {})
        assert result["undetermined"]
        report("missing block or checkout", result)
    marker = f.base / "child.pid"
    script = f"sleep 20 &\necho $! > {str(marker)!r}\nwait"
    started = time.monotonic()
    with mock.patch.object(sp, "CHAIN_LIMIT_S", 0.15):
        result = body()
        sp._check_chain(result, script, f.repo, os.environ.copy())
    assert time.monotonic() - started < 3 and rows(result, "verification-overtime")
    pid = int(marker.read_text())
    # A killed orphan can remain a zombie until the host's init reaps it.
    status = Path(f"/proc/{pid}/stat")
    deadline = time.monotonic() + 2
    def terminated():
        try:
            return status.read_text().split()[2] in {"Z", "X"}
        except (FileNotFoundError, ProcessLookupError):
            return True
    while not terminated() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert terminated()
    report("actual bounded chain timeout; descendant terminated", result)


def AC4():
    banned = ("./doit test", "doit test", "/checkout/doit test", "pytest", "pytest -q",
              "python -m pytest", "/usr/bin/python3 -m pytest -q", "true && pytest -q",
              "env X=1 pytest -q", "pytest --ignore src/test_a.py", "pytest src/",
              "python3 -u -m pytest", "pytest -k 'test_file.py'", "echo hi # note\npytest -q",
              "/usr/bin/env -u UNUSED python3 -m pytest -q")
    controls = ("pytest src/test_a.py", "python -m pytest -q src/test_a.py",
                "/usr/bin/python3 src/test_a.py", "echo 'pytest -q'", "# ./doit test\ntrue",
                "printf '%s' './doit test'", "pytest --ignore src/test_a.py src/test_b.py",
                'echo "&&" pytest -q')
    f = Fixture()
    for command in banned:
        f.spec(command)
        with mock.patch.object(sp, "_check_tests") as tests, mock.patch.object(sp, "_check_chain") as chain:
            result = f.run()
        assert rows(result, "full-suite"), command
        assert not tests.called and not chain.called, command
        report("zero executions: " + command, result)
    for command in controls:
        assert not sp._full_suite(command), command
        print("explicit-file/quoted/comment control: " + command)
    result = body()
    sp._check_chain(result, "echo 'unterminated", f.repo, {})
    # Shell syntax failures are not red-test verdicts.
    assert not rows(result, "verification-red")


def AC5():
    f = Fixture()
    path = f.spec()
    original = path.read_text()
    for yaml, invalid in (("source: valid", False), ('source: "a: b"', False),
                          ("source: a: b", True), ("- scalar", True), (None, False)):
        text = original if yaml is None else "---\n" + yaml + "\n---\n" + original
        try:
            sp.merge_gate._frontmatter_grant(text)
            parser_invalid = False
        except sp.merge_gate.Undetermined:
            parser_invalid = True
        assert parser_invalid == invalid
        path.write_text(text)
        result = f.run()
        assert bool(rows(result, "frontmatter")) == invalid
        report("authoritative frontmatter: " + str(yaml), result)
    for fault in (ImportError("parser unavailable"), RuntimeError("parser failed")):
        with mock.patch.object(sp.merge_gate, "_frontmatter_grant", side_effect=fault):
            result = f.run()
        assert result["undetermined"]
        report("parser unavailable/error", result)
    path.write_text("---\nsource: valid\n---\n" + original)
    with mock.patch.dict(sys.modules, {"yaml": None}):
        result = f.run()
    assert any("frontmatter" in x for x in result["undetermined"])
    report("YAML unavailable must not use regex fallback as a clean parse", result)


def AC7():
    valid = body()
    valid.update(lag_s=1.0, findings=[dict(check=4, id="new", line="new on master")],
                 red_on_master=["red"], undetermined=["visible"])
    for check in ("verification-red", "verification-overtime", "full-suite", "frontmatter", "non-spec-hold"):
        valid["refused"].append(dict(check=check, id="test", reason="diagnostic", seconds=0.0))
    sp._validate(valid)
    report("all core and sibling refusal rows plus legacy fields", valid)
    import copy
    faults = [dict(seconds=-1.0), dict(seconds=math.inf), dict(seconds=math.nan), dict(seconds=True),
              dict(seconds=1), dict(check="unknown"), dict(id=""), dict(reason=""), dict(extra=1)]
    for fault in faults:
        invalid = copy.deepcopy(valid)
        invalid["refused"][0].update(fault)
        try:
            sp._validate(invalid)
        except AssertionError:
            continue
        raise AssertionError(fault)
    for value in (-1.0, math.inf, math.nan, True, 1):
        try:
            sp._validate({**valid, "cpu_s": value})
        except AssertionError:
            continue
        raise AssertionError(value)
    f = Fixture()
    f.spec()
    samples = [10.0, 10.25, 20.0, 20.5]
    with mock.patch.object(sp, "_child_cpu", side_effect=samples):
        first, second = f.run(), f.run()
    assert first["cpu_s"] == 0.25 and second["cpu_s"] == 0.5
    report("first per-spec CPU delta", first)
    report("second per-spec CPU delta excludes earlier spec", second)
    f.spec("/usr/bin/python3 -c 'sum(i*i for i in range(300000))'")
    def hook(sid, result, written, checkout):
        sp._process(["/usr/bin/python3", "-c", "sum(range(300000))"], cwd=checkout)
        result["refused"].append(dict(check="non-spec-hold", id="sibling", reason="stand-in", seconds=0.0))
    events = sp._events(f.root)
    written = next(e for e in reversed(events) if e["type"] == "spec-written")
    result = sp.run("L-spec-9001", root=f.root, events=events, hooks=[hook],
                    expected_identity=sp._text_identity(f.root, written))
    assert result["cpu_s"] > 0 and rows(result, "non-spec-hold")
    report("actual check and hook child CPU before append", result)
    result = sp.run("unknown", root=f.root, events=[])
    assert type(result["cpu_s"]) is float and result["cpu_s"] >= 0
    report("CPU on early failure", result)


def preflight_line(f, sid, minute, **fields):
    """Write a spec-preflight event the way the registered emitter's file would."""
    event = dict(type="spec-preflight", subject=sid, project=f.project,
                 ts=(NOW + timedelta(minutes=minute)).isoformat(), **fields)
    with (f.root / "events" / "L-spec-preflight-fixture.jsonl").open("a") as out:
        out.write(json.dumps(event) + "\n")


def stored(f, kind="spec-preflight"):
    return [e for e in sp._events(f.root) if e["type"] == kind]


def AC6():
    import tomllib
    cron = next(r for r in tomllib.loads((Path(sp.__file__).resolve().parent.parent / "crons.toml")
                                         .read_text())["row"] if r["name"] == "spec-preflight")
    assert cron["max_runtime_s"] == 3300 and cron["schedule"].startswith("4,9,14")
    # The budget holds the seeded 180s first chain and still clears the cron cap
    # in the worst case of one overrunning run past the deadline.
    assert 180 < sp.TICK_BUDGET_S and sp.TICK_BUDGET_S + sp.TEST_CAP_S + sp.CHAIN_LIMIT_S < cron["max_runtime_s"]
    print(f"budget {sp.TICK_BUDGET_S}s supports a 180s chain inside the {cron['max_runtime_s']}s cron cap")

    # Eligibility matrix, directly.
    def ev(kind, sid="s", days=0.0, **fields):
        return dict(type=kind, subject=sid, ts=(NOW - timedelta(days=days)).isoformat(), **fields)
    cases = [
        ("four-day-old unbuilt", [ev("spec-written", days=4)], True),
        ("thirty-day-old unbuilt", [ev("spec-written", days=30)], True),
        ("rewritten after build-started", [ev("spec-written", days=5), ev("build-started", days=4),
                                           ev("spec-written", days=1)], True),
        ("built after revision", [ev("spec-written", days=5), ev("build-started", days=4)], False),
        ("shipped", [ev("spec-written", days=4), ev("shipped", days=1)], False),
        ("closed", [ev("spec-written", days=4), ev("spec-closed", days=1)], False),
        ("killed", [ev("spec-written", days=4), ev("spec-killed", days=1)], False),
        ("malformed time", [dict(type="spec-written", subject="s", ts="not-a-time")], False),
        ("missing time", [dict(type="spec-written", subject="s")], False),
        ("naive time", [dict(type="spec-written", subject="s", ts="2026-11-01T00:00:00")], False),
        ("future time", [ev("spec-written", days=-1)], False),
        ("no spec-written", [ev("build-started")], False),
    ]
    for name, events, expected in cases:
        assert sp._eligible("s", events, NOW) == expected, name
        print(f"eligibility {name}: {expected}")

    # Pending: old-schema evidence is rechecked once per newest revision.
    old = dict(type="spec-preflight", subject="s")
    new = dict(old, refused=[], _src="x")
    assert sp.pending([ev("spec-written", _src="w1"), old]) == ["s"]
    assert sp.pending([ev("spec-written", _src="w1"), dict(new, spec_written_src="w1")]) == []
    assert sp.pending([ev("spec-written", _src="w1"), dict(new, spec_written_src="w0")]) == ["s"]
    assert sp.pending([ev("spec-written", _src="w1"), dict(new, spec_written_src="w1"),
                       ev("spec-written", _src="w2")]) == ["s"]

    # old-schema -> new-schema -> second tick, through the real append.
    f = Fixture()
    sid = "L-spec-9001"
    f.spec(sid=sid)
    preflight_line(f, sid, -5, spec=sid, findings=[], red_on_master=[], undetermined=[])
    calls = []
    def fake(spec_id, **kw):
        calls.append(spec_id)
        return body() | dict(spec=spec_id)
    def tick(f, label):
        out = __import__("io").StringIO()
        with mock.patch.object(sp, "run", side_effect=fake), mock.patch.object(sp, "after_run", []), \
                __import__("contextlib").redirect_stdout(out):
            assert sp.tick(root=f.root, now=NOW) == 0
        print(f"{label}: runs={len(calls)} {out.getvalue().strip()}")
        return out.getvalue()
    assert sp.pending(sp._events(f.root)) == [sid]
    tick(f, "old-schema recheck")
    assert calls == [sid] and sp.pending(sp._events(f.root)) == []
    latest = stored(f)[-1]
    assert "refused" in latest and latest["spec_written_src"]
    tick(f, "second tick")
    assert calls == [sid], "valid completed evidence must not be rerun"
    f.spec(sid=sid)  # rewrite resets
    assert sp.pending(sp._events(f.root)) == [sid]
    tick(f, "rewritten revision")
    assert calls == [sid, sid]

    # Ordered queue of 12 with a simulated 180s first chain; others 100s each.
    q = Fixture()
    sids = [f"L-spec-{9400 + i}" for i in range(12)]
    for x in sids:
        q.spec(sid=x)
    clock = [0.0]
    ran = []
    def slow(spec_id, **kw):
        ran.append(spec_id)
        clock[0] += 180 if len(ran) == 1 else 100
        return body() | dict(spec=spec_id)
    def queue_tick():
        out = __import__("io").StringIO()
        with mock.patch.object(sp.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(sp, "run", side_effect=slow), mock.patch.object(sp, "after_run", []), \
                __import__("contextlib").redirect_stdout(out):
            assert sp.tick(root=q.root, now=NOW) == 0
        return out.getvalue()
    first = queue_tick()
    assert ran == sids[:9] and ran != sids[:sp.TICK_MAX], ran
    assert "processed=9 deferred=3" in first and ", ".join(sids[9:]) in first, first
    print("tick 1 (180s first chain):", first.strip())
    clock[0] = 0.0
    second = queue_tick()
    assert ran == sids and "processed=3 deferred=0" in second, second
    assert [e["subject"] for e in stored(q)] == sids, "oldest first, each exactly once"
    assert all(e["lag_s"] >= 0 for e in stored(q))
    print("tick 2 (deferred work attempted next):", second.strip())
    clock[0] = 0.0
    assert queue_tick().startswith("spec-preflight: processed=0")


def AC8():
    import contextlib
    import io
    import pwd
    import autodispatch as ad
    real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".do-it" / "events"
    def sentinel():
        # The real ledger is live (other actors append), so size/mtime would flake;
        # observe instead that no fixture subject ever reaches it.
        if not real.is_dir():
            return {}
        counts = {p.name: p.read_bytes().count(b"L-spec-9001") for p in real.glob("*.jsonl")}
        return {name: n for name, n in counts.items() if n}
    before, root_before = sentinel(), sorted(p.name for p in Path(os.environ["DOIT_ROOT"]).rglob("*"))

    f = Fixture()
    f.spec()
    sid = "L-spec-9001"
    seen = []
    def stand_in(spec_id, result, written, checkout):
        assert checkout is not None and Path(checkout).is_dir(), "hook ran without master checkout"
        assert (Path(checkout) / ".git").exists()
        seen.append((spec_id, written["_src"]))
        result["refused"].append(dict(check="non-spec-hold", id="sibling", reason="dependency stand-in",
                                      seconds=0.0))
    def grant_stand_in(spec_id, result, written, checkout):
        seen.append(("grant", written["_src"]))
    def tick():
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sp.tick(root=f.root, now=NOW)
        assert code == 0, err.getvalue()
        return out.getvalue(), err.getvalue()
    def read_back():
        """Independent process: only the stored, authorized JSONL is consulted."""
        code = ("import json, sys, pathlib, fold\n"
                "root = pathlib.Path(sys.argv[1])\n"
                "fold.ROOT, fold.EVENTS, fold.PROJECT = root, root / 'events', ''\n"
                "ignored = {id(e) for e in fold.fold(fold.read_events(use_cache=False))[2]}\n"
                "print(json.dumps([e for e in fold.read_events(use_cache=False) if id(e) not in ignored"
                " and e['type'] == 'spec-preflight']))\n")
        done = subprocess.run(["/usr/bin/python3", "-I", "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); exec(sys.argv.pop(1))",
                               str(Path(sp.__file__).resolve().parent), code, str(f.root)],
                              capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)
    with mock.patch.object(sp, "after_run", [grant_stand_in, stand_in]):
        tick()
        rows = read_back()
        assert len(rows) == 1 and rows[0]["subject"] == sid
        assert rows[0]["refused"][0]["check"] == "non-spec-hold" and type(rows[0]["cpu_s"]) is float
        written = [e for e in sp._events(f.root) if e["type"] == "spec-written"][-1]
        assert rows[0]["spec_written_src"] == written["_src"]
        assert seen == [("grant", written["_src"]), (sid, written["_src"])]
        print("real append round trip, independent read-back:", json.dumps(rows[0], sort_keys=True))
        tick(); tick()
        assert len(seen) == 2 and len(read_back()) == 1, "no duplicate emission for one revision"
        print("repeated ticks: hooks ran", len(seen), "times; stored events", len(read_back()))
        events = sp._events(f.root)
        own = [e for e in events if e.get("subject") == sid]
        hold = ad._preflight_hold(own)
        assert hold == "held: spec-preflight refused (non-spec-hold: sibling)", hold
        f.spec()  # new revision
        tick()
        assert len(seen) == 4 and len(read_back()) == 2
        assert read_back()[-1]["spec_written_src"] != rows[0]["spec_written_src"]
        print("new revision: hooks ran", len(seen), "times in total")

    def boom(spec_id, result, written, checkout):
        raise RuntimeError("fixture")
    f.spec()
    with mock.patch.object(sp, "after_run", [boom]):
        tick()
    latest = read_back()[-1]
    assert any("hook" in x and "RuntimeError" in x for x in latest["undetermined"]), latest
    own = [e for e in sp._events(f.root) if e.get("subject") == sid]
    assert ad._preflight_hold(own).startswith("held: spec-preflight undetermined")
    print("raising hook:", latest["undetermined"])

    # Source text changing while hooks run defers the append.
    path = f.spec()
    count = len(read_back())
    def edit(spec_id, result, written, checkout):
        path.write_text(path.read_text() + "\nedited during the pass\n")
    with mock.patch.object(sp, "after_run", [edit]):
        out, err = tick()
    assert "recheck next pass" in err and len(read_back()) == count
    print("text change during pass:", err.strip())

    assert sentinel() == before and sorted(p.name for p in Path(os.environ["DOIT_ROOT"]).rglob("*")) == root_before
    print("real ledger and process-root sentinels unchanged")


GROUPS = {name: value for name, value in list(globals().items())
          if name in {"AC1", "AC2", "AC3", "AC4", "AC5", "AC6", "AC7", "AC8"}}
if __name__ == "__main__":
    selected = sys.argv[1:] or list(GROUPS)
    if any(name not in GROUPS for name in selected):
        print("unknown or not yet implemented acceptance group", file=sys.stderr)
        sys.exit(2)
    for name in selected:
        GROUPS[name]()
        print(f"L-spec-2442 {name} ok", flush=True)
