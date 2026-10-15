#!/usr/bin/env python3
"""Hermetic owed digest regressions; the Telegram boundary is always a stub."""
import contextlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# Isolate before importing any module that resolves DOIT_ROOT.
_ISO = tempfile.TemporaryDirectory(prefix="owed-digest-test-")
os.environ["DOIT_ROOT"] = str(pathlib.Path(_ISO.name) / "root")
os.environ["HOME"] = str(pathlib.Path(_ISO.name) / "home")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import crons
import dispatch
import fold
import owed
import owed_digest as digest

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
ASK = "Please approve the operator check in full."


def ts(days=0):
    return (NOW - timedelta(days=days)).isoformat()


def event(kind, spec="L-spec-9001", days=0, actor="operator", **kv):
    return dict(type=kind, subject=spec, ts=ts(days), actor=actor, **kv)


def fixture():
    """Hand-declared lifecycle expectations, ten of each status = 110 rows."""
    all_ev, expected = [], {}
    for i in range(10):
        for j, status in enumerate(("waiting", "due", "blocked", "failed", "correcting",
                                    "quarantined", "met", "waived", "expired", "dropped", "unshipped")):
            spec = f"L-spec-{9000 + i * 11 + j}"
            expected[spec] = status
            evs = [event("owed-ac", spec, 20, criterion="verify", wake_at=ts(-30 if status == "waiting" else 19))]
            if status != "unshipped":
                evs.append(event("shipped", spec, 19, actor="executor"))
            if status == "dropped":
                evs.append(event("spec-killed", spec, 18))
            if status in ("blocked", "expired"):
                evs.append(event("owed-unobservable", spec, 10, actor="owed-sweeper", criterion="verify",
                                 capability="operator-action" if status == "blocked" else "unknown-capability",
                                 why=ASK))
            if status in ("failed", "correcting"):
                evs.append(event("owed-failed", spec, 11, actor="owed-sweeper", criterion="verify", evidence="unmet"))
            if status == "correcting":
                evs.append(event("owed-corrective", spec, 10, criterion="verify", corrective="L-spec-9999"))
            if status == "quarantined":
                evs.append(event("owed-quarantined", spec, 10, actor="sweep", criterion="verify",
                                 reason="cannot build verification", spec_sha256="fixture"))
            if status == "met":
                evs.append(event("owed-met", spec, 10, actor="owed-sweeper", criterion="verify", evidence="proof"))
            if status == "waived":
                evs.append(event("owed-waived", spec, 10, criterion="verify", reason="operator waived"))
            all_ev.extend(evs)
    return sorted(all_ev, key=lambda e: e["ts"]), expected


def sweep(outcomes, old=False):
    return [event("sweep-run", "L-sweep", 2 if old else 0.1 - i / 1000,
                  actor="sweep", outcome=outcome, rows=1, reason="dispatch refused")
            for i, outcome in enumerate(outcomes)]


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="owed-digest-case-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.events = self.root / "events"
        self.events.mkdir()
        for target, key, value in ((fold, "ROOT", self.root), (fold, "EVENTS", self.events),
                                   (fold, "PROJECT", None)):
            p = patch.object(target, key, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"DOIT_ROOT": str(self.root)})
        p.start()
        self.addCleanup(p.stop)
        self.sent = []
        self.sender = lambda argv: self.send(argv)
        self.install([event("decision", text="a readable ledger")])

    def send(self, argv):
        self.sent.append(argv)
        return subprocess.CompletedProcess(argv, 0, "200\n", "")

    def install(self, events):
        grouped = {}
        for e in events:
            row = dict(e)
            actor = row.pop("actor")
            grouped.setdefault(actor, []).append(row)
        for actor, rows in grouped.items():
            (self.events / f"L-{actor}-fixture.jsonl").write_text(
                "".join(json.dumps(e) + "\n" for e in rows))

    def state(self):
        return json.loads((self.root / "state/owed-digest.json").read_text())

    def run_digest(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return digest.run(now=kw.pop("now", NOW), sender=self.sender, **kw)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_recount(self):
        evs, expected = fixture()
        obj = digest.build(evs, NOW)
        recount = Counter(r["status"] for s in fold.fold(evs)[0].values() for r in owed.checks(s["evs"], NOW))
        self.assertEqual(obj["counts"]["by_status"], dict(recount))
        self.assertEqual(set(obj), {"at", "counts", "needs_you", "health", "m1"})
        self.assertEqual(set(obj["counts"]), {"by_status", "blocked_by_capability", "sweep"})
        self.assertEqual(set(obj["counts"]["by_status"]), set(owed.STATUSES))
        self.assertEqual({r["spec"] for r in obj["needs_you"]}, {s for s, v in expected.items() if v == "blocked"})
        self.assertEqual(obj["counts"]["blocked_by_capability"], {"operator-action": 10})
        # Independent, hand-labelled recount: no owed/checks call in this oracle.
        self.assertEqual(obj["counts"]["by_status"], dict(Counter(expected.values())))
        self.assertEqual(obj["m1"], {"terminal_unobservable": 10, "verify_unbuildable": 10,
                                      "sweep_failed_one_row_24h": 0})
        self.assertEqual(len(expected), 110)
        self.assertEqual({r["kind"] for r in obj["health"]},
                         {"expired", "failed", "quarantined", "correcting-stale"})
        zero = digest.build([], NOW)
        self.assertEqual(zero["counts"]["by_status"], dict.fromkeys(owed.STATUSES, 0))
        print("PASS by_status recount; needs_you; recount (110 hand-labelled rows)")

    def test_sweep(self):
        evs = sweep(["failed", "batched", "failed", "failed", "failed"])
        obj = digest.build(sweep(["failed"], old=True) + evs, NOW)
        self.assertEqual(obj["counts"]["sweep"], dict(runs_24h=5, failed_24h=4,
                         consecutive_failed=3, last_batched_at=evs[1]["ts"]))
        self.assertEqual(obj["m1"]["sweep_failed_one_row_24h"], 4)
        for outcome in ("batched", "nothing-due", "open"):
            obj = digest.build(sweep(["failed", "failed", outcome]), NOW)
            self.assertEqual(obj["counts"]["sweep"]["consecutive_failed"], 0)
        self.assertEqual(digest.build([], NOW)["counts"]["sweep"]["last_batched_at"], None)
        print("PASS sweep streak; sweep reset; 24h window")

    def test_atomic(self):
        digest.write_state({"old": True})
        path = self.root / "state/owed-digest.json"
        with patch.object(digest.os, "replace", side_effect=OSError("rename interrupted")):
            with self.assertRaises(OSError):
                digest.write_state({"new": True})
        self.assertEqual(json.loads(path.read_text()), {"old": True})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(path.parent.iterdir()), [path])
        print("PASS atomic state write")

    def test_daily(self):
        evs, _ = fixture()
        self.install(evs)
        self.assertEqual(self.run_digest(), 0)
        md = self.root / "content/L-owed-digest-2026-10-15.md"
        text = md.read_text()
        self.assertLess(text.index("Counts by status and cause"), text.index("NEEDS YOU"))
        self.assertLess(text.index("NEEDS YOU"), text.index("SYSTEM HEALTH"))
        needs = text.split("NEEDS YOU")[1].split("SYSTEM HEALTH")[0]
        for row in self.state()["needs_you"]:
            self.assertIn(row["spec"], needs)
            self.assertIn(row["criterion"], needs)
            self.assertIn(ASK, needs)
        self.assertNotIn("quarantined", needs)
        fresh = fold.read_events()  # SECOND independent store read, after run returned.
        emitted = [e for e in fresh if e["type"] == "owed-digest"]
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["path"], str(md))
        self.assertEqual(emitted[0]["counts"], self.state()["counts"])
        self.assertNotIn(emitted[0], fold.fold(fresh)[2])
        raw = json.loads((self.events / digest.LEDGER_NAME).read_text())
        self.assertNotIn("actor", raw)
        self.assertEqual(self.run_digest(now=NOW + timedelta(minutes=1)), 0)
        self.assertEqual(len([e for e in fold.read_events() if e["type"] == "owed-digest"]), 1)
        self.assertEqual(self.sent, [])
        self.assertIn("none", digest.render_md(digest.build([], NOW)).split("NEEDS YOU")[1])
        print("PASS md sections; ask verbatim; ledger round trip; one event per date")

    def test_new_alert(self):
        evs, _ = fixture()
        blocked = [e for e in evs if e["subject"] == "L-spec-9002"]
        self.install(blocked)
        def sender(argv):
            self.assertEqual(len(self.state()["needs_you"]), 1)
            return self.send(argv)
        self.sender = sender
        self.assertEqual(self.run_digest(alert=True), 0)
        old_at = self.state()["at"]
        self.assertEqual(self.run_digest(alert=True, now=NOW + timedelta(minutes=30)), 0)
        self.assertNotEqual(self.state()["at"], old_at)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], str(self.root / "telegram/tg-send.sh"))
        self.assertEqual(len(self.sent[0]), 2)
        self.assertIn(ASK, self.sent[0][1])
        self.assertLessEqual(len(self.sent[0][1]), 1000)
        self.assertFalse((self.root / "content").exists())
        self.assertFalse((self.events / digest.LEDGER_NAME).exists())
        # Leave needs_you, then reopen with a newer unobservable timestamp.
        self.install([e for e in blocked if e["type"] != "owed-unobservable"])
        self.assertEqual(self.run_digest(alert=True), 0)
        reopened = dict(blocked[-1], ts=ts(1))
        self.install(blocked[:-1] + [reopened])
        self.assertEqual(self.run_digest(alert=True), 0)
        self.assertEqual(len(self.sent), 2)
        print("PASS alert rewrites state; alert writes no md/event; alert new row once; alert re-opened row; only tg-send.sh")

    def test_send_failure(self):
        evs, _ = fixture()
        self.install([e for e in evs if e["subject"] == "L-spec-9002"])
        for code, stdout in ((1, "200\n"), (0, "429\n"), (0, ""), (0, "200\n500\n")):
            self.sender = lambda argv: subprocess.CompletedProcess(argv, code, stdout, "")
            self.assertNotEqual(self.run_digest(alert=True), 0)
            self.assertFalse((self.root / "state/owed-alerted.json").exists())
        self.sender = lambda argv: self.send(argv)
        self.assertEqual(self.run_digest(alert=True), 0)
        self.assertEqual(len(self.sent), 1)
        print("PASS alert retry on send failure; alert 429 is failure")

    def test_streak_alert(self):
        self.install(sweep(["failed", "failed"]))
        self.assertEqual(self.run_digest(alert=True), 0)
        self.assertEqual(self.sent, [])
        # Failed HTTP responses must leave the streak eligible for retry too.
        self.install(sweep(["failed"] * 3))
        self.sender = lambda argv: subprocess.CompletedProcess(argv, 0, "429\n", "")
        self.assertNotEqual(self.run_digest(alert=True), 0)
        self.sender = lambda argv: self.send(argv)
        for n in range(3, 7):
            self.install(sweep(["failed"] * n))
            self.assertEqual(self.run_digest(alert=True), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("3", self.sent[0][1])
        self.assertIn("dispatch refused", self.sent[0][1])
        self.install(sweep(["failed"] * 6 + ["open"] + ["failed"] * 3))
        self.assertEqual(self.run_digest(alert=True), 0)
        self.assertEqual(len(self.sent), 2)
        print("PASS alert streak 3; alert streak 2 silent; alert streak retry")

    def test_health_and_empty(self):
        evs, expected = fixture()
        self.install([e for e in evs if expected[e["subject"]] != "blocked"])
        self.assertEqual(self.run_digest(alert=True), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(digest.build([], NOW)["counts"]["sweep"]["runs_24h"], 0)
        with patch.object(fold, "EVENTS", self.root / "empty-owed-ledger"):
            fold.EVENTS.mkdir()
            empty_file = fold.EVENTS / "L-operator-fixture.jsonl"
            empty_file.write_text(json.dumps(event("decision", text="no owed rows")) + "\n")
            self.assertEqual(self.run_digest(alert=True), 0)
            self.assertEqual(self.state()["needs_you"], [])
            self.assertEqual(self.sent, [])
        print("PASS alert health silent; empty ledger silent")

    def test_fail_closed(self):
        digest.write_state({"old": True})
        for alert in (False, True):
            for problem in ("missing", "empty", "unreadable", "read", "checks"):
                before = self.snapshot()
                with contextlib.ExitStack() as stack:
                    if problem in ("missing", "empty"):
                        path = self.root / problem
                        if problem == "empty":
                            path.mkdir(exist_ok=True)
                        stack.enter_context(patch.object(fold, "EVENTS", path))
                    elif problem == "unreadable":
                        self.events.chmod(0)
                        stack.callback(self.events.chmod, 0o700)
                    else:
                        stack.enter_context(patch.object(fold if problem == "read" else owed,
                            "read_events" if problem == "read" else "checks", side_effect=RuntimeError("undetermined")))
                        if problem == "checks":
                            self.install([event("owed-ac", criterion="verify")])
                    unchanged = self.snapshot() if problem != "unreadable" else before
                    self.assertNotEqual(self.run_digest(alert=alert), 0, problem)
                self.assertEqual(self.snapshot(), unchanged)
        self.assertEqual(self.sent, [])
        print("PASS unreadable ledger fails closed")

    def test_dry_run(self):
        evs, _ = fixture()
        self.install(evs + sweep(["failed"] * 3))
        for alert in (False, True):
            before = self.snapshot()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(digest.run(alert=alert, dry_run=True, now=NOW, sender=self.sender), 0)
            printed = json.loads(out.getvalue())
            self.assertEqual(printed["digest"], digest.build(fold.read_events(), NOW))
            self.assertTrue(printed["telegrams"])
            self.assertEqual(before, self.snapshot())
        self.assertEqual(self.sent, [])
        print("PASS dry run writes nothing; dry run no sender call")

    def test_large_alert(self):
        evs, _ = fixture()
        self.install(evs)
        long_ask = "Please review this operator request. " * 8
        for e in evs:
            if e["type"] == "owed-unobservable":
                e["why"] = long_ask
        self.install(evs)
        all_keys = set()
        for _ in range(10):
            if len(all_keys) == 10:
                break
            self.assertEqual(self.run_digest(alert=True), 0)
            record = json.loads((self.root / "state/owed-alerted.json").read_text())
            for key in set(record["rows"]) - all_keys:
                spec, criterion, _since = key.split("/", 2)
                self.assertIn(f"{spec}/{criterion}: {long_ask.strip()}", self.sent[-1][1])
            all_keys = set(record["rows"])
            self.assertLessEqual(len(self.sent[-1][1]), 1000)
        self.assertEqual(len(all_keys), 10)
        self.assertGreater(len(self.sent), 1)
        self.assertIn("more new rows await the next pass (message limit)", self.sent[0][1])
        print("PASS capped alerts report deferred row count and retry remaining rows")

    def test_security(self):
        row = {"spec": "L-spec-9001", "criterion": "verify", "since": ts(),
               "ask": "unsafe`\n\x00\x1b" + "x" * 400}
        obj = digest.build([], NOW)
        obj["needs_you"] = [row]
        md = digest.render_md(obj)
        self.assertNotIn("\x00", md)
        self.assertNotIn("\x1b", md)
        self.assertIn("``unsafe` ", md)
        self.assertNotIn("x" * 301, md)
        print("PASS free-text sanitisation")

    def test_cron_and_dispatch(self):
        rows = crons.rows()
        jobs = {r["name"]: r for r in rows}
        for name, schedule, suffix in (("owed-digest", "0 6 * * *", ""),
                                        ("owed-digest-alert", "*/30 * * * *", " --alert")):
            row = jobs[name]
            self.assertEqual(row["schedule"], schedule)
            self.assertEqual((row["where"], row["owner"], row["project"]), ("user", "thinker", "do-it-v2"))
            self.assertIn(f"cron-run {name} -- ", row["command"])
            self.assertIn(f"owed-digest{suffix} >>", row["command"])
            for other in rows:
                self.assertEqual(bool(re.search(row["sig"], crons.print_line(other["name"]))), other["name"] == name)
        doit = pathlib.Path(__file__).resolve().parent.parent / "doit"
        self.assertIn('owed-digest) shift; exec python3 "$SRC/owed_digest.py" "$@" ;;', doit.read_text())
        self.assertIn("doit owed-digest", subprocess.check_output([str(doit), "help"], text=True))
        self.assertEqual(subprocess.run([str(doit), "owed-digest", "--dry-run"], capture_output=True).returncode, 0)
        print("PASS cron rows; doit dispatch line")


if __name__ == "__main__":
    unittest.main()
