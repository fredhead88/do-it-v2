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
from unittest.mock import patch

os.environ["DOIT_NO_POKE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fold, window_gate  # noqa: E402

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
                      "key = ('log-' + args[args.index('--job') + 1]) if '--job' in args else ('jobs-' + args[2])\n"
                      "p = pathlib.Path(os.environ['FAKE_DIR']) / key\n"
                      "if not p.exists(): sys.exit(1)\nprint(p.read_text())\n")
        gh.chmod(0o755)
        self.path = str(bindir) + os.pathsep + os.environ.get("PATH", "")
        fold.ROOT = self.root
        fold.EVENTS = self.root / "events"
        fold.EVENTS.mkdir(parents=True)
        os.environ.update(DOIT_ROOT=str(self.root), DOIT_WINDOW_PREDEPLOY=str(self.fake), FAKE_DIR=str(self.fdir),
                          DOIT_MAIN="master", DOIT_LEDGER_FILE="L-executor-0001.jsonl",
                          DOIT_GATE_LEDGER_FILE="L-executor-0001.jsonl")
        os.environ.pop("FAKE_BASE", None)
        os.environ.pop("DOIT_WINDOW_GATE", None)
        os.environ.pop("DOIT_WINDOW_VERIFY_TIMEOUT_SECS", None)
        # One default service fixture for every historical red case. No network.
        import base_red
        self.known = set()
        self.base_reader = lambda project, sha: {"sha": sha, "complete": True}
        self.log_reader = lambda repo, doc: []
        self.service = types.SimpleNamespace(parse_log=base_red.parse_log,
            classify=lambda project, sha, ids: {
                "complete": True, "pre_existing": [n for n in ids if n in self.known],
                "new": [n for n in ids if n not in self.known]})

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
        (self.fdir / ("log-" + jid)).write_text("\n".join(
            "pg-parity (Postgres)\tRun suite\t2026-10-17T00:00:00Z " + kind + " " + node + " - " + message
            for node in nodes))

    def go(self, specs=None, fixes_base=False):
        specs = specs or list(self.branches)
        for s in specs:
            fold.append(["merge-gate-clean", s, f"branch={self.branches[s]}", "project=albert-scott"])
        with patch.dict(os.environ, FAKE_BASE=self.base, PATH=self.path), \
             patch.dict(sys.modules, base_red=self.service), \
             patch.object(window_gate, "base_red_snapshot", side_effect=self.base_reader), \
             patch.object(window_gate, "candidate_logs", side_effect=self.log_reader):
            result = window_gate.main_(["w1", "--project", "albert-scott", "--specs", ",".join(specs)]
                                       + (["--fixes-base"] if fixes_base else []))
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


def main():
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
    check(len(fx.calls()) == 1, "AC2 whole window dropped without a second run")

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

    # AC4: no spec owns the failure: whole window dropped + escalation
    fx = Fx({"A": "a.txt", "B": "b.txt", "C": "c.txt"})
    fx.run_n(1, 1, red(["api/tests/test_z.py::test_q"]))
    fx.run_n(2, 0, GREEN)
    rc = fx.go()
    check(rc == 2 and fx.master() == fx.base, "AC4 master unmoved")
    check(sorted(e["subject"] for e in fx.events("window-dropped")) == ["A", "B", "C"], "AC4 all dropped")
    check(len(fx.events("escalation-blocking")) == 1 and len(fx.calls()) == 1, "AC4 one escalation, one run")

    # AC4b: second run red -> whole remainder dropped, never a third run
    fx = Fx({"A": "a.txt", "B": "api/tests/test_x.py"})
    fx.run_n(1, 1, red(["api/tests/test_x.py::test_y"]))
    fx.run_n(2, 1, red(["api/tests/test_z.py::test_q"]))
    rc = fx.go()
    check(rc == 2 and fx.master() == fx.base and len(fx.calls()) == 2 and not fx.events("shipped"),
          "AC4b second red drops the window; exactly two runs")
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

    cases = ["green", "cap", "cap4", "owner-X", "owner-B", "second-red", "fingerprint-fail", "off", "unset"]
    for case in cases:
        names = ["a", "b", "c", "d"] if case in ("cap", "cap4") else ["a", "b", "c"] if case == "green" else ["a", "b"]
        fx, sf = fixture(names, extra=case in ("cap", "cap4", "owner-X", "owner-B", "second-red", "fingerprint-fail"))
        env = sf.scripts(fx.tmp, fail=case == "fingerprint-fail")
        env["DOIT_WINDOW_RECHAIN"] = "off" if case == "off" else "on"
        if case == "cap4":
            env["DOIT_WINDOW_MAX_MIGRATIONS"] = "4"
        before = sh(["status", "--porcelain"], fx.repo)
        refs = {b: sh(["rev-parse", b], fx.repo) for b in fx.branches.values()}
        try:
            with patch.dict(os.environ, env):
                if case == "unset":
                    os.environ.pop("DOIT_WINDOW_RECHAIN", None)
                if case != "cap4":
                    os.environ.pop("DOIT_WINDOW_MAX_MIGRATIONS", None)
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
            elif case in ("cap", "cap4"):
                check(shipped == (["a", "b", "c", "X"] if case == "cap" else names + ["X"]), "ac5 migration cap excludes only fourth")
                check([e["subject"] for e in fx.events("window-deferred")] == (["d"] if case == "cap" else []), "ac5 deferred event")
                check(not fx.events("window-dropped"), "ac5 no deferred drop")
            elif case == "owner-X":
                check(rc == 1 and shipped == names and len(fx.calls()) == 2, "ac6 X drops migrations stay")
                check([e["subject"] for e in fx.events("window-dropped")] == ["X"], "ac6 owners mapping")
            elif case in ("owner-B", "second-red"):
                check(len(fx.calls()) == 2 and len(fp_calls) == 2, "ac6 two runs two final-tree fingerprints")
                check(any(e["subject"] == "b" and e.get("reason") == "migration-bisect" for e in fx.events("window-dropped")), "ac6 B migration-bisect")
                check((rc == 1 and shipped == ["a", "X"] and rechain.routed_head(fx.repo, fx.master()) == "a") if case == "owner-B" else
                      (rc == 2 and fx.master() == fx.base and not shipped), "ac6 retry outcome " + case)
            elif case == "fingerprint-fail":
                check(rc == 1 and shipped == ["X"] and len(fx.calls()) == 1, "ac3f fingerprint failure rebuilds without migrations")
                check(set(e["subject"] for e in fx.events("rechain-declined")) == set(names), "ac3f all migration declines")
                check(rechain.routed_head(fx.repo, fx.master()) == "h0", "ac3f unchanged migration candidate")
            else:
                check(rc == 1 and shipped == ["a"] and not fps and not fx.events("rechained"), "ac9 disabled behavior " + case)
                check([e["subject"] for e in fx.events("merge-conflict")] == ["b"], "ac9 disabled migration conflict")
            check(before == sh(["status", "--porcelain"], fx.repo) == "", "ac8 clean worktree preserved " + case)
            check(refs == {b: sh(["rev-parse", b], fx.repo) for b in refs}, "ac8 builder refs unchanged " + case)
            check(all(not pathlib.Path(c.split(" ", 1)[1]).exists() for c in calls), "ac8 disposable resources gone " + case)
            print("ac3f/ac4/ac5/ac6/ac9", case, "checked")
            criterion = {"green": "AC4", "cap": "AC5", "cap4": "AC5", "owner-X": "AC6",
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
        with patch.object(window_gate, "BASE_RED_MARGIN_SECS", .2), \
             patch.object(window_gate, "verify_timeout_secs", return_value=.2), \
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
        rc = fx.go()
        drops = fx.events("window-dropped")
        check(rc == 2 and [e["subject"] for e in drops] == ["A", "B"] and all(
            e["reason"] == "unattributed" and set(e["nodes"]) == ({unknown, v} if mixed else {unknown}) for e in drops), "mixed_ids whole window named")
        check("unattributed" in fx.events("escalation-blocking")[0]["why"] and unknown in fx.events("escalation-blocking")[0]["why"], "unattributed escalation")
    response("unattributed_drops_window_mixed_ids", ["AC10", "AC11"], {"dropped": [e["subject"] for e in drops], "nodes": drops[0]["nodes"], "reason": "unattributed"})

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


def test_window_attribution():
    attribution_checks()
    assert not FAILS, FAILS


test_window_attribution()
parity_checks()
timeout_checks()
rechain_checks()
main()
