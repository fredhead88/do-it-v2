#!/usr/bin/env python3
"""Hermetic quarantine ledger, lifecycle and write-boundary proofs."""
import ast
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
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

    def expanded(node):
        # Only substring presence matters below. Visit each binding once rather
        # than duplicating its text along every path through the alias graph.
        pending, seen, texts = [node], set(), []
        while pending:
            value = pending.pop()
            texts.append(ast.unparse(value))
            for name in (n.id for n in ast.walk(value) if isinstance(n, ast.Name)):
                if name not in seen:
                    seen.add(name)
                    pending.extend(bindings.get(name, []))
        return " ".join(texts)

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

    def tree_observation(self, ident="w1", sha="sha1", tree="tree1", specs=("S1",),
                         nodes=("tests/test_a.py::test_a",), at=T, **changes):
        common = dict(project="demo", subject=ident, ts=at.isoformat(), sha=sha)
        candidate = dict(common, type="window-candidate", specs=list(specs), verified_on="base")
        observed = dict(common, type="window-tree-verified", window=ident, tree=tree,
                        green=False, new_ids=list(nodes), _src="verified:"+sha)
        observed.update(changes)
        return [candidate, observed]

    def nightly_observation(self, nodes=("tests/test_seed.py::test_seed",), at=T, **changes):
        row = dict(type="base-red-nightly", actor="base-red", project="demo",
                   subject="nightly:demo", ts=at.isoformat(), sha="master-sha",
                   complete=True, suite="nightly", ids=list(nodes), _src="nightly:1")
        row.update(changes)
        return row

    def proposal(self, func, events, now=T, project="demo"):
        output = io.StringIO()
        with redirect_stderr(output):
            result = func(events, project, now)
        reports = [json.loads(line) for line in output.getvalue().splitlines()]
        return result, reports

    def persist_observations(self, events, project="demo"):
        for event in events:
            actor = "base-red" if event["type"] == "base-red-nightly" else "executor"
            fields = {k: v for k, v in event.items()
                      if k not in ("type", "actor", "ts", "subject", "project", "_src")}
            with patch.dict(os.environ, DOIT_LEDGER_FILE="L-"+actor+"-local.jsonl"), \
                    patch.object(fold, "NOW", q._time(event["ts"])):
                fold.append([event["type"], event["subject"], "project="+project] +
                            [k+":="+json.dumps(v) for k, v in fields.items()])

    def fresh_events(self):
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.resolve())}
        return json.loads(subprocess.check_output(
            [sys.executable, "-c", "import fold,json; print(json.dumps(fold.read_events(use_cache=False)))"],
            env=env, text=True))

    def proof_2107(self, ac, **facts):
        body = dict(test=self._testMethodName, assertions="passed", **facts)
        encoded = json.dumps(body, sort_keys=True).encode()
        print(json.dumps(dict(url="cli://quarantine/2107/"+ac, status=0,
                              body_sha256=hashlib.sha256(encoded).hexdigest(), body_excerpt=body)))

    def test_2107_ac1_independent_trees(self):
        node = "tests/test_a.py::test_a"
        first = self.tree_observation(at=T-timedelta(days=7))
        second = self.tree_observation("w2", "sha2", "tree2", ("S2",))
        events = first+second
        expected = [dict(node=node, rule="two-specs", windows=[
            'tree:["w1","sha1","tree1"]', 'tree:["w2","sha2","tree2"]'])]
        self.assertEqual(q.spec_due(events, "demo", T), expected)
        self.assertEqual(q.spec_due(list(reversed(events)), "demo", T), expected)
        same_subject = self.tree_observation("w1", "sha2", "tree2", ("S2",))
        self.assertEqual(len(q.spec_due(first+same_subject, "demo", T)[0]["windows"]), 2)
        negatives = [first, first+first,
                     first+self.tree_observation("w2", "sha2", "tree1", ("S2",)),
                     first+self.tree_observation("w2", "sha2", "tree2", ("S1",)),
                     self.tree_observation(specs=("S1", "S2"))+second,
                     first+self.tree_observation("w2", "sha2", "tree2", ("S2",), green=True),
                     first+[dict(e, project="other") for e in second],
                     first+self.tree_observation("w2", "sha2", "tree2", ("S2",), at=T+timedelta(seconds=1)),
                     self.tree_observation(at=T-timedelta(days=7, seconds=1))+second,
                     events+[self.quarantine(node, T-timedelta(days=1))]]
        for bad in negatives:
            self.assertEqual(q.spec_due(bad, "demo", T), [])
        prior = self.quarantine(node, T-timedelta(days=8))
        until = q._time(prior["until"])
        lifted = self.row("test-unquarantined", node, until)
        for at in (until-timedelta(seconds=1), until):
            pair = self.tree_observation(at=at)+second
            self.assertEqual(q.spec_due(pair+[prior, lifted], "demo", T), [])
        fresh = self.tree_observation(at=until+timedelta(seconds=1))+second
        self.assertEqual(len(q.spec_due(fresh+[prior, lifted], "demo", T)), 1)
        future_prior = dict(prior, ts=(T+timedelta(seconds=1)).isoformat())
        self.assertEqual(q.spec_due(events+[future_prior], "demo", T), expected)
        self.persist_observations(events)
        q.apply("demo", now=T)
        for at in (T+timedelta(days=7), T+timedelta(days=7, minutes=1)):
            self.assertEqual(q.apply("demo", now=at)["recorded"], [])
        stored = self.fresh_events()
        self.assertEqual(sum(e["type"] == "test-quarantined" for e in stored), 1)
        self.assertEqual(sum(e["type"] == "question" for e in stored), 1)
        self.proof_2107("AC1", persisted_rows=1, persisted_questions=1)

    def test_2107_ac2_observation_diagnostics(self):
        first = self.tree_observation()
        second = self.tree_observation("w2", "sha2", "tree2", ("S2",))
        cases = []
        for key, value, reason in (("green", None, "invalid-green"), ("green", "false", "invalid-green"),
                                    ("new_ids", None, "invalid-ids"), ("new_ids", "node", "invalid-ids"),
                                    ("new_ids", [3], "invalid-ids"), ("tree", "", "invalid-identity"),
                                    ("sha", "", "invalid-identity")):
            cases.append(([first[0], dict(first[1], **{key: value})], reason))
        for key, reason in (("green", "invalid-green"), ("new_ids", "invalid-ids"),
                            ("tree", "invalid-identity"), ("sha", "invalid-identity")):
            row = dict(first[1]); row.pop(key)
            cases.append(([first[0], row], reason))
        cases += [([first[1]], "missing-membership"),
                  ([dict(first[0], specs=[]), first[1]], "missing-membership"),
                  ([dict(first[0], specs=[3]), first[1]], "invalid-membership"),
                  ([first[0], dict(first[0], specs=["conflict"]), first[1]], "contradictory-membership")]
        rehearsal = dict(first[0], type="window-rehearsed", tree="tree1", base_sha="base", state="red")
        for change in (dict(sha=None), dict(sha="base")):
            cases.append(([dict(rehearsal, **change), first[1]], "missing-membership"))
        cases.append(([dict(rehearsal, tree="wrong"), first[1]], "contradictory-tree"))
        for bad, reason in cases:
            with self.subTest(reason=reason, bad=bad):
                result, reports = self.proposal(q.spec_due, bad+second)
                self.assertEqual(result, [])
                self.assertEqual(reports[0]["excluded_count"], 1)
                self.assertEqual(reports[0]["excluded"][0]["reason"], reason)
                self.assertEqual(reports[0]["excluded"][0]["source"], "verified:sha1")
        # Exact rehearsal sha, candidate priority, and event-order-independent joins.
        other = self.tree_observation("w1", "sha2", "tree2", ("S2",))
        r2 = dict(other[0], type="window-rehearsed", tree="tree2", base_sha="base", state="red")
        fallback = [rehearsal, first[1], r2, other[1]]
        for rows in (fallback, list(reversed(fallback)),
                     [dict(first[0], specs=[]), *fallback],
                     [first[0], dict(rehearsal, specs=["S2"]), first[1], *second]):
            self.assertEqual(len(q.spec_due(rows, "demo", T)), 1)
        mixed = first+second+[dict(first[1], _src="broken", green=None)]
        result, reports = self.proposal(q.spec_due, mixed)
        self.assertEqual(len(result), 1)
        self.assertEqual(reports[0]["excluded_count"], 1)
        self.persist_observations(mixed)
        with redirect_stderr(io.StringIO()) as output:
            q.apply("demo", now=T)
        self.assertIn('"excluded_count": 1', output.getvalue())
        self.proof_2107("AC2", malformed_cases=len(cases), valid_pair_survives=True)

    def test_2107_ac3_master_seed(self):
        a, b = "tests/test_seed.py::test_seed", "tests/test_b.py::test_b"
        nightly = self.nightly_observation((b, a))
        expected = [dict(node=n, rule="master-seed", windows=['nightly:["nightly:1","master-sha"]'])
                    for n in (b, a)]
        self.assertEqual(q.seed_due([nightly], "demo", T), expected)
        self.assertEqual(q.seed_due([dict(nightly, ids={"pytest": [b, a]})], "demo", T), expected)
        for change in (dict(complete=False), dict(suite="local-hermetic"), dict(suite=None),
                       dict(project="other"), dict(ts=(T+timedelta(seconds=1)).isoformat())):
            self.assertEqual(q.seed_due([dict(nightly, **change)], "demo", T), [])
            self.assertEqual(q.seed_due([nightly, dict(nightly, **change)], "demo", T), expected)
        absent = dict(nightly); absent.pop("suite")
        self.assertEqual(q.seed_due([absent], "demo", T), [])
        for key in ("sha", "_src"):
            invalid = dict(nightly); invalid.pop(key)
            result, reports = self.proposal(q.seed_due, [invalid])
            self.assertEqual(result, [])
            self.assertEqual(reports[0]["excluded_count"], 1)
        later = dict(nightly, ts=(T+timedelta(minutes=1)).isoformat(), _src="nightly:2", ids=[])
        self.assertEqual(q.seed_due([nightly, later], "demo", T+timedelta(minutes=1)), [])
        tie = dict(nightly, _src="nightly:9", ids=[])
        self.assertEqual(q.seed_due([nightly, tie], "demo", T), [])
        self.assertEqual(q.seed_due([tie, nightly], "demo", T), [])
        result, reports = self.proposal(q.seed_due, [])
        self.assertEqual(result, [])
        self.assertEqual(reports[0]["unavailable"], "no-counted-nightly")
        seed = self.quarantine(a, T-timedelta(days=8), rule="master-seed")
        for rows in ([seed], [seed, self.row("test-unquarantined", a)]):
            self.assertEqual(q.seed_due(rows+[nightly], "demo", T), [])
        for change in (dict(project="other"), dict(actor="executor"),
                       dict(ts=(T+timedelta(seconds=1)).isoformat())):
            self.assertEqual(q.seed_due([dict(seed, **change), nightly], "demo", T), expected)
        prior = self.quarantine(a, T-timedelta(days=8), rule="two-specs")
        until = q._time(prior["until"])
        for at in (until-timedelta(seconds=1), until):
            self.assertEqual(q.seed_due([prior, dict(nightly, ids=[a], ts=at.isoformat())], "demo", T), [])
        self.assertEqual(len(q.seed_due([prior, nightly], "demo", T)), 2)
        self.persist_observations([dict(nightly, ids=[])])
        self.assertEqual(q.apply("demo", now=T)["recorded"], [])
        self.persist_observations([dict(nightly, ts=(T+timedelta(minutes=1)).isoformat())])
        self.assertEqual(len(q.apply("demo", now=T+timedelta(minutes=1))["recorded"]), 2)
        self.persist_observations([dict(nightly, ids=["tests/test_new.py::test_new"],
                                        ts=(T+timedelta(minutes=2)).isoformat())])
        self.assertEqual(q.apply("demo", now=T+timedelta(minutes=2))["recorded"], [])
        rows = self.fresh_events()
        self.assertEqual(sum(e["type"] == "test-quarantined" for e in rows), 2)
        self.assertEqual(sum(e["type"] == "question" for e in rows), 2)
        self.proof_2107("AC3", initial_batch=2, gate_prevents_later_seed=True)

    def test_2107_ac4_apply_store_round_trip(self):
        import quarantine_file as policy
        import rechain
        project = "albert-scott"
        repo = self.root / "repos" / project
        (repo / Path(policy.PATH).parent).mkdir(parents=True)
        (repo / policy.PATH).write_text("# quarantine\n")
        rechain.out(repo, "init", "-q", "-b", "fixture-main")
        rechain.out(repo, "add", ".")
        rechain.out(repo, *rechain.IDENT, "commit", "-qm", "fixture")
        pair = self.tree_observation()+self.tree_observation("w2", "sha2", "tree2", ("S2",))
        a, b = "tests/test_a.py::test_a", "tests/test_seed.py::test_seed"
        self.persist_observations(pair+[self.nightly_observation((a, b))], project)
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            result = q.apply(project, now=T)
            self.assertEqual(len(result["recorded"]), 2)
            rows = self.fresh_events()
            quarantines = [e for e in rows if e["type"] == "test-quarantined"]
            self.assertEqual({e["rule"] for e in quarantines}, {"two-specs", "master-seed"})
            for row in quarantines:
                question = next(e for e in rows if e["_src"] == row["fix"])
                self.assertEqual(question["type"], "question")
                self.assertEqual(question["blocks"], [row["node"]])
                self.assertEqual(question["deadline"], row["until"])
                self.assertTrue(row["owner"])
                self.assertEqual(row["since"], T.isoformat())
                self.assertEqual(q._time(row["until"]), T+timedelta(days=7))
                self.assertEqual(row["windows"],
                                 (q.spec_due(pair, "demo", T) if row["rule"] == "two-specs"
                                  else q.seed_due([next(e for e in rows if e["type"] == "base-red-nightly")],
                                                  project, T))[0]["windows"])
            content = policy.render("# quarantine\n", rows, project, T)
            for line in content.splitlines()[1:]:
                fields = line.split("\t")
                self.assertEqual(len(fields), 4)
                self.assertLessEqual(datetime.fromisoformat(fields[2]).date(), (T+timedelta(days=30)).date())
            queue = [e for e in rows if e["type"] == "queue-add"]
            self.assertEqual(len(queue), 1)
            branch = queue[0]["branch"]
            self.assertEqual(rechain.text(repo, branch, policy.PATH), content)
            # Deliver the queued policy in this disposable repository, so expiry queues removal.
            rechain.out(repo, "reset", "--hard", branch)
            for minutes in (1, 2):
                self.persist_observations([self.verified(T+timedelta(minutes=minutes), subject="clean")], project)
            for _ in range(2):
                unchanged = q.apply(project, now=T+timedelta(minutes=3))
                self.assertEqual(unchanged, {"recorded": [], "lifted": []})
            until = T+timedelta(days=7)
            self.assertEqual(len(q.apply(project, now=until)["lifted"]), 2)
            self.assertEqual(q.apply(project, now=until+timedelta(minutes=1)), {"recorded": [], "lifted": []})
            final = self.fresh_events()
            for kind, count in (("test-quarantined", 2), ("question", 2), ("queue-add", 2),
                                ("test-unquarantined", 2), ("escalation-blocking", 2)):
                self.assertEqual(sum(e["type"] == kind for e in final), count, kind)
            removal = [e for e in final if e["type"] == "queue-add"][-1]
            self.assertEqual(rechain.text(repo, removal["branch"], policy.PATH), "# quarantine\n")
        # An expired non-seed row does not allow unchanged nightly evidence to seed it.
        prior_node = "tests/test_prior.py::test_prior"
        q.record("demo", prior_node, "two-specs", ["tree:prior"], now=T)
        self.persist_observations([self.nightly_observation((prior_node,))])
        for at in (until, until+timedelta(minutes=1)):
            self.assertEqual(q.apply("demo", now=at)["recorded"], [])
        self.persist_observations([self.nightly_observation((prior_node,), at=until+timedelta(minutes=2))])
        self.assertEqual(len(q.apply("demo", now=until+timedelta(minutes=2))["recorded"]), 1)
        self.proof_2107("AC4", fresh_process_readback=True, initial_rows=2, queue_adds=2)

    def test_2107_ac6_input_and_ownership(self):
        import merge_queue
        import quarantine_file as policy
        node = "tests/test_a.py::test_a"
        pair = self.tree_observation()+self.tree_observation("w2", "sha2", "tree2", ("S2",))
        for value in ("not-a-node", node+"\nforged", node+"\towner", node+"\x00", node+"\x7f", node+"\x80"):
            bad = [dict(e, new_ids=[value]) if e["type"] == "window-tree-verified" else e for e in pair]
            self.assertEqual(self.proposal(q.spec_due, bad)[0], [])
            for rule in ("two-specs", "master-seed"):
                with self.assertRaises(ValueError):
                    q.record("demo", value, rule, ["safe"], now=T)
        for field in ("subject", "sha", "tree", "_src", "window"):
            bad = [dict(e, **{field: "injected\nrow"}) if e["type"] == "window-tree-verified" else e
                   for e in pair]
            self.assertEqual(self.proposal(q.spec_due, bad)[0], [], field)
        for rule in ("two-specs", "master-seed"):
            for value in ("bad\nrow", "bad\x80row", "bad\u2028row", "bad\u2029row"):
                with self.assertRaises(ValueError):
                    q.record("demo", node, rule, [value], now=T)
        self.assertEqual(self.fresh_events(), [])
        with self.assertRaises(ValueError):
            q.apply("../foreign", now=T)
        self.assertEqual(q.spec_due(pair, "foreign", T), [])
        self.assertEqual(q.seed_due([self.nightly_observation()], "foreign", T), [])
        self.persist_observations(pair)
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), \
                patch("subprocess.run", side_effect=AssertionError("dispatch forbidden")):
            q.apply("demo", now=T)
        rows = self.fresh_events()
        row = q.active(rows, "demo", T)[node]
        question = next(e for e in rows if e["_src"] == row["fix"])
        self.assertTrue(row["owner"])
        self.assertEqual(question["blocks"], [node])
        self.assertEqual(len(policy.render("", rows, "demo", T).strip().split("\t")), 4)
        alarm = dict(type="nightly-red", project="demo", subject="nightly:demo", ts=T.isoformat(),
                     ids=[node, "unidentified suite failure"])
        self.assertEqual(fold_emits.nightly_unowned(rows+[alarm], "demo"),
                         [node, "unidentified suite failure"])
        with patch.object(merge_queue, "datetime") as clock:
            clock.now.return_value = T
            self.assertEqual(merge_queue.merge_freeze(rows+[alarm], "demo"),
                             "unowned nightly red: unidentified suite failure")
            self.assertIsNone(merge_queue.merge_freeze(rows+[dict(alarm, ids=[node])], "demo"))
        self.proof_2107("AC6", ownership_question=question["type"], unknown_failure_preserved=True)

    def test_2107_ac5_legacy_policy(self):
        node = "tests/test_a.py::test_a"
        drops = [self.row("window-dropped", at=T-timedelta(hours=i), window=str(i),
                          nodes=[node]) for i in range(q.BLAME_WINDOWS)]
        self.assertEqual(q.due(drops, "demo", T),
                         [dict(node=node, rule="repeat-blame", windows=["0", "1", "2"])])
        self.assertEqual(q.due([dict(e, owned_nodes=[node]) for e in drops], "demo", T), [])
        clean = [self.verified(T+timedelta(minutes=i)) for i in (1, 2)]
        for rule in ("two-specs", "master-seed", "auto-base-red"):
            row = self.quarantine(node, rule=rule)
            self.assertEqual(q.lifts([row]+clean, "demo", T+timedelta(minutes=3)), [])
            self.assertEqual(q.lifts([row]+clean, "demo", T+timedelta(days=7)), [(node, "until")])
        self.assertEqual(q.lifts([self.quarantine(node, rule="base-red")]+clean,
                                 "demo", T+timedelta(minutes=3)), [(node, "green-2")])
        self.assertEqual(test_source_writes(Path(q.__file__).read_text()), [])
        aliases = 'p0 = Path("tests/test_a.py")\n' + "\n".join(
            f"p{i} = p{i-1} / p{i-1}" for i in range(1, 40))
        self.assertTrue(test_source_writes(aliases+'\np39.write_text("bad")'))
        self.assertTrue(test_source_writes('a = b; b = a / "tests/test_a.py"; a.write_text("bad")'))
        self.proof_2107("AC5", legacy_lifts_preserved=True, scoped_green_exemptions=3)

    def test_1799_ac1_owned_nodes_never_due(self):
        def pre_change_due(events, project, now):
            now = q._time(now)
            blamed = {}
            for e in events:
                if (e.get("type") == "window-dropped" and e.get("project") == project
                        and now-timedelta(hours=q.BLAME_HOURS) <= q._time(e["ts"]) <= now
                        and e.get("window")):
                    for node in e.get("nodes", []):
                        blamed.setdefault(node, set()).add(e["window"])
            current = q.active(events, project, now)
            return [dict(node=n, rule="repeat-blame", windows=sorted(ws))
                    for n, ws in sorted(blamed.items()) if len(ws) >= q.BLAME_WINDOWS and n not in current]

        drops = [self.row("window-dropped", at=T-timedelta(hours=h),
                          window=w, nodes=["A"])
                 for w, h in (("1", 40), ("2", 20), ("3", 0))]
        expected = [dict(node="A", rule="repeat-blame", windows=["1", "2", "3"])]
        owned = [dict(drops[0], owned_nodes=["A"]), *drops[1:]]
        expired = drops + [self.row("window-dropped", at=T-timedelta(hours=49),
                                   window="old", nodes=["A"], owned_nodes=["A"])]
        results = {}
        for name, events, want in (("owned", owned, []), ("unowned", drops, expected),
                                   ("expired-owned", expired, expected)):
            old, new = pre_change_due(events, "demo", T), q.due(events, "demo", T)
            self.assertEqual(old, expected, name)
            self.assertEqual(new, want, name)
            results[name] = dict(old=old, new=new)

        for value in (None, "A", {"A": True}, 1):
            self.assertEqual(q.due([dict(drops[0], owned_nodes=value), *drops[1:]],
                                   "demo", T), expected)
        for change in (dict(project="other"), dict(type="window-verified"),
                       dict(ts=(T+timedelta(seconds=1)).isoformat())):
            self.assertEqual(q.due(drops + [dict(owned[0], **change)], "demo", T), expected)
        for h in (0, q.BLAME_HOURS):
            mention = self.row("window-dropped", at=T-timedelta(hours=h),
                               owned_nodes=["A"], nodes=[])
            self.assertEqual(q.due(drops + [mention], "demo", T), [])
        self.assertEqual(q.active(owned + [self.quarantine()], "demo", T),
                         {"A": self.quarantine()})
        body = {"marker": "L-spec-1799 AC1 ok", "results": results}
        encoded = json.dumps(body, sort_keys=True).encode()
        print(json.dumps(dict(url="cli://quarantine/1799/AC1", status=0,
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
