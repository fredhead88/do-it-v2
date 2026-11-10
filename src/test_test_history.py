#!/usr/bin/env python3
"""Checks for `test_history` (L-spec-2401). Run: python3 test_test_history.py [-k test_ac1_]

Scratch `DOIT_ROOT` and `HOME` are set before any product module is imported.
The GitHub boundary (`ghlimit.run`, `ghlimit.gate`, the artifact download) is
faked; no network, no real `gh`.
"""
import hashlib, io, json, os, pathlib, shutil, stat, subprocess as sp, sys, tempfile, unittest, zipfile

HERE = pathlib.Path(__file__).resolve().parent
TMP = pathlib.Path(tempfile.mkdtemp(prefix="test-history-"))
ROOT = TMP / "root"
os.environ["HOME"] = str(TMP / "home")
os.environ["DOIT_ROOT"] = str(ROOT)
os.environ["DOIT_READ_CACHE"] = "0"
os.environ.pop("DOIT_PROJECT", None)
sys.path.insert(0, str(HERE))
import ghlimit, test_history  # noqa: E402

PROJECT = "demo"
TRACKED = ["tests/test_a.py", "tests/test_b.py", "tests/test_d.py", "api/tests/sub/test_c.py",
           "src/test_x.py", "tests/test_fa.py", "tests/test_fb.py", "tests/test_fc.py",
           "tests/test_fd.py", "tests/test_fe.py", "tests/test_ff.py", "tests/helper.py"]
KEYS = {"runs", "failures", "last_failure", "real", "flaky", "unknown"}
REPO = ROOT / "repos" / PROJECT


def _git(*a):
    sp.run(["git", "-c", "user.name=t", "-c", "user.email=t@x", *a], cwd=REPO, check=True, capture_output=True)


def make_repo():
    shutil.rmtree(ROOT, ignore_errors=True)
    REPO.mkdir(parents=True)
    _git("init", "-q")
    for t in TRACKED:
        (REPO / t).parent.mkdir(parents=True, exist_ok=True)
        (REPO / t).write_text("# fixture\n")
    _git("add", ".")
    _git("commit", "-qm", "fixture")
    _git("remote", "add", "origin", "git@github.com:example/demo.git")
    (ROOT / "events").mkdir()


def write_events(rows):
    with open(ROOT / "events" / "L-fixture-local.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps({"v": 1, "project": PROJECT, **r}) + "\n")


def junit_zip(cases):
    """cases: [(file, name, 'pass'|'fail'|'error'|'skip')] -> zip bytes."""
    body = "".join(
        f'<testcase classname="{f[:-3].replace("/", ".")}" name="{n}">'
        + {"pass": "", "fail": "<failure/>", "error": "<error/>", "skip": "<skipped/>"}[k]
        + "</testcase>" for f, n, k in cases)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("junit.xml", f"<testsuites><testsuite>{body}</testsuite></testsuites>")
    return buf.getvalue()


def artifact(aid, run, sha, created, cases, **extra):
    return ({"id": aid, "name": f"junit-pytest-{aid}", "expired": False, "created_at": created,
             "size_in_bytes": 1000, "workflow_run": {"id": run, "head_sha": sha}, **extra}, junit_zip(cases))


class FakeGH:
    """Stands in for `ghlimit.run`, `ghlimit.gate` and `test_history.subprocess`."""
    def __init__(self, artifacts=(), list_result=None, gate_ok=True):
        self.artifacts = {a["id"]: (a, z) for a, z in artifacts}
        self.list_result = list_result
        self.gate_ok = gate_ok
        self.calls = []                  # ("run"|"gate"|"download", detail)
        self.waits = []

    def run(self, argv, wait=True):
        self.waits.append(wait)
        self.calls.append(("run", argv[2]))
        if self.list_result is not None:
            return self.list_result
        return (0, json.dumps({"total_count": len(self.artifacts),
                               "artifacts": [a for a, _ in self.artifacts.values()]}), "")

    def gate(self, wait=True):
        self.waits.append(wait)
        self.calls.append(("gate", None))
        return {"ok": self.gate_ok, "remaining": 1, "reset": 0, "waited_s": 0}

    def sub_run(self, argv, **kw):
        aid = int(argv[2].split("/")[-2])
        self.calls.append(("download", aid))
        import types
        return types.SimpleNamespace(returncode=0, stdout=self.artifacts[aid][1], stderr=b"")

    def __enter__(self):
        self.saved = (ghlimit.run, ghlimit.gate, test_history.subprocess)
        import types
        ghlimit.run, ghlimit.gate = self.run, self.gate
        test_history.subprocess = types.SimpleNamespace(run=self.sub_run)
        return self

    def __exit__(self, *e):
        ghlimit.run, ghlimit.gate, test_history.subprocess = self.saved


def load(path):
    return json.loads(pathlib.Path(path).read_text())


def src(doc, name):
    return next(s for s in doc["sources"] if s["name"] == name)


def row(typ, ts, **kw):
    return {"type": typ, "ts": ts, **kw}


class Base(unittest.TestCase):
    def setUp(self):
        make_repo()


class TestHistory(Base):
    def test_ac1_shape(self):
        S = "a" * 40
        write_events([
            row("master-full-run", "2026-11-01T00:00:00+00:00", sha=S, complete=False, run_ids=["R0"],
                ids=["tests/test_a.py::T::t[p1]", "tests/test_a.py::T::t[p2]",
                     "pg-parity::Start hosted PG18 service"]),
            row("base-red-nightly", "2026-11-02T00:00:00+00:00", sha=S, complete=True, suite="full",
                run_ids=["R5", "R6"], ids=["api/tests/sub/test_c.py::t"]),
            row("base-red", "2026-11-03T00:00:00+00:00", sha=S, complete=True,
                ids={"pytest": ["src/test_x.py::TestX::t"], "vitest": ["a.test.ts::x"]}),
            row("base-red", "2026-11-03T01:00:00+00:00", sha=S, complete=True,
                ids=["tests/test_d.py::t", "dashboard::foo"]),
            row("selected-canary", "2026-11-04T00:00:00+00:00", schema=2, sha=S, run_id="C1",
                full_run_id="C2", full_failures=1, ids=["tests/test_a.py::t"]),
            row("verdict", "2026-11-05T00:00:00+00:00", subject="L-spec-1", pre_existing=["src/test_x.py::t"]),
            row("window-rehearsed", "2026-11-06T00:00:00+00:00", rehearsal=True, subject="rh-1", sha=S,
                run_url="https://github.com/example/demo/actions/runs/777"),
            row("window-tree-verified", "2026-11-06T01:00:00+00:00", rehearsal=True, subject="rh-1", sha=S,
                new_ids=["tests/test_b.py::t"]),
        ])
        arts = [artifact(1, 901, "b" * 40, "2026-11-07T00:00:00Z",
                         [("tests/test_a.py", "t", "pass"), ("tests/test_b.py", "t", "fail")]),
                artifact(2, 902, "c" * 40, "2026-11-08T00:00:00Z",
                         [("tests/test_a.py", "t", "pass"), ("tests/test_b.py", "t", "pass")])]
        with FakeGH(arts):
            path = test_history.build(PROJECT)
        self.assertEqual(path, ROOT / "test-evidence" / PROJECT / "history.json")
        doc = load(path)
        self.assertEqual(set(doc), {"project", "generated_at", "sources", "tests"})
        self.assertEqual([s["name"] for s in doc["sources"]], list(test_history.SOURCES))
        self.assertEqual(len(doc["sources"]), 7)
        for k in doc["tests"]:
            self.assertIn(k, TRACKED)
            self.assertNotIn("::", k)
        self.assertIn("src/test_x.py", doc["tests"])
        self.assertIn("1 non-pytest", src(doc, "ledger-master-full-run")["reason"])
        self.assertIn("2 non-pytest", src(doc, "ledger-base-red")["reason"])
        self.assertIn("failures only", src(doc, "ledger-base-red-nightly")["reason"])
        for k, r in doc["tests"].items():
            self.assertEqual(set(r), KEYS)
            self.assertEqual(r["real"] + r["flaky"] + r["unknown"], r["failures"], k)
            self.assertEqual(r["last_failure"] is None, r["failures"] == 0, k)
        self.assertEqual(src(doc, "ledger-rehearsal")["rows"], 2)
        self.assertEqual(src(doc, "junit-artifacts")["rows"], 2)
        # test_a: ledger master-full-run, canary fail; passes in two junit runs (different shas) => 2 fails
        self.assertEqual(doc["tests"]["tests/test_a.py"]["failures"], 2)
        self.assertEqual(doc["tests"]["tests/test_b.py"]["failures"], 2)   # rehearsal 777 + junit 901

    def test_ac2_classification(self):
        A, B = "a" * 40, "b" * 40
        F = lambda n: "tests/test_f%s.py" % n
        runs = [
            (1, 1, A, "2026-11-01T10:00:00Z", [(F("a"), "t", "fail")]),
            (2, 2, A, "2026-11-01T11:00:00Z", [(F("a"), "t", "pass")]),
            (3, 3, A, "2026-11-01T10:00:00Z", [(F("b"), "t", "fail")]),
            (4, 4, B, "2026-11-01T11:00:00Z", [(F("b"), "t", "pass")]),
            (5, 5, A, "2026-11-01T10:00:00Z", [(F("c"), "t", "fail")]),
            (6, 6, A, "2026-11-01T09:00:00Z", [(F("d"), "t", "pass")]),
            (7, 7, A, "2026-11-01T10:00:00Z", [(F("d"), "t", "fail")]),
            (8, 8, B, "2026-11-01T09:00:00Z", [(F("e"), "t", "pass")]),
            (9, 9, A, "2026-11-01T10:00:00Z", [(F("e"), "t", "fail")]),
            (10, 10, A, "2026-11-01T10:00:00Z", [(F("f"), "a", "fail"), (F("f"), "b", "pass")]),
        ]
        write_events([])
        with FakeGH([artifact(*r) for r in runs]):
            doc = load(test_history.build(PROJECT))
        want = {"a": "flaky", "b": "real", "c": "unknown", "d": "flaky", "e": "unknown", "f": "unknown"}
        for k, cls in want.items():
            r = doc["tests"][F(k)]
            self.assertEqual(r["failures"], 1, k)
            self.assertEqual({c: r[c] for c in ("real", "flaky", "unknown")},
                             {c: int(c == cls) for c in ("real", "flaky", "unknown")}, k)
            self.assertEqual(r["last_failure"], "2026-11-01T10:00:00Z", k)
        self.assertEqual(doc["tests"][F("f")]["runs"], 1)

    def test_ac3_sources_and_unreadable(self):
        S = "a" * 40
        write_events([row("base-red", "2026-11-03T00:00:00+00:00", sha=S, complete=True, ids=["tests/test_a.py::t"]),
                      row("base-red", "2026-11-01T00:00:00+00:00", sha=S, complete=True, ids=[])])
        forbidden = ("HTTP 403: Resource not accessible by personal access token")
        with FakeGH(list_result=(1, "", forbidden)) as g:
            doc = load(test_history.build(PROJECT))
        j = src(doc, "junit-artifacts")
        self.assertIsNone(j["rows"])
        self.assertIn("403", j["reason"])
        br = src(doc, "ledger-base-red")
        self.assertEqual((br["rows"], br["from"], br["to"]), (2, "2026-11-01T00:00:00+00:00", "2026-11-03T00:00:00+00:00"))
        self.assertEqual(len(doc["sources"]), 7)
        self.assertEqual(doc["tests"]["tests/test_a.py"]["failures"], 1)
        self.assertNotIn(True, g.waits)
        with FakeGH(list_result=(75, "", "ghlimit: quota low, skipped")) as g:
            doc = load(test_history.build(PROJECT))
        j = src(doc, "junit-artifacts")
        self.assertIsNone(j["rows"])
        self.assertIn("quota", j["reason"])
        self.assertNotIn(True, g.waits)
        # gate refuses mid-way
        with FakeGH([artifact(1, 1, S, "2026-11-01T00:00:00Z", [("tests/test_a.py", "t", "pass")])],
                    gate_ok=False) as g:
            doc = load(test_history.build(PROJECT))
        self.assertIn("quota", src(doc, "junit-artifacts")["reason"])
        self.assertFalse([c for c in g.calls if c[0] == "download"])
        self.assertNotIn(True, g.waits)
        # empty ledger, healthy gh
        write_events([])
        with FakeGH() as g:
            doc = load(test_history.build(PROJECT))
        for s in doc["sources"]:
            self.assertEqual(s["rows"], 0, s)
            self.assertIsNone(s["from"])
            self.assertIsNone(s["to"])
        self.assertEqual(doc["tests"], {})
        self.assertNotIn(True, g.waits)

    def test_ac4_dedup_and_bounds(self):
        S = "a" * 40
        write_events([
            row("master-full-run", "2026-11-01T12:00:00+00:00", sha=S, complete=True, run_ids=["1"],
                ids=["tests/test_a.py::t"]),
            row("base-red-nightly", "2026-11-01T12:00:00+00:00", sha=S, complete=True, run_ids=["1", "2"],
                ids=["tests/test_b.py::t"]),
            row("master-full-run", "2026-11-02T00:00:00+00:00", sha=S, complete=True, run_ids=["3"],
                ids=["tests/test_d.py::t"]),
            row("master-full-run", "2026-11-03T00:00:00+00:00", sha=S, complete=True, run_ids=["4"],
                ids=["tests/test_d.py::t"]),
            row("verdict", "2026-11-05T00:00:00+00:00", subject="L-spec-1", pre_existing=["src/test_x.py::t"]),
            row("verdict", "2026-11-05T00:00:00+00:00", subject="L-spec-1", pre_existing=["src/test_x.py::t2"]),
        ])
        arts = [artifact(1, 1, S, "2026-11-01T12:00:00Z",
                         [("tests/test_a.py", "t", "fail"), ("tests/test_b.py", "t", "fail")])]
        with FakeGH(arts):
            doc = load(test_history.build(PROJECT))
        self.assertEqual(doc["tests"]["tests/test_a.py"]["failures"], 1)
        self.assertEqual(doc["tests"]["tests/test_b.py"]["failures"], 1)
        self.assertEqual(doc["tests"]["tests/test_d.py"]["failures"], 2)
        self.assertEqual(doc["tests"]["src/test_x.py"]["failures"], 1)
        # 250 artifacts -> 200 downloads, newest first, each preceded by a gate call
        write_events([])
        many = [artifact(i, 1000 + i, S, "2026-10-%02dT%02d:00:00Z" % (1 + i // 24 % 28, i % 24),
                         [("tests/test_a.py", "t", "pass")]) for i in range(1, 251)]
        with FakeGH(many) as g:
            doc = load(test_history.build(PROJECT))
        downloads = [c for c in g.calls if c[0] == "download"]
        self.assertEqual(len(downloads), 200)
        newest = sorted(many, key=lambda a: a[0]["created_at"], reverse=True)[:200]
        self.assertEqual({d[1] for d in downloads}, {a[0]["id"] for a in newest})
        for i, c in enumerate(g.calls):
            if c[0] == "download":
                self.assertEqual(g.calls[i - 1][0], "gate")
        self.assertEqual(src(doc, "junit-artifacts")["rows"], 200)
        self.assertNotIn(True, g.waits)
        # oversized artifact skipped and counted
        big = artifact(1, 1, S, "2026-11-01T00:00:00Z", [("tests/test_a.py", "t", "pass")], )
        big[0]["size_in_bytes"] = 9 * 1024 * 1024
        ok = artifact(2, 2, S, "2026-11-02T00:00:00Z", [("tests/test_a.py", "t", "pass")])
        with FakeGH([big, ok]) as g:
            doc = load(test_history.build(PROJECT))
        self.assertEqual([c[1] for c in g.calls if c[0] == "download"], [2])
        self.assertIn("1 artifacts over", src(doc, "junit-artifacts")["reason"])

    def test_ac5_store_and_readonly(self):
        S = "a" * 40
        write_events([row("base-red", "2026-11-03T00:00:00+00:00", sha=S, complete=True,
                          ids=["tests/test_a.py::t"])])
        ledger = ROOT / "events"

        def digest():
            return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(ledger.iterdir())}

        def tree():
            return {p for p in ROOT.rglob("*")}
        before_ledger, before_tree = digest(), tree()
        with FakeGH():
            path = test_history.build(PROJECT)
        first = load(path)["tests"]
        stub = TMP / "gh-stub"
        stub.write_text('#!/bin/bash\ncase "$*" in\n'
                        '"api rate_limit") echo \'{"resources":{"core":{"limit":5000,"remaining":5000,"reset":1},'
                        '"graphql":{"limit":5000,"remaining":5000,"reset":1}}}\';;\n'
                        '*artifacts*) if [ -n "$STUB_FAIL" ]; then echo "HTTP 403: nope" >&2; exit 1; fi;'
                        ' echo \'{"total_count":0,"artifacts":[]}\';;\n*) exit 1;;\nesac\n')
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        env = {**os.environ, "DOIT_REAL_GH": str(stub), "GH_LIMIT_STATE": str(TMP / "ghstate" / "s.json")}
        r = sp.run([sys.executable, str(HERE / "test_history.py"), PROJECT], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(pathlib.Path(r.stdout.strip()), path)
        self.assertEqual(load(path)["tests"], first)
        self.assertEqual(digest(), before_ledger)
        created = tree() - before_tree
        ev = ROOT / "test-evidence" / PROJECT
        self.assertTrue(all(p == ev or ev in p.parents or p in ev.parents for p in created), created)
        self.assertEqual([p.name for p in ev.iterdir()], ["history.json"])
        r = sp.run([sys.executable, str(HERE / "test_history.py"), PROJECT], env={**env, "STUB_FAIL": "1"},
                   capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stderr)
        # a failure mid-write leaves the previous file intact and no temp file
        prev = path.read_bytes()
        real_replace = os.replace

        def boom(*a):
            raise OSError("simulated")
        os.replace = boom
        try:
            with FakeGH():
                with self.assertRaises(OSError):
                    test_history.build(PROJECT, now="2030-01-01T00:00:00Z")
        finally:
            os.replace = real_replace
        self.assertEqual(path.read_bytes(), prev)
        self.assertEqual([p.name for p in ev.iterdir()], ["history.json"])


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
