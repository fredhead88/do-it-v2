#!/usr/bin/env python3
"""Checks on window_gate (L-spec-0765). Run: python3 test_window_gate.py

Hermetic: real `git` against temp repos (a bare `origin` and a clone at
`$R/repos/albert-scott`), `fold.ROOT`/`fold.EVENTS` repointed at a fresh temp
root per fixture, and a FAKE `predeploy_gate.sh` (DOIT_WINDOW_PREDEPLOY) that
replays a scripted verdict per call and records each invocation. No network, no
`gh`, no model call. The fake stands in for the companion interface of
L-spec-0764 (JSON verdict line + exit 0/1/2); the real script is not exercised.
"""
import atexit, hashlib, json, os, pathlib, subprocess, sys, tempfile, time
import types
import contextlib
import shlex
from unittest.mock import patch

_ledger_root = tempfile.TemporaryDirectory(prefix="window-gate-test-")
atexit.register(_ledger_root.cleanup)
os.environ["DOIT_ROOT"] = _ledger_root.name
os.environ.pop("DOIT_LEDGER_FILE", None)
os.environ["DOIT_NO_POKE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fold, window_gate, merge_queue, ghlimit  # noqa: E402

HERE = pathlib.Path(__file__).parent
FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("FAIL", msg)


def sh(args, cwd):
    p = subprocess.run(["git", "-c", "user.email=t@x", "-c", "user.name=t", *args], cwd=str(cwd),
                       capture_output=True, text=True)
    assert p.returncode == 0, f"git {args}: {p.stderr}"
    return p.stdout.strip()


FAKE = """#!/bin/bash
D="$FAKE_DIR"
if [ "$PREDEPLOY_GATE_DEPLOY_SHA" = "${FAKE_BASE:-}" ]; then
  n=base
  echo base >> "$D/base-calls"
else
  n=$(cat "$D/count" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$D/count"
  echo "$PREDEPLOY_GATE_DEPLOY_SHA|$(echo "$PREDEPLOY_GATE_CI_CHECKS" | tr '\\n' ';')" >> "$D/calls"
fi
echo "$PREDEPLOY_GATE_DISPATCH_TIMEOUT_MIN" >> "$D/timeouts"
[ -e "$D/run$n.sleep" ] && sleep "$(cat "$D/run$n.sleep")"
cat "$D/run$n.out" 2>/dev/null
exit "$(cat "$D/run$n.rc" 2>/dev/null || echo 2)"
"""

ARRAY = """REQUIRED_CHECKS=(
  "pytest"
  'pg-parity (Postgres)'
  "Auth Coverage · Secret Scan · CVE Check"
  "lint PR-changed migrations + sql/core freeze + single head"
  "TypeScript · Biome · Tests · File Sizes"
  "Repo lint guards"
)
"""


GREEN = ('{"verdict":"green","checks":{"pytest":"ran","pg-parity (Postgres)":"ran","TypeScript · Biome · Tests · File Sizes":"ran"},'
         '"new_failures":[],"run_url":"https://github.com/o/r/actions/runs/1"}')


def red(nodes, url="https://github.com/o/r/actions/runs/2"):
    return json.dumps({"verdict": "red", "checks": {"pytest": "ran"}, "new_failures": nodes, "run_url": url})


class Fx:
    def __init__(self, specs, dashboard=False, dirty=False):
        self.tempdir = tempfile.TemporaryDirectory(prefix="wg-")
        atexit.register(self.tempdir.cleanup)
        self.tmp = pathlib.Path(self.tempdir.name)
        config = self.tmp / "grading.toml"
        config.write_text("")
        os.environ["DOIT_GRADING_TOML"] = str(config)
        self.root = self.tmp / "r"
        self.origin = self.tmp / "o.git"
        sh(["init", "--bare", str(self.origin)], self.tmp)
        self.repo = self.root / "repos" / "albert-scott"
        self.repo.mkdir(parents=True)
        sh(["init", "-b", "master", str(self.repo)], self.tmp)
        sh(["remote", "add", "origin", str(self.origin)], self.repo)
        (self.repo / "api/tests").mkdir(parents=True)
        (self.repo / "api/app").mkdir(parents=True)
        (self.repo / "api/app/lib.py").write_text("X = 1\n")
        (self.repo / "api/tests/test_x.py").write_text("from api.app import lib\n")
        (self.repo / "api/tests/test_z.py").write_text("pass\n")
        (self.repo / "base.txt").write_text("b\n")
        gate = self.repo / window_gate.PREDEPLOY
        gate.parent.mkdir(parents=True)
        gate.write_text(ARRAY)
        classifier = self.repo / "scripts/ci/actions_changes_gate.sh"
        classifier.write_text("printf '%s\\n' " + shlex.quote(json.dumps({
            "checks": [window_gate.PYTEST_CHECK, window_gate.PG_CHECK],
            "unclassified": [], "everything": False, "reason": "fixture scope"})) + "\n")
        sh(["add", "-A"], self.repo); sh(["commit", "-m", "init"], self.repo)
        sh(["push", "origin", "master"], self.repo)
        self.base = sh(["rev-parse", "master"], self.repo)
        self.branches = {}
        for name, path in specs.items():
            sh(["checkout", "-q", "-b", f"br-{name}", "master"], self.repo)
            p = self.repo / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"{name}\n" + (p.read_text() if p.exists() else ""))
            sh(["add", "-A"], self.repo); sh(["commit", "-m", name], self.repo)
            self.branches[name] = f"br-{name}"
        sh(["checkout", "-q", "-b", "live-scratch", "master"], self.repo)   # the operator's own checkout
        if dirty:
            (self.repo / "base.txt").write_text("dirty\n")
            (self.repo / "staged.txt").write_text("s\n")
            sh(["add", "staged.txt"], self.repo)
        self.fake = self.tmp / "fake.sh"
        self.fake.write_text(FAKE)
        self.fdir = self.tmp / "f"
        self.fdir.mkdir()
        self.run_n("base", 0, GREEN)
        # A local gh double supplies only job metadata and logs; no network.
        bindir = self.tmp / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text("#!/usr/bin/python3\nimport os, pathlib, sys\n"
                      "args = sys.argv[1:]\n"
                      "if args == ['api', 'rate_limit']:\n"
                      " import json, time\n"
                      " print(json.dumps({'resources': {k: {'limit': 5000, 'remaining': 5000, 'reset': int(time.time()) + 3600} for k in ('core', 'search', 'graphql')}})); sys.exit(0)\n"
                      "if args[0] == 'api':\n"
                      " key = 'jobs-' + args[1].split('/runs/')[1].split('/')[0]\n"
                      " p = pathlib.Path(os.environ['FAKE_DIR']) / key\n"
                      " if not p.exists(): sys.exit(1)\n"
                      " print(p.read_text()); sys.exit(0)\n"
                      "key = ('log-' + args[args.index('--job') + 1]) if '--job' in args else ('jobs-' + args[2])\n"
                      "p = pathlib.Path(os.environ['FAKE_DIR']) / key\n"
                      "if not p.exists(): sys.exit(1)\nprint(p.read_text())\n")
        gh.chmod(0o755)
        self.path = str(bindir) + os.pathsep + os.environ.get("PATH", "")
        fold.ROOT = self.root
        fold.EVENTS = self.root / "events"
        fold.EVENTS.mkdir(parents=True)
        os.environ.update(DOIT_ROOT=str(self.root), DOIT_WINDOW_PREDEPLOY=str(self.fake), FAKE_DIR=str(self.fdir),
                          GH_LIMIT_STATE=str(self.tmp / "ghlimit.json"), DOIT_REAL_GH=str(gh),
                          DOIT_MAIN="master", DOIT_LEDGER_FILE="L-executor-0001.jsonl",
                          DOIT_GATE_LEDGER_FILE="L-executor-0001.jsonl")
        pathlib.Path(os.environ["GH_LIMIT_STATE"]).write_text(json.dumps(dict(
            checked_at=time.time(), spent_since_check=0,
            core=dict(limit=5000, remaining=5000, reset=time.time() + 3600),
            graphql=dict(limit=5000, remaining=5000, reset=time.time() + 3600))))
        os.environ.pop("FAKE_BASE", None)
        os.environ.pop("DOIT_WINDOW_GATE", None)
        os.environ.pop("DOIT_WINDOW_VERIFY_TIMEOUT_SECS", None)
        # One default service fixture for every historical red case. No network.
        import base_red
        self.known = set()
        self.base_reader = lambda project, sha: {"sha": sha, "complete": True}
        self.log_reader = lambda repo, doc: []
        self.service = types.SimpleNamespace(_owner=lambda repo: "o/r", parse_log=base_red.parse_log,
            classify=lambda project, sha, ids, **kwargs: {
                "complete": True, "pre_existing": [n for n in ids if n in self.known],
                "new": [n for n in ids if n not in self.known]})
        self.service.recheck = lambda project, sha, ids: {"red": [], "green": list(ids), "absent": [], "undetermined": []}

    def run_n(self, n, rc, out="", sleep=None):
        (self.fdir / f"run{n}.rc").write_text(str(rc))
        (self.fdir / f"run{n}.out").write_text(out + "\n")
        if sleep:
            (self.fdir / f"run{n}.sleep").write_text(str(sleep))

    def pg(self, n, nodes=(), conclusion="failure", message="AssertionError: parity", kind="FAILED"):
        rid = "100" if n == "base" else str(100 + n)
        jid = str(int(rid) + 1000)
        self.run_n(n, 0 if conclusion == "success" else 1, json.dumps({
            "verdict": "green" if conclusion == "success" else "red",
            "checks": {"pytest": "ran", window_gate.PG_CHECK: {"state": "ran", "run_id": int(rid)}},
            "new_failures": [], "run_url": "https://github.com/o/r/actions/runs/" + rid}))
        (self.fdir / ("jobs-" + rid)).write_text(json.dumps({"jobs": [{
            "name": window_gate.PG_CHECK, "databaseId": int(jid), "conclusion": conclusion}]}))
        verdict = json.loads((self.fdir / f"run{n}.out").read_text())
        verdict["checks"][window_gate.DASHBOARD_CHECK] = "ran"
        self.run_n(n, 0 if conclusion == "success" else 1, json.dumps(verdict))
        (self.fdir / ("log-" + jid)).write_text("\n".join(
            "pg-parity (Postgres)\tRun suite\t2026-10-17T00:00:00Z " + kind + " " + node + " - " + message
            for node in nodes))

    def go(self, specs=None, fixes_base=False, window="w1", after=None):
        specs = specs or list(self.branches)
        for s in specs:
            fold.append(["merge-gate-clean", s, f"branch={self.branches[s]}", "project=albert-scott"])
            fold.append(["queue-add", s, f"branch={self.branches[s]}", "project=albert-scott", "rule=operator"])
        with patch.dict(os.environ, FAKE_BASE=self.base, PATH=self.path), \
             patch.dict(sys.modules, base_red=self.service), \
             patch.object(window_gate, "base_red_snapshot", side_effect=self.base_reader), \
             patch.object(window_gate, "candidate_logs", side_effect=self.log_reader):
            result = window_gate.main_([window, "--project", "albert-scott", "--specs", ",".join(specs)]
                                       + (["--fixes-base"] if fixes_base else [])
                                       + (["--after", after] if after else []))
        printed = [p.read_text() for p in self.fdir.glob("run*.out")]
        if any("NEW failed:" in text or "NEW suite-error:" in text or
               window_gate.parse_verdict(text)[1] for text in printed):
            check(all(e.get("nodes") for e in self.events("window-dropped")),
                  "nodes_never_empty across every named red fixture")
        return result

    def calls(self):
        p = self.fdir / "calls"
        return p.read_text().splitlines() if p.exists() else []

    def events(self, typ=None):
        ev = fold.read_events()
        return [e for e in ev if typ is None or e.get("type") == typ]

    def master(self):
        return sh(["rev-parse", "refs/heads/master"], self.repo)

    def origin_refs(self):
        return sh(["for-each-ref", "--format=%(refname)"], self.origin).split()


def timeout_checks():
    """R8: resolve at call time, and pass the same budget to the fake gate."""
    def response(criterion, body):
        encoded = json.dumps(body, sort_keys=True).encode()
        print(json.dumps(dict(criterion=criterion, url="cli://window_gate/" + criterion,
                              status=0, body_sha256=hashlib.sha256(encoded).hexdigest(),
                              body_excerpt=body)))

    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "look.toml"
        absent = pathlib.Path(tmp) / "absent.toml"
        path.write_text("[window_gate]\ndispatch_timeout_min = 70\n")
        key = "PREDEPLOY_GATE_DISPATCH_TIMEOUT_MIN"
        cases = [({key: "30"}, path, 30.0), ({}, path, 70.0),
                 ({}, absent, 45.0), ({key: "30.5"}, path, 30.5)]
        actual = [window_gate.dispatch_timeout_min(env, file) for env, file, _ in cases]
        check(actual == [expected for _, _, expected in cases], "ac-timeout precedence env/toml/default/fraction")
        print("ac-timeout precedence", actual)
        response("AC1", actual)

        shipped = window_gate.dispatch_timeout_min({})
        check(shipped == 70.0, "ac-timeout shipped config")
        response("AC2", dict(dispatch_timeout_min=shipped))

        bad_env = ["", "abc", "0", "-5", "nan", "inf", "-inf", True, False, None, "1e999"]
        invalid_env = [window_gate.dispatch_timeout_min({key: value}, path) for value in bad_env]
        check(invalid_env == [70.0] * len(bad_env), "ac-timeout invalid env falls through")
        invalid_toml = []
        for text in ("0", "-5", '"x"', "true", "nan", "inf", "[]", "{}"):
            path.write_text("[window_gate]\ndispatch_timeout_min = " + text + "\n")
            invalid_toml.append(window_gate.dispatch_timeout_min({}, path))
        for text in ("[window_gate]\n", "[other]\ndispatch_timeout_min = 70\n",
                     "window_gate = 70\n", "[[window_gate]]\ndispatch_timeout_min = 70\n",
                     "[malformed", "\xff"):
            path.write_text(text)
            invalid_toml.append(window_gate.dispatch_timeout_min({}, path))
        path.write_bytes(b"\xff")
        invalid_toml.append(window_gate.dispatch_timeout_min({}, path))
        invalid_toml.append(window_gate.dispatch_timeout_min({}, absent))
        with patch("builtins.open", side_effect=PermissionError("unreadable")):
            invalid_toml.append(window_gate.dispatch_timeout_min({}, path))
        check(invalid_toml == [45.0] * len(invalid_toml), "ac-timeout invalid/unreadable toml falls through")
        response("AC5", dict(env=invalid_env, toml=invalid_toml))

        kill_times = [window_gate.verify_timeout_secs(env) for env in
                      ({}, {key: "45"}, {"DOIT_WINDOW_VERIFY_TIMEOUT_SECS": "7"})]
        check(window_gate.KILL_MARGIN_MIN == 10 and kill_times == [4800.0, 3300.0, 7.0],
              "ac-timeout kill margin and override")
        check(all(window_gate.verify_timeout_secs({"DOIT_WINDOW_VERIFY_TIMEOUT_SECS": value}) == 4800.0
                  for value in bad_env), "ac-timeout invalid kill override falls through")
        check(window_gate.verify_timeout_secs({}, absent) == 3300.0, "ac-timeout fallback kill margin")
        response("AC4", dict(kill_times=kill_times, kill_margin_min=window_gate.KILL_MARGIN_MIN))

        # Reuse the existing real-local-git fixture and fake shell script.
        path.write_text("[window_gate]\ndispatch_timeout_min = 70\n")
        fx = Fx({})
        try:
            recorded = []
            for env, file, expected in (({}, path, "70"), ({key: "30"}, path, "30"), ({}, absent, "45")):
                fx.run_n(len(recorded) + 1, 0, GREEN)
                with patch.dict(os.environ), patch.object(window_gate, "DEFAULT_TOML_PATH", file):
                    os.environ.pop(key, None)
                    os.environ.update(env)
                    result = window_gate.verify(fx.repo, fx.base, ["pytest"], "timeout")
                recorded.append((fx.fdir / "timeouts").read_text().splitlines()[-1])
                check(result["state"] == "green" and recorded[-1] == expected,
                      "ac-timeout child env " + expected)
            response("AC3", dict(child_env=recorded))
        finally:
            import shutil
            shutil.rmtree(fx.tmp)


def cap_clock_response(ac, body):
    encoded = json.dumps(body, sort_keys=True).encode()
    print(json.dumps(dict(criterion=ac, url="cli://window_gate/1145/" + ac, status=0,
                          body_sha256=hashlib.sha256(encoded).hexdigest(), body_excerpt=body)))


def held(cond, marker):
    check(cond, marker)
    if cond:
        print(marker)


def clock_checks():
    real_events, original_now = fold.EVENTS, fold.NOW
    ledger = f"L-executor-clock-{time.time_ns()}.jsonl"
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, DOIT_GATE_LEDGER_FILE=ledger), \
            patch.object(fold, "ROOT", pathlib.Path(tmp)), \
            patch.object(fold, "EVENTS", pathlib.Path(tmp) / "events"):
        calls, restored = [], []
        clock = time.time()
        for n in range(2):
            before = after = clock + n * 3
            with patch.object(window_gate, "datetime", wraps=window_gate.datetime) as dates:
                dates.now.return_value = window_gate.datetime.fromtimestamp(before, window_gate.timezone.utc)
                window_gate.ev("window-opened", "clock", values=[1, "two", None], count=n,
                               enabled=True, **({"project": "custom"} if n else {}))
            # Read the file anew after each append; never use append's return value.
            events = [json.loads(line) for line in (fold.EVENTS / ledger).read_text().splitlines()]
            stamp = window_gate.datetime.fromisoformat(events[-1]["ts"]).timestamp()
            calls.append(dict(ts=events[-1]["ts"], fresh=before - 2 <= stamp <= after + 2))
            restored.append(fold.NOW is original_now)
        clock_ok = calls[0]["ts"] != calls[1]["ts"] and all(c["fresh"] for c in calls)
        held(clock_ok, "ac-clock ts differ")
        held(all(restored), "ac-clock fold.NOW restored")
        failures = []
        for error in (RuntimeError("append failed"), SystemExit("append exited")):
            raised = False
            try:
                with patch.object(fold, "append", side_effect=error):
                    window_gate.ev("window-opened", "clock-failure")
            except (RuntimeError, SystemExit) as caught:
                raised = caught is error
            failures.append(raised and fold.NOW is original_now)
        held(all(failures), "ac-clock restored on failure")
        payload_ok = (os.environ["DOIT_LEDGER_FILE"] == ledger
                      and [e["project"] for e in events] == [window_gate.PROJECT, "custom"]
                      and [e["count"] for e in events] == [0, 1]
                      and all(e["values"] == [1, "two", None] and e["enabled"] is True for e in events))
        check(payload_ok, "clock preserves routing, project and JSON values")
    untouched = not (real_events / ledger).exists()
    held(untouched, "ac-clock real ledger untouched")
    cap_clock_response("AC4", dict(calls=calls, restored=all(restored), failures=all(failures),
                                    real_ledger_untouched=untouched, payload_preserved=payload_ok))


def cap_checks():
    key = "DOIT_WINDOW_MAX_MERGES"
    with tempfile.TemporaryDirectory() as tmp:
        path, absent = pathlib.Path(tmp) / "look.toml", pathlib.Path(tmp) / "absent.toml"
        path.write_text("[window_gate]\nmax_merges = 8\n")
        cases = [({key: value}, path, 12) for value in (12, "12", 12.0, "12.0", "1.2e1")]
        cases += [({}, path, 8), ({}, absent, 10)]
        actual = [window_gate.max_merges(env, file) for env, file, _ in cases]
        precedence = actual == [v for _, _, v in cases] and all(type(v) is int for v in actual)
        held(precedence, "ac-cap precedence")
        bad = ["abc", "0", "-5", "nan", "inf", "-inf", "10.5", True, False, None, "", "1e999", [], {}]
        env_values = [window_gate.max_merges({key: value}, path) for value in bad]
        toml_values = []
        for text in ('"abc"', "0", "-5", "nan", "inf", "10.5", "true", "false", '""', "[]", "{}"):
            path.write_text("[window_gate]\nmax_merges = " + text + "\n")
            toml_values.append(window_gate.max_merges({}, path))
        for text in ("[window_gate]\n", "[other]\nmax_merges = 8\n", "window_gate = 8\n",
                     "[[window_gate]]\nmax_merges = 8\n", "[malformed"):
            path.write_text(text)
            toml_values.append(window_gate.max_merges({}, path))
        path.write_bytes(b"\xff")
        toml_values.append(window_gate.max_merges({}, path))
        toml_values.append(window_gate.max_merges({}, absent))
        with patch("builtins.open", side_effect=PermissionError("unreadable")):
            toml_values.append(window_gate.max_merges({}, path))
        path.write_text("[window_gate]\nmax_merges = 8.0\n")
        check(type(window_gate.max_merges({}, path)) is int and window_gate.max_merges({}, path) == 8,
              "integer-valued TOML float accepted")
        invalid = (env_values == [8] * len(bad) and toml_values == [10] * len(toml_values)
                   and all(type(v) is int for v in env_values + toml_values))
        held(invalid, "ac-cap invalid falls through")
        cap_clock_response("AC1", dict(precedence=precedence, invalid_falls_through=invalid))
    import tomllib
    configured = tomllib.loads((HERE.parent / 'look.toml').read_text())['window_gate']['max_merges']
    shipped = window_gate.max_merges({})
    check(type(shipped) is int and shipped == configured, "shipped cap follows versioned config")
    print(shipped, type(shipped).__name__)
    cap_clock_response("AC3", dict(value=shipped, type=type(shipped).__name__))

    def boundary(size, override=None):
        with patch.dict(os.environ):
            os.environ.pop(key, None)
            if override is not None:
                os.environ[key] = str(override)
            fx = Fx({f"S{n}": f"s{n}.txt" for n in range(size)})
            try:
                fx.run_n(1, 0, GREEN)
                try:
                    rc = fx.go()
                except SystemExit as error:
                    cap = shipped if override is None else override
                    return (size > cap and str(error) == f"gate-window: {size} merges exceeds the window cap of {cap}"
                            and fx.master() == fx.base and not fx.calls() and not fx.events("shipped"))
                return (size <= (shipped if override is None else override) and rc == 0
                        and len(fx.events("shipped")) == size and len(fx.calls()) == 1)
            finally:
                fx.tempdir.cleanup()

    accepted, refused = boundary(shipped), boundary(shipped + 1)
    env_refused = boundary(7, 6)
    held(accepted and refused, f"ac-cap boundary {shipped} ok {shipped + 1} refused")
    held(env_refused, "ac-cap env 6 refuses 7")
    cap_clock_response("AC2", dict(cap=shipped, at_cap_accepted=accepted, above_cap_refused=refused,
                                  env_six_refuses_seven=env_refused))


def parity_checks():
    def response(ac, body):
        encoded = json.dumps(body, sort_keys=True).encode()
        print(json.dumps(dict(criterion=ac, url="cli://window_gate/1057/" + ac,
                              status=0, body_sha256=hashlib.sha256(encoded).hexdigest(), body_excerpt=body)))

    node = "api/tests/test_x.py::test_y"
    fx = Fx({"A": "a.txt"})
    fx.pg(1, [node])
    rc = fx.go()
    check(rc == 2 and fx.master() == fx.base and not fx.events("shipped"), "1057 AC1a pytest green pg red blocks")
    response("AC1a", dict(exit=rc, master_unmoved=fx.master() == fx.base))

    fx = Fx({"A": "a.txt", "B": "api/tests/test_x.py", "C": "c.txt"})
    fx.pg(1, [node])
    fx.pg(2, conclusion="success")
    rc = fx.go()
    shipped = [e["subject"] for e in fx.events("shipped")]
    check(rc == 1 and shipped == ["A", "C"] and len(fx.calls()) == 2, "1057 AC1b pg owner only, one retry")
    check([e["subject"] for e in fx.events("window-dropped")] == ["B"], "1057 AC1b only B dropped")
    response("AC1b", dict(exit=rc, shipped=shipped, candidate_runs=len(fx.calls())))

    fx = Fx({"A": "api/tests/test_x.py", "B": "b.txt"})
    fx.pg("base", [node])
    fx.pg(1, [node])
    rc = fx.go(fixes_base=True)
    check(rc == 2 and not fx.events("window-dropped") and fx.master() == fx.base,
          "1057 AC1c identical base failure pre-existing, no drop or advance")
    check(len(fx.events("window-pre-existing")) == 1, "1057 AC1c pre-existing count and reason surfaced")
    response("AC1c", dict(exit=rc, drops=len(fx.events("window-dropped")), pre_existing=1))

    for fixes in (False, True):
        fx = Fx({"A": "a.txt"})
        fx.pg("base", [node])
        fx.pg(1, conclusion="success")
        rc = fx.go(fixes_base=fixes)
        check(rc == (0 if fixes else 2), "1057 AC1d red base fixes flag " + str(fixes))
        check(bool(fx.events("window-opened")) == fixes, "1057 AC1d refused before window opens")
        check(fixes or (fx.master() == fx.base and not fx.calls()
                        and fx.events("escalation-blocking")[0]["why"].startswith("base-not-releasable")),
              "1057 AC1d base-not-releasable escalation")
        response("AC1d", dict(fixes_base=fixes, exit=rc, opened=bool(fx.events("window-opened"))))

    # Same node but changed failure signature is NEW, not pre-existing.
    fx = Fx({"A": "api/tests/test_x.py", "B": "b.txt"})
    fx.pg("base", [node])
    fx.pg(1, [node], message="ValueError: new regression")
    fx.pg(2, conclusion="success")
    check(fx.go(fixes_base=True) == 1 and [e["subject"] for e in fx.events("window-dropped")] == ["A"],
          "1057 changed pg failure signature drops owner")

    fx = Fx({"A": "api/tests/test_x.py", "B": "b.txt"})
    fx.pg(1, [node + "[param]"], kind="ERROR")
    fx.pg(2, conclusion="success")
    check(fx.go() == 1 and fx.events("window-dropped")[0]["nodes"] == [node + "[param]"],
          "1057 pg ERROR and parameterized node id retained")

    for broken in ("missing", "invalid-json", "absent-job", "duplicate-job", "cancelled", "missing-log"):
        fx = Fx({"A": "a.txt"})
        fx.pg(1, [node])
        jobs = fx.fdir / "jobs-101"
        if broken == "missing":
            jobs.unlink()
        elif broken == "invalid-json":
            jobs.write_text("not json")
        elif broken in ("absent-job", "duplicate-job"):
            data = json.loads(jobs.read_text())
            data["jobs"] = [] if broken == "absent-job" else data["jobs"] * 2
            jobs.write_text(json.dumps(data))
        elif broken == "cancelled":
            data = json.loads(jobs.read_text())
            data["jobs"][0]["conclusion"] = "cancelled"
            jobs.write_text(json.dumps(data))
        else:
            (fx.fdir / "log-1101").unlink()
        rc = fx.go()
        check(rc == 2 and fx.master() == fx.base and not fx.events("window-dropped"),
              "1057 pg evidence fails closed without dropping specs: " + broken)

    for dash in (False, True):
        fx = Fx({"A": "dashboard/a.ts" if dash else "a.txt"})
        fx.run_n(1, 0, GREEN)
        # Read the base blob, never a dirty working-tree edit.
        (fx.repo / window_gate.PREDEPLOY).write_text("REQUIRED_CHECKS=(unclassified)\n")
        rc = fx.go()
        checks = fx.calls()[0].split("|", 1)[1].strip(";").split(";") if fx.calls() else []
        expected = ["pytest", "pg-parity (Postgres)"] + ([window_gate.DASHBOARD_CHECK] if dash else [])
        check(rc == 0 and checks == expected, "1057 AC2 exact ci-verify subset " + str(dash))
        response("AC2", dict(dashboard=dash, checks=checks, exit=rc))

    for text, reason in ((ARRAY.replace('  "Repo lint guards"', '  "New required check"'), "required-checks-unclassified"),
                         ("REQUIRED_CHECKS=(\n  \"pytest\"\n", "required-checks-unreadable"),
                         ('REQUIRED_CHECKS=("$DYNAMIC")\n', "required-checks-unreadable"),
                         ('# no required array\n', "required-checks-unreadable")):
        fx = Fx({})
        (fx.repo / window_gate.PREDEPLOY).write_text(text)
        sh(["add", window_gate.PREDEPLOY], fx.repo)
        sh(["commit", "-m", "required array fixture"], fx.repo)
        fx.base = sh(["rev-parse", "HEAD"], fx.repo)
        sh(["update-ref", "refs/heads/master", fx.base], fx.repo)
        sh(["checkout", "-b", "br-A"], fx.repo)
        (fx.repo / "a.txt").write_text("A\n")
        sh(["add", "."], fx.repo)
        sh(["commit", "-m", "A"], fx.repo)
        fx.branches["A"] = "br-A"
        rc = fx.go()
        check(rc == 2 and fx.master() == fx.base and not fx.calls()
              and fx.events("escalation-blocking")[0]["why"].startswith(reason), "1057 AC2 " + reason)
        response("AC2", dict(reason=reason, exit=rc, master_unmoved=fx.master() == fx.base))


def inherited_base_pg_checks():
    """Real first-parent history and classifier, with only CI replaced by fixtures."""
    def fixture(base_path="dashboard/base.ts", intermediate=None):
        fx = Fx({})
        classifier = fx.repo / "scripts/ci/actions_changes_gate.sh"
        classifier.write_text("""python3 - <<'PY'
import json, os, pathlib
paths = pathlib.Path(os.environ['CHANGED_PATHS_FILE']).read_bytes().split(b'\\0')
checks = ['pytest']
if any(p.startswith(b'api/tests/') for p in paths):
    checks.append('pg-parity (Postgres)')
print(json.dumps(dict(checks=checks, unclassified=[], everything=False, reason='fixture paths')))
PY
""")
        sh(["add", "-A"], fx.repo); sh(["commit", "-m", "path classifier"], fx.repo)
        parent = sh(["rev-parse", "HEAD"], fx.repo)
        for name in ([intermediate, base_path] if intermediate else [base_path]):
            path = fx.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("base change\n")
            sh(["add", "-A"], fx.repo); sh(["commit", "-m", name], fx.repo)
        fx.base = sh(["rev-parse", "HEAD"], fx.repo)
        sh(["update-ref", "refs/heads/master", fx.base], fx.repo)
        sh(["checkout", "-b", "br-A"], fx.repo)
        (fx.repo / "api/tests/new.py").write_text("candidate\n")
        sh(["add", "-A"], fx.repo); sh(["commit", "-m", "PG-relevant candidate"], fx.repo)
        fx.branches["A"] = "br-A"
        sh(["checkout", "live-scratch"], fx.repo)
        fx.run_n("base", 2, json.dumps(dict(verdict="undetermined", checks={
            window_gate.PG_CHECK: {"state": "skipped"}})))
        fx.pg(1, conclusion="success")
        fx.pg(2, conclusion="success")
        return fx, parent

    fx, parent = fixture()
    rc = fx.go()
    inherited = fx.events("window-base-pg-inherited")
    check(rc == 0 and len(fx.events("shipped")) == 1 and len(inherited) == 1 and
          inherited[0]["base"] == fx.base[:12] and inherited[0]["from_sha"] == parent[:12],
          "inherited PG: skipped base and green parent advance with audit event")
    check(len(fx.calls()) == 2 and fx.calls()[0] == parent + "|" + window_gate.PG_CHECK + ";",
          "inherited PG: parent verified with PG only before candidate")

    fx, _ = fixture(base_path="api/tests/base.py")
    rc = fx.go()
    check(rc == 2 and fx.master() == fx.base and not fx.calls() and
          not fx.events("window-base-pg-inherited") and
          fx.events("escalation-blocking")[0]["why"] == "base-not-releasable: pg-parity is undetermined",
          "inherited PG: PG-relevant parent-to-base diff blocks before parent verification")

    for state in ("red", "undetermined"):
        fx, parent = fixture()
        if state == "red":
            fx.pg(1, ["api/tests/test_z.py::old"])
        else:
            fx.run_n(1, 2, json.dumps(dict(verdict="undetermined", checks={})))
        rc = fx.go()
        check(rc == 2 and fx.master() == fx.base and len(fx.calls()) == 1 and
              not fx.events("window-base-pg-inherited") and not fx.events("window-opened") and
              fx.events("escalation-blocking")[0]["why"] == "base-not-releasable: pg-parity is " + state,
              "inherited PG: parent " + state + " blocks without searching older history")

    for state, fixes in (("green", False), ("red", False), ("undetermined", True)):
        fx, _ = fixture()
        if state == "green":
            fx.pg("base", conclusion="success")
        elif state == "red":
            fx.pg("base", ["api/tests/test_z.py::old"])
        real_git = window_gate.git
        with patch.object(window_gate, "git", wraps=real_git) as calls:
            rc = fx.go(fixes_base=fixes)
        check(rc == (0 if state == "green" else 2) and
              not any("--max-count=31" in call.args for call in calls.call_args_list) and
              not fx.events("window-base-pg-inherited"),
              "inherited PG: no ancestor walk for base " + state + " fixes=" + str(fixes))

    # A multi-spec scoped window has intermediate merge commits with no CI.
    # Its ledger identifies the prior tested tip, as in the live c32r case.
    fx, parent = fixture(intermediate="dashboard/intermediate.ts")
    window_gate.ev("window-verified", "prior", sha=fx.base, verified_on=parent,
                   scope=[window_gate.PYTEST_CHECK, window_gate.DASHBOARD_CHECK])
    rc = fx.go()
    check(rc == 0 and len(fx.calls()) == 2 and fx.calls()[0].startswith(parent + "|") and
          fx.events("window-base-pg-inherited")[0]["from_sha"] == parent[:12],
          "inherited PG: scoped window skips its untested intermediate commits")

    fx, parent = fixture(intermediate="api/tests/intermediate.py")
    window_gate.ev("window-verified", "prior", sha=fx.base, verified_on=parent,
                   scope=[window_gate.PYTEST_CHECK, window_gate.DASHBOARD_CHECK])
    rc = fx.go()
    check(rc == 2 and not fx.calls() and not fx.events("window-base-pg-inherited"),
          "inherited PG: ledger omission cannot override PG-relevant classifier result")

    print("inherited_base_pg_checks: complete")


def main():
    inherited_base_pg_checks()
    scope_checks()
    # AC1 + AC7: green window; dirty live checkout untouched
    fx = Fx({"A": "a.txt", "B": "b.txt", "C": "c.txt"}, dirty=True)
    pre = (sh(["status", "--porcelain"], fx.repo), (fx.repo / ".git/index").read_bytes())
    fx.run_n(1, 0, GREEN)
    rc = fx.go()
    check(rc == 0, f"AC1 exit 0, got {rc}")
    cand = fx.calls()[0].split("|")[0] if fx.calls() else None
    check(cand == fx.master() and cand != fx.base, "AC1 master == candidate sha")
    check(len(fx.events("shipped")) == 3 and all(e["sha"] == cand for e in fx.events("shipped")), "AC1 three shipped events")
    check(len(fx.events("window-verified")) == 1, "AC1 one window-verified")
    check(len(fx.calls()) == 1 and fx.calls()[0].endswith("|pytest;pg-parity (Postgres);"), "AC1/AC5 one candidate dispatch, pytest and pg-parity")
    post = (sh(["status", "--porcelain"], fx.repo), (fx.repo / ".git/index").read_bytes())
    check(pre == post, "AC7 live status and index untouched")
    check((fx.repo / "base.txt").read_text() == "dirty\n", "AC7 dirty file intact")
    check(not any("ci-verify" in r or "window" in r for r in fx.origin_refs()), "AC9 ci-verify ref deleted after run")
    check(not window_gate.lock_path().exists(), "lock released")
    check(subprocess.run(["git", "-C", str(fx.repo), "rev-parse", "--verify", "-q", "refs/heads/window/w1"]).returncode != 0,
          "window ref removed")

    # Real predeploy_gate.sh verdict shape: each check is {"state": "ran", ...}, not a bare
    # string. Before the fix this green verdict was read as red with nodes=[] (windows
    # win-20261010-0101 and -0201 dropped every spec on a green CI run).
    fx = Fx({"A": "a.txt"})
    real = json.dumps({"verdict": "green", "sha": "x", "new_failures": [],
                       "checks": {window_gate.PG_CHECK: "ran", "pytest": {"state": "ran", "run_id": 38010269666,
                                             "url": "https://github.com/o/r/actions/runs/3/job/4"}}})
    fx.run_n(1, 0, real)
    rc = fx.go()
    check(rc == 0 and fx.master() != fx.base, f"real-shape green verdict ships (rc={rc})")
    check(not fx.events("window-dropped"), "real-shape green verdict drops nothing")

    # AC2: collection error (no verdict line, gate says red): nothing ships
    fx = Fx({"A": "a.txt", "B": "b.txt"})
    fx.run_n(1, 1, "ERROR collecting api/tests/test_q.py")
    rc = fx.go()
    check(rc == 2 and fx.master() == fx.base, "AC2 non-zero, master unmoved")
    check(not fx.events("shipped"), "AC2 nothing shipped")
    check(len(fx.calls()) == 1, "AC2 unnamed failure holds without a second run")

    # AC3: a new failure drops only the owner; two dispatches
    fx = Fx({"A": "a.txt", "B": "api/tests/test_x.py", "C": "c.txt"})
    fx.run_n(1, 1, red(["api/tests/test_x.py::test_y"]))
    fx.run_n(2, 0, GREEN)
    rc = fx.go()
    shipped = sorted(e["subject"] for e in fx.events("shipped"))
    check(rc == 1 and shipped == ["A", "C"], f"AC3 A,C ship (rc={rc} shipped={shipped})")
    d = fx.events("window-dropped")
    check(len(d) == 1 and d[0]["subject"] == "B" and d[0]["nodes"] == ["api/tests/test_x.py::test_y"]
          and d[0]["run_url"].endswith("/2"), "AC3 B dropped with node id and url")
    c = [x.split("|")[0] for x in fx.calls()]
    check(len(c) == 2 and c[0] != c[1] and fx.master() == c[1], "AC3 two dispatches, master at second sha")
    ls = sh(["ls-tree", "-r", "--name-only", fx.master()], fx.repo).split()
    check("a.txt" in ls and "c.txt" in ls and "api/tests/test_x.py" in ls, "AC3 second candidate has A and C")

    # AC3b: ownership by import closure (spec touches api/app/lib.py, test imports it)
    fx = Fx({"A": "a.txt", "B": "api/app/lib.py"})
    fx.run_n(1, 1, red(["api/tests/test_x.py::test_y"]))
    fx.run_n(2, 0, GREEN)
    rc = fx.go()
    check(rc == 1 and [e["subject"] for e in fx.events("window-dropped")] == ["B"]
          and [e["subject"] for e in fx.events("shipped")] == ["A"], "AC3b import-closure owner dropped")

    # L-spec-1127 R3.4: bisect unattributed reds; ship the first green half.
    fx = Fx({"A": "a.txt", "B": "b.txt", "C": "c.txt"})
    fx.run_n(1, 1, red(["api/tests/test_z.py::test_q"]))
    fx.run_n(2, 0, GREEN)
    rc = fx.go()
    check(rc == 1 and fx.master() != fx.base and
          [e["subject"] for e in fx.events("shipped")] == ["A"], "AC4 first green half ships")
    check(not fx.events("window-dropped") and
          [r['spec'] for r in merge_queue.queue(fold.read_events())] == ["B", "C"], "AC4 unresolved half pending")
    check([e["halves"] for e in fx.events("window-bisected")] == [[["A"], ["B", "C"]]] and
          len(fx.events("escalation-blocking")) == 1 and len(fx.calls()) == 3, "AC4 bisect with unresolved-half escalation")

    # AC4b: independently red singleton after owner removal; no unsafe advance
    fx = Fx({"A": "a.txt", "B": "api/tests/test_x.py"})
    fx.run_n(1, 1, red(["api/tests/test_x.py::test_y"]))
    fx.run_n(2, 1, red(["api/tests/test_z.py::test_q"]))
    rc = fx.go()
    check(rc == 2 and fx.master() == fx.base and len(fx.calls()) == 2 and not fx.events("shipped"),
          "AC4b red singleton rejected after its own second judgment")
    check(len(fx.events("escalation-blocking")) == 1, "AC4b escalation")

    # AC5: dashboard check only when dashboard/ changed
    fx = Fx({"A": "dashboard/x.ts", "B": "b.txt"})
    fx.run_n(1, 0, GREEN)
    fx.go()
    check(fx.calls() and "TypeScript · Biome · Tests · File Sizes" in fx.calls()[0], "AC5 dashboard dispatched when needed")

    # AC6: reuse, not copy
    src = (HERE / "window_gate.py").read_text()
    import re
    check(not re.search(r"workflow run|gh run list|git/refs", src), "AC6 no own dispatch/poll")
    check("predeploy_gate.sh" in src, "AC6 invokes predeploy_gate.sh")

    # AC9: stuck run -> ref deleted, master unmoved, lock released
    fx = Fx({"A": "a.txt"})
    fx.run_n(1, 0, GREEN, sleep=30)
    os.environ["DOIT_WINDOW_VERIFY_TIMEOUT_SECS"] = "1"
    t0 = time.time()
    rc = fx.go()
    check(rc == 2 and fx.master() == fx.base and time.time() - t0 < 25, "AC9 times out, master unmoved")
    check(not any("ci-verify" in r for r in fx.origin_refs()) and not window_gate.lock_path().exists(), "AC9 ref + lock cleaned")
    check(not fx.events("shipped") and not fx.events("window-dropped"), "AC9 undetermined drops nothing")

    # lock held by a live pid -> exit 3
    fx = Fx({"A": "a.txt"})
    window_gate.lock_path().parent.mkdir(parents=True, exist_ok=True)
    window_gate.lock_path().write_text(str(os.getppid()))
    check(fx.go() == 3 and not fx.calls(), "lock held -> exit 3")
    window_gate.lock_path().unlink()

    # merge conflict -> merge-conflict event, others still verified
    fx = Fx({"A": "base.txt", "B": "base.txt", "C": "c.txt"})
    fx.run_n(1, 0, GREEN)
    rc = fx.go()
    check(rc == 1 and [e["subject"] for e in fx.events("merge-conflict")] == ["B"]
          and sorted(e["subject"] for e in fx.events("shipped")) == ["A", "C"], "conflict: B re-dispatched, A and C ship")

    # R8 bypass + wrong project
    fx = Fx({"A": "a.txt"})
    os.environ["DOIT_WINDOW_GATE"] = "off"
    rc = fx.go()
    check(rc == 4 and len(fx.events("window-gate-bypassed")) == 1 and fx.master() == fx.base, "R8 bypass logged, no merge")
    os.environ.pop("DOIT_WINDOW_GATE")
    try:
        window_gate.main_(["w2", "--project", "other"]); check(False, "other project refused")
    except SystemExit:
        pass

    # AC8: executor contract mentions every exit code and the bypass
    ex = (HERE.parent / "agents" / "executor.md").read_text()
    for token in ("**0**", "**1**", "**2**", "**3**", "**4**", "DOIT_WINDOW_GATE=off", "doit gate-window"):
        check(token in ex, f"AC8 executor.md names {token}")

    # --help
    check(window_gate.main_(["--help"]) == 0, "help exits 0")

    if FAILS:
        print(f"{len(FAILS)} failure(s)"); sys.exit(1)
    print("test_window_gate ok")


def rechain_checks():
    """L-spec-0932 ac3f/ac4/ac5/ac6/ac9, preserving the existing checks above."""
    import shutil
    from unittest.mock import patch
    from test_rechain import Fixture, response
    import rechain

    def fixture(names, extra=False):
        fx = Fx({})
        with tempfile.TemporaryDirectory() as seed:
            sf = Fixture(seed)
            for path in sh(["ls-tree", "-r", "--name-only", sf.base], sf.repo).splitlines():
                p = fx.repo / path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(rechain.blob(sf.repo, sf.base, path))
        sh(["add", "."], fx.repo)
        sh(["commit", "-m", "migration fixture"], fx.repo)
        fx.base = sh(["rev-parse", "HEAD"], fx.repo)
        sh(["update-ref", "refs/heads/master", fx.base], fx.repo)
        sf = Fixture.__new__(Fixture)
        sf.repo, sf.base, sf.events = fx.repo, fx.base, []
        for name in names:
            spec, branch, _ = sf.branch(name)
            fx.branches[spec] = branch
        if extra:
            sh(["checkout", "-b", "br-X", fx.base], fx.repo)
            (fx.repo / "api/tests/test_x.py").write_text("pass # X\n")
            sh(["add", "."], fx.repo)
            sh(["commit", "-m", "X"], fx.repo)
            fx.branches["X"] = "br-X"
        sh(["checkout", "live-scratch"], fx.repo)
        return fx, sf

    cases = ["green", "no-cap", "owner-X", "owner-B", "second-red", "fingerprint-fail", "off", "unset"]
    # L-spec-1534: the actual child fails on each prepare entry point.
    from contextlib import redirect_stdout
    from io import StringIO
    for scenario in ("initial", "partial", "rebuilt", "bisect", "bisect-none"):
        fx, sf = fixture(["a"], extra=scenario != "initial")
        env = sf.scripts(fx.tmp)
        env["DOIT_WINDOW_RECHAIN"] = "on"
        script = fx.tmp / "fp.py"
        original = script.read_text()
        fail_on = 1 if scenario in ("initial", "partial") else 2
        script.write_text('import pathlib, sys\n'
                          'counter = pathlib.Path(' + repr(str(fx.tmp / "fp-count")) + ')\n'
                          'count = int(counter.read_text()) + 1 if counter.exists() else 1\n'
                          'counter.write_text(str(count))\n'
                          'if count == ' + str(fail_on) + ':\n'
                          '    sys.stderr.write("fixture fingerprint failed\\n")\n'
                          '    sys.exit(2)\n' + original)
        if scenario == "rebuilt":
            fx.run_n(1, 1, red(["api/tests/test_x.py::test_x"]))
        elif scenario.startswith("bisect"):
            fx.run_n(1, 1, red(["unowned.py::test_unknown"]))
            fx.run_n(2, 1 if scenario == "bisect-none" else 0,
                     red(["api/tests/test_x.py::test_x"]) if scenario == "bisect-none" else GREEN)
        else:
            fx.run_n(1, 0, GREEN)
        stdout = StringIO()
        with patch.dict(os.environ, env), redirect_stdout(stdout):
            rc = fx.go()
        declined = [e for e in fx.events("rechain-declined") if e["subject"] == "a"]
        forbidden = [e for e in fx.events() if e["subject"] == "a" and
                     e["type"] in ("merge-conflict", "window-dropped", "shipped", "rejected-criterion")]
        expected_rc = 1 if scenario in ("partial", "bisect") else 2
        check(rc == expected_rc and len(declined) == 1 and not forbidden and
              declined[0].get("stderr_tail") == ["fixture fingerprint failed"] and
              declined[0].get("stage") == "fingerprint" and
              declined[0]["why"] == "final-tree fingerprint or private PG failed" and
              "re-chain failed for a" in stdout.getvalue(), "rechain-failed is not a conflict " + scenario)
        if rc == 2:
            why = fx.events("escalation-blocking")[-1]["why"]
            check(why.startswith("re-chain failed") and "conflicts" not in why and
                  rechain.FINGERPRINT not in why, "rechain failure escalation " + scenario)
        else:
            check([e["subject"] for e in fx.events("shipped")] == ["X"], "rechain partial ships plain " + scenario)
        check("a" in [row["spec"] for row in merge_queue.queue(fold.read_events())],
              "rechain failure stays queued " + scenario)
        if scenario.startswith("bisect"):
            check(bool(fx.events("window-bisected")), "rechain failure reached half " + scenario)
        response("1534-AC4-AC5", dict(scenario=scenario, exit=rc, stage="fingerprint",
                                      stderr_tail=["fixture fingerprint failed"], conflicts=0,
                                      migration_outcomes=0, pending=True))
        fx.tempdir.cleanup()

    fx = Fx({"A": "base.txt", "B": "base.txt"})
    fx.run_n(1, 0, GREEN)
    check(fx.go() == 1 and any(e["subject"] == "B" and "base.txt" in e["files"]
                              for e in fx.events("merge-conflict")), "1534 genuine conflict stays conflict")
    response("1534-AC5-conflict", dict(real_conflict=True, files=["base.txt"]))
    fx.tempdir.cleanup()
    for case in cases:
        names = ["a", "b", "c", "d"] if case == "no-cap" else ["a", "b", "c"] if case == "green" else ["a", "b"]
        fx, sf = fixture(names, extra=case in ("no-cap", "owner-X", "owner-B", "second-red", "fingerprint-fail"))
        env = sf.scripts(fx.tmp, fail=case == "fingerprint-fail")
        env["DOIT_WINDOW_RECHAIN"] = "off" if case == "off" else "on"
        before = sh(["status", "--porcelain"], fx.repo)
        refs = {b: sh(["rev-parse", b], fx.repo) for b in fx.branches.values()}
        try:
            with patch.dict(os.environ, env):
                if case == "unset":
                    os.environ.pop("DOIT_WINDOW_RECHAIN", None)
                fx.run_n(1, 1 if case in ("owner-X", "owner-B", "second-red") else 0,
                         red(["api/tests/test_x.py::test_x"]) if case == "owner-X" else
                         red([rechain.TEST + "::test_head"]) if case in ("owner-B", "second-red") else GREEN)
                fx.run_n(2, 1 if case == "second-red" else 0,
                         red([rechain.TEST + "::test_head"]) if case == "second-red" else GREEN)
                rc = fx.go()
            shipped = [e["subject"] for e in fx.events("shipped")]
            fps = fx.events("rechain-fingerprint")
            calls = (fx.tmp / "calls").read_text().splitlines() if (fx.tmp / "calls").exists() else []
            fp_calls = [c for c in calls if c.startswith("fingerprint ")]
            if case == "green":
                check(rc == 0 and shipped == names and len(fx.calls()) == 1, "ac4 three migrations one CI")
                check(len(fp_calls) == 1 and len(fps) == 1, "ac4 final fingerprint once")
                check([e["subject"] for e in fx.events("rechained")] == ["b", "c"], "ac4 B C rechained")
                check([e["subject"] for e in fx.events("shipped") if e.get("rechained")] == ["b", "c"], "ac4 shipped rechain marker")
                check(rechain.routed_head(fx.repo, fx.master()) == "c", "ac4 one final head C")
                for child, parent in [("a", "h0"), ("b", "a"), ("c", "b")]:
                    check(rechain.revision(rechain.text(fx.repo, fx.master(), rechain.VERSIONS + child + ".py")) == (child, parent), "ac4 chain " + child)
                commits = sh(["rev-list", "--first-parent", fx.base + ".." + fx.master()], fx.repo).splitlines()
                check(len(commits) == 4 and fps[0]["sha"] == fx.master(), "ac4 three merges one fixup before CI")
            elif case == "no-cap":
                check(shipped == names + ["X"], "all explicit migration candidates integrate")
                check(not fx.events("window-deferred") and not fx.events("window-dropped"), "no migration-cap deferral")
            elif case == "owner-X":
                check(rc == 1 and shipped == names and len(fx.calls()) == 2, "ac6 X drops migrations stay")
                check([e["subject"] for e in fx.events("window-dropped")] == ["X"], "ac6 owners mapping")
            elif case in ("owner-B", "second-red"):
                check(len(fx.calls()) == 2 and len(fp_calls) == 1,
                      "ac6 shared migration owners removed before verified nonmigration retry")
                drops = fx.events("window-dropped")
                check([e["subject"] for e in drops] == names + (["X"] if case == "second-red" else []) and
                      all(e["reason"] == "owner" and e["owned_nodes"] == [rechain.TEST + "::test_head"]
                          for e in drops[:2]), "ac6 both demonstrated migration owners drop")
                check((rc == 1 and shipped == ["X"] and rechain.routed_head(fx.repo, fx.master()) == "h0") if case == "owner-B" else
                      (rc == 2 and fx.master() == fx.base and not shipped), "ac6 verified remainder preserves migration safety " + case)
            elif case == "fingerprint-fail":
                check(rc == 1 and shipped == ["X"] and len(fx.calls()) == 1, "ac3f fingerprint failure rebuilds without migrations")
                check(set(e["subject"] for e in fx.events("rechain-declined")) == set(names), "ac3f all migration declines")
                check(rechain.routed_head(fx.repo, fx.master()) == "h0", "ac3f unchanged migration candidate")
            elif case == "unset":
                check(rc == 0 and shipped == names and len(fps) == 1 and
                      rechain.routed_head(fx.repo, fx.master()) == "b", "unset re-chains by default")
            else:
                check(rc == 1 and shipped == ["a"] and not fps and not fx.events("rechained"), "ac9 disabled behavior " + case)
                check([e["subject"] for e in fx.events("merge-conflict")] == ["b"], "ac9 disabled migration conflict")
            check(before == sh(["status", "--porcelain"], fx.repo) == "", "ac8 clean worktree preserved " + case)
            check(refs == {b: sh(["rev-parse", b], fx.repo) for b in refs}, "ac8 builder refs unchanged " + case)
            check(all(not pathlib.Path(c.split(" ", 1)[1]).exists() for c in calls), "ac8 disposable resources gone " + case)
            print("ac3f/ac4/ac5/ac6/ac9", case, "checked")
            criterion = {"green": "AC4", "no-cap": "AC5", "owner-X": "AC6",
                         "owner-B": "AC6", "second-red": "AC6", "fingerprint-fail": "AC3",
                         "off": "AC9", "unset": "AC9"}[case]
            response(criterion, dict(case=case, exit=rc, shipped=shipped, ci_calls=len(fx.calls()),
                                     fingerprint_calls=len(fp_calls), fixups=len(fps),
                                     deferred=[e["subject"] for e in fx.events("window-deferred")],
                                     dropped=[e["subject"] for e in fx.events("window-dropped")]))
        finally:
            shutil.rmtree(fx.tmp)


def attribution_checks():
    """L1110: real temporary git/ledger, stubbed external evidence only."""
    import base_red, io, re, zipfile
    from unittest.mock import Mock

    def response(case, acs, body):
        encoded = json.dumps(body, sort_keys=True).encode()
        check(not FAILS, case + " has no failed assertions")
        print(json.dumps(dict(case=case, criteria=acs, url="cli://window_gate/1110/" + case,
                              status=0 if not FAILS else 1,
                              body_sha256=hashlib.sha256(encoded).hexdigest(), body_excerpt=body)))

    v, s, p, pg = ("dashboard/src/a.test.ts::suite > case", "dashboard/src/b.ts",
                    "api/tests/test_x.py::t", "api/tests/test_pg.py::t")
    output = f"NEW failed: {v}\nNEW suite-error: {s}\nNEW failed: {p}"
    step = "pg-parity::Run suite"
    fx = Fx({"A": "a.txt"})
    doc = {"checks": {window_gate.PG_CHECK: {"run_id": 10}}}
    res = dict(output=output, nodes=[], doc=doc, pg_failures={})
    with patch.object(window_gate, "candidate_logs", return_value=[
            ("10", "pg-parity\tRun suite\t##[error]failed api/tests/test_pg.py")]), \
         patch.object(base_red, "parse_log", wraps=base_red.parse_log) as parser:
        parsed = window_gate.candidate_failures(fx.repo, res)
    check({k: parsed[k] for k in base_red.KINDS} == {
        "pytest": {p}, "vitest": {v}, "suite-error": {s}, "pg-parity": {step}}, "ids_all_kinds equality")
    check(parser.call_count == 2, "ids_all_kinds shared parser consulted for output and zip")
    response("ids_all_kinds", ["AC1"], {k: sorted(parsed[k]) for k in base_red.KINDS})

    # Canonical identity must survive classification and both event paths.
    for known in (False, True):
        fx = Fx({"A": "api/tests/test_pg.py", "B": "b.txt"})
        fx.pg(1, [pg])
        # Fx.pg creates a real per-run signature; zip also names the same step.
        fx.log_reader = lambda repo, doc: [("101", "pg-parity\tRun suite\t##[error]failed")]
        fx.known = {pg} if known else set()
        classify = Mock(wraps=fx.service.classify)
        fx.service.classify = classify
        fx.run_n(2, 0, GREEN)
        rc = fx.go()
        check(classify.call_args_list[0].args[2] == [pg], "pg_dual_source classify canonical node once")
        events = fx.events("window-pre-existing" if known else "window-dropped")
        check(events[0]["ids" if known else "nodes"] == [pg], "pg_dual_source event canonical node once")
        check(rc == (0 if known else 1), "pg_dual_source outcome")
    response("pg_dual_source", ["AC1"], {"canonical": [pg], "event_paths": ["pre-existing", "dropped"]})

    fx = Fx({"D": "api/tests/test_pg.py", "A": "a.txt"})
    fx.pg(1)
    fx.run_n(2, 0, GREEN)
    fx.log_reader = lambda repo, doc: [("101", "pg-parity\tRun suite\t##[error]failed api/tests/test_pg.py")]
    check(fx.go() == 1 and fx.events("window-dropped")[0]["subject"] == "D" and
          fx.events("window-dropped")[0]["nodes"] == [step], "owners_pg_step no finer node retains step")
    response("owners_pg_step", ["AC1", "AC8"], {"canonical": step, "owner": "D"})

    source = (HERE / "window_gate.py").read_text()
    check(not re.search(r"NEW (?:failed|suite-error)", source), "no_format_reimpl")
    response("no_format_reimpl", ["AC1"], {"shared_parser_only": True})

    # Real zip bytes, quota and API arguments: no run-view text fetch fallback.
    sh(["remote", "set-url", "origin", "https://github.com/o/r.git"], fx.repo)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("Dashboard/1_Run.txt", output)
    import ghlimit
    with patch.object(ghlimit, "gate", return_value={"ok": True}), \
         patch.object(ghlimit, "real_gh", return_value="fake-gh"), \
         patch.object(window_gate.subprocess, "run", return_value=types.SimpleNamespace(
             returncode=0, stdout=buf.getvalue())) as fetch:
        with patch.object(window_gate, "git", return_value=types.SimpleNamespace(stdout="https://github.com/o/r.git")):
            logs = window_gate.candidate_logs(fx.repo, {"checks": {"pytest": {"run_id": 12}}})
    check(logs == [("12", output)] and fetch.call_args.args[0] == [
        "fake-gh", "api", "repos/o/r/actions/runs/12/logs"], "zip bytes API path")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("pg-parity (Postgres)/3_Run suite.txt", "api/tests/test_pg.py\n##[error]failed")
    with patch.object(ghlimit, "gate", return_value={"ok": True}), \
         patch.object(ghlimit, "run", return_value=(0, json.dumps({"jobs": [{"name": window_gate.PG_CHECK,
             "steps": [{"number": 3, "name": "Run suite", "conclusion": "failure"}]}]}), "")), \
         patch.object(window_gate.subprocess, "run", return_value=types.SimpleNamespace(
             returncode=0, stdout=buf.getvalue())), \
         patch.object(window_gate, "git", return_value=types.SimpleNamespace(stdout="https://github.com/o/r.git")):
        parsed = window_gate.candidate_failures(fx.repo, {"doc": {"checks": {window_gate.PG_CHECK: {"run_id": 12}}}})
    check(parsed["pg-parity"] == {step} and parsed["pg_files"] == {step: {"api/tests/test_pg.py"}}, "zip pg metadata supplies actual step and files")

    for unnamed in (True, False):
        fx = Fx({"A": "a.txt"})
        fx.known = {v}
        fx.run_n(1, 1, output.splitlines()[0])
        parser = fx.service.parse_log
        def parse(text):
            result = parser(text)
            if unnamed: result["undetermined"].append("vitest: one unnamed new failure")
            return result
        fx.service.parse_log = parse
        rc = fx.go()
        check(rc == (2 if unnamed else 0) and not fx.events("window-dropped"), "undetermined_not_clean no drops")
        check((fx.master() == fx.base) == unnamed, "undetermined_not_clean master")
        check(bool(fx.events("escalation-blocking")) == unnamed, "undetermined_not_clean escalation")
    response("undetermined_not_clean", ["AC2"], {"unnamed_blocks": True, "empty_proceeds": True})

    for retry in (False, True):
        fx = Fx({"A": "a.txt", "B": "dashboard/new.test.ts", "C": "c.txt"})
        fx.known = {v, s, p}
        new = "dashboard/new.test.ts::suite > new"
        fx.run_n(1, 1, output + ("\nNEW failed: " + new if retry else "") + "\n" + red([]))
        fx.run_n(2, 1, output + "\n" + red([]))
        rc = fx.go()
        check(rc == (1 if retry else 0), "all_pre_existing_advances exit")
        check([e["subject"] for e in fx.events("window-dropped")] == (["B"] if retry else []), "b9 only B drops")
        check([e["subject"] for e in fx.events("shipped")] == (["A", "C"] if retry else ["A", "B", "C"]), "b9 shipped")
        pre = fx.events("window-pre-existing")
        check(len(pre) == (2 if retry else 1) and all(set(e["ids"]) == fx.known and
            e["base_sha"] == fx.base and re.fullmatch("[a-f0-9]{40}", e["base_sha"]) and e["run_url"] for e in pre), "b9 pre-existing fields")
        check(set(fx.events("window-verified")[0]["pre_existing"]) == fx.known and
              fx.master() == fx.calls()[-1].split("|")[0] and not fx.events("escalation-blocking"), "b9 advances run two")
        response("b9_one_vitest_node_run2_advances" if retry else "all_pre_existing_advances",
                 ["AC3", "AC7", "AC11"], {"drops": [e["subject"] for e in fx.events("window-dropped")],
                 "shipped": [e["subject"] for e in fx.events("shipped")], "pre_existing": sorted(fx.known), "runs": len(fx.calls())})

    # Exercise the actual process boundary, including a descendant in the group.
    reader = window_gate.base_red_snapshot
    check(window_gate.BASE_RED_MARGIN_SECS >= 600, "shipped margin at least 600")
    for mode in ("timeout", "exit3", "exit2", "bad-json", "missing"):
        fx = Fx({"A": "a.txt"})
        cli = fx.tmp / "bin/doit"
        cli.write_text("#!/usr/bin/python3\nimport os,sys,json,subprocess,time,pathlib\n"
            "d=pathlib.Path(os.environ['FAKE_DIR'])\n"
            "with (d/'base-red-calls').open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n" +
            ("child=subprocess.Popen(['sleep','30'])\n(d/'child').write_text(str(child.pid))\ntime.sleep(30)\n" if mode == "timeout" else
             "print('not json')\n" if mode == "bad-json" else "print('{}')\nsys.exit(" + ("3" if mode == "exit3" else "2") + ")\n"))
        cli.chmod(0o755)
        fx.base_reader = reader
        fx.run_n(1, 1, output)
        # Allow the fake baseline process to start on a busy build box while
        # keeping the 30-second ensure child bounded well below its sleep.
        with patch.object(window_gate, "BASE_RED_MARGIN_SECS", .1), \
             patch.object(window_gate, "verify_timeout_secs", return_value=.5), \
             patch.object(window_gate, "BASE_RED_COMMAND", str(fx.tmp / "missing") if mode == "missing" else "doit"):
            rc = fx.go()
        calls = (fx.fdir / "base-red-calls").read_text().splitlines() if mode != "missing" else []
        if calls:
            argv = [json.loads(c) for c in calls]
            check(sum("--ensure" in a for a in argv) == 1 and len(argv) == 2 and
                  all(a[:3] == ["base-red", "albert-scott", fx.base] for a in argv), "one ensure then plain read " + mode)
        check(rc == 2 and fx.master() == fx.base and not fx.events("shipped") and not fx.events("window-dropped"), "base incomplete preserves window " + mode)
        why = fx.events("escalation-blocking")[0]["why"]
        check("base incomplete" in why and all(n in why for n in (v, s, p)), "base incomplete names ids " + mode)
        if mode == "timeout":
            pid = int((fx.fdir / "child").read_text())
            stat = pathlib.Path(f"/proc/{pid}/stat")
            check(not stat.exists() or stat.read_text().split()[2] == "Z", "base_incomplete_timeout child killed")
        response("base_incomplete_" + mode, ["AC4"], {"ensure_calls": 0 if mode == "missing" else 1, "plain_reads": 0 if mode == "missing" else 1, "why": why})

    # The pg-only trap is order dependent; a killed ensure marker survives.
    for stale in (False, True):
        fx = Fx({"A": "a.txt"})
        order, complete = [], False
        marker = fx.fdir / "dispatch-marker"
        if stale: marker.touch()
        def read(project, sha, ensure):
            nonlocal complete
            order.append("ensure" if ensure else "read")
            if not ensure or not marker.exists(): complete = True
            marker.touch(exist_ok=True)
            return {"sha": sha, "complete": complete}
        fx.base_reader = reader
        original = window_gate.verify
        def verify(repo, sha, checks, window):
            if sha == fx.base:
                order.append("baseline")
                check(complete, "base_red_before_baseline avoids pg-only trap")
            return original(repo, sha, checks, window)
        fx.known = {v}
        fx.run_n(1, 1, "NEW failed: " + v)
        with patch.object(window_gate, "call_base_red", side_effect=read), patch.object(window_gate, "verify", side_effect=verify):
            rc = fx.go()
        check(rc == 0 and order == (["ensure", "read", "baseline"] if stale else ["ensure", "baseline"])
              and marker.exists(), "stale_marker_recovers read without deleting marker")
        response("stale_marker_recovers" if stale else "base_red_before_baseline", ["AC5"], {"order": order, "marker_retained": marker.exists()})

    # A base id match cannot hide a changed pg signature; no signature uses classify.
    for signature in ("same", "changed", "none"):
        fx = Fx({"A": "api/tests/test_x.py", "B": "b.txt"})
        node = "api/tests/test_x.py::test_y"
        fx.known = {node}
        if signature != "none": fx.pg("base", [node])
        fx.pg(1, [node], message="different" if signature == "changed" else "AssertionError: parity")
        fx.run_n(2, 0, GREEN)
        rc = fx.go(fixes_base=signature != "none")
        check(rc == {"same": 2, "changed": 1, "none": 0}[signature], "pg_signature_new " + signature)
        check([e["subject"] for e in fx.events("window-dropped")] == (["A"] if signature == "changed" else []), "pg signature owner")
    response("pg_signature_new_fixes_base_not_advanced", ["AC6", "AC7"], {"same": "base-not-releasable", "changed": "A dropped", "none": "pre-existing advances"})

    # Candidate blobs, relative imports, directory indexes, cycles and path safety.
    fx = Fx({})
    files = {"dashboard/src/a.test.ts": "import '../lib';\n", "dashboard/lib/index.ts": "export * from './util';\n",
             "dashboard/lib/util.ts": "import '../src/a.test';\n", "dashboard/src/b.ts": "pass\n",
             "api/tests/test_pg.py": "pass\n"}
    for path, text in files.items():
        dest = fx.repo / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
    sh(["add", "."], fx.repo); sh(["commit", "-m", "candidate imports"], fx.repo)
    rev = sh(["rev-parse", "HEAD"], fx.repo)
    touched = window_gate.OwnershipPaths({"A": {"dashboard/src/a.test.ts"}, "B": {"dashboard/lib/util.ts"},
        "C": {s}, "D": {"api/tests/test_pg.py"}, "E": {"api/app/lib.py"}}, {step: {"api/tests/test_pg.py"}})
    cases = [(v, ["A"]), (s, ["C"]), (step, ["D"]), (p, ["E"])]
    for node, expected in cases:
        check(window_gate.owners(fx.repo, rev, node, touched) == expected, "owners exact " + node)
    del touched["A"]
    check(window_gate.owners(fx.repo, rev, v, touched) == ["B"], "owners_vitest two-hop index")
    check(window_gate.unattributed_ids(fx.repo, rev, [v, s, step, p], touched) == [], "unattributed_helper known")
    unknown = "dashboard/absent.test.ts::suite > lost"
    check(window_gate.unattributed_ids(fx.repo, rev, [unknown], touched) == [unknown], "unattributed_helper missing")
    check(len(window_gate.js_import_closure(fx.repo, rev, "dashboard/src/a.test.ts")) == 3 and
          len(window_gate.js_import_closure(fx.repo, rev, "dashboard/src/a.test.ts", limit=2)) == 2, "closure_cycle bounded")
    with patch.object(window_gate, "git", side_effect=AssertionError("unsafe git read")):
        for bad in ("../outside.ts", "/absolute.ts", "-option.ts", "dashboard/../outside.ts"):
            check(window_gate.owners(fx.repo, rev, bad, touched) == [], "owners_path_escape " + bad)
    response("owners_vitest_suite_error_pg_step_pytest_path_escape_closure", ["AC8", "AC9"], {"direct": ["A"], "two_hop": ["B"], "suite": ["C"], "pg_step": ["D"], "pytest": ["E"], "cycle_size": 3, "bounded_size": 2})

    for mixed in (False, True):
        fx = Fx({"A": "dashboard/src/a.test.ts", "B": "b.txt"})
        fx.run_n(1, 1, "NEW failed: " + unknown + ("\nNEW failed: " + v if mixed else ""))
        if not mixed:
            fx.run_n(2, 0, GREEN); fx.run_n(3, 0, GREEN)
        rc = fx.go()
        drops = fx.events("window-dropped")
        if not mixed:
            check(rc == 1 and not drops and [e["halves"] for e in fx.events("window-bisected")] == [[["A"], ["B"]]] and
                  [e["subject"] for e in fx.events("shipped")] == ["A"], "unattributed_drops_window now bisects two-spec fixture")
            continue
        check(rc == 2 and [e["subject"] for e in drops] == ["A"] and
              drops[0]["reason"] == "owner" and set(drops[0]["nodes"]) == {unknown, v} and
              drops[0]["owned_nodes"] == [v] and drops[0]["unattributed_nodes"] == [unknown],
              "mixed_ids demonstrated owner only; undetermined sibling pending")
        check(fx.events("escalation-blocking") and fx.master() == fx.base,
              "mixed_ids unresolved remainder cannot advance")
    response("unattributed_drops_window_mixed_ids", ["AC10", "AC11"], {"dropped": [e["subject"] for e in drops], "nodes": drops[0]["nodes"], "reason": "owner"})

    fx = Fx({"A": "api/tests/test_x.py", "B": "api/tests/test_z.py"})
    fx.run_n(1, 1, red([p])); fx.run_n(2, 1, red(["api/tests/test_z.py::q"]))
    check(fx.go() == 2 and len(fx.calls()) == 2 and all(e["nodes"] for e in fx.events("window-dropped")) and
          fx.events("escalation-blocking"), "run2_new_id_cap nodes_never_empty")
    response("run2_new_id_cap", ["AC11"], {"runs": 2, "nodes": [e["nodes"] for e in fx.events("window-dropped")]})

    paragraph = (HERE.parent / "agents/builder.md").read_text().split("**Pre-existing failures**", 1)[1].split("## Deviations", 1)[0]
    check("doit base-red" in paragraph and "./doit base-red" not in paragraph and
          '$(git merge-base HEAD origin/<main>)' in paragraph and "scratch worktree" in paragraph and
          "non-zero" in paragraph and "127" in paragraph and '"not known"' in paragraph and
          '"no pre-existing failures"' in paragraph and not re.search(r"\b[a-f0-9]{7,40}\b", paragraph), "builder_contract")
    response("builder_contract", ["AC12"], {"consult_first": True, "unknown_exit127_falls_back": True})

    # Force a fresh import to fail if it tries to import base_red at module scope.
    import builtins, importlib.util
    original_import = builtins.__import__
    def guard(name, *args, **kwargs):
        if name == "base_red": raise ImportError("intentionally unavailable")
        return original_import(name, *args, **kwargs)
    spec = importlib.util.spec_from_file_location("isolated_window_gate", HERE / "window_gate.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules):
        sys.modules.pop("base_red", None)
        with patch("builtins.__import__", side_effect=guard): spec.loader.exec_module(module)
    fx = Fx({"A": "a.txt"}); fx.run_n(1, 0, GREEN)
    check(fx.go() == 0, "lazy_import_without_base_red then stubbed window")
    response("lazy_import_without_base_red", ["AC13"], {"fresh_import": True, "stubbed_window": "shipped"})


def rerun_checks():
    """L1127: real git/strict ledger, with external verify evidence stubbed."""
    from datetime import datetime, timedelta, timezone
    import contextlib, io

    def response(case, acs, body):
        encoded = json.dumps(body, sort_keys=True).encode()
        check(not FAILS, case + " has no failed assertions")
        print(json.dumps(dict(case=case, criteria=acs, url="cli://window_gate/1127/" + case,
                              status=0 if not FAILS else 1,
                              body_sha256=hashlib.sha256(encoded).hexdigest(), body_excerpt=body)))

    # Tree identity includes an empty commit and a real --no-ff candidate.
    fx = Fx({"A": "a.txt"})
    branch = sh(["rev-parse", "br-A"], fx.repo)
    tree = window_gate.tree_of(fx.repo, branch)
    sh(["checkout", "-q", "br-A"], fx.repo)
    sh(["commit", "--allow-empty", "-m", "same content"], fx.repo)
    empty = sh(["rev-parse", "HEAD"], fx.repo)
    candidate, _, _ = window_gate.build_candidate(fx.repo, "identity", fx.base,
                                                  [("A", "br-A", empty)])
    check(branch != empty and tree == window_gate.tree_of(fx.repo, empty) ==
          window_gate.tree_of(fx.repo, candidate) == sh(["rev-parse", candidate + "^{tree}"], fx.repo),
          "tree_of_equal_content")
    check(len(sh(["rev-list", "--parents", "-1", candidate], fx.repo).split()) == 3,
          "tree_of real no-ff merge")
    response("tree_of_equal_content", ["AC1"], {"equal": True, "merge_parents": 2})
    try:
        window_gate.tree_of(fx.repo, "nonexistent")
        check(False, "tree_of_unresolved must raise")
    except RuntimeError:
        pass
    response("tree_of_unresolved", ["AC1"], {"raises": "RuntimeError"})

    now = datetime.now(timezone.utc)
    def prior(green=True, required=None, **extra):
        return dict(type="window-tree-verified", tree="a" * 40,
                    ts=now.isoformat(), green=green, window="prior", **extra,
                    **({"required": required} if required is not None else {}))
    older = prior(ts_unused=True)
    older["ts"] = (now - timedelta(hours=1)).isoformat()
    newer = prior(False)
    check(window_gate.prior_result([newer, older], "a" * 40) == newer, "newest timestamp wins")
    check(window_gate.prior_result([older, newer], "a" * 40) == newer, "newer red wins")
    check(window_gate.prior_result([newer, prior(True, window_unused=True)], "a" * 40)["green"] is True,
          "newer green wins ties in ledger order")
    for value in (None, "true", 1, "missing"):
        e = prior(value)
        if value == "missing": e.pop("green")
        check(window_gate.prior_result([e], "a" * 40) is None, "non-bool not a result")
    stale = prior(); stale["ts"] = (now - timedelta(days=7, minutes=1)).isoformat()
    check(window_gate.prior_result([stale], "a" * 40) is None and
          window_gate.prior_result([prior()], "b" * 40) is None, "stale/different tree ignored")
    response("prior_result_rules", ["AC3"], {"newest": True, "invalid_ignored": True})

    unknown = "api/tests/absent.py::lost"
    def verdict(state="green", nodes=()):
        return dict(state=state, nodes=list(nodes), url="cli://candidate", doc={})

    def exercise(names, bad=(), owner=None, uncertain=(), incomplete=()):
        fx = Fx({s: f"{s}.py" for s in names})
        calls, contents = [], []
        def verify(repo, sha, required, window):
            if sha == fx.base: return verdict()
            present = [s for s in names if window_gate.git(repo, "cat-file", "-e", f"{sha}:{s}.py", check=False).returncode == 0]
            calls.append(present); contents.append(sha)
            if tuple(present) in incomplete:
                fx.service.classify = lambda *a, **kw: {"complete": False}
            else:
                fx.service.classify = lambda project, base, nodes, **kw: {"complete": True, "pre_existing": [], "new": nodes}
            if tuple(present) in uncertain: return verdict("undetermined")
            if owner and present == owner[0]: return verdict("red", [owner[1]])
            return verdict("red", [unknown]) if any(s in bad for s in present) else verdict()
        return fx, calls, contents, verify

    # Exact real-ledger roundtrips: force fold's strict append validation too.
    append = fold.append
    for state in ("green", "red", "undetermined"):
        fx = Fx({"A": "a.txt"})
        def verify(repo, sha, required, window):
            return verdict() if sha == fx.base else verdict(state, [unknown] if state == "red" else [])
        def strict(args): return append(args, strict=True)
        with patch.object(window_gate, "verify", side_effect=verify), patch.object(fold, "append", side_effect=strict):
            fx.go()
        recorded = [e for e in fold.read_events() if e["type"] == "window-tree-verified"]
        check(len(recorded) == (0 if state == "undetermined" else 1), "tree_verified_roundtrip count " + state)
        if recorded:
            e = recorded[0]
            check(e["window"] == "w1" and len(e["tree"]) == 40 and
                  e["tree"] == window_gate.tree_of(fx.repo, e["sha"]) and
                  e["green"] is (state == "green") and e["new_ids"] == ([unknown] if state == "red" else []) and
                  e["required"] == [window_gate.PYTEST_CHECK, window_gate.PG_CHECK] and
                  all("actor" not in json.loads(line) for line in (fold.EVENTS / "L-executor-0001.jsonl").read_text().splitlines()) and
                  window_gate.prior_result(fold.read_events(), e["tree"]) == e, "tree_verified_roundtrip fields " + state)
    response("tree_verified_roundtrip", ["AC2"], {"green_events": 1, "red_events": 1, "strict": True, "fresh_read": True})
    response("undetermined_not_recorded", ["AC2", "AC5"], {"events": 0})

    def seed(fx, green=True, required=None, specs=None):
        specs = specs or list(fx.branches)
        entries = [(s, fx.branches[s], sh(["rev-parse", fx.branches[s]], fx.repo)) for s in specs]
        candidate, _, _ = window_gate.build_candidate(fx.repo, "seed", fx.base, entries)
        kv = dict(window="prior", sha=candidate, tree=window_gate.tree_of(fx.repo, candidate), green=green, new_ids=[])
        if required is not None: kv["required"] = required
        window_gate.ev("window-tree-verified", "prior", **kv)
        return kv["tree"]

    all_checks = [window_gate.PYTEST_CHECK, window_gate.PG_CHECK, window_gate.DASHBOARD_CHECK]
    for required in (None, [window_gate.PYTEST_CHECK, window_gate.PG_CHECK]):
        fx = Fx({"A": "dashboard/a.ts"}); seed(fx, required=required)
        fx.run_n(1, 0, GREEN)
        check(fx.go() == 0 and len(fx.calls()) == 1, "reuse_requires_superset dispatch")
    response("reuse_requires_superset", ["AC3"], {"missing_required_dispatches": 1, "narrow_required_dispatches": 1})

    fx = Fx({"A": "a.txt"}); tree = seed(fx, required=all_checks)
    def no_candidate(repo, sha, required, window):
        check(sha == fx.base, "green must not dispatch candidate")
        return verdict()
    output = io.StringIO()
    with patch.object(window_gate, "verify", side_effect=no_candidate), contextlib.redirect_stdout(output): rc = fx.go()
    verified = fx.events("window-verified")[0]
    check(rc == 0 and verified["actions_minutes"] == 0 and verified["runs"] == [] and verified["sha"] == fx.master() and
          [e["subject"] for e in fx.events("shipped")] == ["A"] and
          f"tree {tree} already verified in prior" in output.getvalue(), "reuse_green_no_dispatch")
    response("reuse_green_no_dispatch", ["AC4"], {"candidate_dispatches": 0, "actions_minutes": verified["actions_minutes"], "advanced": True})

    fx = Fx({"A": "a.txt"}); seed(fx, False, all_checks); fx.run_n(1, 0, GREEN)
    check(fx.go() == 0 and len(fx.calls()) == 1, "red_never_reused")
    response("red_never_reused", ["AC4", "AC5"], {"dispatches": 1})

    pg = "api/tests/test_pg.py::old"
    for fixes_base in (False, True):
        fx = Fx({"A": "a.txt"}); seed(fx, False, all_checks); fx.known.add(pg)
        if fixes_base: fx.pg("base", [pg])
        fx.pg(1, [pg])
        check(fx.go(fixes_base=fixes_base) == (2 if fixes_base else 0) and len(fx.calls()) == 1 and
              fx.events("window-pre-existing") and not fx.events("window-dropped"), "red_pre_existing_pg_redispatched")
        check(fx.events("window-tree-verified")[-1]["new_ids"] == [] and
              fx.events("window-tree-verified")[-1]["green"] is False, "red audit after pre-existing filter")
    response("red_pre_existing_pg_redispatched", ["AC4"], {"dispatches": 1, "pre_existing": [pg], "ordinary_base_advances": True, "fixes_base_escalates": True})

    fx = Fx({"A": "api/tests/test_x.py", "B": "b.txt"}); seed(fx, required=all_checks, specs=["B"])
    fx.run_n(1, 1, red(["api/tests/test_x.py::bad"]))
    check(fx.go() == 1 and len(fx.calls()) == 1 and fx.events("window-verified")[0]["actions_minutes"] == 45 and
          len(fx.events("window-verified")[0]["runs"]) == 1, "reuse_cap_terminates")
    response("reuse_cap_terminates", ["AC5"], {"dispatches": 1, "actions_minutes": 45})

    for state in ("undetermined", "red"):
        fx = Fx({"A": "a.txt"}); fx.run_n(1, 2 if state == "undetermined" else 1, red([unknown]))
        check(fx.go() == 2, "initial failed window")
        fx.run_n(2, 0, GREEN)
        check(fx.go() == 0 and len(fx.calls()) == 2, "rerun dispatches after " + state)
        check("rerun `doit gate-window` once the cause is fixed" in fx.events("escalation-blocking")[0]["revert"], "escalate remedy retained")
    response("undetermined_redispatched", ["AC5"], {"rerun_dispatches": 1})
    response("rerun_after_red_dispatches", ["AC5"], {"rerun_dispatches": 1, "remedy_retained": True})

    for failure in ("tree", "ledger"):
        fx = Fx({"A": "a.txt"}); fx.run_n(1, 0, GREEN)
        real_git, read = window_gate.git, fold.read_events
        def git(repo, *args, **kw):
            if args[0] == "rev-parse" and args[-1].endswith("^{tree}"): raise RuntimeError("unresolved")
            return real_git(repo, *args, **kw)
        def unreadable():
            # Fail only the cache read, preserving append validation/initial lookup.
            import inspect
            if not fx.events_path_seen and any(f.function == "judge" for f in inspect.stack()):
                fx.events_path_seen = True
                raise OSError("ledger unreadable")
            return read()
        fx.events_path_seen = False
        with (patch.object(window_gate, "git", side_effect=git) if failure == "tree" else
              patch.object(fold, "read_events", side_effect=unreadable)):
            check(fx.go() == 0 and len(fx.calls()) == 1, "lookup failure dispatches " + failure)
    response("tree_of_failure_dispatches", ["AC5"], {"tree_failure_dispatches": 1, "ledger_failure_dispatches": 1})

    fx, calls, tips, verify = exercise("ABCDEF", "D")
    with patch.object(window_gate, "verify", side_effect=verify): rc = fx.go()
    halves = [e["halves"] for e in fx.events("window-bisected")]
    drops = fx.events("window-dropped")
    check(rc == 1 and halves == [[["A", "B", "C"], ["D", "E", "F"]], [["D"], ["E", "F"]]] and
          calls == [list("ABCDEF"), list("ABC"), list("DEF"), ["D"], list("EF")] and
          len(drops) == 1 and drops[0]["subject"] == "D" and drops[0]["reason"] == "bisect-single" and drops[0]["nodes"] and
          [e["subject"] for e in fx.events("shipped")] == list("ABC") and fx.master() == tips[1] and
          [r['spec'] for r in merge_queue.queue(fold.read_events())] == list("EF"), "bisect_six_one_bad")
    response("bisect_six_one_bad", ["AC6", "AC10"], {"halves": halves, "calls": calls, "pending": ["E", "F"], "first_green_advanced": True})
    response("bisect_outside_run_cap", ["AC10"], {"bisect_dispatches": len(calls) - 1})

    fx, calls, _, verify = exercise("AB", "A")
    with patch.object(window_gate, "verify", side_effect=verify): rc = fx.go()
    check(rc == 1 and [e["subject"] for e in fx.events("shipped")] == ["B"] and
          fx.events("window-dropped")[0]["reason"] == "bisect-single", "bisect_two_specs")
    response("bisect_two_specs", ["AC7"], {"shipped": ["B"], "dropped": ["A"]})

    fx, calls, _, verify = exercise("ABCD", "C", owner=(["C", "D"], "C.py::bad"))
    with patch.object(window_gate, "verify", side_effect=verify): rc = fx.go()
    check(rc == 1 and [(e["subject"], e["reason"]) for e in fx.events("window-dropped")] == [("C", "bisect-owner")] and
          [r['spec'] for r in merge_queue.queue(fold.read_events())] == ["D"], "bisect_owner_half")
    response("bisect_owner_half", ["AC7"], {"dropped": ["C"], "reason": "bisect-owner", "pending": ["D"]})

    fx, calls, _, verify = exercise("AB", "AB")
    with patch.object(window_gate, "verify", side_effect=verify): rc = fx.go()
    check(rc == 2 and fx.master() == fx.base and all(s in fx.events("escalation-blocking")[-1]["why"] for s in "AB"), "bisect_no_green_half")
    response("bisect_no_green_half", ["AC7"], {"master_unmoved": True, "dropped": ["A", "B"]})

    for mode in ("undetermined", "incomplete"):
        fx, calls, _, verify = exercise("ABCD", "C", **({"uncertain": [("C", "D")]} if mode == "undetermined" else {"incomplete": [("C", "D")]}))
        with patch.object(window_gate, "verify", side_effect=verify): rc = fx.go()
        check(rc == 1 and fx.master() != fx.base and not fx.events("window-dropped") and fx.events("escalation-blocking") and
              [e["subject"] for e in fx.events("shipped")] == ["A", "B"] and
              [r['spec'] for r in merge_queue.queue(fold.read_events())] == ["C", "D"] and
              len(fx.events("window-tree-verified")) == 2, "bisect_undetermined_half " + mode)
    response("bisect_undetermined_half", ["AC7"], {"undetermined_and_base_incomplete": "escalate; no drops; unresolved half pending; verified half ships"})

    fx, calls, _, verify = exercise("A", "A")
    with patch.object(window_gate, "verify", side_effect=verify): fx.go()
    check(not fx.events("window-bisected") and fx.events("window-dropped")[0]["reason"] == "bisect-single", "no_bisect_single_or_mixed")
    response("no_bisect_single_or_mixed", ["AC7"], {"single_unchanged": True, "mixed_covered_by_1110": True})

    fx = Fx({"A": "api/tests/test_x.py", "B": "b.txt", "C": "c.txt"})
    fx.run_n(1, 1, red(["api/tests/test_x.py::bad"])); fx.run_n(2, 1, red([unknown]))
    check(fx.go() == 2 and len(fx.calls()) == 4 and fx.events("window-bisected") and fx.master() == fx.base and
          [e["subject"] for e in fx.events("window-dropped")] == ["A"], "bisect_on_second_run retains undetermined siblings")
    response("bisect_on_second_run", ["AC10"], {"dispatches": 4, "bisects": 1, "master_unmoved": True})

    table = []
    import math
    for n in range(2, 7):
        for bad in range(n):
            names = "ABCDEF"[:n]
            fx, calls, _, verify = exercise(names, names[bad])
            with patch.object(window_gate, "verify", side_effect=verify): fx.go()
            splits = fx.events("window-bisected")
            limit = math.ceil(math.log2(n))
            check(len(calls) - 1 <= 2 * limit and all(len(e["halves"]) == 2 and all(e["halves"]) for e in splits), "bisect bound")
            depth = {tuple(names): 0}
            for e in splits:
                parent = tuple(e["halves"][0] + e["halves"][1])
                for half in e["halves"]: depth[tuple(half)] = depth[parent] + 1
            check(max(depth.values()) <= limit, "bisect depth bound")
            table.append([n, bad, len(calls) - 1])
    # Identical content of two halves exercises the same cache path recursively.
    fx = Fx({"A": "a.txt", "B": "b.txt"})
    real_prepare = window_gate.build_candidate
    shared = [None]
    def repeat(repo, window, base, entries):
        tip, kept, clashes = real_prepare(repo, window, base, entries)
        if len(entries) == 1:
            if shared[0] is None: shared[0] = tip
            tip = shared[0]
        return tip, kept, clashes
    dispatches = []
    def verify(repo, sha, required, window):
        if sha == fx.base: return verdict()
        dispatches.append(sha)
        return verdict("red", [unknown]) if len(dispatches) == 1 else verdict()
    with patch.object(window_gate, "build_candidate", side_effect=repeat), patch.object(window_gate, "verify", side_effect=verify): fx.go()
    check(len(dispatches) == 2, "bisect repeated tree reused")
    response("bisect_bounds_table", ["AC8"], {"rows_n_bad_dispatches": table, "max_depth_within_bound": True, "repeated_half_dispatches": 1})

    # Exercise real prepare/fingerprint against a migration-bearing half.
    from test_rechain import Fixture
    import rechain
    for fail_half in (False, True):
        fx = Fx({})
        with tempfile.TemporaryDirectory() as seed_root:
            seed_fx = Fixture(seed_root)
            for path in sh(["ls-tree", "-r", "--name-only", seed_fx.base], seed_fx.repo).splitlines():
                target = fx.repo / path; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(rechain.blob(seed_fx.repo, seed_fx.base, path))
        sh(["add", "."], fx.repo); sh(["commit", "-m", "migration base"], fx.repo)
        fx.base = sh(["rev-parse", "HEAD"], fx.repo)
        sh(["update-ref", "refs/heads/master", fx.base], fx.repo)
        sf = Fixture.__new__(Fixture); sf.repo, sf.base, sf.events = fx.repo, fx.base, []
        for name in "ABCD":
            if name == "C":
                _, branch, _ = sf.branch(name)
            else:
                branch = "br-" + name
                sh(["checkout", "-b", branch, fx.base], fx.repo)
                (fx.repo / (name + ".txt")).write_text(name)
                sh(["add", "."], fx.repo); sh(["commit", "-m", name], fx.repo)
            fx.branches[name] = branch
        sh(["checkout", "live-scratch"], fx.repo)
        env = sf.scripts(fx.tmp); env["DOIT_WINDOW_RECHAIN"] = "on"
        fp_calls, seen = [], []
        fingerprint = rechain.fingerprint
        def fp(repo, sha, window, **kw):
            fp_calls.append(sha)
            if fail_half and len(fp_calls) == 2: raise ValueError("half fingerprint failed")
            return fingerprint(repo, sha, window, **kw)
        def verify(repo, sha, required, window):
            if sha == fx.base: return verdict()
            seen.append(sha)
            has_c = window_gate.git(repo, "cat-file", "-e", sha + ":" + rechain.VERSIONS + "C.py", check=False).returncode == 0
            if has_c:
                check(rechain.text(repo, sha, rechain.FINGERPRINT).startswith("final:"), "half carries actual fingerprint")
            return verdict("red", [unknown]) if len(seen) == 1 else verdict()
        with patch.dict(os.environ, env), patch.object(rechain, "fingerprint", side_effect=fp), patch.object(window_gate, "verify", side_effect=verify):
            check(fx.go() == 1, "bisect migration partially advances")
        check(len(fp_calls) == 2 and [e["halves"] for e in fx.events("window-bisected")] == [[["A", "B"], ["C", "D"]]] and
              not any(e.get("reason") == "migration-bisect" for e in fx.events("window-dropped")), "bisect_rechain_half")
        if fail_half:
            check([e["subject"] for e in fx.events("rechain-declined")] == ["C"] and
                  window_gate.git(fx.repo, "cat-file", "-e", seen[-1] + ":" + rechain.VERSIONS + "C.py", check=False).returncode != 0,
                  "declined C excluded from half candidate")
    # bisect_window accepts only a prepare callback and must never build directly.
    import inspect
    check("build_candidate(" not in inspect.getsource(window_gate.bisect_window), "bisect_window never builds directly")
    response("bisect_rechain_half", ["AC9"], {"fingerprint_calls": 2, "half_carries_fingerprint": True, "declined_C_excluded": True})
    response("bisect_vs_migration_bisect", ["AC9"], {"unattributed_bisects": True, "migration_bisect_drops": 0, "owner_red_covered_by_rechain_checks": True})


def test_window_attribution():
    attribution_checks()
    assert not FAILS, FAILS


def train_checks():
    """Hermetic two-window train, including cancellation and exact-base advancement."""
    import inspect
    import threading
    from datetime import datetime, timedelta, timezone

    observations = {}

    def mark(name, condition, body=None):
        check(condition, "ac-train " + name)
        if condition:
            print("ac-train " + name)
        observations[name] = body if body is not None else condition

    disk = types.SimpleNamespace(total=100, used=50)
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "look.toml"
        path.write_text("[window_gate]\ntrain_depth = 2\n")
        with patch.object(window_gate.shutil, "disk_usage", return_value=disk):
            answer = window_gate.train_depth({}, path, root=tmp, events=[])
            cases = [window_gate.train_depth({"DOIT_WINDOW_TRAIN_DEPTH": value}, path, root=tmp, events=[])["configured"]
                     for value in ("3", "0", "bad", "1.5", True)]
            missing = window_gate.train_depth({}, pathlib.Path(tmp) / "absent", root=tmp, events=[])
        mark("depth config", answer["depth"] == answer["configured"] == 2 and cases == [3, 1, 1, 1, 1] and missing["depth"] == 1,
             dict(default=answer, overrides=cases, missing=missing))
        depths = []
        for usage in (types.SimpleNamespace(total=100, used=90), types.SimpleNamespace(total=100, used=91), OSError("unreadable")):
            kw = dict(side_effect=usage) if isinstance(usage, Exception) else dict(return_value=usage)
            with patch.object(window_gate.shutil, "disk_usage", **kw):
                depths.append(window_gate.train_depth({}, path, root=tmp, events=[])["depth"])
        mark("depth disk", depths == [2, 1, 1], depths)

    fx = Fx({})
    now = datetime.now(timezone.utc)
    events = [dict(type="window-verified", subject=f"n{i}", sha=str(i), dropped=["A"] if i < 3 else [], ts=now.isoformat())
              for i in range(5)]
    with patch.object(window_gate.shutil, "disk_usage", return_value=disk):
        rates = [window_gate.red_rate(events[:4], now), window_gate.red_rate(events, now)]
        high = window_gate.train_depth({}, root=fx.root, events=events, now=now)
        low = window_gate.train_depth({}, root=fx.root, events=events[1:] + [dict(events[-1], subject="extra")], now=now)
        old = window_gate.red_rate(events, now + timedelta(days=2))
        mark("depth red-rate", rates[0]["rate"] is None and rates[1] == dict(rate=.6, red=3, undetermined=0, other=0, non_green=3, windows=5) and
             high["depth"] == 1 and low["depth"] == 2 and old["windows"] == 0, dict(rates=rates, high=high, low=low))
        window_gate.ev("train-depth", "merge-train", depth=2, reason="test")
        ledger = next(fold.EVENTS.glob("*.jsonl"))
        before = ledger.read_bytes()
        with patch.object(window_gate, "ev") as emit, patch.object(window_gate.subprocess, "run") as send:
            window_gate.train_depth({}, root=fx.root, events=events, now=now)
        mark("depth pure", ledger.read_bytes() == before and not emit.called and not send.called)
        with patch.object(window_gate.subprocess, "run", return_value=types.SimpleNamespace(returncode=1)) as send:
            window_gate.record_depth(high, fx.events(), os.environ)
            window_gate.record_depth(high, fx.events(), os.environ)
        changes = fx.events("train-depth")
        mark("depth event once", len(changes) == 2 and changes[-1].get("telegram") == "failed" and send.call_count == 1, changes)

    # Shared helpers use real git and slot files; only the gate/service is fake.
    def predecessor(fx):
        entries = [("A", fx.branches["A"], sh(["rev-parse", fx.branches["A"]], fx.repo))]
        candidate, kept, clashes = window_gate.build_candidate(fx.repo, "N", fx.base, entries)
        check(kept == ["A"] and not clashes and window_gate.acquire("N", 0), "predecessor fixture")
        window_gate.ev("window-candidate", "N", sha=candidate, verified_on=fx.base, specs=["A"], slot=0)
        return candidate, entries

    def merged(fx, candidate, entries):
        window_gate.advance(fx.repo, "master", fx.base, candidate, "N", ["A"], [], 1, [], entries, False)
        window_gate.release(0)

    with patch.object(window_gate.shutil, "disk_usage", return_value=disk), \
         patch.dict(os.environ, DOIT_WINDOW_TRAIN_DEPTH="2", DOIT_WINDOW_TRAIN_POLL_S="0.02"):
        fx = Fx({"A": "a.txt", "B": "b.txt"})
        candidate, entries = predecessor(fx)
        check(window_gate.acquire("M", 1), "second slot acquired")
        mark("slots", [p.read_text() for p in (window_gate.lock_path(0), window_gate.lock_path(1))] ==
             [f"{os.getpid()} N", f"{os.getpid()} M"] and not (fx.root / "state/window-gate.lock").exists())
        window_gate.ev("window-cancelled", "N", after="old", reason="base-moved")
        mark("in-flight", [e["window"] for e in window_gate.in_flight(fx.events())] == ["N"])
        reasons = [fx.go(["B"]), fx.go(["B"], after="absent"), fx.go(["B"], after="N")]
        window_gate.release(1)
        window_gate.release(0)
        check(window_gate.acquire("M", 0), "reclaimed slot")
        mark("in-flight slot reuse", not window_gate.in_flight(fx.events()) and fx.go(["B"], after="N") == 3)
        window_gate.release(0)
        window_gate.lock_path(0).write_text("999999999 N")
        dead = not window_gate.in_flight(fx.events())
        window_gate.release(0)
        window_gate.ev("window-verified", "N", sha=candidate, kept=["A"], dropped=[])
        terminal = fx.go(["B"], after="N")
        mark("exit3 reasons", reasons == [3, 3, 3] and terminal == 3 and dead and not list((fx.root / "state").glob("window-gate.*.lock")), reasons)
        for bad in ("../N", "", "N with space"):
            try:
                window_gate.main_(["w", "--project", "albert-scott", "--after", bad])
            except SystemExit:
                pass
            else:
                check(False, "invalid after id refused")

        post_drop = []
        for kind in ("window-dropped", "merge-conflict", "second-candidate"):
            fx = Fx({"A": "a.txt", "B": "b.txt"})
            candidate, entries = predecessor(fx)
            if kind == "second-candidate":
                window_gate.ev("window-candidate", "N", sha=candidate, verified_on=fx.base, specs=["A"], slot=0)
            else:
                window_gate.ev(kind, "A", window="N", nodes=["a.py::test"])
            post_drop.append(fx.go(["B"], after="N"))
            window_gate.release(0)
        mark("after exit3 post-drop", post_drop == [3, 3, 3], post_drop)

        # Success with an origin that legitimately lags the local predecessor tip.
        fx = Fx({"A": "a.txt", "B": "b.txt"})
        candidate, entries = predecessor(fx)
        fx.run_n(1, 0, GREEN)
        real_verify = window_gate.verify
        def finish(repo, sha, required, window):
            result = real_verify(repo, sha, required, window)
            if sha != fx.base:
                merged(fx, candidate, entries)
            return result
        with patch.object(window_gate, "verify", side_effect=finish):
            rc = fx.go(["B"], window="N1", after="N")
        built = [e for e in fx.events("window-candidate") if e["subject"] == "N1"]
        verified = [e for e in fx.events("window-verified") if e["subject"] == "N1"]
        mark("candidate event", len(built) == 1 and all(k in built[0] for k in ("sha", "verified_on", "specs", "slot", "after")), built)
        mark("base from predecessor", built[0]["verified_on"] == candidate and
             window_gate.git(fx.repo, "merge-base", "--is-ancestor", candidate, built[0]["sha"], check=False).returncode == 0, built)
        mark("verified fields", verified[0]["after"] == "N" and verified[0]["replaced"] == verified[0]["verified_on"] == candidate, verified)
        mark("merge when N merged", rc == 0 and fx.master() == built[0]["sha"] and not fx.events("window-cancelled"))
        mark("origin lags still merges", rc == 0 and sh(["rev-parse", "master"], fx.origin) == fx.base)
        check(not list((fx.root / "state").glob("window-gate.*.lock")) and
              not window_gate.git(fx.repo, "show-ref", "--verify", "refs/heads/window/N1", check=False).stdout, "train cleanup")

        # Every cancellation uses the same held slot and makes exactly one fresh attempt.
        for reason in ("predecessor-red", "predecessor-partial", "predecessor-rechained", "predecessor-undetermined",
                       "predecessor-dead", "predecessor-timeout", "base-moved"):
            fx = Fx({"A": "a.txt", "B": "b.txt"})
            candidate, entries = predecessor(fx)
            fx.run_n(1, 0, GREEN); fx.run_n(2, 0, GREEN)
            seen, slots, blocked = [], [], []
            real_verify = window_gate.verify
            real_wait = window_gate.wait_predecessor
            def finish(repo, sha, required, window):
                if required == [window_gate.PG_CHECK]:
                    return dict(state="green", nodes=[], url="", doc={}, pg_failures={})
                result = real_verify(repo, sha, required, window)
                seen.append(sha)
                slots.append(window_gate.lock_path(1).read_text())
                if len(seen) == 1:
                    if reason == "predecessor-red":
                        window_gate.escalate("N", "arbitrary non-red words", "red")
                    elif reason == "predecessor-undetermined":
                        window_gate.escalate("N", "red appears in free text but is ignored", "base-not-releasable")
                    elif reason == "predecessor-dead":
                        window_gate.release(0)
                    elif reason == "predecessor-partial":
                        window_gate.ev("window-verified", "N", sha=candidate, kept=["A"], dropped=["other"])
                    elif reason == "predecessor-rechained":
                        merged(fx, candidate, entries)
                        window_gate.ev("shipped", "A", window="N", sha=candidate, rechained=True)
                    elif reason == "base-moved":
                        window_gate.ev("window-verified", "N", sha=fx.base, kept=["A"], dropped=[])
                else:
                    # Even with slot 0 free, cancellation leaves N1 in flight.
                    window_gate.release(0)
                    blocked.append(fx.go(["A"], window="outsider"))
                return result
            def waiting(train):
                if reason == "predecessor-timeout":
                    raise window_gate.TrainCancelled(reason)
                return real_wait(train)
            with patch.object(window_gate, "verify", side_effect=finish), \
                 patch.object(window_gate, "wait_predecessor", side_effect=waiting), \
                 patch.object(window_gate.time, "time", return_value=1700000000):
                rc = fx.go(["B"], window="N1", after="N")
            cancellations = fx.events("window-cancelled")
            builds = [e for e in fx.events("window-candidate") if e["subject"] == "N1"]
            shipped = [e for e in fx.events("shipped") if e["subject"] == "B"]
            mark("cancel " + reason, rc == 0 and len(cancellations) == 1 and cancellations[0]["reason"] == reason and
                 len(builds) == 2 and "after" not in builds[-1] and len(shipped) == 1 and shipped[0]["sha"] != builds[0]["sha"], cancellations)
            check(blocked == [3] and slots == [f"{os.getpid()} N1"] * 2 and
                  len({e["verified_on"] for e in builds}) <= 2, "rebase holds same slot and at most two bases")
            window_gate.release(0)
        mark("rebase same slot", blocked == [3] and slots == [f"{os.getpid()} N1"] * 2, slots)
        mark("distinct verified_on", len({e["verified_on"] for e in builds}) == 2, builds)
        for outcome in ("red", "undetermined", "base-not-releasable"):
            window_gate.escalate("outcome-test", "irrelevant free text", outcome)
        mark("escalate outcome", {e.get("outcome") for e in fx.events("escalation-blocking")} ==
             {"red", "undetermined", "base-not-releasable"}, fx.events("escalation-blocking"))

        # Foreign and unreadable origins cancel; a lagging ancestor was accepted above.
        for mode in ("foreign", "unreadable"):
            fx = Fx({"A": "a.txt", "B": "b.txt"})
            candidate, entries = predecessor(fx)
            merged(fx, candidate, entries)
            if mode == "foreign":
                foreign = sh(["rev-parse", fx.branches["B"]], fx.repo)
                sh(["push", "origin", foreign + ":refs/heads/foreign"], fx.repo)
                sh(["update-ref", "refs/heads/master", foreign], fx.origin)
            else:
                sh(["remote", "set-url", "origin", str(fx.tmp / "missing")], fx.repo)
            reason = window_gate.predecessor_state(fx.repo, "master", "N", candidate)
            mark("origin " + mode + " base-moved", reason == "base-moved", reason)

        # Bound is timeout + ten minutes, with a deterministic injected clock.
        clock, sleeps = [0.0], []
        def sleep(seconds):
            sleeps.append(seconds); clock[0] += seconds
        with patch.object(window_gate, "predecessor_state", return_value=None), \
             patch.object(window_gate, "verify_timeout_secs", return_value=8):
            try:
                window_gate.wait_predecessor({}, clock=lambda: clock[0], sleep=sleep)
            except window_gate.TrainCancelled as exc:
                mark("wait bound", str(exc) == "predecessor-timeout" and abs(sum(sleeps) - 608) < 1e-6, sum(sleeps))
            else:
                check(False, "wait must time out")

        fx = Fx({"A": "a.txt", "B": "b.txt"})
        candidate, entries = predecessor(fx)
        fx.run_n(1, 0, GREEN); fx.run_n(2, 0, GREEN)
        fx.fake.write_text(FAKE.replace('cat "$D/run$n.out" 2>/dev/null',
            'if [ "$n" = 1 ]; then\ntrap \'echo TERM > "$D/term"; exit 2\' TERM\n'
            'touch "$D/started"\nsleep 30\ntouch "$D/ended"\nfi\ncat "$D/run$n.out" 2>/dev/null'))
        def turn_red():
            deadline = time.monotonic() + 5
            while not (fx.fdir / "started").exists() and time.monotonic() < deadline:
                time.sleep(.01)
            window_gate.ev("window-dropped", "A", window="N", nodes=["a.py::test"])
        thread = threading.Thread(target=turn_red)
        thread.start()
        rc = fx.go(["B"], window="N1", after="N")
        thread.join(timeout=5)
        mark("early cancel", rc == 0 and (fx.fdir / "term").exists() and not (fx.fdir / "ended").exists() and
             fx.events("window-cancelled")[0]["reason"] == "predecessor-red")
        window_gate.release(0)

        fx = Fx({"A": "a.txt"}); fx.run_n(1, 0, GREEN)
        with patch.object(window_gate, "predecessor_state", side_effect=AssertionError("depth1 must not poll")) as poll:
            rc = fx.go()
        mark("no poll depth1", rc == 0 and not poll.called)
        mark("verify signature unchanged", list(inspect.signature(window_gate.verify).parameters) == ["repo", "cand", "required", "window"])

        # Red attribution waits for the predecessor and asks base-red about the root.
        node = "api/tests/test_z.py::test_bad"
        for merges in (True, False):
            fx = Fx({"A": "a.txt", "B": "api/tests/test_z.py"})
            candidate, entries = predecessor(fx)
            requests, owner_calls = [], []
            fx.base_reader = lambda project, sha: requests.append(sha) or dict(sha=sha, complete=True)
            fx.run_n(1, 1, red([node])); fx.run_n(2, 0, GREEN)
            real_verify, real_owners = window_gate.verify, window_gate.owners
            def finish(repo, sha, required, window):
                result = real_verify(repo, sha, required, window)
                if sha != fx.base and not fx.events("window-verified") and not fx.events("escalation-blocking"):
                    if merges:
                        merged(fx, candidate, entries)
                    else:
                        window_gate.escalate("N", "failed", "red")
                return result
            def own(*args):
                owner_calls.append(window_gate.predecessor_state(fx.repo, "master", "N", candidate))
                return real_owners(*args)
            with patch.object(window_gate, "verify", side_effect=finish), patch.object(window_gate, "owners", side_effect=own):
                rc = fx.go(["B"], window="N1", after="N")
            drops = [e for e in fx.events("window-dropped") if e.get("window") == "N1"]
            mark("red after N merged drops" if merges else "red N unmerged drops nothing",
                 (rc == 2 and len(drops) == 1 and owner_calls and all(v == "" for v in owner_calls)) if merges
                 else (rc == 0 and not drops and not owner_calls), drops)
            check(requests and all(sha == fx.base for sha in requests), "base-red root only")
            window_gate.release(0)
        mark("base-red sha is root", requests == [fx.base, fx.base], requests)

        # The train can slide forward after the original root window has landed.
        fx = Fx({"A": "a.txt", "B": "b.txt", "C": "c.txt"})
        candidate, entries = predecessor(fx)
        merged(fx, candidate, entries)
        second_entries = [("B", fx.branches["B"], sh(["rev-parse", fx.branches["B"]], fx.repo))]
        second, kept, clashes = window_gate.build_candidate(fx.repo, "N1", candidate, second_entries)
        check(kept == ["B"] and not clashes and window_gate.acquire("N1", 1), "sliding predecessor fixture")
        window_gate.ev("window-candidate", "N1", sha=second, verified_on=candidate, after="N", specs=["B"], slot=1)
        requests = []
        fx.base_reader = lambda project, sha: requests.append(sha) or dict(sha=sha, complete=True)
        fx.run_n(1, 0, GREEN)
        real_verify = window_gate.verify
        def slide(repo, sha, required, window):
            if required == [window_gate.PG_CHECK]:
                return dict(state="green", nodes=[], url="", doc={}, pg_failures={})
            result = real_verify(repo, sha, required, window)
            window_gate.advance(repo, "master", candidate, second, "N1", ["B"], [], 1, [], second_entries, False, after="N")
            window_gate.release(1)
            return result
        with patch.object(window_gate, "verify", side_effect=slide):
            rc = fx.go(["C"], window="N2", after="N1")
        mark("base-red sliding root", rc == 0 and requests == [candidate] and
             not list((fx.root / "state").glob("window-gate.*.lock")), requests)

        # A failed guard on the rebuilt attempt cannot start a third verification.
        fx = Fx({"A": "a.txt", "B": "b.txt"})
        candidate, entries = predecessor(fx)
        fx.run_n(1, 0, GREEN); fx.run_n(2, 0, GREEN)
        real_verify = window_gate.verify
        def fail_first(repo, sha, required, window):
            result = real_verify(repo, sha, required, window)
            if sha != fx.base:
                window_gate.escalate("N", "not releasable", "undetermined")
            return result
        with patch.object(window_gate, "verify", side_effect=fail_first), \
             patch.object(window_gate, "advance", side_effect=RuntimeError("old-value guard")) as advancing:
            rc = fx.go(["B"], window="N1", after="N")
        builds = [e for e in fx.events("window-candidate") if e["subject"] == "N1"]
        mark("second advance undetermined", rc == 2 and fx.master() == fx.base and len(builds) == 2 and
             advancing.call_count == 1 and not fx.events("shipped") and
             fx.events("escalation-blocking")[-1]["outcome"] == "undetermined")
        window_gate.release(0)

        # A post-drop rebuild keeps the same base and emits exactly one event per build.
        fx = Fx({"A": "api/tests/test_z.py", "B": "b.txt"})
        fx.run_n(1, 1, red([node])); fx.run_n(2, 0, GREEN)
        rc = fx.go()
        built = fx.events("window-candidate")
        mark("candidate once per build", rc == 1 and len(built) == 2 and len({e["sha"] for e in built}) == 2 and
             {e["verified_on"] for e in built} == {fx.base}, built)

        # A held slot is respected even when a disk/red-rate stand-down lowers depth.
        fx = Fx({"A": "a.txt", "B": "b.txt"})
        candidate, entries = predecessor(fx)
        with patch.dict(os.environ, DOIT_WINDOW_TRAIN_DEPTH="1"):
            check(fx.go(["B"], window="N1", after="N") == 3, "depth1 refuses another train window")
        window_gate.release(0)
        torn = fold.EVENTS / "L-executor-0099.jsonl"
        torn.write_text('{"type":')
        check(window_gate.train_depth({}, events=None)["depth"] == 1 and fx.go(["B"]) == 3,
              "unreadable ledger refuses train admission")

    root = HERE.parent
    executor = (root / "agents/executor.md").read_text()
    configured = window_gate.train_depth({}, events=[])["configured"]
    readers = [str(path.relative_to(root)) for folder in (root / "src", root / "agents")
               for path in folder.rglob("*") if path.suffix in (".py", ".md") and "window-verified" in path.read_text()]
    harvest = [path for path in (root / "src").glob("harvest*.py") if "window-verified" in path.read_text()]
    mark("readers config caller", configured == 2 and type(configured) is int and
         "one window at a time" in executor and "in_flight()" in executor and "queue head" in executor and
         not harvest, dict(configured=configured, readers=readers, harvest_reads_verified=bool(harvest)))

    body = json.dumps(observations, sort_keys=True)
    print(json.dumps(dict(url="cli://gate-window/train_checks", status=0 if not FAILS else 1,
                         body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=observations), sort_keys=True))


def scope_checks():
    """Hermetic window-scope contract, including malicious candidates and gh."""
    import inspect, shlex, signal
    from datetime import datetime, timezone

    all_checks = [window_gate.PYTEST_CHECK, window_gate.PG_CHECK, window_gate.DASHBOARD_CHECK]
    records = {}

    def mark(ac, name, ok, value=None):
        check(ok, "scope " + name)
        print("ac-scope " + name)
        records.setdefault(ac, []).append(dict(case=name, passed=bool(ok), value=value))

    def script(answer):
        return "printf '%s\\n' " + shlex.quote(json.dumps(answer)) + "\n"

    def answer(checks=None, **kw):
        return dict(checks=["pytest"] if checks is None else checks,
                    unclassified=[], everything=False, reason="fixture python", **kw)

    def fixture(paths=("api/new.py",), source=None):
        fx = Fx({})
        classifier = fx.repo / "scripts/ci/actions_changes_gate.sh"
        if source is False:
            classifier.unlink()
        else:
            classifier.write_text(script(answer()) if source is None else source)
        sh(["add", "-A"], fx.repo); sh(["commit", "-m", "classifier base"], fx.repo)
        fx.base = sh(["rev-parse", "HEAD"], fx.repo)
        sh(["update-ref", "refs/heads/master", fx.base], fx.repo)
        sh(["checkout", "-b", "br-A"], fx.repo)
        for name in paths:
            path = fx.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("candidate\n")
        sh(["add", "-A"], fx.repo)
        sh(["commit", "--allow-empty", "-m", "candidate"], fx.repo)
        cand = sh(["rev-parse", "HEAD"], fx.repo)
        fx.branches["A"] = "br-A"
        sh(["checkout", "live-scratch"], fx.repo)
        return fx, cand

    fx, cand = fixture()
    result = window_gate.scope(fx.repo, fx.base, cand, all_checks)
    mark("AC1", "python-only", result == dict(checks=["pytest"], everything=False, reason="fixture python"), result)
    seen = []
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        if args[0][0] == "bash":
            seen.append(dict(env=kwargs["env"], cwd=kwargs["cwd"], args=args[0],
                             paths=pathlib.Path(kwargs["env"]["CHANGED_PATHS_FILE"]).read_bytes()))
        return real_popen(*args, **kwargs)

    with patch.object(subprocess, "Popen", side_effect=spy):
        result = window_gate.scope(fx.repo, fx.base, cand, all_checks)
    mark("AC1", "classifier from base", len(seen) == 1 and seen[0]["paths"] == b"api/new.py\0" and
         set(seen[0]["env"]) == {"PATH", "EVENT_NAME", "CHANGED_PATHS_FILE"} and
         seen[0]["env"]["EVENT_NAME"] == "window" and seen[0]["args"][-1] == "--json" and
         str(seen[0]["cwd"]) != str(fx.repo) and not pathlib.Path(seen[0]["cwd"]).exists())
    malicious = fx.repo / "scripts/ci/actions_changes_gate.sh"
    marker = fx.tmp / "executed-candidate"
    sh(["checkout", "br-A"], fx.repo)
    malicious.write_text("touch " + shlex.quote(str(marker)) + "\nexit 1\n")
    sh(["add", "-A"], fx.repo); sh(["commit", "-m", "malicious classifier"], fx.repo)
    cand = sh(["rev-parse", "HEAD"], fx.repo)
    result = window_gate.scope(fx.repo, fx.base, cand, all_checks)
    mark("AC1", "candidate script ignored", result["checks"] == ["pytest"] and not marker.exists(), result)

    fx, cand = fixture(source=script(answer([window_gate.PG_CHECK])))
    result = window_gate.scope(fx.repo, fx.base, cand, all_checks)
    mark("AC3", "floor pytest", result["checks"] == all_checks[:2], result)
    fx, cand = fixture(("dashboard/new.ts",))
    result = window_gate.scope(fx.repo, fx.base, cand, all_checks)
    mark("AC3", "floor dashboard", result["checks"] == ["pytest", window_gate.DASHBOARD_CHECK], result)
    result = window_gate.scope(fx.repo, fx.base, cand, all_checks, fixes_base=True)
    mark("AC3", "floor pg fixes-base", result == dict(checks=all_checks, everything=True, reason="scope-fallback: fixes-base"), result)
    full_answer = answer(); full_answer["everything"] = True
    fx, cand = fixture(source=script(full_answer))
    result = window_gate.scope(fx.repo, fx.base, cand, all_checks)
    mark("AC3", "everything full", result["checks"] == all_checks and result["everything"], result)
    fx, cand = fixture(source=script(answer([next(iter(window_gate.CI_VERIFY_EXCLUDED))])))
    result = window_gate.scope(fx.repo, fx.base, cand, all_checks)
    mark("AC3", "never empty", result == dict(checks=all_checks, everything=True, reason="scope-fallback: empty intersection"), result)

    def fallback(result):
        return result["checks"] == all_checks and result["everything"] is True and result["reason"].startswith("scope-fallback:")

    for letter, source in (("a", False), ("b", "exit 7\n"), ("d", script(answer(["unknown check"]))),
                           ("e", script(dict(answer(), unclassified=["unknown/path"])) )):
        fx, cand = fixture(source=source)
        result = window_gate.scope(fx.repo, fx.base, cand, all_checks)
        mark("AC4", "fallback " + letter, fallback(result), result)
    malformed = ["", "echo 'run all checks'\n", "printf '{}\\n{}\\n'\n", "echo '[]'\n"]
    for obj in ({}, {"checks": "pytest"}, dict(answer(), checks=[1]), dict(answer(), unclassified=None),
                dict(answer(), everything="false"), dict(answer(), reason=None)):
        malformed.append(script(obj))
    outputs = []
    for source in malformed:
        fx, cand = fixture(source=source)
        outputs.append(window_gate.scope(fx.repo, fx.base, cand, all_checks))
    mark("AC4", "fallback c", all(map(fallback, outputs)), outputs)
    fx, cand = fixture(())
    results = [window_gate.scope(fx.repo, fx.base, cand, all_checks),
               window_gate.scope(fx.repo, "missing-ref", cand, all_checks)]
    mark("AC4", "fallback f", all(map(fallback, results)), results)
    fx, cand = fixture(source="sleep 120 & wait\n")
    timeouts = []

    def short_classifier(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        if args[0][0] == "bash":
            communicate = proc.communicate
            def bounded(*a, **kw):
                timeouts.append(kw.get("timeout"))
                if kw.get("timeout") == 60:
                    kw["timeout"] = .05
                return communicate(*a, **kw)
            proc.communicate = bounded
        return proc

    with patch.object(subprocess, "Popen", side_effect=short_classifier), \
         patch.object(os, "killpg", wraps=os.killpg) as kill:
        result = window_gate.scope(fx.repo, fx.base, cand, all_checks)
    mark("AC4", "fallback g", fallback(result) and "timeout" in result["reason"] and
         timeouts[0] == 60 and any(call.args[1] == signal.SIGKILL for call in kill.call_args_list), result)
    with patch.object(pathlib.Path, "write_bytes", side_effect=PermissionError("fixture")):
        result = window_gate.scope(fx.repo, fx.base, cand, all_checks)
    mark("AC4", "fallback h", fallback(result) and "PermissionError" in result["reason"], result)
    with patch.object(subprocess, "run", side_effect=LookupError("fixture")):
        result = window_gate.scope(fx.repo, fx.base, cand, all_checks)
    check(fallback(result) and result["reason"] == "scope-fallback: LookupError", "scope unexpected exception")
    fx, cand = fixture((os.fsdecode(b"api/non-utf8-\xff.py"),), source=script(full_answer))
    seen.clear()
    with patch.object(subprocess, "Popen", side_effect=spy):
        result = window_gate.scope(fx.repo, fx.base, cand, all_checks)
    mark("AC4", "non-utf8 path", result["checks"] == all_checks and b"\xff" in seen[0]["paths"], result)

    rows = [("dashboard/a.ts",), ("dashboard/src/a.test.ts",), ("api/a.py",), ("sql/a.sql",),
            ("scripts/ci/a.sh",), ("docs/a.md",), (), ("unknown/file",), ("README.md",),
            ("api/a.py", "dashboard/a.ts"), ("sql/a.sql", "docs/a.md"), ("scripts/ci/a.sh", "dashboard/a.ts"),
            ("docs/a.md", "api/a.py"), ("unknown/file", "dashboard/a.ts"), ("dashboard/a.ts", "sql/a.sql"),
            ("api/a.py", "sql/a.sql"), ("docs/a.md", "scripts/ci/a.sh"), ("tests/a.py",),
            ("pipelines/a.py",), ("dashboard/a.ts", "api/a.py", "sql/a.sql")]
    successes = 0
    for paths in rows:
        fx, cand = fixture(paths)
        required = window_gate.scope(fx.repo, fx.base, cand, all_checks)["checks"]
        floor = {"pytest"} | ({window_gate.DASHBOARD_CHECK} if any(p.startswith("dashboard/") for p in paths) else set())
        successes += floor <= set(required)
    mark("AC5", "monotone 20/20", successes == len(rows) == 20, dict(rows=rows, passed=successes))

    fx, _ = fixture()
    fx.run_n(1, 0, GREEN)
    rc = fx.go()
    event = fx.events("window-verified")[0]
    mark("AC2", "run_window passes scoped checks", rc == 0 and fx.calls()[0].endswith("|pytest;"), fx.calls())
    mark("AC2", "python-only no pg dispatch", not (fx.fdir / "base-calls").exists() and
         all(window_gate.PG_CHECK not in call for call in fx.calls()), fx.calls())
    mark("AC2", "verify signature unchanged", list(inspect.signature(window_gate.verify).parameters) ==
         ["repo", "cand", "required", "window"])
    mark("AC6", "window-verified scope fields", event["scope"] == ["pytest"] and
         event["scope_reason"] == "fixture python" and event["selfhosted_minutes"] == 0 and
         event["minutes_source"] == "estimate", {k: event[k] for k in ("scope", "scope_reason", "actions_minutes", "minutes_source")})

    fx = Fx({"A": "api/tests/test_x.py", "B": "b.py"})
    fx.run_n(1, 1, red(["api/tests/test_x.py::bad"])); fx.run_n(2, 0, GREEN)
    scopes = [dict(checks=["pytest", window_gate.DASHBOARD_CHECK], everything=False, reason="first"),
              dict(checks=["pytest", window_gate.PG_CHECK], everything=False, reason="second")]
    scoped = []
    def rescope(repo, base, cand, checks, **kw):
        scoped.append((base, cand))
        if len(scoped) == 2:
            check(not (fx.fdir / "base-calls").exists(), "scope baseline lazy before later scope")
        return scopes[len(scoped) - 1]
    with patch.object(window_gate, "scope", side_effect=rescope):
        rc = fx.go()
    event = fx.events("window-verified")[0]
    mark("AC2", "rescope after drop", rc == 1 and len(scoped) == 2 and scoped[0][1] != scoped[1][1] and
         all(base == fx.base for base, _ in scoped), [base == fx.base for base, _ in scoped])
    mark("AC2", "baseline follows scope", (fx.fdir / "base-calls").read_text().splitlines() == ["base"])
    mark("AC6", "last verification wins", event["scope"] == scopes[-1]["checks"] and event["scope_reason"] == "second", event["scope"])
    mark("AC6", "dashboard charge follows dispatch", event["actions_minutes"] == 2 * window_gate.RUN_MINUTES + window_gate.DASHBOARD_MINUTES, event["actions_minutes"])

    fx, _ = fixture(source=False)
    fx.run_n(1, 0, GREEN); rc = fx.go()
    event = fx.events("window-verified")[0]
    mark("AC6", "everything fallback charges dashboard", rc == 0 and event["scope"] == all_checks and
         event["actions_minutes"] == window_gate.RUN_MINUTES + window_gate.DASHBOARD_MINUTES, event["actions_minutes"])
    fx, _ = fixture()
    fx.pg("base", ["api/tests/test_z.py::old"]); fx.run_n(1, 0, GREEN)
    rc = fx.go(fixes_base=True)
    check(rc == 0 and window_gate.PG_CHECK in fx.calls()[0] and
          fx.events("window-verified")[0]["scope"] == all_checks, "scope fixes-base dispatch repairs red base")

    fx = Fx({})
    window_gate.advance(fx.repo, "master", fx.base, fx.base, "legacy", [], [], 2, ["url"], [], True)
    event = fx.events("window-verified")[0]
    expected = dict(v=1, type="window-verified", subject="legacy", project=window_gate.PROJECT,
                    sha=fx.base, verified_on=fx.base, replaced=fx.base, runs=["url"], kept=[], dropped=[],
                    pre_existing=[], actions_minutes=100)
    mark("AC6", "advance without scope unchanged", {k: v for k, v in event.items() if k not in ("ts", "actor", "_src")} == expected,
         sorted(event))

    def job(seconds, **runner):
        return dict(status="completed", runner_name="Hosted Agent", labels=["ubuntu-latest"], started_at="2026-10-20T00:00:00Z", completed_at=datetime.fromtimestamp(
            datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() + seconds, timezone.utc).isoformat()) | runner
    docs = [{"checks": {"pytest": {"run_id": 10}, window_gate.DASHBOARD_CHECK: {"run_id": 10}}},
            {"checks": {window_gate.PG_CHECK: {"run_id": 11}}}]
    jobs = {"10": [job(61, labels=["ubuntu-latest"]), job(120, labels=["ubuntu-latest"])],
            "11": [job(90, labels=["self-hosted"])]}
    for rid, data in jobs.items():
        (fx.fdir / ("jobs-" + rid)).write_text(json.dumps(dict(jobs=data)))
    with patch.dict(os.environ, PATH=fx.path, FAKE_DIR=str(fx.fdir)), \
         patch.object(ghlimit, "gate", return_value={"ok": True}), \
         patch.object(sys.modules["base_red"], "_owner", return_value="o/r"), \
         patch.object(subprocess, "run", wraps=subprocess.run) as gh:
        result = window_gate.run_minutes(fx.repo, docs)
    mark("AC9", "minutes measured", result == dict(actions_minutes=4, selfhosted_minutes=2, minutes_source="measured"), result)
    mark("AC9", "minutes selfhosted split", result["actions_minutes"] == 4 and result["selfhosted_minutes"] == 2, result)
    mark("AC9", "minutes run counted once", gh.call_count == 2, gh.call_count)
    (fx.fdir / "jobs-11").write_text(json.dumps(dict(jobs=[job(90, labels=["self-hosted"]) ])))
    with patch.dict(os.environ, PATH=fx.path, FAKE_DIR=str(fx.fdir)), \
         patch.object(sys.modules["base_red"], "_owner", return_value="o/r"):
        check(window_gate.run_minutes(fx.repo, docs) == result, "scope self-hosted REST labels")
    bad_results = []
    with patch.dict(os.environ, PATH=fx.path, FAKE_DIR=str(fx.fdir)), \
         patch.object(sys.modules["base_red"], "_owner", return_value="o/r"):
        for bad in ("missing", "not json", json.dumps(dict(jobs=[{}])),
                    json.dumps(dict(jobs=[dict(job(61), started_at=None)]))):
            (fx.fdir / "jobs-10").write_text(bad)
            bad_results.append(window_gate.run_minutes(fx.repo, docs))
        bad_results.append(window_gate.run_minutes(fx.repo, [{"checks": {"pytest": "ran"}}]))
        with patch.object(subprocess, "run", return_value=types.SimpleNamespace(returncode=1, stdout="")):
            bad_results.append(window_gate.run_minutes(fx.repo, docs))
    mark("AC9", "minutes estimate fallback", all(r["minutes_source"] == "estimate" and r["selfhosted_minutes"] == 0
         and r["actions_minutes"] == 95 for r in bad_results[:-2]) and
         bad_results[-2]["actions_minutes"] == 45 and bad_results[-1]["actions_minutes"] == 95, bad_results)
    fx, _ = fixture()
    fx.run_n(1, 0, json.dumps(dict(verdict="green", checks={"pytest": {"state": "ran", "run_id": 77}})))
    (fx.fdir / "jobs-77").write_text(json.dumps(dict(jobs=jobs["10"] + jobs["11"])))
    rc = fx.go()
    event = fx.events("window-verified")[0]
    mark("AC6", "measured minutes roundtrip", rc == 0 and event["actions_minutes"] == 4 and
         event["selfhosted_minutes"] == 2 and event["minutes_source"] == "measured",
         {k: event[k] for k in ("actions_minutes", "selfhosted_minutes", "minutes_source")})
    fx, _ = fixture(source=script(answer(["pytest", window_gate.PG_CHECK])))
    fx.run_n("base", 0, json.dumps(dict(verdict="green", checks={window_gate.PG_CHECK: {"state": "ran", "run_id": 76}})))
    fx.run_n(1, 0, json.dumps(dict(verdict="green", checks={name: {"state": "ran", "run_id": 77}
                                                        for name in ("pytest", window_gate.PG_CHECK)})))
    for rid, data in ((76, [job(60, labels=["ubuntu-latest"])]), (77, jobs["10"] + jobs["11"])):
        data[0].update(name=window_gate.PG_CHECK, conclusion="success")
        (fx.fdir / f"jobs-{rid}").write_text(json.dumps(dict(jobs=data)))
    rc = fx.go()
    event = fx.events("window-verified")[0]
    check(len(fx.events("window-run-minutes")) == 1 and fx.events("window-run-minutes")[0]["run_ids"] == ["77"],
          "honest AC3 base-sha run excluded from candidate minutes")
    mark("AC9", "minutes include baseline", rc == 0 and event["actions_minutes"] == 5 and
         event["selfhosted_minutes"] == 2 and event["minutes_source"] == "measured",
         {k: event[k] for k in ("actions_minutes", "selfhosted_minutes", "minutes_source")})
    for ac, cases in records.items():
        encoded = json.dumps(cases, sort_keys=True).encode()
        print(json.dumps(dict(criterion=ac, url="cli://window_gate/scope/" + ac,
                             status=0 if all(c["passed"] for c in cases) else 1,
                             body_sha256=hashlib.sha256(encoded).hexdigest(), body_excerpt=cases)))


def infra_checks():
    evidence = []
    for phase in ("candidate", "verification", "subprocess"):
        fx = Fx({"L-spec-9149": "feature.py"})
        if phase == "candidate":
            cm = patch.object(window_gate, "build_candidate", side_effect=RuntimeError("git worktree add: No space left on device"))
        elif phase == "subprocess":
            cm = patch.object(window_gate, "verify", side_effect=OSError(28, "No space left on device"))
        else:
            fx.run_n(1, 1, "No space left on device")
            from contextlib import nullcontext
            cm = nullcontext()
        with cm:
            rc = fx.go()
        rows = fx.events("infra-failure")
        check(rc == 2 and len(rows) == 1, "infra " + phase + " emits one hold")
        check(not fx.events("window-dropped") and not fx.events("merge-conflict") and not fx.events("rejected-criterion"),
              "infra " + phase + " never drops work")
        check(rows and rows[0]["subject"] == "w1" and rows[0]["retry_at"] > rows[0]["ts"], "infra retry instant is future")
        evidence.extend(rows)
    body = json.dumps(evidence, sort_keys=True).encode()
    print(json.dumps(dict(criterion="AC5", url="cli://infra-retry/AC5", status=0 if not FAILS else 1,
                         body_sha256=hashlib.sha256(body).hexdigest(), body_excerpt=evidence)))


def merge_simple_checks():
    records = []
    def mark(name, ok, **fields):
        check(ok, "merge-simple AC4 " + name)
        records.append(dict(case=name, passed=bool(ok), **fields))

    for state in ("red", "undetermined"):
        fx = Fx({"S": "api/tests/test_x.py"})
        fx.run_n(1, 1 if state == "red" else 2,
                 red(["api/tests/test_x.py::test_bad"]) if state == "red" else "unverifiable")
        rc = fx.go()
        mark(state, rc != 0 and fx.master() == fx.base and not fx.events("shipped") and
             (bool(fx.events("window-dropped")) if state == "red" else bool(fx.events("escalation-blocking"))),
             main_unchanged=fx.master() == fx.base, shipped=len(fx.events("shipped")))

    fx = Fx({"S": "single.txt"})
    fx.run_n(1, 0, GREEN)
    updates, emitted = [], []
    real_git, real_ev = window_gate.git, window_gate.ev
    def record_git(repo, *args, **kwargs):
        result = real_git(repo, *args, **kwargs)
        if args[:2] == ("update-ref", "refs/heads/master"):
            updates.append(args)
        return result
    def record_event(kind, subject, **fields):
        if kind in {"shipped", "window-verified"}:
            emitted.append((kind, fx.master() == fields["sha"]))
        return real_ev(kind, subject, **fields)
    with patch.object(window_gate, "git", side_effect=record_git), \
         patch.object(window_gate, "ev", side_effect=record_event):
        rc = fx.go()
    mark("green atomic advance", rc == 0 and fx.master() != fx.base and
         updates == [("update-ref", "refs/heads/master", fx.master(), fx.base)] and
         emitted == [("shipped", True), ("window-verified", True)],
         old_asserted=True, emitted_after_move=emitted)
    candidate = fx.master()
    sh(["update-ref", "refs/heads/master", fx.base, candidate], fx.repo)
    before = len(fx.events("shipped"))
    try:
        window_gate.advance(fx.repo, "master", candidate, candidate, "stale", ["S"], [], 1, [],
                            [("S", "br-S", candidate)], False)
    except RuntimeError:
        refused = True
    else:
        refused = False
    mark("stale old main", refused and fx.master() == fx.base and len(fx.events("shipped")) == before,
         refused=refused, main_unchanged=fx.master() == fx.base)
    encoded = json.dumps(records, sort_keys=True).encode()
    ok = all(row["passed"] for row in records)
    print(json.dumps(dict(url="cli://merge-simple/AC4", status=0 if ok else 1,
                         body_sha256=hashlib.sha256(encoded).hexdigest(), body_excerpt=records)))
    if ok:
        print("merge-simple AC4 status=0")


def base_kind_checks():
    from unittest.mock import Mock
    start = len(FAILS)
    old, new = "old.py::test_base", "api/tests/test_x.py::test_new"
    partial = dict(complete=False, workflows=["Python Tests"],
                   kinds_covered=["pg-parity", "pytest"], undetermined=[])
    fx = Fx({"A": "api/tests/test_x.py", "B": "b.txt"})
    fx.base_reader = lambda project, sha: dict(partial, sha=sha)
    fx.run_n(1, 1, red([old, new]))
    fx.run_n(2, 0, GREEN)
    fx.service.classify = Mock(return_value=dict(complete=True, pre_existing=[old], new=[new],
        needed=["pytest"], covered=["pg-parity", "pytest"], uncovered=[], undetermined=False))
    check(fx.go() == 1, "base-kind partial Python base advances remainder")
    check(fx.service.classify.call_args.args == ("albert-scott", fx.base, sorted([old, new])),
          "base-kind classify receives all NEW ids")
    check([e["ids"] for e in fx.events("window-pre-existing")] == [[old]], "base-kind base id recorded")
    check([e["subject"] for e in fx.events("window-dropped")] == ["A"], "base-kind new id attributed")
    check(not fx.events("escalation-blocking"), "base-kind no base incomplete escalation")

    fx = Fx({"A": "dashboard/src/x.test.ts"})
    fx.base_reader = lambda project, sha: dict(partial, sha=sha)
    node = "dashboard/src/x.test.ts::renders"
    fx.run_n(1, 1, red([node]))
    fx.service.classify = lambda *a, **kw: dict(complete=False, pre_existing=[], new=[node],
        needed=["vitest"], covered=["pg-parity", "pytest"], uncovered=[node], undetermined=False)
    check(fx.go() == 2 and fx.master() == fx.base and not fx.events("window-dropped"),
          "base-kind uncovered vitest keeps master and specs")
    check("base incomplete (needs ['vitest'], base has ['pg-parity', 'pytest'])" in
          fx.events("escalation-blocking")[0]["why"], "base-kind needs and coverage escalation")

    pg = "pg-parity::Run parity"
    for mode in ("inherited", "undetermined", "not-required", "fixes-base", "missing-covered"):
        fx = Fx({"A": "a.txt", **({"B": "b.txt"} if mode == "not-required" else {})})
        fx.base_reader = lambda project, sha: dict(partial, sha=sha)
        fx.run_n(1, 1, red([pg]))
        answer = dict(complete=False, pre_existing=[], new=[pg], needed=["pg-parity"],
                      covered=["pytest"], uncovered=[pg], undetermined=mode == "undetermined")
        if mode == "missing-covered":
            del answer["covered"]
        fx.service.classify = Mock(return_value=answer)
        baseline = dict(state="red" if mode == "fixes-base" else "green", pg_failures={})
        # not-required starts with a green PG baseline from the first candidate.
        if mode == "not-required":
            fx.run_n(1, 1, red(["a.py::first"]))
            fx.run_n(2, 1, red([pg]))
            fx.service.classify.side_effect = [dict(complete=True, pre_existing=[], new=["a.py::first"]), answer]
            scopes = [dict(checks=[window_gate.PYTEST_CHECK, window_gate.PG_CHECK]),
                      dict(checks=[window_gate.PYTEST_CHECK])]
        else:
            scopes = [dict(checks=[window_gate.PYTEST_CHECK, window_gate.PG_CHECK])]
        with patch.object(window_gate, "base_pg", return_value=(baseline, [])), \
             patch.object(window_gate, "scope", side_effect=scopes), \
             patch.object(window_gate, "unattributed_ids", return_value=[]), \
             patch.object(window_gate, "owners", return_value=["A"]):
            rc = fx.go(fixes_base=mode == "fixes-base")
        whys = [e["why"] for e in fx.events("escalation-blocking")]
        excused = mode in ("inherited", "missing-covered")
        if excused:
            check(rc == 2 and bool(fx.events("window-dropped")) and
                  all("base incomplete" not in why for why in whys), "base-kind PG baseline attribution " + mode)
        else:
            check(rc == 2 and fx.master() == fx.base and "base incomplete" in whys[-1],
                  "base-kind PG incomplete " + mode)
            check(not fx.events("window-dropped") if mode != "not-required" else
                  len(fx.events("window-dropped")) == 1, "base-kind no drop on uncovered PG pass " + mode)
            check("base undetermined" in whys[-1] if mode == "undetermined" else
                  "needs ['pg-parity']" in whys[-1], "base-kind PG reason " + mode)
        check(pg in fx.service.classify.call_args.args[2], "base-kind PG ids passed to classify " + mode)
    if len(FAILS) == start:
        print("base-kind AC4 status=0")

    start = len(FAILS)
    fx = Fx({"A": "a.txt"})
    cli = fx.tmp / "base-red-command"
    cli.write_text("#!/usr/bin/python3\nimport pathlib,sys,json\n"
                   "d=pathlib.Path(__file__).parent\n"
                   "sys.stdout.write((d/'answer').read_text())\n"
                   "sys.exit(int((d/'status').read_text()))\n")
    cli.chmod(0o755)
    valid = dict(partial, sha=fx.base, ids={"pytest": []})
    complete = dict(valid, complete=True, workflows=sorted(window_gate.BASE_RED_WORKFLOWS))
    cases = [("partial", 3, valid, True), ("complete", 0, complete, True)]
    for field, value in (("undetermined", ["unknown"]), ("workflows", ["unknown"]),
                         ("kinds_covered", ["unknown"]), ("ids", []), ("workflows", {}),
                         ("kinds_covered", {}), ("sha", "0" * 40), ("complete", True),
                         ("workflows", [1]), ("kinds_covered", [1]), ("complete", 0)):
        cases.append((field + repr(value), 3, dict(valid, **{field: value}), False))
    for field in ("workflows", "kinds_covered"):
        cases.append(("missing " + field, 3, {k: v for k, v in valid.items() if k != field}, False))
    cases.extend([("exit2", 2, valid, False), ("exit1", 1, valid, False),
                  ("false exit0", 0, valid, False),
                  ("single workflow complete", 0, dict(complete, workflows=["Python Tests"]), False),
                  ("bad-json", 3, "not-json", False)])
    with patch.object(window_gate, "BASE_RED_COMMAND", str(cli)):
        for name, status, answer, accepted in cases:
            (fx.tmp / "answer").write_text(answer if isinstance(answer, str) else json.dumps(answer))
            (fx.tmp / "status").write_text(str(status))
            got = window_gate.call_base_red("albert-scott", fx.base, False)
            check(got == (answer if accepted else {"sha": fx.base, "complete": False}),
                  "base-kind CLI validation " + name)
    if len(FAILS) == start:
        print("base-kind AC5 status=0")


def local_owner_ac1():
    """Real runner regression: branch tips, not the current checkout, own failures."""
    import contextlib
    import grading_env
    with tempfile.TemporaryDirectory(prefix="local-owner-") as temp:
        top = pathlib.Path(temp)
        repo = top / "repo"
        repo.mkdir()
        sh(["init", "-b", "main"], repo)
        path = repo / "test_named.py"
        path.write_text("def test_first(): assert True\ndef test_second(): assert True\n")
        sh(["add", "."], repo); sh(["commit", "-m", "base"], repo)
        base = sh(["rev-parse", "HEAD"], repo)
        entries = []
        for name, first, second in (("A", False, True), ("B", True, False), ("C", True, True)):
            sh(["checkout", "-B", name, base], repo)
            path.write_text(f"def test_first(): assert {first}\ndef test_second(): assert {second}\n")
            sh(["add", "."], repo); sh(["commit", "--allow-empty", "-m", name], repo)
            entries.append((name, name, sh(["rev-parse", "HEAD"], repo)))
        nodes = ["test_named.py::test_first", "test_named.py::test_second"]
        row = {"venv": "/opt/albert-scott/.venv", "node_dirs": []}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, DOIT_SCRATCH=str(top / "scratch")))
            stack.enter_context(patch.object(grading_env, "_project_row", return_value=row))
            stack.enter_context(patch("loadgate.begin", return_value=None))
            got = window_gate.local_owners(repo, "fixture", entries, nodes + nodes, window_base=base)
            assert got == {"A": nodes[:1], "B": nodes[1:]}, got
            sh(["checkout", "-B", "D", base], repo)
            path.write_text("def test_first(): assert False\ndef test_second(): assert False\n")
            (repo / "test_added.py").write_text("def test_new(): assert False\n")
            sh(["add", "."], repo); sh(["commit", "-m", "two failures plus newly added test"], repo)
            added = "test_added.py::test_new"
            tip = sh(["rev-parse", "HEAD"], repo)
            second = window_gate.local_owners(repo, "fixture", [("D", "D", tip)], nodes + [added], window_base=base)
            assert second == {"D": nodes + [added]}, second
            with patch("build_runner._run_tree", side_effect=AssertionError("empty input launched")):
                assert window_gate.local_owners(repo, "fixture", [], nodes, window_base=base) == {}
                assert window_gate.local_owners(repo, "fixture", entries, [], window_base=base) == {}
        return {"mapping": got, "multiple_and_added": second,
                "added_snapshot": {"member": tip, "baseline": base},
                "snapshots": [{"member": s, "sha": sha, "baseline": base}
                                                for s, _, sha in entries], "empty_inputs": "passed"}


@contextlib.contextmanager
def local_owner_fixture(db=False):
    import grading_env
    with tempfile.TemporaryDirectory(prefix="lo-") as temp:
        top = pathlib.Path(temp)
        repo = top / "repo"
        repo.mkdir()
        sh(["init", "-b", "main"], repo)
        path = repo / "test_named.py"
        path.write_text("def test_first(): assert True\ndef test_second(): assert True\n")
        sh(["add", "."], repo); sh(["commit", "-m", "base"], repo)
        base = sh(["rev-parse", "HEAD"], repo)
        row = {"venv": "/opt/albert-scott/.venv", "node_dirs": []}
        if db:
            row.update(db_env=["SCRATCH_PG_URL"], db_setup={"argv": ["/bin/true"]})
        def branch(name, code, parent=base):
            sh(["checkout", "-B", name, parent], repo)
            path.write_text(code)
            sh(["add", "."], repo); sh(["commit", "--allow-empty", "-m", name], repo)
            return (name, name, sh(["rev-parse", "HEAD"], repo))
        with patch.dict(os.environ, DOIT_SCRATCH=str(top / "scratch")), \
             patch.object(grading_env, "_project_row", return_value=row), \
             patch("loadgate.begin", return_value=None):
            yield top, repo, base, branch


def local_owner_ac2():
    import build_runner
    first, second = "test_named.py::test_first", "test_named.py::test_second"
    outcomes = {}
    with local_owner_fixture() as (top, repo, base, branch):
        cases = {
            "import": "import nonexistent_local_owner_module\ndef test_first(): assert False\n",
            "setup": "def setup_function(): assert False\ndef test_first(): assert False\n",
            "teardown": "def teardown_function(): assert False\ndef test_first(): assert False\n",
            "skip": "import pytest\n@pytest.mark.skip\ndef test_first(): assert False\n",
            "xfail": "import pytest\n@pytest.mark.xfail\ndef test_first(): assert False\n",
            "absent-selector": "def test_other(): assert False\n",
            "mixed": "import pytest\ndef test_first(): assert False\n@pytest.mark.skip\ndef test_second(): pass\n",
        }
        for name, code in cases.items():
            member = branch(name, code)
            ids = [first, second] if name == "mixed" else [first]
            got = window_gate.local_owners(repo, "fixture", [member], ids, window_base=base)
            assert got == {}, (name, got)
            outcomes[name] = "undetermined/no-owner"
        good = branch("introduced", "def test_first(): assert False\n")
        for name, member, pinned in (("missing-object", ("bad", "bad", "0" * 40), base),
                                     ("missing-branch", ("bad", "deleted-branch", good[2]), base),
                                     ("unresolved-base", good, "0" * 40)):
            assert window_gate.local_owners(repo, "fixture", [member], [first], window_base=pinned) == {}
            outcomes[name] = "no-owner"
        missing = branch("missing-file", "def test_first(): assert False\n")
        sh(["rm", "test_named.py"], repo); sh(["commit", "-m", "remove test"], repo)
        missing = (missing[0], missing[1], sh(["rev-parse", "HEAD"], repo))
        with patch.object(build_runner, "_run_tree", side_effect=AssertionError("absent member launched")) as run:
            assert window_gate.local_owners(repo, "fixture", [missing], [first], window_base=base) == {}
            assert not run.called
        outcomes["absent-member-file"] = "no provision"
        # A stale member's own merge-base is red even though the window base is fixed.
        stale_base = branch("old-red", "def test_first(): assert False\n")[2]
        stale = branch("stale", "def test_first(): assert False\n", stale_base)
        fixed = branch("fixed", "def test_first(): assert True\n", stale_base)[2]
        assert window_gate.local_owners(repo, "fixture", [stale], [first], window_base=fixed) == {}
        outcomes["stale-base"] = dict(member=stale[2], baseline=stale_base, window_base=fixed, owners={})
        bad_base = branch("uncertain-base", cases["import"])[2]
        repaired = branch("tip-on-uncertain", "def test_first(): assert False\n", bad_base)
        assert window_gate.local_owners(repo, "fixture", [repaired], [first], window_base=bad_base) == {}
        outcomes["undetermined-baseline"] = "no-owner"
        mixed = branch("independent-bad", cases["import"])
        got = window_gate.local_owners(repo, "fixture", [mixed, good], [first], window_base=base)
        assert got == {"introduced": [first]}, got
        outcomes["independent-member"] = got
        sentinel = top / "injected"
        unsafe = ["test_named.py::test_first;touch " + str(sentinel), "test_named.py::-x",
                  "/test_named.py::test_first", "../test_named.py::test_first", "suite-error:pytest",
                  "test_named.py::test_first$(touch x)", "test_named.py::test_first`touch x`"]
        with patch.object(build_runner, "_run_tree", side_effect=AssertionError("unsafe launch")) as run:
            for node in unsafe:
                assert window_gate.local_owners(repo, "fixture", [good], [node], window_base=base) == {}
            assert window_gate.local_owners(repo, "fixture", [("../escape", good[1], good[2])],
                                           [first], window_base=base) == {}
            assert not run.called and not sentinel.exists()
        outcomes["injection"] = "sentinel absent; no launch"
        for name, exception in (("timeout", build_runner.WallCap("deadline")),
                                ("provision-exception", RuntimeError("provision"))):
            with patch.object(build_runner, "_run_tree", side_effect=exception):
                assert window_gate.local_owners(repo, "fixture", [good], [first], window_base=base) == {}
            outcomes[name] = "no-owner"
        tail = top / "report.tail"
        def fake_report(payload, code=0):
            tail.write_text(payload)
            return ([{"exit": code, "tail": str(tail), "failing_ids": [first]}], [], [], [], {})
        valid = {"ids": [first], "collection_exit": 0, "exit": 1,
                 "inspection": {"collected": [first], "collection_error": False},
                 "phases": {"collected": [first], "collection_error": False,
                            "reports": {first: {"setup": "passed", "call": "failed", "teardown": "passed"}}}}
        for name, payload, code in (("exit1-only", "FAILED " + first, 1),
                                    ("traceback-only", "Traceback " + first, 0),
                                    ("absent-report", "", 0), ("malformed", "DOIT_LOCAL_RECHECK={", 0),
                                    ("signal", "DOIT_LOCAL_RECHECK=" + json.dumps(valid), -15)):
            with patch.object(build_runner, "_run_tree", side_effect=lambda *a, p=payload, c=code: fake_report(p, c)):
                assert window_gate.local_owners(repo, "fixture", [good], [first], window_base=base) == {}
            outcomes[name] = "no-owner"
        # Valid parameter spaces and brackets survive as literal argv entries.
        param = branch("param", "import pytest\n@pytest.mark.parametrize('value', [1], ids=['a b'])\ndef test_first(value): assert False\n")
        node = first + "[a b]"
        got = window_gate.local_owners(repo, "fixture", [param], [node], window_base=base)
        assert got == {"param": [node]}, got
        outcomes["literal-parameter"] = got
    return outcomes


def local_owner_ac3():
    """Append with production ev/drop_specs and read through fresh-process fold."""
    from datetime import datetime, timedelta, timezone
    import base_red
    import grading_env
    import lane_rules
    import nextstep
    import packet
    nodes = ["test_named.py::test_first", "test_named.py::test_second"]
    report = {}
    for mode in ("one", "two", "all", "none", "mixed", "second", "stale"):
        fx = Fx({"L-spec-9001": "a.txt", "L-spec-9002": "b.txt", "L-spec-9003": "c.txt"})
        sh(["checkout", "master"], fx.repo)
        for name in ("a.txt", "b.txt", "c.txt"):
            (fx.repo / name).write_text("ok")
        code = ("import pathlib, pytest\n"
                "def test_first(): assert pathlib.Path('a.txt').read_text() == 'ok'\n"
                "def test_second():\n"
                "    text = pathlib.Path('b.txt').read_text()\n"
                "    if text == 'uncertain': pytest.skip('uncertain')\n"
                "    assert text == 'ok'\n")
        (fx.repo / "test_named.py").write_text(code)
        sh(["add", "."], fx.repo); sh(["commit", "-m", "shared selected tests"], fx.repo)
        initial = sh(["rev-parse", "HEAD"], fx.repo)
        stale = None
        if mode == "stale":
            (fx.repo / "a.txt").write_text("red")
            sh(["add", "."], fx.repo); sh(["commit", "-m", "old red base"], fx.repo)
            old = sh(["rev-parse", "HEAD"], fx.repo)
            sh(["checkout", "-B", "br-L-spec-9001", old], fx.repo)
            (fx.repo / "a-member.txt").write_text("stale change")
            sh(["add", "."], fx.repo); sh(["commit", "-m", "stale member"], fx.repo)
            stale = sh(["rev-parse", "HEAD"], fx.repo)
            sh(["checkout", "master"], fx.repo)
            (fx.repo / "a.txt").write_text("ok")
            sh(["add", "."], fx.repo); sh(["commit", "-m", "later base fix"], fx.repo)
        fx.base = sh(["rev-parse", "HEAD"], fx.repo)
        for sid, path in (("L-spec-9001", "a.txt"), ("L-spec-9002", "b.txt"), ("L-spec-9003", "c.txt")):
            if stale and sid == "L-spec-9001":
                continue
            sh(["checkout", "-B", fx.branches[sid], fx.base], fx.repo)
            value = ("bad" if sid == "L-spec-9001" and mode in ("one", "two", "all", "mixed", "second") else
                     "bad" if sid == "L-spec-9002" and mode in ("two", "all", "second", "stale") else
                     "uncertain" if sid == "L-spec-9002" and mode == "mixed" else "ok")
            (fx.repo / path).write_text(value)
            sh(["add", "."], fx.repo); sh(["commit", "--allow-empty", "-m", sid], fx.repo)
        sh(["checkout", "live-scratch"], fx.repo)
        fx.service._RECHECK_PLUGIN = base_red._RECHECK_PLUGIN
        selected = nodes[:1] if mode in ("one", "none", "second") else nodes
        fx.run_n(1, 1, red(selected))
        fx.run_n(2, 1 if mode == "second" else 0, red(nodes[1:]) if mode == "second" else GREEN)
        for number in range(3, 9):
            fx.run_n(number, 0, GREEN)
        invocations = []
        original = window_gate.local_owners
        def observed(repo, project, entries, ids, **kw):
            last_red = [e for e in fold.read_events() if e["type"] == "window-tree-verified"][-1]
            assert last_red["new_ids"] == ids and last_red["green"] is False
            invocations.append(dict(members=[e[0] for e in entries], ids=ids, base=kw["window_base"]))
            return original(repo, project, entries, ids, **kw)
        members = list(fx.branches)[:2] if mode == "all" else list(fx.branches)
        with patch.object(grading_env, "_project_row", return_value={"venv": "/opt/albert-scott/.venv", "node_dirs": []}), \
             patch("loadgate.begin", return_value=None), \
             patch.object(window_gate, "local_owners", side_effect=observed):
            result = fx.go(specs=members)
        reader = subprocess.run(["/usr/bin/python3", "-c",
            "import sys,json;sys.path.insert(0,sys.argv[1]);import fold;print(json.dumps(fold.read_events()))", str(HERE.resolve())],
            env=dict(os.environ, DOIT_ROOT=str(fx.root)), capture_output=True, text=True, timeout=20)
        assert reader.returncode == 0, reader.stderr
        rows = json.loads(reader.stdout)
        drops = [r for r in rows if r["type"] == "window-dropped"]
        expected = {"one": ["L-spec-9001"], "two": ["L-spec-9001", "L-spec-9002"],
                    "all": ["L-spec-9001", "L-spec-9002"],
                    "none": [], "mixed": ["L-spec-9001"], "second": ["L-spec-9001", "L-spec-9002"],
                    "stale": ["L-spec-9002"]}[mode]
        assert [r["subject"] for r in drops] == expected, (mode, drops)
        assert result == (2 if mode == "all" else 1), (mode, result)
        if mode == "all":
            assert sh(["rev-parse", "master"], fx.repo) == fx.base and len(fx.calls()) == 1
        assert invocations and invocations[0]["ids"] == selected and invocations[0]["base"] == fx.base
        assert invocations[0]["members"] == members
        if mode == "second":
            assert len(invocations) == 2 and invocations[1]["ids"] == nodes[1:]
        assert bool([r for r in rows if r["type"] == "window-bisected"]) == (mode == "none")
        for drop in drops:
            owned = nodes[:1] if drop["subject"] == "L-spec-9001" else nodes[1:]
            assert drop["reason"] == "owner" and drop["evidence"] == "local-repro"
            assert drop["tests"] == drop["owned_nodes"] == drop["nodes"] == owned
            assert all(k in drop for k in ("branch", "window", "run_url", "quarantined", "unattributed_nodes"))
            sid = drop["subject"]
            before = datetime.now(timezone.utc) - timedelta(hours=1)
            fixtures = [dict(type="spec-written", subject=sid, ts=before.isoformat(), actor="spec-writer",
                             project="albert-scott", path=str(fx.tmp / "spec.md"), footprint=["a.txt"],
                             _src="L-spec-writer-fixture.jsonl:1"),
                        dict(type="build-done", subject=sid, ts=before.isoformat(), actor="builder", status="DONE",
                             project="albert-scott", _src="L-builder-fixture.jsonl:1")]
            evidence = fixtures + rows
            steps = nextstep.next_steps(evidence, {sid: {"state": "graded", "evs": fixtures + [drop]}},
                                       datetime.now(timezone.utc), lane_rules.load())
            assert any(s.item == sid and s.action == "rebuild" for s in steps), steps
            spec = fx.tmp / "spec.md"
            spec.write_text("## Verification\n```bash\n/usr/bin/python3 -c 'print(1)'\n```\n")
            verification = fx.tmp / "verify.sh"
            verification.write_text("#!/bin/bash\ntrue\n")
            args = types.SimpleNamespace(subject=sid, role="builder", project="albert-scott", repo=str(fx.repo),
                                         base_sha=fx.base, worktree=str(fx.repo), charter=None,
                                         produces=None, conventions=None, hint=None)
            with patch.object(fold, "read_events", return_value=evidence), \
                 patch.object(packet, "ROOT", fx.root), patch.object(packet, "CONTENT", fx.tmp), \
                 patch.object(packet, "write_verify_script", return_value=verification):
                ctx = packet.Ctx(args)
                lines = packet.p_builder(ctx)
            marker = next(i for i, line in enumerate(lines) if line.startswith("Dropped from window"))
            packet_nodes = [line.strip() for line in lines[marker + 1:] if line.startswith("   test_")]
            assert packet_nodes == owned, (packet_nodes, owned)
            drop["reader_rebuild"] = "rebuild"
            drop["packet_nodes"] = packet_nodes
        report[mode] = dict(exit=result, invocations=invocations, persisted_drops=drops,
                            window_events=[r for r in rows if r["type"] in ("window-tree-verified", "window-bisected")],
                            fresh_process_read=True)
    # The optional callback also runs before a recursive split. Old direct
    # callers still split immediately when they omit it.
    order = []
    def prepare(batch):
        order.append(("prepare", batch))
        return "candidate", batch, {}
    def judge(candidate, batch):
        return dict(state="red" if len(batch) > 1 else "green", nodes=nodes[:1], repo="fixture", paths={})
    def local(batch, result):
        order.append(("local", batch))
        return {batch[0]: nodes[:1]}
    def dropped(batch, result, reason):
        order.append(("drop", batch, reason))
    with patch.object(window_gate, "owners", return_value=[]):
        chosen = window_gate.bisect_window("recursive", ["A", "B", "C", "D"], prepare, judge,
                                           dropped, lambda _: None, local=local)
    assert chosen == ("candidate", ["B"]), (chosen, order)
    assert [(v[0], v[1]) for v in order if v[0] == "local"] == [("local", ["A", "B"]), ("local", ["C", "D"])]
    report["recursive"] = order
    return report


def local_owner_ac4():
    """Real private Postgres, runner environment, tail capture and teardown."""
    import build_runner
    import scratch
    source = '''
import os, pathlib, psycopg2
def test_first():
    assert os.environ['TMP'] == os.environ['TMPDIR'] == os.environ['TEMP']
    assert pathlib.Path(os.environ['TMPDIR']).is_dir()
    # The runner owns the endpoint; never read an operator env file.
    with psycopg2.connect(os.environ['SCRATCH_PG_URL']) as connection:
        assert connection.get_dsn_parameters()['host'].startswith('/')
        with connection.cursor() as cursor:
            cursor.execute('CREATE TABLE local_owner_fixture (value integer)')
            cursor.execute('INSERT INTO local_owner_fixture VALUES (1848)')
    with psycopg2.connect(os.environ['SCRATCH_PG_URL']) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT value FROM local_owner_fixture')
            assert cursor.fetchone() == (1848,)
    assert pathlib.Path('verdict').read_text() == 'green'
def test_unselected():
    raise RuntimeError('UNSELECTED SENTINEL EXECUTED')
'''
    with local_owner_fixture(db=True) as (top, repo, base, branch):
        sh(["checkout", "-B", "pg-base", base], repo)
        (repo / "test_named.py").write_text(source)
        (repo / "verdict").write_text("green")
        sh(["add", "."], repo); sh(["commit", "-m", "pg base"], repo)
        base = sh(["rev-parse", "HEAD"], repo)
        (repo / "verdict").write_text("red")
        sh(["add", "."], repo); sh(["commit", "-m", "pg red"], repo)
        tip = sh(["rev-parse", "HEAD"], repo)
        ids = ["test_named.py::test_first"]
        calls = []
        postgres_pids = []
        provision = build_runner._provision
        def provisioned(tree, auxiliary, row, env, deadline):
            summary = provision(tree, auxiliary, row, env, deadline)
            postgres_pids.append(int((auxiliary / "pg/data/postmaster.pid").read_text().splitlines()[0]))
            return summary
        original = build_runner._run_tree
        def recorded(*args):
            answer = original(*args)
            rows = answer[0]
            states = window_gate._local_phases(rows, ids)
            calls.append(dict(sha=args[1], states=states, env=answer[4],
                              tree=rows[0]["cwd"], selected=ids))
            return answer
        with patch.object(build_runner, "_run_tree", side_effect=recorded), \
             patch.object(build_runner, "_provision", side_effect=provisioned):
            got = window_gate.local_owners(repo, "fixture", [("pg", "pg-base", tip)], ids, window_base=base)
        assert got == {"pg": ids}, (got, calls)
        assert [c["sha"] for c in calls] == [tip, base]
        assert [c["states"][ids[0]] for c in calls] == ["red", "green"]
        assert all(c["env"]["pg"] == "private" for c in calls)
        assert all(not pathlib.Path(c["tree"]).exists() for c in calls)
        assert not list(scratch.sub("runner").iterdir())
        # Run-level deadline kills the process group and runner cleanup stops PG.
        sleeping = branch("timeout", "import time\ndef test_first(): time.sleep(30)\n", base)
        until = time.monotonic() + 5
        with patch.object(build_runner, "_provision", side_effect=provisioned):
            assert window_gate.local_owners(repo, "fixture", [sleeping], ids,
                                           window_base=base, deadline=until) == {}
        assert time.monotonic() - until < 5
        assert not list(scratch.sub("runner").iterdir())
        for pid in postgres_pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("scratch PostgreSQL process survived cleanup")
        # No local environment/temp implementation: installed _run_tree owns it.
        import inspect
        implementation = inspect.getsource(window_gate.local_owners)
        assert "build_runner._run_tree" in implementation and "short_tmpdir" not in implementation
        return dict(mapping=got, calls=calls, postgres_round_trip="committed/fresh-connection",
                    sentinel="unselected", cleanup="success/failure/timeout passed")


def local_owner_ac5():
    import build_runner
    import grading_env
    import loadgate
    ids = ["test_named.py::test_first"]
    outcomes = {}
    with local_owner_fixture() as (top, repo, base, branch):
        good = branch("A", "def test_first(): assert False\n")
        other = branch("B", "def test_first(): assert False\n")
        with patch.object(grading_env, "_project_row", return_value={}), \
             patch.object(build_runner, "_run_tree", side_effect=AssertionError("unconfigured launched")) as run:
            assert window_gate.local_owners(repo, "unconfigured", [good], ids, window_base=base) == {}
            assert not run.called
        with patch.object(build_runner, "_run_tree", side_effect=AssertionError("missing file launched")) as run:
            assert window_gate.local_owners(repo, "fixture", [good], ["missing.py::test_x"], window_base=base) == {}
            assert not run.called
        outcomes["short-circuits"] = "missing row and member file: zero launches"
        original = build_runner._run_tree
        active, maximum, calls, deadlines = 0, 0, [], []
        def recorded(*args):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            calls.append(args[1]); deadlines.append(args[-1])
            try:
                assert len(args[4]) == 1 and args[4][0][0] == "local-owner"
                return original(*args)
            finally:
                active -= 1
        admissions = []
        class Gate:
            def __init__(self, held=False):
                self.held = held
            def check(self, item, role, **kw):
                assert role == "grader" and kw["action"] == "grade"
                admissions.append((item, role))
                return types.SimpleNamespace(held=self.held, would_deny=True)
        def begin(**kw):
            assert kw["running"]["window_gate"] == 1 and kw["allow_enforce"] is True
            return Gate()
        cache = {}
        started = time.monotonic()
        with patch.object(build_runner, "_run_tree", side_effect=recorded), \
             patch.object(loadgate, "begin", side_effect=begin), \
             patch.object(build_runner, "run", side_effect=AssertionError("wide run")), \
             patch.object(window_gate, "verify", side_effect=AssertionError("hosted call")):
            got = window_gate.local_owners(repo, "fixture", [good, other], ids + ids,
                                           window_base=base, cache=cache)
            assert got == {"A": ids, "B": ids}, got
            count = len(calls)
            assert window_gate.local_owners(repo, "fixture", [good, other], ids,
                                           window_base=base, cache=cache) == got
            assert len(calls) == count == len(set(calls)) == 3
        assert maximum == 1 and len(admissions) == 3
        assert all(started < d <= time.monotonic() + 300 for d in deadlines)
        assert deadlines[0] == deadlines[1], "tip/baseline must share the member deadline"
        outcomes["sequential-cache"] = dict(launches=len(calls), maximum=maximum, admission_checks=len(admissions))
        with patch.object(loadgate, "begin", return_value=Gate(held=True)), \
             patch.object(build_runner, "_run_tree", side_effect=AssertionError("held launched")) as run:
            assert window_gate.local_owners(repo, "fixture", [good], ids, window_base=base) == {}
            assert not run.called
        outcomes["held"] = "zero launches, no wait/retry"
        with patch.object(build_runner, "_run_tree", side_effect=recorded), \
             patch.object(loadgate, "begin", return_value=None):
            until = time.monotonic() + 30
            assert window_gate.local_owners(repo, "fixture", [good], ids, window_base=base, deadline=until) == {"A": ids}
            assert deadlines[-2:] == [until, until]
        outcomes["off-and-enclosing-deadline"] = "passed"
        # Preserve the frozen legacy alarm and place a second deadline outside it.
        before = time.monotonic()
        proc = subprocess.run(["/usr/bin/python3", str(HERE / "test_window_failure_disposition.py")],
                              capture_output=True, text=True, timeout=170)
        assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-3000:])
        elapsed = time.monotonic() - before
        assert elapsed < 170
        outcomes["frozen-disposition"] = dict(exit=proc.returncode, elapsed_s=round(elapsed, 2),
                                              result=proc.stderr.splitlines()[-1:])
    return outcomes


if "--local-owner-ac" in sys.argv:
    selected = sys.argv[sys.argv.index("--local-owner-ac") + 1:]
    cases = {"AC1": local_owner_ac1, "AC2": local_owner_ac2, "AC3": local_owner_ac3,
             "AC4": local_owner_ac4, "AC5": local_owner_ac5}
    assert len(selected) == 1 and selected[0] in cases, "missing local-owner AC case"
    body = cases[selected[0]]()
    raw = json.dumps(body, sort_keys=True).encode()
    print(json.dumps(dict(url="cli://L-spec-1848/" + selected[0], status=0,
                          body_sha256=hashlib.sha256(raw).hexdigest(), body_excerpt=body)))
    sys.exit(0)


if '--window-chain-check' not in sys.argv:
    base_kind_checks()
def honest_rate_checks():
    """L-spec-1578: production append/read fixtures, no live GitHub calls."""
    from datetime import datetime, timedelta, timezone
    passed_cases = []
    def report(ac, start, body):
        encoded = json.dumps(body, sort_keys=True).encode()
        passed = len(FAILS) == start
        print(json.dumps(dict(url="cli://honest-rate/" + ac, status=0 if passed else 1,
                              body_sha256=hashlib.sha256(encoded).hexdigest(), body_excerpt=body)))
        if passed:
            passed_cases.append(ac)

    start = len(FAILS)
    now = datetime.now(timezone.utc)
    def event(kind, name, **fields):
        return dict(type=kind, subject=name, ts=now.isoformat(), **fields)
    events = [event("window-verified", "g1", dropped=[]), event("window-verified", "g2", dropped=[]),
              event("window-verified", "r", dropped=["S"]),
              event("escalation-blocking", "u", outcome="undetermined"),
              event("escalation-blocking", "b", outcome="base-not-releasable")]
    rates = [window_gate.red_rate(events, now)]
    check(rates[0] == dict(rate=.6, red=1, undetermined=2, other=0, non_green=3, windows=5), "honest AC1 counts")
    rates.append(window_gate.red_rate(events + [event("window-opened", "o"), event("escalation-blocking", "o")], now))
    check(rates[-1]["other"] == 1 and rates[-1]["non_green"] == 4, "honest AC1 outcome-less")
    early = (now - timedelta(minutes=1)).isoformat()
    small = [[event("escalation-blocking", "x", outcome="undetermined") | {"ts": early}, event("window-verified", "x", dropped=[])],
             [event("window-dropped", "S", window="x", nodes=["node"]), event("window-verified", "x", dropped=[])],
             [event("window-verified", "x", dropped=[]), event("escalation-blocking", "x", outcome="undetermined") | {"ts": early}]]
    for rows, red_count, non_green in zip(small, (0, 1, 0), (0, 1, 0)):
        rate = window_gate.red_rate(rows, now)
        check(rate["red"] == red_count and rate["non_green"] == non_green and rate["rate"] is None, "honest AC1 chronology/sticky/threshold")
    check(window_gate.red_rate([event("window-verified", "x", dropped=[]) | {"ts": "bad"}], now)["windows"] == 0,
          "honest AC1 malformed timestamp")
    with tempfile.TemporaryDirectory() as tmp:
        config = pathlib.Path(tmp) / "look.toml"
        config.write_text("[window_gate]\ntrain_depth = 2\n")
        with patch.object(window_gate.shutil, "disk_usage", return_value=types.SimpleNamespace(total=100, used=50)):
            depths = [window_gate.train_depth({}, config, root=tmp, events=rows, now=now) for rows in
                      (events, [event("window-verified", str(i), dropped=["S"] if i < 3 else []) for i in range(5)])]
    check(depths[0]["depth"] == 2 and depths[0]["reason"] == "configured" and depths[0]["red_rate"] == .2 and
          depths[1]["depth"] == 1 and depths[1]["reason"] == "red-rate-over-0.40", "honest AC1 train stays red-only")
    report("AC1", start, dict(rates=rates, depths=depths))

    start = len(FAILS)
    fx = Fx({})
    def job(seconds, **fields):
        return dict(runner_name="Hosted Agent", labels=["ubuntu-latest"], status="completed",
                    started_at="2026-10-20T00:00:00Z", completed_at=(datetime(2026, 10, 20, tzinfo=timezone.utc)
                    + timedelta(seconds=seconds)).isoformat()) | fields
    pages = json.dumps(dict(jobs=[job(61), job(20)])) + "\n" + json.dumps(dict(jobs=[
        job(0, runner_name=None), job(59, run_attempt=2)]))
    (fx.fdir / "jobs-10").write_text(pages)
    (fx.fdir / "jobs-11").write_text(json.dumps(dict(jobs=[job(90, labels=["self-hosted"])])))
    docs = [dict(checks={"a": {"run_id": 10}, "b": {"run_id": "10", "state": "cached"}}),
            dict(checks={"c": {"state": "ran", "run_id": 11}})]
    with patch.object(sys.modules["base_red"], "_owner", return_value="o/r"), \
         patch.object(ghlimit, "gate", return_value={"ok": True}) as gate, \
         patch.object(window_gate.subprocess, "run", wraps=subprocess.run) as calls, \
         patch.object(ghlimit, "sleep", side_effect=AssertionError("must not sleep")):
        result = window_gate.run_minutes(fx.repo, docs)
        argv = [c.args[0] for c in calls.call_args_list]
        check(result == dict(actions_minutes=4, selfhosted_minutes=2, minutes_source="measured"), "honest AC2 measured totals")
        check(len(argv) == 2 and all(a[0] == os.environ["DOIT_REAL_GH"] and a[1] == "api" and
              "jobs?filter=all&per_page=100" in a[2] and "--paginate" in a and "--slurp" not in a for a in argv),
              "honest AC2 API pagination once per id")
        check(all(c.kwargs["timeout"] == 60 for c in calls.call_args_list) and
              all(c.kwargs == {"wait": False} for c in gate.call_args_list), "honest AC2 bounded nonwaiting")
        fallback = []
        for bad in ("not JSON", "{}", "[]", json.dumps(dict(jobs=[])), json.dumps(dict(jobs=[None])),
                    json.dumps(dict(jobs=[job(-1)])), json.dumps(dict(jobs=[job(61, started_at=None)])),
                    json.dumps(dict(jobs=[job(61, completed_at=None)])),
                    json.dumps(dict(jobs=[job(0, runner_name=None, status="queued")])),
                    json.dumps(dict(jobs=[job(1, labels="bad")])),
                    json.dumps(dict(jobs=[job(1, completed_at="bad")]))):
            (fx.fdir / "jobs-10").write_text(bad)
            fallback.append(window_gate.run_minutes(fx.repo, docs))
        for doc in (dict(checks={"a": "ran"}), {}, dict(checks={"a": {"run_id": 10}, "b": "ran"})):
            fallback.append(window_gate.run_minutes(fx.repo, [doc]))
        for failure in (types.SimpleNamespace(returncode=1, stdout="", stderr="failure"),
                        subprocess.TimeoutExpired("gh", 60),
                        types.SimpleNamespace(returncode=1, stdout="", stderr="API rate limit exceeded")):
            kw = dict(side_effect=failure) if isinstance(failure, Exception) else dict(return_value=failure)
            with patch.object(subprocess, "run", **kw) as api, patch.object(ghlimit, "_mark_exhausted") as exhausted, \
                 patch.object(ghlimit, "_bump_spent") as spent:
                fallback.append(window_gate.run_minutes(fx.repo, docs))
                check(api.call_count == 1 and spent.call_count == 1, "honest AC2 failure one charged attempt")
                check(exhausted.call_count == int(getattr(failure, "stderr", "") == "API rate limit exceeded"), "honest AC2 quota exhausted")
        with patch.object(ghlimit, "gate", return_value={"ok": False}), patch.object(subprocess, "run") as api:
            fallback.append(window_gate.run_minutes(fx.repo, docs))
            check(api.call_count == 0, "honest AC2 closed quota")
        with patch.object(sys.modules["base_red"], "_owner", side_effect=ValueError("local origin")), \
             patch.object(ghlimit, "gate") as no_gate:
            fallback.append(window_gate.run_minutes(fx.repo, docs))
            check(no_gate.call_count == 0, "honest AC2 slug before quota")
        check(all(r["minutes_source"] == "estimate" for r in fallback), "honest AC2 all failures estimate")
        (fx.fdir / "jobs-10").write_text(pages)
        mixed = window_gate.run_minutes(fx.repo, [dict(checks={"a": {"run_id": 10}, "b": {"state": "cached"}})])
        check(mixed["minutes_source"] == "measured" and mixed["actions_minutes"] == 4, "honest AC2 undispatched check skipped")
        check(window_gate.run_minutes(fx.repo, [dict(checks={"a": {"state": "cached"}})])["minutes_source"] == "estimate",
              "honest AC2 nonempty docs need run id")
        check(window_gate.run_minutes(fx.repo, []) == dict(actions_minutes=0, selfhosted_minutes=0, minutes_source="measured"),
              "honest AC2 all cache zero")
    quota = json.loads(pathlib.Path(os.environ["GH_LIMIT_STATE"]).read_text())
    check(quota["spent_since_check"] > 0, "honest AC2 temporary quota charged")
    report("AC2", start, dict(result=result, argv=argv, fallbacks=len(fallback), mixed=mixed, quota_spent=quota["spent_since_check"]))

    start = len(FAILS)
    candidate_scope = dict(checks=["pytest"], everything=False, reason="honest-rate fixture")
    evidence = []
    for state in ("green", "red", "undetermined"):
        fx = Fx({"S": "api/tests/test_x.py"})
        doc = dict(verdict=state, checks={"pytest": {"state": "ran", "run_id": 10}},
                   new_failures=["api/tests/test_x.py::bad"] if state == "red" else [])
        fx.run_n(1, {"green": 0, "red": 1, "undetermined": 2}[state], json.dumps(doc))
        (fx.fdir / "jobs-10").write_text(pages)
        with patch.object(ghlimit, "gate", return_value={"ok": True}), \
             patch.object(window_gate, "scope", return_value=candidate_scope):
            rc = fx.go()
        # Fresh read from the production JSONL append store, independent of fold's reader.
        fresh = [json.loads(line) for file in fold.EVENTS.glob("*.jsonl") for line in file.read_text().splitlines()]
        measured = [e for e in fresh if e["type"] == "window-run-minutes"]
        check(len(measured) == 1, "honest AC3 exactly one " + state)
        if measured:
            row = measured[0]
            check(row["subject"] == row["window"] == "w1" and row["candidate"] == fx.events("window-candidate")[0]["sha"] and
                  row["state"] == state and row["run_ids"] == ["10"] and row["minutes_source"] == "measured" and
                  row["actions_minutes"] == 4 and row["selfhosted_minutes"] == 0, "honest AC3 fields " + state)
            check(all(fold.admits(row, actor) is None for actor in ("executor", "tick", "operator")) and
                  fold.admits(row, "builder") is not None, "honest AC3 actors")
            check(bool(fold.required_reason(dict(row, minutes_source=""))), "honest AC3 required source")
            evidence.append(dict(state=state, exit=rc, event=row))
        check(rc == (0 if state == "green" else 2), "honest AC3 verdict " + state)
        if state == "red":
            check(bool(fx.events("window-dropped")), "honest AC3 attributed drop")
        if state == "green":
            candidate = fx.master()
            sh(["update-ref", "refs/heads/master", fx.base, candidate], fx.repo)
            before = len(measured)
            with patch.object(window_gate, "scope", return_value=candidate_scope):
                check(fx.go(window="cached") == 0 and len(fx.events("window-run-minutes")) == before, "honest AC3 cache no emit")
    for mode in ("no-ids", "fetch-error", "emit-error", "cancel-after", "cancel-inside"):
        fx = Fx({"S": "api/tests/test_x.py"})
        fx.run_n(1, 0, GREEN if mode == "no-ids" else json.dumps(dict(verdict="green", checks={"pytest": {"state": "ran", "run_id": 10}})))
        (fx.fdir / "jobs-10").write_text(pages)
        real_attempt = window_gate._run_attempt
        real_minutes, real_verify, real_wait, real_ev = window_gate.run_minutes, window_gate.verify, window_gate.wait_predecessor, window_gate.ev
        fetch_failed = []
        def minutes(*a, **kw):
            if mode == "fetch-error" and not fetch_failed:
                fetch_failed.append(True)
                raise ValueError("fixture fetch failure")
            return real_minutes(*a, **kw)
        def emit(kind, *a, **kw):
            if mode == "emit-error" and kind == "window-run-minutes":
                raise ValueError("fixture emit failure")
            return real_ev(kind, *a, **kw)
        def verify(*a, **kw):
            if mode == "cancel-inside" and a[1] != fx.base:
                raise window_gate.TrainCancelled("fixture cancellation")
            return real_verify(*a, **kw)
        def attempt(*a, **kw):
            if mode == "cancel-after":
                a = (*a[:-1], {"fixture": True})
            return real_attempt(*a, **kw)
        def wait(*a, **kw):
            if mode == "cancel-after" and fx.events("window-run-minutes"):
                raise window_gate.TrainCancelled("fixture cancellation")
            return real_wait(*a, **kw)
        with patch.object(ghlimit, "gate", return_value={"ok": True}), \
             patch.object(window_gate, "scope", return_value=candidate_scope), \
             patch.object(window_gate, "run_minutes", side_effect=minutes), \
             patch.object(window_gate, "ev", side_effect=emit), \
             patch.object(window_gate, "verify", side_effect=verify), \
             patch.object(window_gate, "wait_predecessor", side_effect=wait), \
             patch.object(window_gate, "_run_attempt", side_effect=attempt):
            rc = fx.go()
        rows = fx.events("window-run-minutes")
        check(len(rows) == int(mode == "cancel-after"), "honest AC3 exclusions/error isolation " + mode)
        check(rc == (2 if mode.startswith("cancel") else 0), "honest AC3 unchanged verdict " + mode)
    report("AC3", start, evidence)
    # Keep the grep -q markers last: pipefail must not see a SIGPIPE from printf.
    for ac in passed_cases:
        print("honest-rate " + ac + " status=0")


def proof_1664(ac, before):
    if len(FAILS) == before:
        body = {"marker": "L-spec-1664 "+ac+" ok", "failures": len(FAILS)-before}
        print(json.dumps(dict(url="cli://window-gate/1664/"+ac, status=0,
                              body_sha256=hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                              body_excerpt=body)))
        print(body["marker"])


def ac4_quarantined_never_drops():
    import quarantine
    from datetime import datetime, timezone
    start = len(FAILS)
    node = "api/tests/test_x.py::test_a"
    charged = "api/tests/test_z.py::test_z"
    def activate():
        quarantine.record("albert-scott", node, "base-red", ["previous"], now=datetime.now(timezone.utc))
    for mode in ("only", "mixed", "empty", "unnamed", "all"):
        fx = Fx({"A": "api/tests/test_z.py", "B": "base.txt"})
        if mode != "empty":
            activate()
        if mode in ("only", "all"):
            fx.run_n(1, 1, red([node]))
        elif mode == "mixed":
            fx.run_n(1, 1, red([node, charged]))
            fx.run_n(2, 0, GREEN)
        elif mode == "unnamed":
            fx.run_n(1, 1, red([]))
        else:
            fx.run_n(1, 0, GREEN)
        rc = fx.go()
        if mode == "unnamed":
            check(rc == 2 and not fx.events("window-dropped"), "AC4 unnamed remains undetermined")
            check(fx.events("escalation-blocking")[-1].get("outcome") == "undetermined", "AC4 unnamed outcome")
        elif mode == "mixed":
            rows = fx.events("window-dropped")
            check(rc == 1 and [e["subject"] for e in rows] == ["A"], "AC4 mixed only charged owner dropped")
            check(all(e["nodes"] == [charged] and e["quarantined"] == [node] for e in rows), "AC4 drop exposes quarantine")
        else:
            check(rc == 0 and not fx.events("window-dropped"), "AC4 ships all " + mode)
            rows = fx.events("window-verified")
            check(rows[-1]["kept"] == ["A", "B"], "AC4 both specs ship " + mode)
            check(rows[-1]["quarantined"] == ([] if mode == "empty" else [node]), "AC4 quarantine list " + mode)
            check(isinstance(rows[-1]["pre_existing"], list), "AC4 pre_existing list")
            check(not fx.events("escalation-blocking"), "AC4 excused failures never undetermined")
    proof_1664("AC4", start)


def ac6_recheck_before_drop():
    start = len(FAILS)
    node = "api/tests/test_x.py::test_a"
    for state in ("red", "green", "absent", "undetermined", "raises", "cached", "incomplete"):
        fx = Fx({"A": "api/tests/test_x.py", "B": "base.txt"})
        fx.run_n(1, 1, red([node]))
        fx.run_n(2, 0, GREEN)
        calls = []
        def recheck(project, sha, ids):
            calls.append((sha, list(ids), len(fx.events("window-dropped"))))
            if state == "raises":
                raise RuntimeError("local runner unavailable")
            return {key: list(ids) if key == state else [] for key in ("red", "green", "absent", "undetermined")}
        fx.service.recheck = recheck
        if state == "cached":
            fx.known.add(node)
        if state == "incomplete":
            fx.service.classify = lambda *a, **kw: {"complete": False}
        rc = fx.go()
        if state in ("red", "cached"):
            check(rc == 0 and not fx.events("window-dropped"), "AC6 base-red ships " + state)
            check(fx.events("window-verified")[-1]["quarantined"] == [node], "AC6 base red quarantine reported")
            rows = fx.events("test-quarantined")
            check(len(rows) == 1 and rows[0]["rule"] == "base-red", "AC6 records base red once")
        elif state == "incomplete":
            check(rc == 2 and not fx.events("window-dropped") and not calls, "AC6 incomplete unchanged")
        else:
            rows = fx.events("window-dropped")
            check(rc == 1 and [r["subject"] for r in rows] == ["A"], "AC6 charged result " + state)
            expected = "undetermined" if state == "raises" else state
            check(rows[0]["rechecked"] == {node: expected}, "AC6 rechecked result visible " + state)
            check(rows[0]["recheck_undetermined"] == ([node] if expected == "undetermined" else []), "AC6 uncertainty visible")
        if state not in ("cached", "incomplete"):
            check(len(calls) == 1 and calls[0][0] == fx.base and calls[0][2] == 0, "AC6 recheck before drop " + state)
        else:
            check(not calls, "AC6 cached/incomplete never rerun")
    for all_red in (False, True):
        fx = Fx({"A": "api/tests/test_x.py", "B": "api/tests/test_z.py", "C": "api/app/lib.py", "D": "d.txt"})
        ids = ["api/tests/test_x.py::base_red", "api/tests/test_z.py::new", "api/app/lib.py::unknown"]
        fx.known = {"old1.py::old", "old2.py::old", "old3.py::old"}
        fx.run_n(1, 1, red(ids))
        fx.run_n(2, 0, GREEN)
        order = []
        def mixed_recheck(project, base, nodes):
            order.append((base, list(nodes), len(fx.events("window-dropped"))))
            return {"red": list(ids) if all_red else ids[:1],
                    "green": [] if all_red else ids[1:2], "absent": [],
                    "undetermined": [] if all_red else ids[2:]}
        fx.service.recheck = mixed_recheck
        rc = fx.go()
        rows = fx.events("window-dropped")
        check(len(order) == 1 and order[0][0] == fx.base and set(order[0][1]) == set(ids)
              and order[0][2] == 0, "AC6 c153 recheck before any drop")
        if all_red:
            check(rc == 0 and not rows and fx.events("window-verified")[-1]["kept"] == ["A", "B", "C", "D"], "AC6 all rechecked red ships every spec")
        else:
            check(rc == 1 and sorted(r["subject"] for r in rows) == ["B", "C"], "AC6 c153 green and unknown owners charged")
            check(all(r["nodes"] == sorted(ids[1:]) and r["quarantined"] == ids[:1]
                      and r["recheck_undetermined"] == ids[2:] for r in rows), "AC6 c153 reasons visible on drops")
            check(fx.events("window-verified")[-1]["recheck_undetermined"] == ids[2:], "AC6 c153 unknown visible on verified")
        check(all(ids[0] not in r["nodes"] for r in rows), "AC6 base red absent from every charged nodes list")
        check(all(r["rule"] == "base-red" for r in fx.events("test-quarantined")), "AC6 c153 base-red quarantine rule")
    proof_1664("AC6", start)


def ac7_tree_guard_attribution():
    start = len(FAILS)
    guard = window_gate.TREE_GUARDS[0] + "test_no_absolute_test_to_test_imports_under_tests_root"
    node = "api/tests/test_x.py::test_a"
    unknown = "api/tests/test_z.py::test_unknown"
    for mode in ("first", "second-prefix", "last", "twice", "owned", "same-owner", "cached", "base-red", "base-unknown", "prefix-unknown", "disagree", "unattributed", "half", "second", "second-mixed", "nonlinear"):
        fx = Fx({"A": "api/tests/test_x.py", "B": "base.txt", "C": "api/app/lib.py", "D": "d.txt"})
        first_ids = [node] if mode in ("second", "second-mixed") else [guard]
        if mode in ("owned", "same-owner"):
            first_ids.append(node)
        if mode == "unattributed":
            first_ids.append(unknown)
        fx.run_n(1, 1, red(first_ids))
        fx.run_n(2, 1, red([guard]+([unknown] if mode == "second-mixed" else [])))
        if mode not in ("second", "second-mixed", "half", "unattributed", "twice"):
            fx.run_n(2, 0, GREEN)
        # A guard found only while bisecting must keep that half pending.
        if mode == "half":
            fx.run_n(1, 1, red([unknown]))
            fx.run_n(2, 1, red([guard]))
            fx.run_n(3, 0, GREEN)
        if mode in ("unattributed", "second-mixed"):
            fx.run_n(3, 0, GREEN)
            fx.run_n(4, 0, GREEN)
        if mode == "cached":
            fx.known.add(guard)
        prefix_calls = []
        base_calls = []
        prefix_state, builds = {}, []
        real_build, real_prefixes = window_gate.build_candidate, window_gate.guard_prefixes
        def build(*args, **kwargs):
            builds.append(args[3])
            return real_build(*args, **kwargs)
        def prefixes_before_attribution(*args, **kwargs):
            prefix_state.update(candidate=sh(["rev-parse", "refs/heads/window/w1"], fx.repo),
                                builds=len(builds), drops=len(fx.events("window-dropped")), events=[e for e in fx.events() if e["type"] in
                                ("rechained", "rechain-declined", "rechain-fingerprint")])
            return real_prefixes(*args, **kwargs)
        real_owners = window_gate.owners
        def owners(repo, rev, n, touched):
            check(n != guard, "AC7 guard never passed to owners")
            return real_owners(repo, rev, n, touched)
        def recheck(project, sha, ids):
            if sha == fx.base:
                base_calls.append(list(ids))
                state = "red" if mode == "base-red" else "undetermined" if mode == "base-unknown" else "green"
                return {key: list(ids) if key == state else [] for key in ("red", "green", "absent", "undetermined")}
            prefix_calls.append(sha)
            candidate = sh(["rev-parse", "refs/heads/window/w1"], fx.repo)
            check(candidate == prefix_state["candidate"] and len(builds) == prefix_state["builds"], "AC7 prefix attribution neither rebuilds nor moves the candidate")
            check([e for e in fx.events() if e["type"] in ("rechained", "rechain-declined", "rechain-fingerprint")] == prefix_state["events"], "AC7 prefix attribution emits no rechain events")
            check(len(fx.events("window-dropped")) == prefix_state["drops"], "AC7 prefix runs append no drops")
            # Side-effect-free prefix reads leave the candidate ref and rechain events alone.
            all_events = [(e["type"], e["_src"]) for e in fx.events()]
            prefixes = sh(["rev-list", "--first-parent", fx.base+".."+candidate], fx.repo).splitlines()[::-1]
            if mode == "disagree":
                state = "green"
            elif mode == "prefix-unknown" and sha != candidate:
                state = "undetermined"
            else:
                state = "red" if (mode in ("first", "same-owner") or sha == prefixes[-1] or (mode in ("second-prefix", "twice") and prefixes.index(sha) >= 1)) else "green"
            check(sh(["rev-parse", "refs/heads/window/w1"], fx.repo) == candidate, "AC7 candidate ref unchanged")
            check([(e["type"], e["_src"]) for e in fx.events()] == all_events, "AC7 no rechain prefix events")
            return {key: list(ids) if key == state else [] for key in ("red", "green", "absent", "undetermined")}
        fx.service.recheck = recheck
        with patch.object(window_gate, "owners", side_effect=owners), \
             patch.object(window_gate, "build_candidate", side_effect=build), \
             patch.object(window_gate, "guard_prefixes", side_effect=prefixes_before_attribution):
            if mode == "nonlinear":
                with patch.object(window_gate, "guard_prefixes", return_value=None):
                    rc = fx.go()
            else:
                rc = fx.go()
        rows = fx.events("window-dropped")
        if mode not in ("unattributed", "half", "second-mixed"):
            check(not fx.events("window-bisected"), "AC7 pure guards never enter bisection")
        if mode in ("base-unknown", "prefix-unknown", "disagree", "nonlinear"):
            check(rc == 2 and not rows, "AC7 uncertain guard nobody dropped " + mode)
            check(fx.events("escalation-blocking")[-1].get("outcome") == "undetermined", "AC7 guard undetermined outcome")
        elif mode in ("cached", "base-red"):
            check(rc == 0 and not rows, "AC7 base-red guard excused " + mode)
            check(not prefix_calls, "AC7 excused guard no prefix runs")
        elif mode in ("first", "second-prefix", "last"):
            expected = "A" if mode == "first" else "B" if mode == "second-prefix" else "D"
            check(rc == 1 and [e["subject"] for e in rows] == [expected], "AC7 only first red prefix owner " + mode)
            check(rows[0]["nodes"] == [guard], "AC7 drop names guard")
            check(len(prefix_calls) <= 4, "AC7 bounded prefix runs")
        elif mode == "twice":
            check(rc == 2 and [r["subject"] for r in rows] == ["B", "C"], "AC7 guard-only runs 1 and 2 charge only their prefix owners")
            check(all(r["nodes"] == [guard] for r in rows), "AC7 guard-only run2 names only guards")
            check(fx.events("escalation-blocking")[-1].get("outcome") == "red", "AC7 guard-only run2 red outcome")
        elif mode == "second":
            check(rc == 2 and [e["subject"] for e in rows] == ["A", "D"], "AC7 run2 guard only its owner dropped")
            check(rows[-1]["nodes"] == [guard], "AC7 run2 guard node")
        elif mode in ("owned", "same-owner"):
            expected = ["A", "D"] if mode == "owned" else ["A"]
            check(rc == 1 and [r["subject"] for r in rows] == expected, "AC7 owned non-guard combines with prefix charges")
            guard_row = next(r for r in rows if guard in r["nodes"])
            check(guard_row["nodes"] == ([guard] if mode == "owned" else [node, guard]), "AC7 guard row includes only its own non-guard charges")
        else:
            check(not prefix_calls, "AC7 non-guard path precedence " + mode)
            check(all(guard not in r["nodes"] for r in rows), "AC7 bisection never blames guards " + mode)
            check(fx.events("window-bisected"), "AC7 existing bisection kept " + mode)
    proof_1664("AC7", start)


def ac8_tree_guards_config():
    start = len(FAILS)
    seed = (
        "tests/test_820_no_abs_test_imports.py::",
        "tests/test_496_r3_guard.py::",
        "tests/test_207_monitoring_denoise.py::TestDigestFoldCronStale::test_query_cron_runs_stale_returns_data_trust_when_failed",
        "tests/deploy/test_ship_snapshot.py::test_ac14_line_counts",
        "tests/deploy/test_protected_stamp.py::test_ac12_deploy_sh_line_count_not_grown",
        "api/tests/profit_v2/test_read_model_refresh.py::test_ac9_deploy_sh_net_zero",
    )
    check(window_gate.tree_guards({}) == seed and window_gate.TREE_GUARDS == seed, "AC8 configured six seeds")
    print(json.dumps(seed))
    with tempfile.TemporaryDirectory(prefix="tree-guards-") as temp:
        path = pathlib.Path(temp)/"look.toml"
        check(window_gate.tree_guards({}, path) == seed, "AC8 missing config fallback")
        for text in ("[broken", '[window_gate]\ntree_guards = "wrong"', '[window_gate]\ntree_guards = [1]', '[window_gate]\ntree_guards = []'):
            path.write_text(text)
            check(window_gate.tree_guards({}, path) == seed, "AC8 malformed config fallback")
    proof_1664("AC8", start)


# Local subprocess fixture: the executable dispatch, main_ admission, flock,
# slots and ledger writes stay real. Only downstream workload/services change.
CHAIN_SITE = r'''
import json, os, pathlib, subprocess, sys, time
def install(frame, event, arg):
    if event != 'call' or frame.f_code.co_name != 'main_' or pathlib.Path(frame.f_code.co_filename).name != 'window_gate.py':
        return install
    sys.settrace(None)
    g = frame.f_globals
    root = pathlib.Path(os.environ['DOIT_ROOT'])
    role = os.environ.get('CHAIN_ROLE', 'holder')
    mode = os.environ.get('CHAIN_CASE', 'normal')
    def note(name):
        with (root / ('work.' + str(os.getpid()))).open('a') as out:
            out.write(name + '\n')
    def wait(name):
        end = time.monotonic() + 30
        while not (root / name).exists():
            if time.monotonic() > end:
                raise RuntimeError('fixture barrier timed out: ' + name)
            time.sleep(.01)
    for name in ('train_events', 'in_flight', 'train_depth', 'acquire', 'release', 'build_candidate', 'verify', 'git'):
        original = g[name]
        def wrapped(*args, _name=name, _original=original, **kw):
            note(_name)
            if role == 'contender':
                raise AssertionError('refused process reached ' + _name)
            return _original(*args, **kw)
        g[name] = wrapped
    def workload(window, project, specs, main, fixes_base=False, after=None, slot=0):
        note('run_window')
        g['ev']('window-candidate', window, sha='fixture', verified_on='fixture-base', specs=specs, slot=slot)
        with (root / 'gate-calls').open('a') as out:
            out.write(json.dumps(dict(window=window, pid=os.getpid())) + '\n')
        if mode == 'exception':
            raise ValueError('fixture propagated exception')
        if mode == 'child':
            child = subprocess.Popen(['/usr/bin/python3', '-c', 'import time; time.sleep(30)'], close_fds=False,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pending_pid = root / 'child-pid.pending'
            pending_pid.write_text(str(child.pid))
            pending_pid.replace(root / 'child-pid')
        if mode not in ('complete', 'cleanup', 'cleanup-exception'):
            wait('finish')
        return 0
    def cleanup(repo, window):
        note('cleanup')
        if role == 'contender':
            raise AssertionError('refused process reached cleanup')
        if mode in ('cleanup', 'cleanup-exception'):
            (root / 'cleaning').write_text(str(os.getpid()))
            wait('finish')
        if mode == 'cleanup-exception':
            raise ValueError('fixture cleanup exception')
    g['run_window'], g['cleanup'] = workload, cleanup
    if mode == 'ledger-unreadable':
        def unreadable(): raise OSError('fixture unreadable')
        g['train_events'] = unreadable
    if mode == 'in-flight': g['in_flight'] = lambda events: [dict(window='prior')]
    if mode in ('invalid-predecessor', 'depth'):
        n = 2 if mode == 'invalid-predecessor' else 1
        g['train_events'] = lambda: [dict(type='window-candidate', subject='prior')] * n
        g['in_flight'] = lambda events: [dict(window='prior')]
        g['train_depth'] = lambda *a, **kw: dict(depth=1)
    if mode == 'slot-unavailable': g['acquire'] = lambda *a: False
    if mode == 'race':
        original_lock = g['chain_lock']
        def barrier(*args, **kw):
            (root / ('ready.' + str(os.getpid()))).touch()
            wait('start')
            return original_lock(*args, **kw)
        g['chain_lock'] = barrier
    return None
sys.settrace(install)
'''


class ChainFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='window-chain-')
        self.root = pathlib.Path(self.temp.name)
        self.children = []
        (self.root / 'events').mkdir()
        site = self.root / 'site'
        site.mkdir()
        (site / 'sitecustomize.py').write_text(CHAIN_SITE)
        self.env = dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1',
                        DOIT_MAIN='master', DOIT_WINDOW_TRAIN_DEPTH='3',
                        DOIT_GATE_LEDGER_FILE='L-executor-0001.jsonl',
                        DOIT_LEDGER_FILE='L-executor-0001.jsonl',
                        PYTHONPATH=str(site) + os.pathsep + str(HERE.resolve()))
        self.env.pop('DOIT_WINDOW_GATE', None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        # Reap detached production window-run children too: their launcher has
        # already exited, so Popen alone does not own those process handles.
        (self.root / 'finish').touch()
        pid_files = list((self.root / 'state').glob('window-run.*.pid'))
        for path in pid_files:
            pid = int(path.read_text())
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            self.wait(lambda: self.probe())
        for process in self.children:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        child = self.root / 'child-pid'
        if child.exists():
            import signal
            try: os.kill(int(child.read_text()), signal.SIGKILL)
            except ProcessLookupError: pass
        self.temp.cleanup()

    def rows(self):
        return [json.loads(line) for path in (self.root / 'events').glob('*.jsonl')
                for line in path.read_text().splitlines()]

    def wait(self, predicate):
        deadline = time.monotonic() + 10
        while not predicate():
            assert time.monotonic() < deadline, 'fixture readiness barrier timed out'
            time.sleep(.01)

    def start(self, route='python', window='owner', role='holder', mode='normal', options=(), bypass=False):
        args = [window, '--project', 'albert-scott', '--specs', 'A', *options]
        if route == 'relative': argv = ['./doit', 'gate-window', *args]
        elif route == 'absolute': argv = [str(HERE.resolve().parent / 'doit'), 'gate-window', *args]
        elif route == 'queue':
            argv = ['/usr/bin/python3', '-c',
                    'import merge_queue; raise SystemExit(merge_queue.window_run("albert-scott", detach=True))']
        else:
            argv = ['/usr/bin/python3', '-c',
                    'import sys, window_gate; sys.exit(window_gate.main_(sys.argv[1:]))', *args]
        env = dict(self.env, CHAIN_ROLE=role, CHAIN_CASE=mode)
        if bypass: env['DOIT_WINDOW_GATE'] = 'off'
        p = subprocess.Popen(argv, cwd=HERE.resolve().parent, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(p)
        return p

    def candidate(self):
        self.wait(lambda: any(r['type'] == 'window-candidate' for r in self.rows()))
        return next(r for r in self.rows() if r['type'] == 'window-candidate')

    def seed_queue(self):
        repo = self.root / 'repos/albert-scott'
        repo.mkdir(parents=True)
        for args in (("init", "-q", "-b", "master"),
                     ("-c", "user.name=fixture", "-c", "user.email=fixture@localhost",
                      "commit", "--allow-empty", "-qm", "base"),
                     ("branch", "br-A")):
            subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
        code = ('import fold; '
                'fold.append(["merge-gate-clean", "A", "branch=br-A", "project=albert-scott"]); '
                'fold.append(["queue-add", "A", "branch=br-A", "project=albert-scott", "rule=operator"])')
        subprocess.run(['/usr/bin/python3', '-c', code], env=self.env, check=True, capture_output=True)

    def probe(self, project='albert-scott', root=None):
        code = ('import window_gate; '
                'exec("with window_gate.chain_lock(\'probe\', ' + repr(project) +
                ', root=' + repr(str(root or self.root)) + ') as refusal:\\n print(refusal is None)")')
        p = subprocess.run(['/usr/bin/python3', '-c', code], env=self.env, capture_output=True, text=True, check=True)
        return p.stdout.strip() == 'True'

    def refused(self, p, attempted, owner, owner_pid, reason='contended'):
        out, err = p.communicate(timeout=5)
        assert p.returncode == 3, (p.returncode, out, err)
        rows = [r for r in self.rows() if r['type'] == 'window-refused']
        row = rows[-1]
        assert row['subject'] == row['window'] == attempted
        assert row['project'] == 'albert-scott' and row['holder_window'] == owner
        assert type(row['holder_pid']) is int and row['holder_pid'] == owner_pid and row['reason'] == reason
        assert not (self.root / ('work.' + str(p.pid))).exists(), 'loser performed admission/gate/cleanup work'
        return row


def chain_ac1():
    observations = []
    for holder in ('relative', 'absolute', 'queue'):
        for contender in ('relative', 'absolute', 'python'):
            for equal in (True, False):
                with ChainFixture() as fx:
                    if holder == 'queue': fx.seed_queue()
                    p = fx.start(holder)
                    actual = fx.candidate()['subject']
                    metadata = json.loads((fx.root / 'state/window-chain.albert-scott.lock').read_text())
                    requested = actual if equal else 'other'
                    before = (fx.root / 'state/window-chain.albert-scott.lock').stat().st_ino
                    started = time.monotonic()
                    loser = fx.start(contender, requested, 'contender')
                    fx.refused(loser, requested, actual, metadata['pid'])
                    assert time.monotonic() - started < 5
                    assert json.loads((fx.root / 'state/window-chain.albert-scott.lock').read_text()) == metadata
                    assert (fx.root / 'state/window-chain.albert-scott.lock').stat().st_ino == before
                    assert len([r for r in fx.rows() if r['type'] == 'window-refused']) == 1
                    assert len((fx.root / 'gate-calls').read_text().splitlines()) == 1
                    assert metadata['pid'] == (p.pid if holder != 'queue' else int(next((fx.root / 'state').glob('window-run.*.pid')).read_text()))
                    assert not fx.probe()
                    (fx.root / 'finish').touch()
                    if holder != 'queue': assert p.wait(timeout=5) == 0
                    else: fx.wait(lambda: fx.probe())
                    row = dict(holder=holder, contender=contender, equal_id=equal, owner_pid=metadata['pid'], exit=3, gate_calls=1)
                    observations.append(row)
                    print('AC1 matrix ' + json.dumps(row))
    assert len(observations) == 18
    with ChainFixture() as fx:
        a = fx.start(window='same', mode='race')
        b = fx.start(window='same', mode='race')
        fx.wait(lambda: len(list(fx.root.glob('ready.*'))) == 2)
        (fx.root / 'start').touch()
        fx.candidate()
        fx.wait(lambda: any(r['type'] == 'window-refused' for r in fx.rows()))
        owner_pid = json.loads((fx.root / 'state/window-chain.albert-scott.lock').read_text())['pid']
        winner, loser = (a, b) if a.pid == owner_pid else (b, a)
        assert winner.pid == owner_pid
        assert loser.wait(timeout=5) == 3
        refusal = next(r for r in fx.rows() if r['type'] == 'window-refused')
        assert (refusal['holder_pid'], refusal['holder_window'], refusal['reason']) in (
            (winner.pid, 'same', 'contended'), (-1, '<unknown>', 'holder-undetermined'))
        assert len((fx.root / 'gate-calls').read_text().splitlines()) == 1
        (fx.root / 'finish').touch()
        assert winner.wait(timeout=5) == 0
        observations.append(dict(case='simultaneous', exits=[0, 3], gate_calls=1))
    for options, bypass in ((('--after', 'absent'), False), (('--after', 'multiple'), False),
                            (('--after', 'owner'), False), (('--fixes-base', '--after', 'absent'), False), ((), True)):
        with ChainFixture() as fx:
            p = fx.start()
            fx.candidate()
            if options == ('--after', 'multiple'):
                subprocess.run(['/usr/bin/python3', '-c',
                    'import window_gate; '
                    '[window_gate.ev("window-candidate", "multiple", sha="fixture", verified_on="fixture-base", specs=["A"], slot=1) for _ in range(2)]'],
                    env=fx.env, check=True, capture_output=True)
            if options == ('--after', 'owner'):
                fx.env['DOIT_WINDOW_TRAIN_DEPTH'] = '1'
            loser = fx.start(window='option', role='contender', options=options, bypass=bypass)
            fx.refused(loser, 'option', 'owner', p.pid)
            (fx.root / 'finish').touch()
            assert p.wait(timeout=5) == 0
            observations.append(dict(options=options, bypass=bypass, exit=3))
    return observations


def window_chain_check(which):
    functions = {'AC1': chain_ac1, 'AC2': chain_ac2, 'AC3': chain_ac3, 'AC4': chain_ac4}
    assert which in functions, 'missing window-chain proof: ' + which
    try:
        body = functions[which]()
    except AssertionError as exc:
        body = dict(error=str(exc))
        encoded = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url='cli://window-chain/' + which, status=1,
                             body_sha256=hashlib.sha256(encoded.encode()).hexdigest(), body_excerpt=body)))
        raise
    encoded = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='cli://window-chain/' + which, status=0,
                         body_sha256=hashlib.sha256(encoded.encode()).hexdigest(), body_excerpt=body)))
    print('window-chain ' + which + ' ok', flush=True)


def chain_ac2():
    import signal
    observations = []
    cases = [('complete', (), 0), ('ledger-unreadable', (), 3), ('in-flight', (), 3),
             ('missing-predecessor', ('--after', 'absent'), 3),
             ('invalid-predecessor', ('--after', 'prior'), 3), ('depth', ('--after', 'prior'), 3),
             ('slot-unavailable', (), 3), ('exception', (), 1), ('cleanup', (), 0),
             ('cleanup-exception', (), 1), ('SIGTERM', (), -signal.SIGTERM),
             ('SIGKILL', (), -signal.SIGKILL), ('child', (), -signal.SIGKILL)]
    for mode, options, code in cases:
        with ChainFixture() as fx:
            p = fx.start(mode=mode, options=options)
            path = fx.root / 'state/window-chain.albert-scott.lock'
            if mode in ('cleanup', 'cleanup-exception'):
                fx.wait(lambda: (fx.root / 'cleaning').exists())
                fx.refused(fx.start(window='during-cleanup', role='contender'), 'during-cleanup', 'owner', p.pid)
                assert not fx.probe()
                (fx.root / 'finish').touch()
            if mode in ('SIGTERM', 'SIGKILL', 'child'):
                fx.candidate()
                if mode == 'child': fx.wait(lambda: (fx.root / 'child-pid').exists())
                p.send_signal(signal.SIGTERM if mode == 'SIGTERM' else signal.SIGKILL)
            out, err = p.communicate(timeout=5)
            assert p.returncode == code, (mode, p.returncode, out, err)
            inode = path.stat().st_ino
            assert fx.probe() and path.stat().st_ino == inode
            if mode == 'child': os.kill(int((fx.root / 'child-pid').read_text()), 0)
            if mode in ('cleanup', 'cleanup-exception', 'exception', 'complete'):
                assert not (fx.root / 'state/window-gate.0.lock').exists()
            row = dict(case=mode, exit=code, reacquired=True, inode_persisted=True)
            observations.append(row)
            print('AC2 lifecycle ' + json.dumps(row))
    for metadata in ('stale', 'live'):
        with ChainFixture() as fx:
            path = fx.root / 'state/window-chain.albert-scott.lock'
            path.parent.mkdir()
            path.write_text(json.dumps(dict(pid=os.getpid() if metadata == 'live' else 999999999, window='old')))
            inode = path.stat().st_ino
            assert fx.probe() and path.stat().st_ino == inode
            observations.append(dict(case=metadata + '-unlocked', acquired=True))
    for bad in ('', '{', '[]', '{"pid":null,"window":"owner"}',
                '{"pid":0,"window":"owner"}', '{"pid":true,"window":"owner"}',
                '{"pid":42,"window":"../bad"}', '{"pid":42}'):
        with ChainFixture() as fx:
            p = fx.start()
            fx.candidate()
            path = fx.root / 'state/window-chain.albert-scott.lock'
            inode = path.stat().st_ino
            path.write_text(bad)
            fx.refused(fx.start(window='unknown', role='contender'), 'unknown', '<unknown>', -1, 'holder-undetermined')
            assert path.read_text() == bad and path.stat().st_ino == inode and not fx.probe()
            (fx.root / 'finish').touch()
            assert p.wait(timeout=5) == 0
            observations.append(dict(case='metadata-' + repr(bad), holder_window='<unknown>', holder_pid=-1))
    with ChainFixture() as fx:
        p = fx.start()
        fx.candidate()
        assert not fx.probe() and fx.probe(project='other-project')
        assert fx.probe(root=fx.root / 'separate-root')
        (fx.root / 'finish').touch()
        assert p.wait(timeout=5) == 0
        observations.append(dict(case='root-and-project-isolation', passed=True))
    # Early returns must close descriptors even when main_ is called repeatedly
    # in one interpreter, rather than relying on process exit to release them.
    with ChainFixture() as fx, patch.object(fold, 'ROOT', fx.root), patch.object(fold, 'EVENTS', fx.root / 'events'):
        descriptors = len(list(pathlib.Path('/proc/self/fd').iterdir()))
        for _ in range(3):
            with patch.object(window_gate, 'train_events', side_effect=OSError('unreadable')):
                assert window_gate.main_(['fd-owner', '--project', 'albert-scott', '--specs', 'A']) == 3
            assert fx.probe()
        assert len(list(pathlib.Path('/proc/self/fd').iterdir())) == descriptors
        observations.append(dict(case='same-interpreter-descriptors', leaked=0))
    return observations


def chain_ac3():
    observations = []
    with ChainFixture() as fx:
        # Real contention append, then retry the pending queue item after exit.
        fx.seed_queue()
        p = fx.start()
        fx.candidate()
        fx.refused(fx.start(window='retry', role='contender'), 'retry', 'owner', p.pid)
        refused_rows = [r for r in fx.rows() if r['subject'] == 'retry']
        assert [r['type'] for r in refused_rows] == ['window-refused']
        assert not any(r['type'] in ('queue-remove', 'shipped', 'window-verified') for r in fx.rows())
        (fx.root / 'finish').touch()
        assert p.wait(timeout=5) == 0
        retry = fx.start(window='retry', mode='complete')
        assert retry.wait(timeout=5) == 0
        assert any(r['type'] == 'window-candidate' and r['subject'] == 'retry' for r in fx.rows())
        reader = ('import fold, json, merge_queue; events=fold.read_events(); '
                  'rows=fold.fold(events)[3]["retry"]; '
                  'assert [e["type"] for e in rows] == ["window-refused", "window-candidate"]; '
                  'assert merge_queue.next_candidate(events) == "A"; '
                  'print(json.dumps(rows[0]))')
        result = subprocess.run(['/usr/bin/python3', '-c', reader], env=fx.env, text=True, capture_output=True, check=True)
        admitted = json.loads(result.stdout)
        assert admitted['actor'] == 'executor' and type(admitted['holder_pid']) is int and admitted['holder_pid'] == p.pid
        raw = next(r for r in fx.rows() if r['type'] == 'window-refused')
        assert all(admitted[k] == v for k, v in raw.items())
        observations.append(dict(case='production-refusal-independent-reader-retry', row=admitted))
    # Every shape uses production append, and a fresh interpreter checks both
    # the committed JSON and the production reader/fold, including actor names.
    with ChainFixture() as fx, patch.object(fold, 'ROOT', fx.root), patch.object(fold, 'EVENTS', fx.root / 'events'):
        valid = dict(window='attempt', project='albert-scott', holder_window='owner', holder_pid=os.getpid(), reason='contended')
        sentinel = dict(valid, holder_window='<unknown>', holder_pid=-1, reason='holder-undetermined')
        for actor in ('executor', 'tick', 'operator'):
            for label, fields in (('known', valid), ('sentinel', sentinel)):
                filename = 'L-' + actor + '-9000.jsonl'
                with patch.dict(os.environ, DOIT_ROOT=str(fx.root), DOIT_GATE_LEDGER_FILE=filename):
                    window_gate.ev('window-refused', 'attempt', **fields)
                observations.append(dict(actor=actor, shape=label, accepted=True))
        # Project omission uses fold.append's existing default; the other
        # required payload fields must be supplied by the caller.
        for name in ('window', 'holder_window', 'holder_pid', 'reason'):
            fields = dict(valid)
            del fields[name]
            observations.append(dict(case='missing-' + name, rejected=True))
            event = dict(type='window-refused', subject='attempt', **fields)
            assert fold.required_reason(event)
            before = sum(len(p.read_text().splitlines()) for p in (fx.root / 'events').glob('*.jsonl'))
            try:
                fold.append(['window-refused', 'attempt'] + [k + ':=' + json.dumps(v) for k, v in fields.items()])
            except SystemExit: pass
            else: raise AssertionError('missing field appended: ' + name)
            assert sum(len(p.read_text().splitlines()) for p in (fx.root / 'events').glob('*.jsonl')) == before
        for name, value in (('holder_window', None), ('holder_pid', None), ('holder_pid', 0)):
            fields = dict(valid, **{name: value})
            assert fold.required_reason(dict(type='window-refused', **fields))
            before = len(fx.rows())
            try:
                fold.append(['window-refused', 'attempt'] + [k + ':=' + json.dumps(v) for k, v in fields.items()])
            except SystemExit: pass
            else: raise AssertionError('null/zero field appended')
            assert len(fx.rows()) == before
            observations.append(dict(case=name, value=value, rejected=True))
        assert fold.admits(dict(type='window-refused', subject='attempt', **valid), 'builder')
        with patch.dict(os.environ, DOIT_LEDGER_FILE='L-builder-9000.jsonl'):
            try:
                fold.append(['window-refused', 'attempt'] + [k + ':=' + json.dumps(v) for k, v in valid.items()], strict=True)
            except SystemExit: pass
            else: raise AssertionError('builder refusal admitted')
        assert len(fx.rows()) == 6
        reader = '''import fold, json
raw = []
for path in fold.EVENTS.glob('*.jsonl'):
    for line in path.read_text().splitlines():
        row = json.loads(line)
        raw.append((row, fold.actor_of(path.name)))
events = fold.read_events()
admitted = fold.fold(events)[3]['attempt']
assert len(raw) == len(events) == len(admitted) == 6
for row, actor in raw:
    assert not fold.required_reason(row) and not fold.admits(row, actor)
    assert any(all(e[k] == v for k, v in row.items()) and e['actor'] == actor for e in admitted)
    assert type(row['holder_pid']) is int
    assert (row['holder_pid'] > 0 and row['holder_window'] == 'owner' and row['reason'] == 'contended') or (row['holder_pid'] == -1 and row['holder_window'] == '<unknown>' and row['reason'] == 'holder-undetermined')
print(json.dumps(dict(committed=len(raw), admitted=len(admitted), actors=sorted(set(e['actor'] for e in events)))))
'''
        result = subprocess.run(['/usr/bin/python3', '-c', reader], env=fx.env, capture_output=True, text=True, check=True)
        observations.append(dict(case='independent-admission-readback', **json.loads(result.stdout)))
        observations.append(dict(actor='builder', accepted=False))
    with ChainFixture() as fx, patch.object(fold, 'ROOT', fx.root), patch.object(fold, 'EVENTS', fx.root / 'events'), \
         patch.object(fold, 'PROJECT', 'albert-scott'), \
         patch.dict(os.environ, DOIT_LEDGER_FILE='L-executor-9000.jsonl'):
        omitted = dict(valid)
        del omitted['project']
        assert fold.required_reason(dict(type='window-refused', **omitted))
        fold.append(['window-refused', 'attempt'] + [k + ':=' + json.dumps(v) for k, v in omitted.items()])
        reader = '''import fold, json
raw = [json.loads(line) for path in fold.EVENTS.glob('*.jsonl') for line in path.read_text().splitlines()]
events = fold.read_events()
admitted = fold.fold(events)[3]['attempt']
assert len(raw) == len(events) == len(admitted) == 1
assert raw[0]['project'] == admitted[0]['project'] == 'albert-scott'
assert not fold.required_reason(raw[0]) and not fold.admits(raw[0], 'executor')
assert admitted[0]['actor'] == 'executor' and type(admitted[0]['holder_pid']) is int
assert all(admitted[0][key] == value for key, value in raw[0].items())
print(json.dumps(dict(project=raw[0]['project'], committed=len(raw), admitted=len(admitted))))
'''
        result = subprocess.run(['/usr/bin/python3', '-c', reader], env=fx.env, capture_output=True, text=True, check=True)
        observations.append(dict(case='missing-project-default', accepted=True, **json.loads(result.stdout)))
    for row in observations: print('AC3 admission ' + json.dumps(row))
    return observations


def chain_ac4():
    chain_signature_checks()
    observations = []
    # Validation must be event-free and must not even create the state directory.
    invalid = [(['--help'], 0), (['../escape', '--project', 'albert-scott', '--specs', 'A'], 1),
               (['..', '--project', 'albert-scott', '--specs', 'A'], 1),
               (['ok', '--project', '../escape', '--specs', 'A'], 1),
               (['ok', '--project', 'other', '--specs', 'A'], 1),
               (['ok', '--project', 'albert-scott', '--specs', 'A', '--after', '../bad'], 1),
               (['ok', '--project', 'albert-scott', '--unknown'], 1),
               (['ok', '--project', 'albert-scott'], 2)]
    for args, expected in invalid:
        with ChainFixture() as fx:
            result = subprocess.run([str(HERE.resolve().parent / 'doit'), 'gate-window', *args],
                                    env=fx.env, capture_output=True, text=True)
            assert result.returncode == expected, (args, result.stderr)
            assert not fx.rows() and not (fx.root / 'state').exists()
            observations.append(dict(args=args, exit=expected, prelock=True))
    with ChainFixture() as fx:
        p = fx.start(window='bypass', mode='complete', bypass=True)
        assert p.wait(timeout=5) == 4
        assert [r['type'] for r in fx.rows()] == ['window-gate-bypassed']
        assert fx.probe()
        observations.append(dict(case='unlocked-bypass', exit=4))
    # Existing git/service fixture exercises ordinary outcomes, not the synthetic
    # blocking workload used only to make process contention deterministic.
    for green in (True, False):
        with patch.dict(os.environ):
            fx = Fx({'A': 'a.txt'})
            try:
                fx.run_n(1, 0 if green else 2, GREEN if green else '')
                result = fx.go(window='ordinary')
                assert result == (0 if green else 2)
                assert (fx.master() != fx.base) is green
                assert bool(fx.events('shipped')) is green
                assert not fx.events('window-refused')
                observations.append(dict(case='ordinary-green' if green else 'ordinary-undetermined', exit=result, advanced=green))
            finally:
                fx.tempdir.cleanup()
    with ChainFixture() as fx:
        for i in range(2):
            p = fx.start(window='repeat', mode='complete')
            assert p.wait(timeout=5) == 0
        assert len((fx.root / 'gate-calls').read_text().splitlines()) == 2
        assert not any(r['type'] == 'window-refused' for r in fx.rows())
        observations.append(dict(case='sequential-repeat', admitted=2))
    with ChainFixture() as fx:
        p = fx.start(window='predecessor')
        fx.candidate()
        fx.refused(fx.start(window='successor', role='contender', options=('--after', 'predecessor')),
                   'successor', 'predecessor', p.pid)
        (fx.root / 'finish').touch()
        assert p.wait(timeout=5) == 0
        observations.append(dict(case='active-predecessor-depth-three', exit=3))
    # Explicit reports for every required regression entry point. Children have
    # no selector argument, so this never recursively selects AC4.
    with ChainFixture() as scratch:
        env = dict(os.environ, DOIT_ROOT=str(scratch.root))
        env.pop('PYTHONPATH', None)
        env = {k: v for k, v in env.items() if not k.startswith('SUPABASE_DB_URL')}
        failures = []
        for argv in (['/usr/bin/python3', 'src/test_window_gate.py'],
                     ['/usr/bin/python3', 'src/test_window_failure_disposition.py'],
                     ['/usr/bin/python3', 'src/test_merge_queue.py'],
                     ['/usr/bin/python3', 'src/test_fold.py'], ['./doit', 'test'], ['./doit']):
            print('AC4 running ' + shlex.join(argv), flush=True)
            result = subprocess.run(argv, cwd=HERE.resolve().parent, env=env, capture_output=True, text=True)
            # Preserve complete failure lists for baseline comparison.
            print('AC4 command ' + shlex.join(argv) + ' exit=' + str(result.returncode), flush=True)
            if result.returncode:
                print(result.stdout, result.stderr, flush=True)
            if result.returncode: failures.append((argv, result))
            if argv == ['./doit']: assert 'HEALTH' in result.stdout, 'HEALTH absent'
            observations.append(dict(command=shlex.join(argv), exit=result.returncode,
                                     health='HEALTH' in result.stdout if argv == ['./doit'] else None))
        if failures:
            observations.extend(chain_base_failures(failures, env))
    assert not FAILS, FAILS
    return observations


def chain_failure_signature(argv, result, repo, base, required_tests=None):
    """Fail closed unless every failure has unchanged, out-of-grant evidence."""
    import re
    text = result.stdout + result.stderr
    if argv == ['./doit', 'test']:
        blocks = re.split(r'^· (test_\S+)\n', text, flags=re.M)
        failures = {}
        for name, output in zip(blocks[1::2], blocks[2::2]):
            if re.search(r'^FAIL ' + re.escape(name) + r' \(exit \d+\)$', output, re.M):
                failures[name] = output
        # Non-test guards must also be accounted for; never hide a trailing red.
        for name in ('retired check', 'machine check'):
            if re.search(r'^FAIL ' + name + r'(?: \(exit \d+\))?$', text, re.M):
                raise AssertionError('unclassified regression guard: ' + name)
        assert failures, 'nonzero suite without a complete failure list'
    else:
        failures = {argv[-1]: text}
    if required_tests is not None:
        assert set(required_tests) <= failures.keys(), 'HEAD-only regression: missing baseline failure'
        extras = sorted(failures.keys() - set(required_tests))
        print('AC4 baseline-only failures ' + json.dumps(dict(count=len(extras), tests=extras,
              reason='absent from HEAD failure list; not attributed to this change')), flush=True)
        failures = {name: failures[name] for name in required_tests}
    signatures = {}
    for name, output in failures.items():
        frames = []
        for filename, line, function, source in re.findall(
                r'  File "([^"]+)", line (\d+), in ([^\n]+)\n\s+([^\n]+)', output):
            if filename.startswith('<') and filename.endswith('>'):
                continue
            path = pathlib.Path(filename).resolve()
            if not path.is_relative_to(repo):
                continue
            relative = path.relative_to(repo).as_posix()
            assert relative not in ('src/window_gate.py', 'src/test_window_gate.py', 'src/fold_emits.py'), relative
            diff = subprocess.check_output(['git', 'diff', base, '--', relative], cwd=repo, text=True)
            assert not diff, 'changed failing source: ' + relative
            frames.append((relative, int(line), function, source))
        errors = re.findall(r'^\w*(?:Error|Exception|Failure)(?::[^\n]*)?$', output, re.M)
        # unittest abbreviates long values using a length that includes the
        # disposable checkout path; retain the actual differing text.
        errors = [re.sub(r'\[\d+ chars\]', '[... chars]', error.replace(str(repo), '<repo>'))
                  for error in errors]
        assert frames and errors, 'unclassified regression failure: ' + name
        signatures[name] = dict(frames=frames, errors=errors)
    return signatures


def chain_signature_checks():
    """Synthetic traceback names must compare the same in different checkouts."""
    repo = HERE.resolve().parent
    with tempfile.TemporaryDirectory(prefix='chain-signature-') as directory:
        baseline = pathlib.Path(directory) / 'tree'
        def failure(tree, filename='src/test_fold.py'):
            output = ('Traceback (most recent call last):\n'
                      '  File "<string>", line 30, in <module>\n'
                      '    exec(code)\n'
                      f'  File "{tree / filename}", line 4207, in check\n'
                      '    assert line_count <= 2465, line_count\n'
                      'AssertionError: 2560\n')
            return subprocess.CompletedProcess([], 1, stdout=output, stderr='')
        # Only the unchanged-source probe is stubbed: this regression concerns
        # traceback attribution, including names that are not files at all.
        with patch.object(subprocess, 'check_output', return_value=''):
            args = ['/usr/bin/python3', 'src/test_fold.py']
            current = chain_failure_signature(args, failure(repo), repo, 'baseline')
            previous = chain_failure_signature(args, failure(baseline), baseline, 'baseline')
            assert current == previous, 'synthetic frame depends on checkout location'
            assert len(current[args[-1]]['frames']) == 1
            # unittest embeds a child's traceback in the outer AssertionError;
            # its checkout prefix and length abbreviation vary across worktrees.
            def nested_failure(tree, abbreviation):
                result = failure(tree)
                result.stdout = result.stdout.replace(
                    'AssertionError: 2560',
                    f'AssertionError: nested File "{tree / "src/test_fold.py"}" [{abbreviation} chars]')
                return result
            current = chain_failure_signature(args, nested_failure(repo, 100), repo, 'baseline')
            previous = chain_failure_signature(args, nested_failure(baseline, 120), baseline, 'baseline')
            assert current == previous, 'nested traceback depends on checkout location'
            # HEAD can fix a baseline-only failure. Compare every HEAD failure
            # without treating an extra baseline failure as a HEAD regression.
            suite_args = ['./doit', 'test']
            head_suite = failure(repo)
            head_suite.stdout = '· test_fold.py\nFAIL test_fold.py (exit 1)\n' + head_suite.stdout
            base_suite = failure(baseline)
            base_suite.stdout = ('· test_fold.py\nFAIL test_fold.py (exit 1)\n' + base_suite.stdout +
                                 '· test_window_gate.py\nFAIL test_window_gate.py (exit 1)\n' +
                                 failure(baseline, 'src/test_window_gate.py').stdout)
            current = chain_failure_signature(suite_args, head_suite, repo, 'baseline')
            previous = chain_failure_signature(suite_args, base_suite, baseline, 'baseline', required_tests=current)
            assert current == previous, 'baseline-only fixed failure hides an identical HEAD failure'
            try:
                chain_failure_signature(suite_args, base_suite, baseline, 'baseline', required_tests={'absent.py'})
            except AssertionError:
                pass
            else:
                raise AssertionError('HEAD-only suite failure accepted as pre-existing')
            try:
                chain_failure_signature(args, failure(repo, 'src/window_gate.py'), repo, 'baseline')
            except AssertionError:
                pass
            else:
                raise AssertionError('in-grant failure accepted as pre-existing')


def chain_base_failures(failures, env):
    """Standing base-red rule: prove identical failures with the same commands."""
    repo = HERE.resolve().parent
    base = subprocess.check_output(['git', 'merge-base', 'HEAD', 'origin/main'], cwd=repo, text=True).strip()
    probe = subprocess.run(['doit', 'base-red', 'do-it-v2', base], cwd=repo, env=env,
                           capture_output=True, text=True)
    print('AC4 base-red exit=' + str(probe.returncode), flush=True)
    observations = []
    # One checkout for this selector, under TMPDIR, removed even on failure.
    with tempfile.TemporaryDirectory(prefix='window-chain-base-') as directory:
        root = pathlib.Path(directory)
        tree = root / 'tree'
        subprocess.run(['git', 'worktree', 'add', '--detach', str(tree), base], cwd=repo,
                       check=True, capture_output=True, text=True)
        try:
            baseline_env = {k: v for k, v in env.items()
                            if not k.startswith('SUPABASE_DB_URL') and k != 'AS_LOCAL_PG_ADMIN'}
            baseline_env['DOIT_ROOT'] = str(root / 'state')
            (root / 'state').mkdir()
            for argv, head in failures:
                print('AC4 baseline running ' + shlex.join(argv), flush=True)
                before = subprocess.run(argv, cwd=tree, env=baseline_env, capture_output=True, text=True)
                print('AC4 baseline command ' + shlex.join(argv) + ' exit=' + str(before.returncode), flush=True)
                current = chain_failure_signature(argv, head, repo, base)
                previous = chain_failure_signature(argv, before, tree, base, required_tests=current)
                print('AC4 failure comparison ' + json.dumps(dict(command=shlex.join(argv), head=current, base=previous)), flush=True)
                assert head.returncode == before.returncode and current == previous, 'HEAD-only regression: ' + shlex.join(argv)
                observations.append(dict(command=shlex.join(argv), head_exit=head.returncode,
                                         base_exit=before.returncode, pre_existing=current))
        finally:
            subprocess.run(['git', 'worktree', 'remove', '--force', str(tree)], cwd=repo, check=True, capture_output=True)
    return observations


if '--window-chain-check' in sys.argv:
    assert len(sys.argv) == 3 and sys.argv[1] == '--window-chain-check'
    window_chain_check(sys.argv[2])
    sys.exit(0)


ac4_quarantined_never_drops()
ac6_recheck_before_drop()
ac7_tree_guard_attribution()
ac8_tree_guards_config()

infra_checks()
train_checks()
rerun_checks()
test_window_attribution()
clock_checks()
cap_checks()
parity_checks()
timeout_checks()
rechain_checks()
main()
merge_simple_checks()
honest_rate_checks()
if FAILS:
    sys.exit(1)
