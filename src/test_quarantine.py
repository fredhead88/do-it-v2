#!/usr/bin/env python3
"""Hermetic quarantine ledger, lifecycle and write-boundary proofs."""
import ast
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = tempfile.TemporaryDirectory(prefix="quarantine-tests-")
os.environ["DOIT_ROOT"] = _ROOT.name
os.environ["DOIT_NO_POKE"] = "1"
import fold
import fold_emits
import quarantine as q
import thinker_inbox

T = datetime(2026, 10, 28, tzinfo=timezone.utc)


def test_source_writes(source):
    """Trace path bindings too, so an indirect test-source write is rejected."""
    tree = ast.parse(source)
    bindings = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings.setdefault(target.id, []).append(node.value)

    def expanded(node, seen=frozenset()):
        text = ast.unparse(node)
        for name in (n.id for n in ast.walk(node) if isinstance(n, ast.Name)):
            if name not in seen:
                text += " " + " ".join(expanded(v, seen | {name}) for v in bindings.get(name, []))
        return text

    violations = []
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in ("write_text", "write_bytes"):
            path = func.value
        elif name == "open":
            is_path_method = isinstance(func, ast.Attribute) and ast.unparse(func.value) not in ("os", "builtins", "io")
            mode_args = call.args if is_path_method else call.args[1:]
            mode = mode_args[0] if mode_args else next((k.value for k in call.keywords if k.arg == "mode"), None)
            if mode is None or not any(c in expanded(mode) for c in ("w", "a", "x", "+", "O_WRONLY", "O_RDWR")):
                continue
            path = func.value if is_path_method else call.args[0] if call.args else next(
                (k.value for k in call.keywords if k.arg == "file"), ast.Constant(""))
        else:
            continue
        text = expanded(path)
        if ("tests" in text and ".py" in text) or "pytest_known_red_quarantine" in text:
            violations.append(ast.unparse(call))
    return violations


class QuarantineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="quarantine-case-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for obj, name, value in ((fold, "ROOT", self.root),
                                 (fold, "EVENTS", self.root / "events")):
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE="1")
        p.start()
        self.addCleanup(p.stop)

    def row(self, typ, node="A", at=T, **kw):
        return dict(type=typ, node=node, project="demo", actor="base-red",
                    ts=at.isoformat(), **kw)

    def quarantine(self, node="A", at=T, **kw):
        return self.row("test-quarantined", node, at, since=at.isoformat(),
                        until=(at + timedelta(days=7)).isoformat(), fix="q:1", **kw)

    def verified(self, at, **kw):
        return dict(type="window-verified", project="demo", ts=at.isoformat(), **kw)

    def append(self, typ, at=T, **fields):
        with patch.dict(os.environ, DOIT_LEDGER_FILE="L-executor-0001.jsonl"), patch.object(fold, "NOW", at):
            fold.append([typ, "w", "project=demo"] +
                        [k + ":=" + json.dumps(v) for k, v in fields.items()])

    def proof(self, ac):
        body = {"marker": "L-spec-1664 "+ac+" ok", "test": self._testMethodName}
        encoded = json.dumps(body, sort_keys=True).encode()
        print(json.dumps(dict(url="cli://quarantine/1664/"+ac, status=0,
                              body_sha256=hashlib.sha256(encoded).hexdigest(), body_excerpt=body)))
        print(body["marker"])

    def test_ac1_active_and_due(self):
        events = []
        for node, windows, hours in (("A", ["1", "2", "3"], [40, 20, 0]),
                                      ("B", ["1", "1", "2"], [40, 20, 0]),
                                      ("C", ["1", "2", "3"], [49, 20, 0]),
                                      ("D", ["1", "2", "3"], [40, 20, 0])):
            for window, h in zip(windows, hours):
                events.append(self.row("window-dropped", at=T-timedelta(hours=h),
                                       window=window, nodes=[node]))
        events += [dict(self.row("window-dropped", window=w, nodes=["other"]), project="other")
                   for w in ["1", "2", "3"]]
        old = self.quarantine("D", T-timedelta(days=2))
        newest = self.quarantine("D", T-timedelta(days=1))
        events += [old, newest, self.quarantine("expired", T-timedelta(days=7)),
                   self.quarantine("lifted", T-timedelta(days=1)),
                   self.row("test-unquarantined", "lifted"),
                   dict(self.quarantine("foreign"), actor="executor")]
        self.assertEqual(q.active(events, "demo", T), {"D": newest})
        self.assertEqual(q.due(events, "demo", T), [dict(node="A", rule="repeat-blame", windows=["1", "2", "3"])])
        self.proof("AC1")

    def test_ac2_record_round_trip(self):
        node = "tests/test_a.py::test_a"
        q.record("demo", node, "repeat-blame", ["w1", "w2", "w3"], now=T)
        code = "import fold,json; print(json.dumps(fold.read_events(use_cache=False)))"
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent)}
        readback = subprocess.check_output([sys.executable, "-c", code], env=env, text=True)
        rows = json.loads(readback)
        print(readback.strip())
        self.assertEqual([r["type"] for r in rows], ["question", "test-quarantined"])
        question, row = rows
        self.assertEqual(row["fix"], question["_src"])
        self.assertEqual(row["actor"], "base-red")
        self.assertEqual(row["rule"], "repeat-blame")
        self.assertEqual(row["windows"], ["w1", "w2", "w3"])
        self.assertEqual(row["since"], T.isoformat())
        self.assertEqual(row["until"], (T+timedelta(days=7)).isoformat())
        self.assertEqual(question["blocks"], [node])
        self.assertIn(node, question["asks"])
        self.assertEqual(question["deadline"], row["until"])
        self.assertEqual(question["subject"], "quarantine:demo")
        self.assertEqual(question["default"], "the quarantine lifts at until and windows keep shipping")
        ledger = self.root / "events/L-base-red-local.jsonl"
        stored = ledger.read_bytes()
        self.assertTrue(all("actor" not in json.loads(line) for line in stored.splitlines()))
        q.record("demo", node, "repeat-blame", [], now=T)
        self.assertEqual(stored, ledger.read_bytes())
        for w in ["w1", "w2", "w3"]:
            self.append("window-dropped", window=w, nodes=["B"])
        self.assertEqual(len(q.apply("demo", now=T)["recorded"]), 1)
        self.assertEqual(q.apply("demo", now=T)["recorded"], [])
        self.assertEqual([r["rule"] for r in fold.read_events() if r.get("node") == "B"], ["repeat-blame"])
        self.proof("AC2")

    def test_ac3_lift_green_or_until(self):
        q.record("demo", "A", "base-red", ["w"], now=T)
        first, second = T+timedelta(hours=1), T+timedelta(hours=2)
        rows = fold.read_events()
        clean = [self.verified(first), self.verified(second)]
        self.assertEqual(q.lifts(rows+clean, "demo", second), [("A", "green-2")])
        dirty = [self.verified(first), self.verified(first+timedelta(minutes=1), quarantined=["A"]), clean[1]]
        self.assertEqual(q.lifts(rows+dirty, "demo", second), [])
        dirty[1] = self.verified(first+timedelta(minutes=1), pre_existing=["A"])
        self.assertEqual(q.lifts(rows+dirty, "demo", second), [])
        self.assertEqual(q.lifts(rows+[clean[0], dict(clean[1], project="other")], "demo", second), [])
        self.append("window-verified", first, quarantined=[], pre_existing=[])
        self.append("window-verified", second, quarantined=[], pre_existing=[])
        self.assertEqual(q.apply("demo", now=second)["lifted"], [("A", "green-2")])
        self.assertEqual(q.apply("demo", now=second)["lifted"], [])
        self.assertEqual([r["reason"] for r in fold.read_events() if r["type"] == "test-unquarantined"], ["green-2"])
        until = T+timedelta(days=7)
        q.record("demo", "B", "base-red", ["w"], now=T)
        rows = fold.read_events()
        self.assertEqual(q.lifts(rows, "demo", until), [("B", "green-2")])
        bare = [self.quarantine()]
        self.assertEqual(q.lifts(bare, "demo", until), [("A", "until")])
        self.assertEqual(q.lifts(bare+clean, "demo", until), [("A", "green-2")])
        later = [self.verified(until+timedelta(hours=h)) for h in [1, 2]]
        self.assertEqual(q.lifts(bare+later, "demo", until+timedelta(hours=3)), [("A", "until")])
        q.record("demo", "C", "base-red", ["w"], now=second)
        c_until = second+timedelta(days=7)
        self.assertEqual(q.lifts(fold.read_events(), "demo", c_until)[-1], ("C", "until"))
        q.apply("demo", now=c_until)
        events = fold.read_events()
        self.assertEqual([e["reason"] for e in events if e["type"] == "test-unquarantined" and e.get("node") == "C"], ["until"])
        fix = next(r["fix"] for r in events if r.get("node") == "C" and r["type"] == "test-quarantined")
        self.assertNotIn(fix, fold.answered(events))
        with patch.object(thinker_inbox, "now", return_value=c_until):
            self.assertIn(fix, [r["src"] for r in thinker_inbox.open_items()])
        for w in ("w1", "w2", "w3"):
            self.append("window-dropped", window=w, nodes=["R"], project="repeat")
        q.apply("repeat", now=T)
        self.append("window-verified", first, project="repeat", quarantined=[], pre_existing=[])
        self.append("window-verified", second, project="repeat", quarantined=[], pre_existing=[])
        lifted = q.apply("repeat", now=second)
        self.assertEqual(lifted["lifted"], [("R", "green-2")])
        self.assertEqual(lifted["recorded"], [])
        self.assertEqual(q.active(fold.read_events(), "repeat", second), {})
        self.proof("AC3")

    def test_ac9_vocabulary(self):
        for kind, fields in (("test-quarantined", ("project", "node", "rule", "windows", "since", "until", "fix")),
                             ("test-unquarantined", ("project", "node", "reason"))):
            self.assertEqual(fold_emits.EMITS[kind], {"base-red"})
            self.assertEqual(fold_emits.REQUIRED_ROWS[kind], fields)
        self.append("test-quarantined", node="foreign", rule="base-red", windows=["w"],
                    since=T.isoformat(), until=(T+timedelta(days=7)).isoformat(), fix="q:1")
        self.assertEqual(q.active(fold.read_events(), "demo", T), {})
        self.proof("AC9")

    def test_ac10_nothing_weakened(self):
        for filename in ["quarantine.py", "window_gate.py", "base_red.py"]:
            source = Path(__file__).with_name(filename).read_text()
            self.assertEqual(test_source_writes(source), [], filename)
            for line in source.splitlines():
                if not line.lstrip().startswith("#"):
                    self.assertNotIn("pytest_known_red_quarantine", line)
        for mutant in ('open("tests/test_a.py", "w")',
                       'p = Path("tests") / "test_a.py"; p.write_text("bad")',
                       'p = Path("tests/test_a.py"); p.open(mode="a")',
                       'open("pytest_known_red_quarantine.txt", "w")'):
            self.assertTrue(test_source_writes(mutant), mutant)
        self.assertEqual(test_source_writes('open("tests/test_a.py", "r")'), [])
        repo = self.root / "repos/albert-scott"
        (repo / "tests").mkdir(parents=True)
        (repo / "tests/test_a.py").write_text("assert True\n")
        (repo / "marker").write_text("unchanged\n")
        def snapshot():
            return {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in repo.rglob("*") if p.is_file()}
        before = snapshot()
        q.record("albert-scott", "tests/test_a.py::test_a", "base-red", ["w"], now=T)
        q.apply("albert-scott", now=T+timedelta(days=7))
        self.assertEqual(before, snapshot())
        rows = [r for r in fold.read_events() if r["type"] == "test-quarantined"]
        self.assertTrue(rows)
        self.assertTrue(all(r.get("until") and r.get("fix") for r in rows))
        self.proof("AC10")


if __name__ == "__main__":
    unittest.main(verbosity=2)
