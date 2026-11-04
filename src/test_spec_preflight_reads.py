#!/usr/bin/env python3
"""Hermetic read-count and text-change regressions for a preflight pass."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import fold
import spec_preflight as sp

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


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
                self.assert_appends(expected[:4])
                self.assertTrue(stdout.startswith(
                    f"spec-preflight: processed={min(count, 4)} deferred={max(0, count-4)} "
                    "(tick cap 4); skipped=2"), stdout)
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
        self.assertTrue(stdout.startswith("spec-preflight: processed=0 deferred=0 (tick cap 4); skipped=0"))
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


if __name__ == "__main__":
    unittest.main()
