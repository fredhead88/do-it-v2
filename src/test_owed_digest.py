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

NOW = datetime(2026, 10, 15, 6, tzinfo=timezone.utc)
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

    def test_backlog_lines(self):
        with patch.object(dispatch, "CONTENT", self.root / "content"):
            dispatch.CONTENT.mkdir()
            dispatch.spec_path("L-spec-9113").write_text("AC1 [backend]: fixture\nAC2 [backend]: fixture\n")
            evs = [event("owed-ac", "L-spec-9113", 40, criterion=c,
                         wake_at=ts(39) if c == "AC1" else None) for c in ("AC1", "AC2")]
            evs += [event("shipped", "L-spec-9113", 40, actor="executor")]
            evs += [event("owed-routed", "L-spec-9113", 30, criterion=c,
                          route="relabel", capability="operator-action") for c in ("AC1", "AC2")]
            obj = digest.build(evs, NOW)
            self.assertEqual(set(obj["backlog"]), {"no_runner", "unverifiable"})
            self.assertEqual(obj["backlog"]["no_runner"], [
                {"spec": "L-spec-9113", "criterion": "AC1", "due_at": ts(39)},
                {"spec": "L-spec-9113", "criterion": "AC2", "due_at": None}])
            md = digest.render_md(obj)
            for line in [f"OWED NO-RUNNER L-spec-9113 AC1 due {ts(39)}",
                         "OWED NO-RUNNER L-spec-9113 AC2 due none",
                         "OWED UNVERIFIABLE 0 closed today, 0.0% of month (cap 10%)",
                         "OWED UNVERIFIABLE CAPPED 1 rows past 14 days held, closing stopped at cap"]:
                self.assertIn(line, md.splitlines())
                print(line)
            closed = [event("owed-met", "L-spec-9114", criterion=f"AC{i}", evidence="fixture") for i in range(9)]
            closed += [event("owed-unverifiable", "L-spec-9115", criterion="AC1", reason="fixture")]
            obj = digest.build(closed, NOW)
            self.assertEqual(obj["backlog"]["unverifiable"],
                             {"today": 1, "month": 1, "closed": 10, "pct": 10.0, "capped": 0})
            md = digest.render_md(obj)
            self.assertEqual(md.count("OWED UNVERIFIABLE "), 1)
            self.assertIn("OWED UNVERIFIABLE 1 closed today, 10.0% of month (cap 10%)", md)
            self.assertNotIn("CAPPED", md)
            print("OWED UNVERIFIABLE 1 closed today, 10.0% of month (cap 10%)")
            # Nine ordinary closes permit this candidate; it is not yet held by the cap.
            self.assertEqual(digest.build(evs + closed[:9], NOW)["backlog"]["unverifiable"]["capped"], 0)
            empty = digest.render_md(digest.build([], NOW))
            self.assertEqual(empty.count("OWED UNVERIFIABLE "), 1)
            self.assertNotIn("CAPPED", empty)

    def test_recount(self):
        evs, expected = fixture()
        obj = digest.build(evs, NOW)
        recount = Counter(r["status"] for s in fold.fold(evs)[0].values() for r in owed.checks(s["evs"], NOW))
        self.assertEqual(obj["counts"]["by_status"], {**recount, "unverifiable": 0})
        self.assertEqual(set(obj), {"at", "counts", "needs_you", "health", "m1", "backlog"})
        self.assertEqual(set(obj["counts"]), {"by_status", "blocked_by_capability", "sweep"})
        self.assertEqual(set(obj["counts"]["by_status"]), set(owed.STATUSES))
        self.assertEqual({r["spec"] for r in obj["needs_you"]}, {s for s, v in expected.items() if v == "blocked"})
        self.assertEqual(obj["counts"]["blocked_by_capability"], {"operator-action": 10})
        # Independent, hand-labelled recount: no owed/checks call in this oracle.
        self.assertEqual(obj["counts"]["by_status"],
                         {**Counter(expected.values()), "unverifiable": 0})
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
        blocked[-1] = dict(blocked[-1], ts=ts(1))
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
        self.assertIn("NEEDS YOU:", self.sent[0][1])
        self.assertLessEqual(len(self.sent[0][1]), 1000)
        self.assertTrue((self.root / "content/L-owed-digest-2026-10-15.md").exists())
        self.assertEqual(len([e for e in fold.read_events() if e["type"] == "owed-digest"]), 1)
        # Reopening on the same day cannot bypass the daily limit.
        reopened = dict(blocked[-1], ts=(NOW + timedelta(minutes=15)).isoformat())
        self.install(blocked[:-1] + [reopened])
        self.assertEqual(self.run_digest(alert=True, now=NOW + timedelta(minutes=30)), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.run_digest(alert=True, now=NOW + timedelta(days=1)), 0)
        self.assertEqual(len(self.sent), 2)
        self.assertIn(ASK, self.sent[1][1])
        print("PASS daily merge; one summary per UTC day; reopened row waits until next summary")

    def test_operator_age_owner_and_later_decision_filters(self):
        all_ev = []
        for i, (age, owner) in enumerate(((1, "operator"), (2, "operator"), (2.01, "operator"),
                                         (1, "thinker"), (1, "operator"), (1, "operator"))):
            spec = f"L-spec-filter{i}"
            all_ev += [event("owed-ac", spec, 10, criterion="AC1", wake_at=ts(9)),
                       event("shipped", spec, 9, actor="executor"),
                       event("owed-unobservable", spec, age, actor="owed-sweeper", criterion="AC1",
                             capability="operator-action", owner=owner, why=ASK)]
        all_ev.append(event("decision", "L-spec-filter4", 0.5))
        # A decision by ref can live on another subject.
        all_ev.append(event("decision", "elsewhere", 0.5, ref="L-owed-sweeper-fixture.jsonl:6"))
        self.install(all_ev)
        self.assertEqual(self.run_digest(alert=True, now=NOW - timedelta(minutes=1)), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.run_digest(alert=True), 0)
        self.assertIn("L-spec-filter0/AC1", self.sent[0][1])
        self.assertIn("L-spec-filter1/AC1", self.sent[0][1])
        for i in (2, 3, 4, 5):
            self.assertNotIn(f"L-spec-filter{i}/AC1", self.sent[0][1])
        record = json.loads((self.root / "state/owed-alerted.json").read_text())
        self.assertEqual(len(record["rows"]), 6)
        self.assertEqual(self.run_digest(alert=True, now=NOW + timedelta(minutes=15)), 0)
        self.assertEqual(len(self.sent), 1)
        print("PASS 48h boundary; owner; later decisions by subject/ref; silent backlog; 06Z limit")

    def test_send_failure(self):
        evs, _ = fixture()
        blocked = [e for e in evs if e["subject"] == "L-spec-9002"]
        blocked[-1] = dict(blocked[-1], ts=ts(1))
        self.install(blocked)
        for code, stdout in ((1, "200\n"), (0, "429\n"), (0, ""), (0, "200\n500\n")):
            self.sender = lambda argv: subprocess.CompletedProcess(argv, code, stdout, "")
            self.assertNotEqual(self.run_digest(alert=True), 0)
            alerted = json.loads((self.root / "state/owed-alerted.json").read_text())
            self.assertNotIn("sent_date", alerted)
            self.assertEqual(alerted["rows"], [])
        self.sender = lambda argv: self.send(argv)
        self.assertEqual(self.run_digest(alert=True), 0)
        self.assertEqual(len(self.sent), 1)
        print("PASS daily send retry; 429/empty/multiple HTTP codes fail without marking sent")

    def test_streak_alert(self):
        self.install(sweep(["failed"] * 3))
        self.assertEqual(self.run_digest(alert=True, now=NOW + timedelta(hours=1)), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.run_digest(alert=True), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Daily owed summary", self.sent[0][1])
        self.assertIn("SYSTEM HEALTH:", self.sent[0][1])
        for n in range(4, 7):
            self.install(sweep(["failed"] * n))
            self.assertEqual(self.run_digest(alert=True), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.run_digest(alert=True, now=NOW + timedelta(days=1)), 0)
        self.assertEqual(len(self.sent), 2)
        print("PASS sweep failures only appear in one daily summary")

    def test_health_and_empty(self):
        evs, expected = fixture()
        self.install([e for e in evs if expected[e["subject"]] != "blocked"])
        self.assertEqual(self.run_digest(alert=True), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("NEEDS YOU:", self.sent[0][1])
        self.assertEqual(digest.build([], NOW)["counts"]["sweep"]["runs_24h"], 0)
        with patch.object(fold, "EVENTS", self.root / "empty-owed-ledger"):
            fold.EVENTS.mkdir()
            (fold.EVENTS / "L-operator-fixture.jsonl").write_text(
                json.dumps(event("decision", text="no owed rows")) + "\n")
            self.assertEqual(self.run_digest(alert=True), 0)
            self.assertEqual(self.state()["needs_you"], [])
            self.assertEqual(len(self.sent), 1)
        print("PASS health and empty ledger never generate extra messages")

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
        long_ask = "Please review this operator request. " * 8
        for e in evs:
            if e["type"] == "owed-unobservable":
                e.update(why=long_ask, ts=ts(0.1))
        self.install(evs)
        for _ in range(10):
            self.assertEqual(self.run_digest(alert=True), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertLessEqual(len(self.sent[0][1]), 1000)
        self.assertIn("more operator rows in the daily digest", self.sent[0][1])
        record = json.loads((self.root / "state/owed-alerted.json").read_text())
        self.assertGreater(len(record["rows"]), 0)
        self.assertLess(len(record["rows"]), 10)
        for key in record["rows"]:
            spec, criterion, _since = key.split("/", 2)
            self.assertIn(f"{spec}/{criterion}:", self.sent[0][1])
        self.assertEqual(self.run_digest(alert=True, now=NOW + timedelta(days=3)), 0)
        record = json.loads((self.root / "state/owed-alerted.json").read_text())
        self.assertEqual(len(record["rows"]), 10)
        self.assertNotIn("NEEDS YOU:", self.sent[-1][1])
        print("PASS bounded daily merge; deferred count; expired backlog silently retired")

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
        self.assertNotIn("owed-digest-alert", jobs)
        row = jobs["owed-digest"]
        self.assertEqual(row["schedule"], "0 6 * * *")
        self.assertEqual((row["where"], row["owner"], row["project"]), ("user", "thinker", "do-it-v2"))
        self.assertIn("cron-run owed-digest -- ", row["command"])
        self.assertIn("owed-digest --alert >>", row["command"])
        for other in rows:
            self.assertEqual(bool(re.search(row["sig"], crons.print_line(other["name"]))), other["name"] == "owed-digest")
        doit = pathlib.Path(__file__).resolve().parent.parent / "doit"
        self.assertIn('owed-digest) shift; exec python3 "$SRC/owed_digest.py" "$@" ;;', doit.read_text())
        self.assertIn("doit owed-digest", subprocess.check_output([str(doit), "help"], text=True))
        self.assertEqual(subprocess.run([str(doit), "owed-digest", "--dry-run"], capture_output=True).returncode, 0)
        print("PASS single 06Z cron row; doit dispatch line")


if __name__ == "__main__":
    unittest.main()
