#!/usr/bin/env python3
"""Hermetic bill-policy regression cases; each case emits a hashed receipt."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
import gh_bill


def receipt(case, body):
    raw = json.dumps(body, sort_keys=True)
    print(json.dumps({"url": "python://gh_bill/" + case, "status": 200,
                      "body_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                      "body_excerpt": raw}))


def usage(today, yesterday=10, prior=2):
    return [{"date": (today - timedelta(days=i)).isoformat(),
             "netAmount": yesterday if i == 1 else prior} for i in range(1, 9)]


class Decisions(unittest.TestCase):
    def decide(self, items, today=date(2026, 10, 18), limit=450, sent=(), ci="false", health=True, shadow=True):
        return gh_bill.decide(items, today, {"limit": limit, "sent": list(sent)},
                              {"CI_SELF_HOSTED": ci}, health, shadow)

    def test_arithmetic(self):
        today = date(2026, 10, 18)
        for yday, prior, ci, expected, threshold in (
            (10, 2, "false", "none", 12), (13, 2, "false", "alert", 12),
            (13, 10, "false", "none", 15), (4.5, 2, "true", "alert", 4),
            (3.5, 2, "true", "none", 4)):
            result = self.decide(usage(today, yday, prior), ci=ci)
            self.assertEqual((result["median7"], result["threshold"], result["action"]),
                             (prior, threshold, expected))
            receipt("AC1-daily", result)
        result = self.decide(usage(date(2026, 11, 4), 10, 2), today=date(2026, 11, 4))
        self.assertEqual(result["mtd"], 14)
        self.assertAlmostEqual(result["pct_of_limit"], 14 / 450 * 100)
        self.assertAlmostEqual(result["projection"], 14 + (10 + 6 * 2) / 7 * 27)
        receipt("AC1-boundary", result)

    def test_levels(self):
        for amount, sent, ci, expected in ((224.99, [], "false", "none"),
            (225, [], "false", "alert-50"), (337.5, [50], "false", "alert-75"),
            (405, [], "true", "alert-90"), (340, [], "false", "alert-75"),
            (405, [50, 75, 90], "true", "none")):
            result = self.decide([{"date": "2026-10-01", "netAmount": amount}], sent=sent, ci=ci)
            self.assertEqual(result["action"], expected)
            receipt("AC2-level", result)

    def test_flip_matrix(self):
        flips = 0
        for ci in ("true", "false", None):
            for health in (True, False, None):
                for shadow in (True, False, None):
                    result = self.decide([{"date": "2026-10-01", "netAmount": 427.5}],
                                         ci=ci, health=health, shadow=shadow)
                    expected = ("undetermined" if ci is None else "alert-90" if ci == "true"
                                else "flip" if health is True and shadow is True else "flip-refused")
                    self.assertEqual(result["action"], expected)
                    flips += expected == "flip"
                    receipt("AC3-matrix", {"ci": ci, "healthy": health, "shadow": shadow, **result})
        self.assertEqual(flips, 1)
        for items, limit in ((None, 450), ([{"date": "2026-10-01"}], 450), ([], None),
                             ([], 0), ([{"date": "bad", "netAmount": 1}], 450),
                             ([{"date": "2026-10-01", "netAmount": "NaN"}], 450)):
            self.assertEqual(self.decide(items, limit=limit)["action"], "undetermined")

    def test_limits(self):
        cfg = gh_bill.load_limits(REPO / "gh-bill.toml")
        self.assertEqual((cfg["default"], cfg["after_r1"]), (450, 120))
        for ci, age, prior, expected in (("false", 10, None, 450), ("true", 6, None, 450),
                                       ("true", 7, None, 120), ("true", 10, 450, 450)):
            self.assertEqual(gh_bill.limit_for(cfg, "2026-11", ci, age, prior), expected)
            receipt("AC4-limit", {"ci": ci, "age": age, "prior": prior, "limit": expected})
        self.assertEqual(gh_bill.limit_for({**cfg, "2026-10": 450}, "2026-10", "true", 10, None), 450)

    def test_plain_english_for_every_action(self):
        expected = {
            'none': 'No action needed.', 'alert': 'Yesterday cost more than usual.',
            'alert-50': 'Half the monthly limit is spent.',
            'alert-75': 'Most of the monthly limit is spent.',
            'alert-90': 'The monthly limit is nearly used up.',
            'flip': 'Builds have switched to our own verified machine',
            'flip-refused': 'NEEDS YOU:', 'undetermined': 'could not be checked reliably',
        }
        decision = dict(date='2026-10-21', mtd=356.578, yday=32.1,
                        pct_of_limit=79.239, limit=450, projection=618.9,
                        reason='raw error {"internal": "details"}')
        for action, sentence in expected.items():
            with self.subTest(action=action):
                message = gh_bill.render_telegram(dict(decision, action=action))
                self.assertIn(sentence, message)
                self.assertIn('$357 spent so far in October (79% of the $450 limit)', message)
                self.assertIn('Yesterday $32; at the recent pace October ends near $619.', message)
                for raw in ('{', '"', 'internal', '356.578'):
                    self.assertNotIn(raw, message)
                if action != 'none':
                    self.assertIn('Recommend', message)
        missing = self.decide(None)
        message = gh_bill.render_telegram(dict(missing, date='2026-11-01'))
        self.assertIn('spending figures are unavailable', message)
        self.assertIn('Recommend restoring', message)
        self.assertNotIn('{', message)
        # On the first of a month, the bill still describes the previous month.
        boundary = gh_bill.render_telegram(dict(decision, action='none', date='2026-11-01'))
        self.assertIn('so far in October', boundary)


STUB = '''import json, os, sys
from pathlib import Path
def record(kind, value):
    with open(os.environ['RUNNER_LOG'], 'a') as f:
        f.write(json.dumps({'kind': kind, 'value': value}) + '\\n')
record('import', {'path': __file__, 'real_loaded': any(getattr(m, '__file__', '') == os.environ['REAL_RUNNER'] for m in sys.modules.values())})
def state(): return {'CI_SELF_HOSTED': os.environ.get('TEST_CI', 'false')}
def healthy(): return {'healthy': os.environ.get('TEST_HEALTH', 'true') == 'true'}
def shadow_passed(events, kind): return {'ts': '2026-01-01T00:00:00+00:00'} if os.environ.get('TEST_SHADOW', 'true') == 'true' else None
def set(variable, value, **kw):
    record('set', [variable, value, kw])
    return {'ok': os.environ.get('TEST_REFUSE') != 'true', 'reason': 'stub refusal' if os.environ.get('TEST_REFUSE') else 'verified'}
'''


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gh-bill-test-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / "root"
        self.root.mkdir()
        self.state = self.root / "state" / "gh-bill.json"
        self.state.parent.mkdir()
        self.month = (date.today() - timedelta(days=1)).strftime("%Y-%m")
        self.state.write_text(json.dumps({"months": {self.month: {"limit": 450, "sent": []}}}))
        stub = self.home / "runner.py"
        stub.write_text(STUB)
        sender = self.home / "sender"
        sender.write_text('#!/usr/bin/env python3\nimport os,sys,json\nwith open(os.environ["TG_LOG"], "a") as f: f.write(json.dumps(sys.argv[1:]) + "\\n")\n')
        sender.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), DOIT_ROOT=str(self.root),
                        GH_BILL_CI_RUNNER=str(stub), GH_BILL_TG=str(sender),
                        TG_LOG=str(self.home / "messages"), RUNNER_LOG=str(self.home / "runner-log"),
                        REAL_RUNNER=str(HERE / "ci_runner.py"), DOIT_PROJECT="do-it-v2",
                        GH_LIMIT_STATE=str(self.home / "quota"), DOIT_REAL_GH=str(self.home / "gh"))
        self.env.pop("DOIT_LEDGER_FILE", None)
        self.env.pop("DOIT_TEST_LIVE_ROOT", None)

    def lines(self, name):
        path = self.home / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_bill(self, items=None, *, dry=False, live=False, config=None):
        args = [str(REPO / "doit"), "gh-bill"]
        if not live:
            path = self.home / "usage.json"
            path.write_text(json.dumps({"usageItems": items}))
            args += ["--usage-file", str(path)]
        if dry:
            args.append("--dry-run")
        if config is not None:
            code = "import sys;sys.path.insert(0,sys.argv[1]);import gh_bill;gh_bill.CONFIG=gh_bill.Path(sys.argv[2]);sys.exit(gh_bill.main(sys.argv[3:]))"
            args = [sys.executable, "-c", code, str(HERE), str(config), *args[2:]]
        result = subprocess.run(args, cwd=self.home, env=self.env, capture_output=True, text=True)
        self.assertTrue(result.stdout, result.stderr)
        decision = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1 if decision["action"] == "undetermined" else 0, result.stderr)
        imports = [r for r in self.lines("runner-log") if r["kind"] == "import"]
        self.assertTrue(imports)
        self.assertEqual(imports[-1]["value"], {"path": self.env["GH_BILL_CI_RUNNER"], "real_loaded": False})
        return decision

    def fresh_fold(self):
        code = "import sys,json;sys.path.insert(0,sys.argv[1]);import fold;f=fold.fold(fold.read_events());print(json.dumps({'kept':f[3].get('gh-bill',[]),'ignored':f[2]}))"
        result = subprocess.run([sys.executable, "-I", "-c", code, str(HERE)],
                                env=self.env, cwd=self.home, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_fixtures(self):
        fixtures = json.loads((HERE / "fixtures/gh_bill/cases.json").read_text())
        for name, fixture in fixtures.items():
            with self.subTest(name=name):
                self.env.update(TEST_CI=fixture["ci"], TEST_HEALTH=fixture.get("health", "true"),
                                TEST_SHADOW="true")
                # Keep yesterday quiet; choose the measured month's first day.
                day = (date.today() - timedelta(days=1)).replace(day=1)
                items = ([{"date": day.isoformat(), "netAmount": fixture["mtd"]}]
                         if "mtd" in fixture else usage(date.today(), fixture["yday"], 2))
                if name == "undetermined":
                    items = [{"date": day.isoformat()}]
                self.state.write_text(json.dumps({"months": {self.month: {"limit": 450, "sent": []}}}))
                before_messages = len(self.lines("messages"))
                before_sets = len([r for r in self.lines("runner-log") if r["kind"] == "set"])
                before_rows = len(self.fresh_fold()["kept"])
                decision = self.run_bill(items)
                self.assertEqual(decision["action"], name)
                self.assertEqual(len(self.lines("messages")) - before_messages, name != "none")
                if name != 'none':
                    message = self.lines('messages')[-1][0]
                    self.assertEqual(message, gh_bill.render_telegram(decision))
                    self.assertNotIn('{', message)
                    self.assertNotIn('"', message)
                sets = [r for r in self.lines("runner-log") if r["kind"] == "set"]
                self.assertEqual(len(sets) - before_sets, name == "flip")
                if name == "flip":
                    self.assertEqual(sets[-1]["value"][:2], ["CI_SELF_HOSTED", "true"])
                    self.assertEqual(sets[-1]["value"][2]["trigger"], "bill-flip")
                kept = self.fresh_fold()["kept"]
                self.assertEqual(len(kept) - before_rows, 1, "L-spec-1120 bill-check vocabulary required")
                row = kept[-1]
                self.assertEqual((row["subject"], row["project"]), ("gh-bill", "do-it-v2"))
                for key in ("date", "yday", "mtd"):
                    self.assertEqual(row[key], decision[key])
                if name == "alert-75":
                    saved = json.loads(self.state.read_text())["months"][self.month]
                    self.assertEqual(saved["sent"], [50, 75])
                    again = self.run_bill(items)
                    self.assertNotIn(again["action"], ("alert-50", "alert-75"))
                receipt("AC5-" + name, decision)
        self.assertNotIn("variable set", (HERE / "gh_bill.py").read_text())
        wrong_env = dict(self.env, DOIT_LEDGER_FILE="L-thinker-local.jsonl")
        wrong_path = self.root / "events/L-thinker-local.jsonl"
        argv = ["bill-check", "gh-bill", "date=2026-10-18", "yday:=1", "mtd:=2"]
        bad = subprocess.run([str(REPO / "doit"), "append", *argv], cwd=self.home,
                             env=wrong_env,
                             capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("actor mismatch", bad.stderr)
        self.assertFalse(wrong_path.exists())
        recorded = subprocess.run([sys.executable, "-I", "-c",
            "import sys;sys.path.insert(0,sys.argv[1]);import fold;fold.append(sys.argv[2:], strict=False)",
            str(HERE), *argv], cwd=self.home, env=wrong_env, capture_output=True, text=True)
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertIn("recorded-and-ignored", recorded.stderr)
        self.assertEqual(len(wrong_path.read_text().splitlines()), 1)
        folded = self.fresh_fold()
        self.assertEqual(len(folded["ignored"]), 1)
        receipt("AC7-kept-and-ignored", folded)

    def test_bad_reads(self):
        for code, output, quota in ((1, "", False), (0, "", True), (0, "", False), (0, "{}", False)):
            self.env["PATH"] = str(self.home) + os.pathsep + os.environ["PATH"]
            gh = self.home / "gh"
            gh.write_text(f"#!/bin/sh\nprintf '%s' '{output}'\nexit {code}\n")
            gh.chmod(0o755)
            (self.home / "quota").write_text(json.dumps({
                **{key: {"limit": 5000, "remaining": 1 if quota else 5000, "reset": time.time() + 3600}
                   for key in ("core", "graphql")}, "checked_at": time.time(), "spent_since_check": 0}))
            before = len(self.lines("messages"))
            result = self.run_bill(live=True)
            self.assertEqual(result["action"], "undetermined")
            self.assertIsNone(result["yday"])
            self.assertIsNone(result["mtd"])
            self.assertEqual(len(self.lines("messages")) - before, 1)
            self.assertFalse(any(r["kind"] == "set" for r in self.lines("runner-log")))
            receipt("AC5-read-failure", result)
        self.assertEqual(len(self.fresh_fold()["kept"]), 4)

    def test_dry_run(self):
        before = self.state.read_bytes()
        items = [{"date": (date.today() - timedelta(days=1)).replace(day=1).isoformat(), "netAmount": 427.5}]
        result = self.run_bill(items, dry=True)
        self.assertEqual(result["action"], "flip")
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse(self.lines("messages"))
        self.assertFalse(any(r["kind"] == "set" for r in self.lines("runner-log")))
        self.assertFalse((self.root / "events/L-gh-bill-local.jsonl").exists())
        receipt("AC6-dry-run", result)

    def test_bad_configs_and_first_limit(self):
        path = self.home / "limits.toml"
        for text in (None, "", "[bad", "default = -1\nafter_r1 = 120"):
            if text is not None:
                path.write_text(text)
            self.assertIsNone(gh_bill.load_limits(path))
            result = self.run_bill([], config=path)
            self.assertEqual(result["action"], "undetermined")
            receipt("AC4-bad-config", result)
        self.assertEqual(len(self.lines("messages")), 4)
        self.assertFalse(any(r["kind"] == "set" for r in self.lines("runner-log")))
        self.state.unlink()
        result = self.run_bill([])
        self.assertEqual(result["action"], "none")
        self.assertEqual(len(self.lines("messages")), 5)
        self.assertIn("450", self.lines("messages")[-1][0])
        self.assertEqual(json.loads(self.state.read_text())["months"][self.month]["limit"], 450)

    def test_set_refusal(self):
        self.env["TEST_REFUSE"] = "true"
        result = self.run_bill([{"date": (date.today() - timedelta(days=1)).replace(day=1).isoformat(), "netAmount": 427.5}])
        self.assertEqual(result["action"], "flip-refused")
        self.assertIn("stub refusal", result["reason"])
        self.assertIn("NEEDS YOU", self.lines("messages")[-1][0])
        self.assertEqual(self.fresh_fold()["kept"][-1]["action"], "flip-refused")
        receipt("AC5-set-refusal", result)


if __name__ == "__main__":
    unittest.main()
