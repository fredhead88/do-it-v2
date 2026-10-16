#!/usr/bin/env python3
"""Checks on window_gate (L-spec-0765). Run: python3 test_window_gate.py

Hermetic: real `git` against temp repos (a bare `origin` and a clone at
`$R/repos/albert-scott`), `fold.ROOT`/`fold.EVENTS` repointed at a fresh temp
root per fixture, and a FAKE `predeploy_gate.sh` (DOIT_WINDOW_PREDEPLOY) that
replays a scripted verdict per call and records each invocation. No network, no
`gh`, no model call. The fake stands in for the companion interface of
L-spec-0764 (JSON verdict line + exit 0/1/2); the real script is not exercised.
"""
import hashlib, json, os, pathlib, subprocess, sys, tempfile, time
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
n=$(cat "$D/count" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$D/count"
echo "$PREDEPLOY_GATE_DEPLOY_SHA|$(echo "$PREDEPLOY_GATE_CI_CHECKS" | tr '\\n' ';')" >> "$D/calls"
echo "$PREDEPLOY_GATE_DISPATCH_TIMEOUT_MIN" >> "$D/timeouts"
[ -e "$D/run$n.sleep" ] && sleep "$(cat "$D/run$n.sleep")"
cat "$D/run$n.out" 2>/dev/null
exit "$(cat "$D/run$n.rc" 2>/dev/null || echo 2)"
"""

GREEN = ('{"verdict":"green","checks":{"pytest":"ran","TypeScript · Biome · Tests · File Sizes":"ran"},'
         '"new_failures":[],"run_url":"https://github.com/o/r/actions/runs/1"}')


def red(nodes, url="https://github.com/o/r/actions/runs/2"):
    return json.dumps({"verdict": "red", "checks": {"pytest": "ran"}, "new_failures": nodes, "run_url": url})


class Fx:
    def __init__(self, specs, dashboard=False, dirty=False):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="wg", dir="/home/albert/doit-scratch"))
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
        fold.ROOT = self.root
        fold.EVENTS = self.root / "events"
        fold.EVENTS.mkdir(parents=True)
        os.environ.update(DOIT_ROOT=str(self.root), DOIT_WINDOW_PREDEPLOY=str(self.fake), FAKE_DIR=str(self.fdir),
                          DOIT_MAIN="master")
        os.environ.pop("DOIT_WINDOW_GATE", None)
        os.environ.pop("DOIT_WINDOW_VERIFY_TIMEOUT_SECS", None)

    def run_n(self, n, rc, out="", sleep=None):
        (self.fdir / f"run{n}.rc").write_text(str(rc))
        (self.fdir / f"run{n}.out").write_text(out + "\n")
        if sleep:
            (self.fdir / f"run{n}.sleep").write_text(str(sleep))

    def go(self, specs=None):
        specs = specs or list(self.branches)
        for s in specs:
            fold.append(["merge-gate-clean", s, f"branch={self.branches[s]}", "project=albert-scott"])
        return window_gate.main_(["w1", "--project", "albert-scott", "--specs", ",".join(specs)])

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
    check(len(fx.calls()) == 1 and fx.calls()[0].endswith("|pytest;"), "AC1/AC5 one dispatch, pytest only")
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
                       "checks": {"pytest": {"state": "ran", "run_id": 38010269666,
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


timeout_checks()
rechain_checks()
main()
