#!/usr/bin/env python3
"""Checks on window_gate (L-spec-0765). Run: python3 test_window_gate.py

Hermetic: real `git` against temp repos (a bare `origin` and a clone at
`$R/repos/albert-scott`), `fold.ROOT`/`fold.EVENTS` repointed at a fresh temp
root per fixture, and a FAKE `predeploy_gate.sh` (DOIT_WINDOW_PREDEPLOY) that
replays a scripted verdict per call and records each invocation. No network, no
`gh`, no model call. The fake stands in for the companion interface of
L-spec-0764 (JSON verdict line + exit 0/1/2); the real script is not exercised.
"""
import json, os, pathlib, subprocess, sys, tempfile, time

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


main()
