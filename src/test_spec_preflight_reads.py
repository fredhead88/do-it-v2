#!/usr/bin/env python3
"""Hermetic read-count and text-change regressions for a preflight pass."""
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import fold
import ledger_cache
import spec_preflight as sp

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
SEED_SPECS = ("L-spec-9401", "L-spec-9402", "L-spec-9403", "L-spec-9404", "L-spec-9405")
MODES = ("uncached", "cold", "warm", "busy")


def seed(root):
    """The common deterministic ledger for the periodic-reader suites."""
    root = Path(root)
    (root / "events").mkdir(parents=True, exist_ok=True)
    (root / "content").mkdir(exist_ok=True)
    (root / "models.toml").write_text("[seats]\nbuilder = 7\n")
    stamp = lambda minutes: (NOW + timedelta(minutes=minutes)).isoformat()
    rows = {"spec-writer": [], "builder": [], "executor": [], "operator": []}
    for sid, minutes in zip(SEED_SPECS, (-10, -9, -8, -7, -5760)):
        path = root / "content" / (sid + ".md")
        path.write_text(sid + " source snapshot\n## Interfaces\nWrites: src/a.py\n")
        rows["spec-writer"].append(dict(type="spec-written", subject=sid, ts=stamp(minutes),
                                        project="do-it-v2", path=str(path), footprint=["src/a.py"]))
    rows["builder"].append(dict(type="build-started", subject="L-spec-9402", ts=stamp(-6)))
    # A builder cannot ship: the parser must ignore this forged-role event.
    rows["builder"].append(dict(type="shipped", subject="L-spec-9404", ts=stamp(-5)))
    rows["executor"].append(dict(type="spec-killed", subject="L-spec-9403", ts=stamp(-4)))
    rows["operator"].append(dict(type="correction", subject="L-spec-9401", ts=stamp(-3),
                                 ref="L-spec-writer-seed.jsonl:1", set={"project": "seed-corrected"}))
    for actor, events in rows.items():
        (root / "events" / f"L-{actor}-seed.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events))


# The six retained kinds, written out independently of spec_preflight.
SIX = ("spec-written", "spec-preflight", "build-started", "shipped", "spec-closed", "spec-killed")
BASE_T = datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc)


def report(criterion, test, required, exercised, assertions, results, **extra):
    """One JSON evidence line on the process stderr, outside any capture."""
    missing = sorted(set(required) - set(exercised))
    assert not missing, ("case kinds not exercised", missing)
    print(json.dumps(dict(criterion=criterion, test=test, required_case_kinds=sorted(required),
                          exercised_case_kinds=sorted(set(exercised)), assertions=assertions,
                          results=results, **extra), sort_keys=True), file=sys.__stderr__, flush=True)


def write_rows(root, files):
    (Path(root) / "events").mkdir(parents=True, exist_ok=True)
    for name, rows in files.items():
        (Path(root) / "events" / name).write_text("".join(json.dumps(r) + "\n" for r in rows))


def projection_files(content="x"):
    """All six kinds, irrelevant kinds, equal timestamps across files with an
    irrelevant per-file head, repeated writes, old facts, valid and invalid corrections."""
    t = lambda m: (BASE_T + timedelta(minutes=m)).isoformat()
    sw = lambda sid, m, project, **kw: dict(type="spec-written", subject=sid, ts=t(m), project=project,
                                            path=content, **kw)
    return {
        "L-spec-writer-a.jsonl": [dict(type="lesson", subject="L-spec-1", ts=t(1)),
                                  sw("L-spec-1", 2, "p1"), sw("L-spec-1", 5, "p1")],
        "L-spec-writer-b.jsonl": [dict(type="lesson", subject="L-spec-2", ts=t(2)),
                                  sw("L-spec-2", 2, "p2"), sw("L-spec-3", 3, "p3")],
        "L-executor-a.jsonl": [dict(type="spec-killed", subject="L-spec-3", ts=t(4)),
                               dict(type="spec-closed", subject="L-spec-2", ts=t(6)),
                               dict(type="shipped", subject="L-spec-1", ts=t(7)),
                               dict(type="decision", subject="L-spec-1", ts=t(7))],
        "L-builder-a.jsonl": [dict(type="build-started", subject="L-spec-1", ts=t(6)),
                              dict(type="shipped", subject="L-spec-3", ts=t(8)),
                              dict(type="spec-closed", subject="L-spec-3", ts=t(8))],
        "L-spec-preflight-a.jsonl": [dict(type="spec-preflight", subject="L-spec-1", ts=t(3), project="p1"),
                                     dict(type="writes-granted", subject="L-spec-1", ts=t(3), project="p1")],
        "L-operator-a.jsonl": [
            dict(type="correction", subject="L-spec-2", ts=t(9), ref="L-spec-writer-b.jsonl:1",
                 set={"type": "spec-written", "project": "pc", "path": content}),
            dict(type="correction", subject="L-spec-1", ts=t(9), ref="L-spec-writer-a.jsonl:3",
                 set={"project": "p1c"}),
            dict(type="correction", subject="L-spec-1", ts=t(9), ref="L-executor-a.jsonl:4",
                 set={"type": "spec-killed"}),
            dict(type="correction", subject="L-spec-3", ts=t(9), ref="L-executor-a.jsonl:1",
                 set={"type": "lesson"}),
            dict(type="spec-closed", subject="L-spec-9", ts=t(10))],
        "L-builder-b.jsonl": [dict(type="correction", subject="L-spec-1", ts=t(10),
                                   ref="L-spec-writer-a.jsonl:2", set={"project": "forged"})],
    }


def reference_read(root):
    """Independent oracle: reference parser, complete canonical list, full fold."""
    saved = fold.ROOT, fold.EVENTS, fold.PROJECT
    try:
        fold.ROOT, fold.EVENTS, fold.PROJECT = Path(root), Path(root) / "events", ""
        full = fold.read_events(use_cache=False)
        ignored = {id(e) for e in fold.fold(full)[2]}
        return full, [e for e in full if id(e) not in ignored]
    finally:
        fold.ROOT, fold.EVENTS, fold.PROJECT = saved


@contextmanager
def cache_mode(mode, root):
    """Run reads in one of the four equality modes; yields the read-call log."""
    calls, real = [], fold.read_events

    def logged(*args, **kwargs):
        calls.append(dict(kwargs, scope=str(fold.ROOT)))
        if mode == "uncached":
            kwargs["use_cache"] = False
        return real(*args, **kwargs)

    def busy(*args, **kwargs):
        raise ledger_cache.LockTimeout("fixture busy")

    with ExitStack() as stack:
        if mode == "cold":
            shutil.rmtree(Path(root) / "cache", ignore_errors=True)
        stack.enter_context(mock.patch.object(fold, "read_events", side_effect=logged))
        if mode == "busy":
            stack.enter_context(mock.patch.object(ledger_cache, "read", side_effect=busy))
        yield calls


# Frozen from the unmodified base readers (0eeb8ff, real reference parser).
FROZEN = {
    'run:L-spec-9401':
        '{"spec": "L-spec-9401", "findings": [{"check": 1, "id": "seed-corrected", "line": "L-spec-9401 source snapshot"}], "red_on_master": [], "undetermined": []}',
    'run:L-spec-9402':
        '{"spec": "L-spec-9402", "findings": [{"check": 1, "id": "do-it-v2", "line": "L-spec-9402 source snapshot"}], "red_on_master": [], "undetermined": []}',
    'run:L-spec-9403':
        '{"spec": "L-spec-9403", "findings": [{"check": 1, "id": "do-it-v2", "line": "L-spec-9403 source snapshot"}], "red_on_master": [], "undetermined": []}',
    'run:L-spec-9404':
        '{"spec": "L-spec-9404", "findings": [{"check": 1, "id": "do-it-v2", "line": "L-spec-9404 source snapshot"}], "red_on_master": [], "undetermined": []}',
    'run:L-spec-9405':
        '{"spec": "L-spec-9405", "findings": [{"check": 1, "id": "do-it-v2", "line": "L-spec-9405 source snapshot"}], "red_on_master": [], "undetermined": []}',
    'tick':
        '[0, "spec-preflight: processed=2 deferred=0 (tick budget 240s); skipped=3 (requires a valid timestamp under 3 days old, no later build-started, and no closed state); lag_s=L-spec-9401:600.0, L-spec-9404:420.0; deferred specs=none\\n", "", [["{doit}", "append", "spec-preflight", "L-spec-9401", "project=seed-corrected", "spec:=\\"L-spec-9401\\"", "findings:=[{\\"check\\": 1, \\"id\\": \\"seed-corrected\\", \\"line\\": \\"L-spec-9401 source snapshot\\"}]", "red_on_master:=[]", "undetermined:=[\\"hook _discover_coupled: ValueError\\"]", "lag_s:=600.0"], ["{doit}", "append", "spec-preflight", "L-spec-9404", "project=do-it-v2", "spec:=\\"L-spec-9404\\"", "findings:=[{\\"check\\": 1, \\"id\\": \\"do-it-v2\\", \\"line\\": \\"L-spec-9404 source snapshot\\"}]", "red_on_master:=[]", "undetermined:=[\\"hook _discover_coupled: ValueError\\"]", "lag_s:=420.0"]]]',
    'append':
        '[["{doit}", "append", "spec-preflight", "L-spec-9404", "project=do-it-v2", "spec:=\\"L-spec-9404\\"", "findings:=[]", "red_on_master:=[]", "undetermined:=[]"]]',
}


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="preflight-reads-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "events").mkdir()
        (self.root / "content").mkdir()
        self.serial = 0
        self.appends = []
        self.inject = lambda sid: None
        self.feedback = True
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "DOIT_SCRATCH": str(self.root / "scratch"),
        }))
        self.stack.enter_context(mock.patch.object(sp, "_process", side_effect=self.process))
        self.stack.enter_context(mock.patch.object(sp, "_check_interpreter", side_effect=self.check))
        for name in ("_check_shas", "_check_reach", "_check_tests"):
            self.stack.enter_context(mock.patch.object(sp, name))

    def event(self, kind, sid, **fields):
        self.serial += 1
        event = dict(type=kind, subject=sid,
                     ts=(NOW - timedelta(minutes=10) + timedelta(seconds=self.serial)).isoformat(),
                     **fields)
        actor = {"spec-written": "spec-writer", "build-started": "builder",
                 "spec-killed": "executor", "spec-preflight": "spec-preflight"}[kind]
        with (self.root / "events" / f"L-{actor}-fixture.jsonl").open("a") as out:
            out.write(json.dumps(event) + "\n")
        return event

    def spec(self, sid, project="do-it-v2"):
        path = self.root / "content" / (sid + ".md")
        path.write_text(sid + " source snapshot\n## Interfaces\nWrites: src/a.py\n")
        return self.event("spec-written", sid, project=project, path=str(path),
                          footprint=["src/a.py"])

    def check(self, body, text, project, checkout):
        sp._finding(body, 1, project, text.splitlines()[0])
        self.inject(body["spec"])

    def process(self, argv, **kwargs):
        self.assertIsInstance(argv, list)
        if argv[1:3] == ["append", "spec-preflight"]:
            self.appends.append(argv[:])
            body = dict(item.split(":=", 1) for item in argv[5:])
            body = {key: json.loads(value) for key, value in body.items()}
            sp._validate(body)
            if self.feedback:
                self.event("spec-preflight", argv[3], project=argv[4].split("=", 1)[1], **body)
        return subprocess.CompletedProcess(argv, 0, "fixture-head\n", "")

    def counted(self, callback, reads=1, folds=None):
        original_root = fold.ROOT
        original_events, original_project = fold.EVENTS, fold.PROJECT
        read = fold.read_events

        def counted_read(*args, **kwargs):
            self.assertEqual(args, ())
            self.assertEqual(kwargs, {})
            self.assertEqual(fold.ROOT, self.root)
            self.assertEqual(fold.EVENTS, self.root / "events")
            self.assertEqual(fold.PROJECT, "")
            return read(*args, **kwargs)

        with mock.patch.object(fold, "read_events", side_effect=counted_read) as reader, \
                mock.patch.object(fold, "fold", wraps=fold.fold) as folder:
            result = callback()
        self.assertEqual(reader.call_count, reads)
        if folds is not None:
            self.assertEqual(folder.call_count, folds)
        self.assertEqual((fold.ROOT, fold.EVENTS, fold.PROJECT),
                         (original_root, original_events, original_project))
        self.assertTrue((self.root / "cache").exists())
        return result

    def tick(self):
        self.appends.clear()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = self.counted(lambda: sp.tick(root=self.root, now=NOW), folds=1)
        self.assertEqual(result, 0)
        return stdout.getvalue(), stderr.getvalue()

    def assert_appends(self, expected):
        self.assertEqual([(a[3], a[4]) for a in self.appends],
                         [(sid, "project=" + project) for sid, project in expected])
        for argv in self.appends:
            self.assertEqual(argv[:3], [str(sp.DOIT), "append", "spec-preflight"])

    def recheck(self, sid):
        return f"preflight recheck next pass: {sid} spec text changed during the pass\n"

    def answer(self, calls, mode):
        """Serialized answers for the seed; asserts the read pattern per entrance."""
        root = str(self.root)
        cached = {"scope": root}
        pattern = [cached] + ([{"use_cache": False, "scope": root}] if mode == "busy" else [])
        out = {}

        def phase(name, reads, callback):
            start = len(calls)
            value = callback()
            self.assertEqual(calls[start:], pattern * reads, (mode, name))
            return value

        for sid in SEED_SPECS:
            out["run:" + sid] = json.dumps(phase(sid, 1, lambda: sp.run(sid, root=self.root, now=NOW)))
        self.appends.clear()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = phase("tick", 1, lambda: sp.tick(root=self.root, now=NOW))
        out["tick"] = json.dumps([code, stdout.getvalue(), stderr.getvalue(),
                                  [["{doit}" if a == str(sp.DOIT) else a for a in argv] for argv in self.appends]])
        body = dict(spec="L-spec-9404", findings=[], red_on_master=[], undetermined=[])
        self.appends.clear()
        phase("append", 1, lambda: sp._append(self.root, body))
        out["append"] = json.dumps([["{doit}" if a == str(sp.DOIT) else a for a in argv] for argv in self.appends])
        return out

    def test_cache_modes_and_busy_fallback(self):
        self.feedback = False
        seed(self.root)
        tree = lambda: {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                        for d in ("events", "content") for p in sorted((self.root / d).iterdir())}
        before = tree()
        answers = {}
        for mode in MODES:
            with cache_mode(mode, self.root) as calls:
                answers[mode] = self.answer(calls, mode)
            if mode == "cold":
                self.assertTrue(any((self.root / "cache").iterdir()))
            self.assertEqual(tree(), before, mode)
        self.assertEqual(set(FROZEN), set(answers["uncached"]))
        for mode in MODES:
            with self.subTest(mode=mode):
                self.assertEqual(answers[mode], answers["uncached"])
                self.assertEqual(answers[mode], FROZEN)
        # Supplied events and spec_written never read, even with a busy cache.
        with cache_mode("uncached", self.root):
            events = sp._events(self.root)
        written = next((e for e in events if e["type"] == "spec-written"
                        and e.get("subject") == "L-spec-9404"), None)
        self.assertIsNotNone(written, "seeded spec-written event missing from the ledger read")
        with cache_mode("busy", self.root) as calls:
            sp.run("L-spec-9404", root=self.root, now=NOW, events=events)
            sp._append(self.root, dict(spec="L-spec-9404", findings=[], red_on_master=[],
                                       undetermined=[]), spec_written=written)
            self.assertEqual(calls, [])

    def test_one_read_and_order(self):
        for count in (3, 6):
            with self.subTest(count=count):
                # Each subcase needs its own ledger, including real authorization filtering.
                for path in (self.root / "events").iterdir():
                    path.unlink()
                expected = []
                for i in range(count):
                    sid, project = f"L-spec-{9000+i}", f"project-{i}"
                    self.spec(sid, project)
                    if i == 0:
                        self.event("spec-written", sid, project=project + "-new",
                                   path=str(self.root / "content" / (sid + ".md")))
                        project += "-new"
                    expected.append((sid, project))
                for sid, kind in (("L-spec-9100", "build-started"), ("L-spec-9101", "spec-killed")):
                    self.spec(sid)
                    self.event(kind, sid)
                stdout, stderr = self.tick()
                self.assert_appends(expected)
                self.assertTrue(stdout.startswith(
                    f"spec-preflight: processed={count} deferred=0 "
                    "(tick budget 240s); skipped=2"), stdout)
                self.assertEqual(stderr, "")

    def test_midpass_rewrite_and_restamp(self):
        first, second = "L-spec-9200", "L-spec-9201"
        self.spec(first)
        self.spec(second)

        def inject(sid):
            path = self.root / "content" / (sid + ".md")
            self.event("spec-written", sid, project="albert-scott", path=str(path))
            if sid == second:
                path.write_text("different rewritten text of a different length\n")

        self.inject = inject
        stdout, stderr = self.tick()
        self.assert_appends([(first, "do-it-v2")])
        self.assertEqual(stderr, self.recheck(second))
        self.assertTrue(stdout.startswith("spec-preflight: processed=2"))
        events = sp._events(self.root)
        self.assertEqual(len([e for e in events if e["type"] == "spec-written"
                              and e["project"] == "albert-scott"]), 2)
        self.assertEqual([e["subject"] for e in events if e["type"] == "spec-preflight"], [first])
        self.inject = lambda sid: None
        self.tick()
        self.assert_appends([(second, "albert-scott")])
        self.tick()
        self.assert_appends([])

    def test_utime_only(self):
        sid = "L-spec-9300"
        event = self.spec(sid)
        path = Path(event["path"])
        original = path.read_bytes()
        mtime = path.stat().st_mtime_ns
        self.inject = lambda _: os.utime(path, ns=(mtime + 10**9, mtime + 10**9))
        _, stderr = self.tick()
        self.assertEqual(path.read_bytes(), original)
        self.assert_appends([])
        self.assertEqual(stderr, self.recheck(sid))

    def test_deleted_file_does_not_stop_later_spec(self):
        first, second = "L-spec-9400", "L-spec-9401"
        path = Path(self.spec(first)["path"])
        self.spec(second)
        self.inject = lambda sid: path.unlink() if sid == first else None
        stdout, stderr = self.tick()
        self.assert_appends([(second, "do-it-v2")])
        self.assertEqual(stderr, self.recheck(first))
        self.assertTrue(stdout.startswith("spec-preflight: processed=2"))

    def test_guard_oserrors_do_not_stop_pass(self):
        sids = [f"L-spec-{9500+i}" for i in range(3)]
        for sid in sids:
            self.spec(sid)
        original = sp._text_identity
        calls = dict.fromkeys(sids, 0)

        def identity(root, event):
            sid = event["subject"]
            calls[sid] += 1
            if (sid, calls[sid]) in ((sids[0], 1), (sids[1], 2)):
                raise PermissionError("fixture guard failure")
            return original(root, event)

        with mock.patch.object(sp, "_text_identity", side_effect=identity):
            stdout, stderr = self.tick()
        self.assert_appends([(sids[2], "do-it-v2")])
        self.assertEqual(stderr, self.recheck(sids[0]) + self.recheck(sids[1]))
        self.assertTrue(stdout.startswith("spec-preflight: processed=3"))

    def test_unchanged_restamp(self):
        sid = "L-spec-9600"
        event = self.spec(sid)
        self.inject = lambda _: self.event("spec-written", sid, path=event["path"], project="albert-scott")
        _, stderr = self.tick()
        self.assert_appends([(sid, "do-it-v2")])
        self.assertEqual(stderr, "")
        self.inject = lambda _: None
        self.tick()
        self.assert_appends([])

    def test_empty_pass(self):
        stdout, stderr = self.tick()
        self.assert_appends([])
        self.assertTrue(stdout.startswith("spec-preflight: processed=0 deferred=0 (tick budget 240s); skipped=0"))
        self.assertEqual(stderr, "")

    def test_direct_call_fallbacks_and_supplied_events(self):
        sid, project = "L-spec-9700", "fixture-project"
        self.spec(sid, project)
        events = sp._events(self.root)
        written = next(e for e in reversed(events) if e["type"] == "spec-written")
        body = self.counted(lambda: sp.run(sid, root=self.root))
        supplied = self.counted(lambda: sp.run(sid, root=self.root, events=events), reads=0)
        self.assertEqual(body, supplied)
        self.assertEqual(body["findings"], [dict(check=1, id=project, line=sid + " source snapshot")])
        changed = [{**e, "project": "other-proj"} if e is written else e for e in events]
        other = self.counted(lambda: sp.run(sid, root=self.root, events=changed), reads=0)
        self.assertEqual(other["findings"][0]["id"], "other-proj")
        empty = self.counted(lambda: sp.run(sid, root=self.root, events=[]), reads=0)
        self.assertEqual(empty["undetermined"], ["spec: no spec-written"])
        self.feedback = False
        self.counted(lambda: sp._append(self.root, body))
        fallback = self.appends[-1]
        self.counted(lambda: sp._append(self.root, body, spec_written=written), reads=0)
        self.assertEqual(self.appends[-1], fallback)
        self.counted(lambda: sp._append(self.root, body, spec_written={**written, "project": "other-proj"}), reads=0)
        self.assertEqual(self.appends[-1][4], "project=other-proj")

    def test_identity_fields_and_path_fallbacks(self):
        event = self.spec("L-spec-9800")
        path = Path(event["path"])
        stat = path.stat()
        expected = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(sp._text_identity(self.root, event), expected)
        self.assertEqual(sp._text_identity(self.root, {**event, "path": "content/" + path.name}), expected)
        self.assertEqual(sp._text_identity(self.root, {**event, "path": "missing/" + path.name}), expected)
        path.unlink()
        self.assertIsNone(sp._text_identity(self.root, event))


    def test_projection_order_and_authorization(self):
        write_rows(self.root, projection_files())
        full, authorized = reference_read(self.root)
        expected = [e for e in authorized if e["type"] in SIX]
        # Frozen by hand from the seed: corrections applied, forged and unauthorized rows absent.
        frozen = ["L-spec-writer-a.jsonl:2", "L-spec-writer-b.jsonl:1", "L-spec-writer-b.jsonl:2",
                  "L-spec-preflight-a.jsonl:1", "L-spec-writer-b.jsonl:3", "L-spec-writer-a.jsonl:3",
                  "L-builder-a.jsonl:1", "L-executor-a.jsonl:3", "L-executor-a.jsonl:4",
                  "L-operator-a.jsonl:5"]
        self.assertEqual([e["_src"] for e in expected], frozen)
        self.assertGreater(len(full), len(authorized))
        self.assertGreater(len(authorized), len(expected))
        reads, folds = [], []
        real_read, real_fold = fold.read_events, fold.fold

        def read(*a, **k):
            out = real_read(*a, **k)
            reads.append((a, k, len(out)))
            return out

        def folded(events, *a, **k):
            folds.append([e["type"] for e in events])
            return real_fold(events, *a, **k)

        with mock.patch.object(fold, "read_events", side_effect=read), \
                mock.patch.object(fold, "fold", side_effect=folded):
            got = sp._events(self.root)
        self.assertEqual(got, expected)
        self.assertEqual([e["_src"] for e in got], frozen)
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0][2], len(full))
        self.assertEqual(len(folds), 1)
        self.assertEqual(len(folds[0]), len(full))
        self.assertTrue({"lesson", "correction", "writes-granted"} <= set(folds[0]))
        self.assertNotEqual(got, authorized)  # an unchanged full-list return fails here
        by_src = {e["_src"]: e for e in got}
        self.assertEqual(by_src["L-spec-writer-b.jsonl:1"]["project"], "pc")  # type change landed
        self.assertEqual(by_src["L-spec-writer-a.jsonl:3"]["project"], "p1c")
        self.assertEqual(by_src["L-executor-a.jsonl:4"]["type"], "spec-killed")
        for absent in ("L-executor-a.jsonl:1", "L-builder-a.jsonl:2", "L-builder-a.jsonl:3",
                       "L-spec-writer-a.jsonl:1", "L-builder-b.jsonl:1"):
            self.assertNotIn(absent, by_src)
        # Filtering before ordering/fold must differ: drop irrelevant lines per file first.
        early = Path(tempfile.mkdtemp(prefix="early-", dir=self.root))
        files = projection_files()
        write_rows(early, {n: [r for r in rows if r["type"] in SIX] for n, rows in files.items()})
        _, early_authorized = reference_read(early)
        early_rows = [(e["type"], e["subject"], e.get("project")) for e in early_authorized if e["type"] in SIX]
        want_rows = [(e["type"], e["subject"], e.get("project")) for e in expected]
        self.assertNotEqual(early_rows, want_rows)
        kinds = ["retained-" + t for t in SIX] + [
            "irrelevant-kinds", "equal-timestamp-irrelevant-head", "repeated-spec-written", "old-facts",
            "valid-correction-type-change", "valid-correction-project-change",
            "correction-retained-to-irrelevant", "unauthorized-correction", "forged-role-retained-rows",
            "early-filter-differs", "unchanged-full-return-differs"]
        seen = {e["type"] for e in got}
        exercised = ["retained-" + t for t in seen] + kinds[len(SIX):]
        report("AC1", "test_projection_order_and_authorization", kinds, exercised,
               ["exact ordered subsequence", "one read", "one fold of full list", "early filter differs"],
               "pass", canonical_count=len(full), fold_input_count=len(folds[0]),
               authorized_count=len(authorized), retained_source_ids=frozen,
               read_calls=len(reads), fold_calls=len(folds))

    def real_root(self, root, specs, extra_rows=()):
        """A synthetic project repository, spec texts and a ledger for real checks."""
        root = Path(root)
        repo = root / "repos" / "do-it-v2"
        (repo / "src").mkdir(parents=True, exist_ok=True)
        (repo / "src" / "a.py").write_text("X = 1\n")
        (repo / "src" / "test_a.py").write_text("import a\nraise SystemExit(1)\n")
        git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                        check=True, capture_output=True)
        git("init", "-b", "main")
        git("add", "-A")
        git("commit", "-m", "base")
        (root / "content").mkdir(exist_ok=True)
        rows = []
        for sid, ts, kind in specs:
            path = root / "content" / (sid + ".md")
            path.write_text(f"{sid} text\n## Interfaces\nWrites: src/a.py\n\n## Acceptance criteria\n\n"
                            "AC1 [backend]: it works.\nreview_path: run src/test_a.py\n\n"
                            "## Verification\n\n```bash\n/usr/bin/python3 src/test_a.py\n```\n")
            rows.append(dict(type="spec-written", subject=sid, ts=ts, project="do-it-v2", path=str(path)))
        write_rows(root, {"L-spec-writer-x.jsonl": rows, **dict(extra_rows)})

    def test_projection_tick_equality_and_roundtrip(self):
        self.stack.close()  # real _process and real checks from here on
        scratch_env = mock.patch.dict(os.environ, {"DOIT_SCRATCH": str(self.root / "scratch")})
        scratch_env.start()
        self.addCleanup(scratch_env.stop)
        iso = lambda delta: (NOW + delta).isoformat()
        minute = lambda m: timedelta(minutes=m)
        specs = [("L-spec-9801", iso(-minute(40)), "eligible"), ("L-spec-9802", iso(-minute(30)), "eligible"),
                 ("L-spec-9803", iso(-minute(20)), "eligible"), ("L-spec-9804", iso(-minute(10)), "rewritten"),
                 ("L-spec-9805", iso(-minute(50)), "preflighted"), ("L-spec-9806", iso(-minute(45)), "built"),
                 ("L-spec-9807", iso(-minute(44)), "shipped"), ("L-spec-9808", iso(-minute(43)), "closed"),
                 ("L-spec-9809", iso(-minute(42)), "killed"), ("L-spec-9810", iso(-timedelta(days=4)), "stale"),
                 ("L-spec-9811", "not-a-timestamp", "malformed"), ("L-spec-9812", iso(minute(90)), "future")]
        rows = lambda sid, kind, ts, **kw: dict(type=kind, subject=sid, ts=ts, **kw)
        text_of = lambda sid: str(self.root / "content" / (sid + ".md"))
        self.real_root(self.root, specs)
        written = {}
        for line in (self.root / "events" / "L-spec-writer-x.jsonl").read_text().splitlines():
            row = json.loads(line)
            written[row["subject"]] = row
        extra = [dict(written["L-spec-9804"], ts=iso(-minute(9)))]
        with (self.root / "events" / "L-spec-writer-x.jsonl").open("a") as out:
            out.write(json.dumps(extra[0]) + "\n")
        write_rows(self.root, {
            "L-spec-writer-x.jsonl": [json.loads(l) for l in
                                      (self.root / "events" / "L-spec-writer-x.jsonl").read_text().splitlines()],
            "L-spec-preflight-x.jsonl": [rows("L-spec-9805", "spec-preflight", iso(-minute(48)), project="do-it-v2"),
                                         rows("L-spec-9802", "writes-granted", iso(-minute(29)), project="do-it-v2",
                                              spec_written_src="L-spec-writer-x.jsonl:2")],
            "L-builder-x.jsonl": [rows("L-spec-9806", "build-started", iso(-minute(40)))],
            "L-executor-x.jsonl": [rows("L-spec-9807", "shipped", iso(-minute(35))),
                                   rows("L-spec-9809", "spec-killed", iso(-minute(35))),
                                   rows("L-spec-9801", "lesson", iso(-minute(35)))],
            "L-operator-x.jsonl": [rows("L-spec-9808", "spec-closed", iso(-minute(35)))]})
        sids = [s[0] for s in specs]

        def one_pass(reference, step):
            clock = {"t": 0.0}
            captured = []
            real_run = sp.run

            def run(*a, **k):
                out = real_run(*a, **k)
                clock["t"] += step
                return out

            def recorder(kind):
                return lambda *a, **k: captured.append(
                    [kind, json.dumps(a[1:] if kind != "append" else [a[1]], sort_keys=False, default=str),
                     json.dumps(k.get("spec_written"), default=str)])

            with ExitStack() as stack:
                if reference:
                    stack.enter_context(mock.patch.object(sp, "_project", side_effect=lambda events: events))
                stack.enter_context(mock.patch.object(sp, "time", SimpleNamespace(monotonic=lambda: clock["t"])))
                stack.enter_context(mock.patch.object(sp, "run", side_effect=run))
                stack.enter_context(mock.patch.object(sp, "_append", side_effect=recorder("append")))
                stack.enter_context(mock.patch.object(sp, "_append_grant", side_effect=recorder("grant")))
                stack.enter_context(mock.patch.object(sp, "_append_coupled_files", side_effect=recorder("files")))
                events = sp._events(self.root)
                waiting = sp.pending(events)
                eligible = [sid for sid in waiting if sp._eligible(sid, events, NOW)]
                direct = json.dumps(real_run("L-spec-9801", root=self.root, now=NOW), sort_keys=False)
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = sp.tick(root=self.root, now=NOW)
            return dict(pending=waiting, eligible=eligible, direct=direct, code=code,
                        stdout=out.getvalue(), stderr=err.getvalue(), appended=captured)

        results = {}
        for mode, step in (("within-budget", 10.0), ("crossing-budget", 200.0)):
            reference, projected = one_pass(True, step), one_pass(False, step)
            self.assertEqual(projected, reference, mode)
            results[mode] = projected
        within, crossing = results["within-budget"], results["crossing-budget"]
        self.assertEqual(within["eligible"], ["L-spec-9801", "L-spec-9802", "L-spec-9803", "L-spec-9804"])
        self.assertIn("processed=4 deferred=0", within["stdout"])
        self.assertIn("processed=2 deferred=2", crossing["stdout"])
        self.assertIn("deferred specs=L-spec-9803, L-spec-9804", crossing["stdout"])
        self.assertEqual(sorted(sp.pending(sp._events(self.root))), sorted(
            ["L-spec-9801", "L-spec-9802", "L-spec-9803", "L-spec-9804", "L-spec-9806", "L-spec-9807",
             "L-spec-9808", "L-spec-9809", "L-spec-9810", "L-spec-9811", "L-spec-9812"]))
        bodies = [json.loads(r[1])[0] for r in within["appended"] if r[0] == "append"]
        self.assertEqual(len(bodies), 4)
        self.assertTrue(all(b["findings"] for b in bodies))
        self.assertEqual(bodies[0]["lag_s"], 2400.0)
        self.assertEqual(json.loads(within["direct"])["red_on_master"], ["src/test_a.py"])
        # Grant dedupe reads the full authorized list: no second grant for 9802.
        self.assertNotIn("L-spec-9802", " ".join(r[1] for r in within["appended"] if r[0] == "grant"))
        # Separate root: real tick, real production append, fresh-process read-back.
        trip = Path(tempfile.mkdtemp(prefix="roundtrip-", dir=self.root))
        self.real_root(trip, [("L-spec-9901", iso(-minute(5)), "eligible")])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(sp.tick(root=trip, now=NOW), 0, err.getvalue())
        src = Path(__file__).resolve().parent
        probe = ("import json,sys,fold\nfrom pathlib import Path\nr=Path(sys.argv[1])\n"
                 "fold.ROOT,fold.EVENTS,fold.PROJECT=r,r/'events',''\n"
                 "print(json.dumps([e for e in fold.read_events(use_cache=False) if e['type']=='spec-preflight']))")
        read = subprocess.run([sys.executable, "-c", probe, str(trip)], cwd=src, capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(src)}, timeout=60)
        self.assertEqual(read.returncode, 0, read.stderr)
        committed = json.loads(read.stdout)
        self.assertEqual(len(committed), 1)
        self.assertEqual((committed[0]["subject"], committed[0]["project"]), ("L-spec-9901", "do-it-v2"))
        self.assertEqual(committed[0]["red_on_master"], ["src/test_a.py"])
        self.assertTrue(committed[0]["findings"])
        self.assertEqual(committed[0]["lag_s"], 300.0)
        kinds = ["eligible-nonempty-findings", "already-preflighted", "later-build-started", "shipped",
                 "spec-closed", "spec-killed", "stale-timestamp", "malformed-timestamp", "future-timestamp",
                 "rewritten-spec", "scheduling-boundary", "within-budget-pass", "real-append-roundtrip",
                 "grant-dedupe-full-list"]
        report("AC2", "test_projection_tick_equality_and_roundtrip", kinds, kinds,
               ["pending/eligibility/direct run/tick stdout+stderr/append payloads equal, unnormalized",
                "boundary crossed on deterministic clock", "fresh-process read-back"], "pass",
               active_limit="TICK_BUDGET_S=%s" % sp.TICK_BUDGET_S,
               clock_sequence={"within-budget": "+10s per run", "crossing-budget": "+200s per run"},
               comparison={m: dict(stdout=r["stdout"], stderr=r["stderr"], pending=r["pending"],
                                   eligible=r["eligible"], appended=r["appended"])
                           for m, r in results.items()},
               check_findings=bodies[0]["findings"], red_on_master=bodies[0]["red_on_master"],
               committed_event_readback=committed)

    def test_projection_entry_contracts(self):
        write_rows(self.root, projection_files(str(self.root / "content" / "x.md")))
        (self.root / "content" / "x.md").write_text("x source snapshot\n## Interfaces\nWrites: src/a.py\n")
        context = lambda: (fold.ROOT, fold.EVENTS, fold.PROJECT)
        before = context()
        full, authorized = reference_read(self.root)
        retained = [e for e in authorized if e["type"] in SIX]
        exercised, counts, failures = [], {}, {}
        real_project = sp._project

        def spy():
            return mock.patch.object(sp, "_project", side_effect=real_project)

        # Direct run and absent-spec_written _append use the projected boundary.
        with spy() as projected, mock.patch.object(fold, "read_events", wraps=fold.read_events) as reads:
            body = sp.run("L-spec-1", root=self.root, now=NOW)
        self.assertEqual((reads.call_count, projected.call_count), (1, 1))
        self.assertEqual(projected.call_args[0][0], authorized)
        self.assertEqual(body["spec"], "L-spec-1")
        counts["direct-run"] = [reads.call_count, projected.call_count]
        with spy() as projected, mock.patch.object(fold, "read_events", wraps=fold.read_events) as reads:
            sp._append(self.root, dict(spec="L-spec-1", findings=[], red_on_master=[], undetermined=[]))
        self.assertEqual((reads.call_count, projected.call_count), (1, 1))
        self.assertEqual(self.appends[-1][4], "project=p1c")
        counts["append-absent-written"] = [reads.call_count, projected.call_count]
        exercised += ["direct-run-projected", "append-absent-written-projected"]
        # Supplied events/spec_written bypass reads.
        with spy() as projected, mock.patch.object(fold, "read_events") as reads:
            sp.run("L-spec-1", root=self.root, now=NOW, events=retained)
            sp._append(self.root, dict(spec="L-spec-1", findings=[], red_on_master=[], undetermined=[]),
                       spec_written=retained[0])
        self.assertEqual((reads.call_count, projected.call_count), (0, 0))
        counts["supplied"] = [0, 0]
        exercised.append("supplied-bypass")
        # Tick: single normal snapshot, one projection, and the text-change guard.
        sid = "L-spec-9990"
        self.spec(sid)
        self.inject = lambda s: (self.root / "content" / (s + ".md")).write_text("changed text of other length\n")
        with spy() as projected:
            _, stderr = self.tick()
        self.assertEqual(projected.call_count, 1)
        self.assertIn(self.recheck(sid), stderr)
        counts["tick"] = [1, 1]
        exercised += ["tick-single-snapshot", "tick-text-change-guard"]
        self.inject = lambda s: None
        # Busy lock: reference fallback, then the full projection.
        full, authorized = reference_read(self.root)
        retained = [e for e in authorized if e["type"] in SIX]
        with cache_mode("busy", self.root) as calls, mock.patch.object(fold, "fold", wraps=fold.fold) as folded:
            got = sp._events(self.root)
        self.assertEqual(got, retained)
        self.assertEqual([c.get("use_cache") for c in calls], [None, False])
        self.assertEqual(folded.call_count, 1)
        self.assertEqual(len(folded.call_args[0][0]), len(full))
        self.assertEqual(context(), before)
        counts["busy-fallback"] = [len(calls), folded.call_count]
        exercised += ["busy-fallback-projected", "context-restored-after-success"]
        # ReentrantRead: surfaces, no reference retry.
        with mock.patch.object(fold, "read_events", side_effect=ledger_cache.ReentrantRead("fixture")) as reads:
            with self.assertRaises(ledger_cache.ReentrantRead):
                sp._events(self.root)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(context(), before)
        counts["reentrant"] = [reads.call_count]
        exercised += ["reentrant-read-no-retry", "context-restored-after-failure"]
        # Failed reference read is never a clean or empty success.
        seq = [ledger_cache.LockTimeout("busy"), OSError("reference read failed")]
        with mock.patch.object(fold, "read_events", side_effect=seq) as reads:
            with self.assertRaises(OSError) as raised:
                sp._events(self.root)
        failures["reference-failure"] = str(raised.exception)
        self.assertEqual(reads.call_count, 2)
        seq = [ledger_cache.LockTimeout("busy"), OSError("reference read failed")]
        err = io.StringIO()
        with mock.patch.object(fold, "read_events", side_effect=seq), redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = sp.tick(root=self.root, now=NOW)
        self.assertEqual(code, 2)
        self.assertIn("preflight ledger unavailable: reference read failed", err.getvalue())
        self.assertEqual(context(), before)
        exercised.append("failed-reference-read")
        # Missing events directory.
        empty = Path(tempfile.mkdtemp(prefix="noevents-", dir=self.root))
        with self.assertRaises(OSError) as raised:
            sp._events(empty)
        failures["missing-events"] = str(raised.exception)
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(sp.tick(root=empty, now=NOW), 2)
        self.assertEqual(context(), before)
        exercised.append("missing-events-directory")
        kinds = ["direct-run-projected", "append-absent-written-projected", "supplied-bypass",
                 "tick-single-snapshot", "tick-text-change-guard", "busy-fallback-projected",
                 "reentrant-read-no-retry", "failed-reference-read", "missing-events-directory",
                 "context-restored-after-success", "context-restored-after-failure"]
        report("AC3", "test_projection_entry_contracts", kinds, exercised,
               ["entry read counts", "fallback keeps full fold then projection", "faults surface",
                "fold root/events/project restored"], "pass", read_counts=counts,
               failure_diagnostics=failures, context_restored=context() == before)


if __name__ == "__main__":
    unittest.main()
