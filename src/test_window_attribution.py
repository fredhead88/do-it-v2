"""Hermetic targeted-attribution classifier and transport regressions."""
import json
import pathlib
import tempfile
import unittest
import subprocess
import threading
import os
import types
from unittest.mock import patch

import window_attribution as wa

NODE = "api/tests/test_victim.py::test_total"
OTHER = "api/tests/test_victim.py::test_other"


def result(ids=(NODE,), **changes):
    return dict.fromkeys(ids, "green") | changes


class Classifier(unittest.TestCase):
    def classify(self, specs="ABC", base=None, singles=None, pairs=None, ids=(NODE,)):
        return wa.classify(list(specs), list(ids), base or [result(ids)] * 3,
                           singles or {s: result(ids) for s in specs}, pairs)

    def test_single_spec_polluter_is_named_even_without_file_ownership(self):
        plan = self.classify(singles={"A": result(), "B": {NODE: "red"}, "C": result()})
        self.assertEqual(plan["drop"], {"B": [NODE]})
        self.assertEqual(plan["proven"], {"B": [NODE]})
        self.assertEqual(plan["environment"], [])
        self.assertEqual(plan["need_pairs"], [])

    def test_any_master_reproduction_is_environment_and_never_spec_blame(self):
        plan = self.classify(base=[result(), {NODE: "red"}, result()],
                             singles={s: {NODE: "red"} for s in "ABC"})
        self.assertEqual(plan["environment"], [NODE])
        self.assertEqual(plan["drop"], {})

    def test_three_master_observations_are_required(self):
        for base in ([result()], [result(), result()], [result()] * 4):
            self.assertEqual(self.classify(base=base)["drop"], {})
            self.assertEqual(self.classify(base=base)["unresolved"], [NODE])

    def test_unknown_is_never_a_clean_single_or_a_fallback_half(self):
        for state in ("undetermined", "skipped", "", None):
            plan = self.classify("ABCDE", singles={s: {NODE: state} for s in "ABCDE"})
            self.assertEqual(plan["drop"], {})
            self.assertEqual(plan["unresolved"], [NODE])

    def test_pairs_drop_newer_member_of_each_reproducing_pair(self):
        initial = self.classify()
        self.assertEqual(initial["need_pairs"], [NODE])
        plan = self.classify(pairs={"A+B": {NODE: "red"}, "A+C": result(), "B+C": result()})
        self.assertEqual(plan["drop"], {"B": [NODE]})
        self.assertEqual(plan["mode"], "pairs")

    def test_no_reproducing_pair_is_unattributed(self):
        plan = self.classify(pairs={"A+B": result(), "A+C": result(), "B+C": result()})
        self.assertEqual(plan["drop"], {})
        self.assertEqual(plan["unresolved"], [NODE])

    def test_large_window_drops_only_newest_half_after_clean_probes(self):
        plan = self.classify("ABCDE")
        self.assertEqual(plan["drop"], {"C": [NODE], "D": [NODE], "E": [NODE]})
        self.assertEqual(plan["mode"], "newest-half")
        self.assertEqual(plan["proven"], {})

    def test_e4_shape_positive_single_prevents_newest_half_fallback(self):
        specs = ["L-spec-" + str(n) for n in (1704, 1910, 1911, 1921, 1924, 1925, 1926, 1927, 1928, 1930, 1931)]
        ids = ["src/lib/server-identity.test.ts::AC6 added caller", "src/lib/server-identity.test.ts::AC6 complete census"]
        singles = {s: result(ids) for s in specs}
        singles["L-spec-1927"] = dict.fromkeys(ids, "red")
        plan = self.classify(specs, ids=ids, singles=singles)
        self.assertEqual(plan["drop"], {"L-spec-1927": ids})
        self.assertEqual(plan["mode"], "singles")

    def test_missing_on_master_is_distinct_from_unknown(self):
        plan = self.classify(base=[{NODE: "absent"}] * 3,
                             singles={"A": result(), "B": {NODE: "red"}, "C": result()})
        self.assertEqual(plan["drop"], {"B": [NODE]})

    def test_partial_environment_cannot_hide_real_spec_failure(self):
        ids = (NODE, OTHER)
        base = [{NODE: "red", OTHER: "green"}] * 3
        singles = {s: result(ids) for s in "ABC"}
        singles["B"] = {NODE: "red", OTHER: "red"}
        plan = self.classify(ids=ids, base=base, singles=singles)
        self.assertEqual(plan["environment"], [NODE])
        self.assertEqual(plan["drop"], {"B": [OTHER]})

    def test_overlapping_pairs_keep_specs_already_cleared_by_one_drop(self):
        plan = self.classify(pairs={"A+B": {NODE: "red"}, "A+C": result(), "B+C": {NODE: "red"}})
        self.assertEqual(plan["drop"], {"B": [NODE]})


class Matrix(unittest.TestCase):
    def test_diagnostic_rechain_and_fingerprint_emit_no_events_or_refs(self):
        import window_gate as wg
        import quarantine_file
        entry = ("A", "branch-A", "tip-A")
        def rechain(*args, ev, **kw):
            ev("rechained", "A")
            return "rechained", None
        def fingerprint(*args, ev, **kw):
            ev("rechain-fingerprint", "diagnostic")
            return "fingerprinted"
        def attribute(repo, base, entries, ids, prepare):
            self.assertEqual(prepare(["A"]), "policy-tree")
            return {"fixture": True}
        with patch.dict(os.environ, DOIT_WINDOW_RECHAIN="on"), \
             patch.object(wg.window_prewindow, "select",
                          side_effect=lambda r, b, m, entries, e, c: (entries, [])), \
             patch.object(wg, "merge_into", return_value=(None, ["migration"])), \
             patch.object(wg.rechain, "resolve", side_effect=rechain) as resolve, \
             patch.object(wg.rechain, "added_versions", return_value=["migration.py"]), \
             patch.object(wg.rechain, "fingerprint", side_effect=fingerprint) as fp, \
             patch.object(wg.fold, "read_events", return_value=[]), \
             patch.object(quarantine_file, "candidate", return_value="policy-tree") as policy, \
             patch.object(wa, "attribute", side_effect=attribute), \
             patch.object(wg, "ev", side_effect=AssertionError("diagnostic ledger write")), \
             patch.object(wg, "git", side_effect=AssertionError("diagnostic ref write")):
            self.assertEqual(wg.targeted_attribution("repo", "master", [entry], [NODE]), {"fixture": True})
        self.assertTrue(resolve.called and fp.called)
        self.assertEqual(policy.call_args.args[1], "fingerprinted")

    def test_master_three_times_and_all_singles_start_in_parallel(self):
        barrier = threading.Barrier(7)
        calls = []
        lock = threading.Lock()
        def run(repo, sha, ids, deadline):
            with lock:
                calls.append((sha, ids, deadline))
            barrier.wait(timeout=3)
            return {"states": {NODE: "red" if sha == "tree-B" else "green"}}
        plan = wa.attribute(None, "master", [(s, "branch-" + s, "sha-" + s) for s in "ABCD"],
                            [NODE], runner=run, prepare=lambda members: "tree-" + "".join(members))
        self.assertEqual([sha for sha, _, _ in calls].count("master"), 3)
        self.assertEqual(len(calls), 7)
        self.assertEqual(len({deadline for _, _, deadline in calls}), 1)
        self.assertEqual(plan["drop"], {"B": [NODE]})
        self.assertEqual(len(plan["evidence"]), 7)

    def test_pair_wave_only_runs_ids_unexplained_by_master_and_singles(self):
        calls = []
        def run(repo, sha, ids, deadline):
            calls.append((sha, ids))
            return {"states": {n: "red" if (sha == "master" and n == OTHER) or
                                (sha == "tree-AB" and n == NODE) else "green" for n in ids}}
        plan = wa.attribute(None, "master", [(s, s, s) for s in "ABC"], [NODE, OTHER],
                            runner=run, prepare=lambda members: "tree-" + "".join(members))
        self.assertEqual(plan["environment"], [OTHER])
        self.assertEqual(plan["drop"], {"B": [NODE]})
        self.assertEqual(sorted((sha, ids) for sha, ids in calls if len(sha) == len("tree-AB")),
                         [("tree-AB", [NODE]), ("tree-AC", [NODE]), ("tree-BC", [NODE])])

    def test_failed_merge_and_missing_probe_cannot_select_a_newest_half(self):
        plan = wa.attribute(None, "master", [(s, s, s) for s in "ABCDE"], [NODE],
                            runner=lambda *a, **k: {"states": result()},
                            prepare=lambda members: None if "C" in members else "ok")
        self.assertEqual(plan["drop"], {})
        self.assertEqual(plan["unresolved"], [NODE])


class Transport(unittest.TestCase):
    def test_wrong_sha_nonce_ids_or_missing_state_are_rejected(self):
        sha = "a" * 40
        payload = dict(version=1, sha=sha, probe_id="nonce", ids=[NODE], states=result())
        self.assertEqual(wa.validate(payload, sha, "nonce", [NODE]), result())
        for field, value in (("sha", "b" * 40), ("probe_id", "stale"), ("ids", [OTHER]),
                             ("states", {}), ("states", {NODE: "skipped"})):
            with self.assertRaises(ValueError):
                wa.validate(dict(payload, **{field: value}), sha, "nonce", [NODE])

    def exercise(self, *, timeout=False, cancelled=False, bad_sha=False):
        import window_gate as wg
        import base_red
        sha = "a" * 40
        commands = []
        pushes = []
        nonce = "1" * 32
        def gh(args, seconds):
            commands.append(args)
            if args[:2] == ["workflow", "run"]:
                return ""
            if args[:2] == ["run", "cancel"]:
                return ""
            if args[:2] == ["run", "download"]:
                directory = pathlib.Path(args[args.index("-D") + 1])
                (directory / "targeted-result.json").write_text(json.dumps(dict(version=1,
                    sha="b" * 40 if bad_sha else sha, probe_id=nonce, ids=[NODE], states=result())))
                return ""
            if args[0] == "api" and "?" in args[1]:
                return json.dumps({"workflow_runs": [{"id": 123, "head_sha": sha,
                    "head_branch": "ci-verify/attribution-" + nonce, "path": wa.WORKFLOW,
                    "display_title": "targeted-attribution " + nonce}]})
            if timeout:
                raise TimeoutError("fixture deadline")
            return json.dumps({"head_sha": sha, "head_branch": "ci-verify/attribution-" + nonce,
                               "status": "completed", "conclusion": "cancelled" if cancelled else "success"})
        def git(repo, *args, **kw):
            pushes.append(args)
            return types.SimpleNamespace(returncode=0)
        with patch.object(wa, "supported", return_value=True), patch.object(wa, "_gh", side_effect=gh), \
             patch.object(wa.uuid, "uuid4", return_value=types.SimpleNamespace(hex=nonce)), \
             patch.object(base_red, "_owner", return_value="fixture/repo"), patch.object(wg, "git", side_effect=git):
            answer = wa.probe("repo", sha, [NODE])
        self.assertEqual(len(pushes), 2)
        self.assertEqual(pushes[-1][-1], ":refs/heads/ci-verify/attribution-" + nonce)
        self.assertEqual(any(c[:2] == ["run", "cancel"] for c in commands), timeout)
        return answer, commands

    def test_dispatch_is_exact_and_result_artifact_is_validated(self):
        answer, commands = self.exercise()
        self.assertEqual(answer["states"], result())
        self.assertIn("tree_sha=" + "a" * 40, commands[0])
        self.assertIn("node_ids=" + json.dumps([NODE]), commands[0])

    def test_timeout_cancels_run_and_deletes_only_its_ref(self):
        self.assertEqual(self.exercise(timeout=True)[0]["states"], {NODE: "undetermined"})

    def test_cancellation_and_wrong_sha_never_count_as_clean(self):
        for kwargs in ({"cancelled": True}, {"bad_sha": True}):
            self.assertEqual(self.exercise(**kwargs)[0]["states"], {NODE: "undetermined"})


class Gate(unittest.TestCase):
    def setUp(self):
        import fold
        import window_gate as wg
        self.fold, self.wg = fold, wg
        self.temp = tempfile.TemporaryDirectory(prefix="targeted-window-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.repo = self.root / "repos" / "albert-scott"
        self.repo.mkdir(parents=True)
        self.git("init", "-q", "-b", "master")
        (self.repo / "base").write_text("base")
        self.git("add", "."); self.git("commit", "-qm", "base")
        self.old = self.git("rev-parse", "HEAD")
        self.entries = []
        for spec in "ABC":
            self.git("switch", "-qc", spec, self.old)
            (self.repo / spec).write_text(spec)
            self.git("add", "."); self.git("commit", "-qm", spec)
            self.entries.append((spec, spec, self.git("rev-parse", "HEAD")))
        self.git("switch", "-q", "master")
        (self.repo / "new-master-fix").write_text("must appear in every probe")
        self.git("add", "."); self.git("commit", "-qm", "newer master")
        self.base = self.git("rev-parse", "HEAD")
        self.git("switch", "-qc", "operator-checkout")
        (self.repo / "base").write_text("operator dirty content")
        self.stack = __import__("contextlib").ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(fold, "ROOT", self.root))
        self.stack.enter_context(patch.object(fold, "EVENTS", self.root / "events"))
        self.stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE="1",
            DOIT_GATE_LEDGER_FILE="L-executor-0001.jsonl", DOIT_WINDOW_RECHAIN="off"))

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), "-c", "user.name=fixture",
            "-c", "user.email=fixture@localhost", *args], text=True, stderr=subprocess.DEVNULL).strip()

    def run_gate(self, mode="polluter", remainder="green"):
        wg, full, probes = self.wg, [], []
        def verify(repo, sha, required, window):
            full.append(sha)
            state = "red" if len(full) == 1 else remainder
            return dict(state=state, nodes=[NODE] if state == "red" else [], candidate=sha,
                        url="fixture://full", doc={}, output="", why="fixture")
        def probe(repo, sha, ids, deadline):
            probes.append(sha)
            self.assertTrue(self.git("ls-tree", sha, "new-master-fix"), "probe must use current master")
            has_b = bool(self.git("ls-tree", sha, "B"))
            states = {n: "red" if (mode == "polluter" and has_b) or mode == "environment"
                      else "undetermined" if mode == "unknown" else "green" for n in ids}
            return {"states": states, "sha": sha}
        def failures(repo, res):
            return dict(pytest=set(res["nodes"]), **{"pg-parity": set(), "vitest": set(),
                "suite-error": set(), "undetermined": [], "pg_files": {}})
        import base_red
        patches = [patch.object(wg, "train_events", return_value=[]),
            patch.object(wg, "resolve_entries", return_value=self.entries),
            patch.object(wg.window_prewindow, "select",
                         side_effect=lambda r, b, m, entries, e, c: (entries, [])),
            patch.object(wg, "required_checks", return_value=[wg.PYTEST_CHECK]),
            patch.object(wg, "scope", return_value={"checks": [wg.PYTEST_CHECK], "reason": "fixture"}),
            patch.object(wg, "base_red_snapshot", return_value={"complete": True}),
            patch.object(wg, "verify", side_effect=verify), patch.object(wg, "candidate_failures", side_effect=failures),
            patch.object(wg, "run_minutes", return_value={"actions_minutes": 0, "selfhosted_minutes": 0, "minutes_source": "fixture"}),
            patch.object(base_red, "classify", side_effect=lambda p, b, ids, **kw: {"complete": True, "new": ids, "pre_existing": []}),
            patch.object(base_red, "recheck", side_effect=lambda p, b, ids: {"green": ids}),
            patch.object(wa, "probe", side_effect=probe),
            patch.object(wg, "bisect_window", side_effect=AssertionError("full-CI bisection forbidden"))]
        with __import__("contextlib").ExitStack() as stack:
            for p in patches: stack.enter_context(p)
            code = wg._run_attempt("window-fixture", "albert-scott", [], "master", False,
                                   self.base, self.base, None, 0, None)
        self.assertEqual(self.git("branch", "--show-current"), "operator-checkout")
        self.assertEqual((self.repo / "base").read_text(), "operator dirty content")
        return code, full, probes, self.fold.read_events()

    def test_only_remainder_full_green_advances_and_polluter_evidence_is_attached(self):
        code, full, probes, events = self.run_gate()
        self.assertEqual(code, 1)
        self.assertEqual(len(full), 2)
        self.assertEqual(len(probes), 6)
        self.assertEqual(self.git("rev-parse", "master"), full[-1])
        self.assertFalse(self.git("ls-tree", full[-1], "B"))
        drops = [e for e in events if e["type"] == "window-dropped"]
        self.assertEqual([(e["subject"], e["reason"], e["nodes"]) for e in drops], [("B", "targeted-attribution", [NODE])])
        self.assertEqual(drops[0]["owned_nodes"], [NODE], "proven polluters must not be auto-quarantined as unowned noise")
        self.assertEqual(len([e for e in events if e["type"] == "window-targeted-attribution"]), 1)
        self.assertFalse(any(e["type"] == "escalation-blocking" for e in events))

    def test_environment_failure_records_master_evidence_and_blames_no_spec(self):
        code, full, _, events = self.run_gate("environment")
        self.assertEqual(code, 0)
        self.assertEqual(len(full), 2)
        self.assertFalse(any(e["type"] == "window-dropped" for e in events))
        self.assertTrue(any(e["type"] == "window-pre-existing" and e["ids"] == [NODE] for e in events))

    def test_unknown_probe_does_not_move_master_or_dispatch_full_halves(self):
        for spec, branch, _ in self.entries:
            self.fold.append(["queue-add", spec, "project=albert-scott", "branch=" + branch, "rule=operator"])
        code, full, _, events = self.run_gate("unknown")
        self.assertEqual(code, 2)
        self.assertEqual(len(full), 1)
        self.assertEqual(self.git("rev-parse", "master"), self.base)
        self.assertFalse(any(e["type"] == "window-dropped" for e in events))
        escalation = next(e for e in events if e["type"] == "escalation-blocking")
        self.assertEqual(escalation["nodes"], [NODE])
        self.assertIn("requeue unattributed specs with failing ids", escalation["default"])
        import window_policy
        import merge_queue
        from datetime import timedelta
        # Let real W5 deadline recovery consume the gate's actual unresolved
        # attribution and escalation events in this isolated ledger.
        now = self.fold.ts(escalation["ts"]) + timedelta(minutes=31)
        self.assertTrue(window_policy.apply_default("window-fixture", escalation["_src"], now))
        rows = merge_queue.queue(self.fold.read_events(), "albert-scott")
        self.assertCountEqual([(r["spec"], r["batch"]) for r in rows], [(s, None) for s in "ABC"])
        requeues = [e for e in self.fold.read_events() if e["type"] == "queue-add" and e.get("rule") == "escalation-default"]
        self.assertEqual(len(requeues), 3)
        self.assertTrue(all(e["nodes"] == [NODE] for e in requeues))

    def test_red_remainder_stops_after_one_full_retry(self):
        code, full, _, events = self.run_gate(remainder="red")
        self.assertEqual(code, 2)
        self.assertEqual(len(full), 2)
        self.assertEqual(self.git("rev-parse", "master"), self.base)


if __name__ == "__main__":
    unittest.main()
