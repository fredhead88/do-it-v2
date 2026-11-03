"""L-spec-2012: member verdicts, parallel alone-tree verification, tree-keyed reuse.

AC1Tests .. AC7Tests map to the seven acceptance criteria. Hermetic: real git in
a scratch repo, the real ledger append/read path under a scratch DOIT_ROOT, a fake
CI transport (no dispatch, no network, no credentials).
"""
import atexit
import inspect
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

_import_root = tempfile.TemporaryDirectory(prefix="wm-import-")
atexit.register(_import_root.cleanup)
os.environ.setdefault("DOIT_ROOT", _import_root.name)
os.environ["DOIT_NO_POKE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold  # noqa: E402
import window_gate  # noqa: E402
import window_members  # noqa: E402

SRC = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SRC.parent
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
UNKNOWN = "api/tests/absent.py::lost"
REQUIRED = ["pytest", "pg-parity (Postgres)"]
REPORTS = []


def report(ac, **body):
    REPORTS.append(dict(ac=ac, **body))
    print(json.dumps(dict(criterion=ac, body_excerpt=body), sort_keys=True))


def git(repo, *args):
    p = subprocess.run(["git", "-c", "user.email=t@x", "-c", "user.name=t", "-C", str(repo), *args],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"git {args}: {p.stderr}"
    return p.stdout.strip()


class Gate:
    """The slice of window_gate that window_members consumes, with a scripted CI transport."""
    merge_into = staticmethod(window_gate.merge_into)
    tree_of = staticmethod(window_gate.tree_of)
    covers = staticmethod(window_gate.covers)
    ev = staticmethod(window_gate.ev)
    TrainCancelled = window_gate.TrainCancelled

    def __init__(self, repo, script=None, delay=0.12):
        self.repo, self.script, self.delay = repo, script or {}, delay
        self.lock = threading.Lock()
        self.inflight = self.peak = 0
        self.calls = []

    def verify(self, repo, cand, required, window):
        files = git(repo, "ls-tree", "-r", "--name-only", cand).split()
        spec = next((n[:-3] for n in files if n.endswith(".py") and len(n) == 4), "?")
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            self.calls.append(spec)
        try:
            time.sleep(self.delay)
        finally:
            with self.lock:
                self.inflight -= 1
        outcome = self.script.get(spec, ("green", []))
        if isinstance(outcome, Exception):
            raise outcome
        state, nodes = outcome
        return dict(state=state, nodes=list(nodes), url=f"fake://ci/{spec}", doc={})


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wm-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name) / "root"
        (self.root / "events").mkdir(parents=True)
        self.saved = (fold.ROOT, fold.EVENTS, os.environ.get("DOIT_ROOT"), os.environ.get("DOIT_LEDGER_FILE"))
        fold.ROOT, fold.EVENTS = self.root, self.root / "events"
        os.environ["DOIT_ROOT"] = str(self.root)
        os.environ.pop("DOIT_GATE_LEDGER_FILE", None)
        self.addCleanup(self.restore)
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "master")
        (self.repo / "base.txt").write_text("b\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        self.base = git(self.repo, "rev-parse", "master")
        self.entries = [self.branch(name) for name in "ABC"]
        self.gate = Gate(self.repo)

    def restore(self):
        fold.ROOT, fold.EVENTS, root, ledger = self.saved
        for key, value in (("DOIT_ROOT", root), ("DOIT_LEDGER_FILE", ledger)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def branch(self, name, content=None, base=None):
        self.count = getattr(self, "count", 0) + 1
        git(self.repo, "checkout", "-q", "-b", f"br-{name}-{self.count}", base or self.base)
        (self.repo / f"{name}.py").write_text(content or f"{name}\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", name)
        sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "master")
        return (name, f"br-{name}", sha)

    def alone_tree(self, entry, base=None):
        cand, conflicts = window_gate.merge_into(self.repo, base or self.base, entry[2], "x")
        self.assertIsNone(conflicts)
        return window_gate.tree_of(self.repo, cand)

    def scope_of(self, cand):
        return list(REQUIRED)

    def attribute(self, entries=None, **kw):
        return window_members.attribute(self.gate, self.repo, self.base, entries or self.entries, "w1",
                                        scope_of=self.scope_of, **kw)

    def events(self, typ):
        return [e for e in fold.read_events(use_cache=False) if e.get("type") == typ]

    def verdict_event(self, tree, state="green", hours_ago=1, required=REQUIRED, now=NOW, **extra):
        e = dict(type="member-verdict", subject="A", spec="A", window="old", tree=tree, state=state, url="",
                 nodes=[], ts=(now - timedelta(hours=hours_ago)).isoformat(), **extra)
        if required is not None:
            e["required"] = list(required)
        return e


class AC1Tests(Scratch):
    def test_three_alone_verifications_run_concurrently_and_persist(self):
        self.gate.script = {"B": ("red", [UNKNOWN])}
        out = self.attribute()
        self.assertEqual(self.gate.peak, 3)
        self.assertEqual(sorted(self.gate.calls), ["A", "B", "C"])
        events = self.events("member-verdict")
        self.assertEqual(sorted((e["spec"], e["state"]) for e in events),
                         [("A", "green"), ("B", "red"), ("C", "green")])
        for e in events:
            entry = next(x for x in self.entries if x[0] == e["spec"])
            self.assertEqual(e["tree"], self.alone_tree(entry))  # member alone onto the window base
            self.assertEqual((e["window"], e["subject"], e["url"]), ("w1", e["spec"], f"fake://ci/{e['spec']}"))
        raw = [json.loads(line) for line in (self.root / "events/L-executor-0001.jsonl").read_text().splitlines()]
        self.assertTrue(raw and all("actor" not in line for line in raw))  # the actor is the filename
        self.assertEqual({e["spec"]: e["nodes"] for e in events}["B"], [UNKNOWN])
        # An independent fresh session reads the same events from the scratch root.
        code = ("import sys, json; sys.path.insert(0, %r); import fold; "
                "print(json.dumps([[e['spec'], e['state'], e['tree']] for e in fold.read_events(use_cache=False) "
                "if e.get('type') == 'member-verdict']))" % str(SRC))
        fresh = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                               env={"DOIT_ROOT": str(self.root), "PATH": "/usr/bin:/bin", "DOIT_NO_POKE": "1"})
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual(sorted(json.loads(fresh.stdout)), sorted([e["spec"], e["state"], e["tree"]] for e in events))
        self.assertEqual(out["drop"], {"B": [UNKNOWN]})
        report("AC1", peak_in_flight=self.gate.peak, trees=sorted(e["tree"][:8] for e in events), persisted=len(events))

    def test_configured_cap_bounds_in_flight_calls(self):
        out = self.attribute(parallel=2)
        self.assertEqual(self.gate.peak, 2)
        self.assertEqual(len(out["dispatched"]), 3)
        report("AC1", cap=2, peak_in_flight=self.gate.peak)

    def test_default_and_configured_parallel_come_from_look_toml(self):
        self.assertEqual(window_members.member_verify_parallel(REPO_ROOT / "look.toml"), 4)
        self.assertEqual(window_members.verdict_fresh_hours(REPO_ROOT / "look.toml"), 24)
        absent = pathlib.Path(self.tmp.name) / "absent.toml"
        self.assertEqual((window_members.member_verify_parallel(absent), window_members.verdict_fresh_hours(absent)), (4, 24))
        for body, expect in (("[merge_queue]\nmember_verify_parallel = 2\nverdict_fresh_hours = 6\n", (2, 6)),
                             ("[merge_queue]\nmember_verify_parallel = 0\nverdict_fresh_hours = -1\n", (4, 24)),
                             ("[merge_queue]\nmember_verify_parallel = true\nverdict_fresh_hours = true\n", (4, 24)),
                             ("[merge_queue]\nmember_verify_parallel = 2.5\nverdict_fresh_hours = nan\n", (4, 24)),
                             ("[merge_queue\n", (4, 24))):
            path = pathlib.Path(self.tmp.name) / "look.toml"
            path.write_text(body)
            self.assertEqual((window_members.member_verify_parallel(path), window_members.verdict_fresh_hours(path)), expect)


class AC2Tests(Scratch):
    def test_red_member_is_charged_and_greens_are_not_repeated(self):
        self.gate.script = {"B": ("red", [UNKNOWN])}
        first = self.attribute(blame=[UNKNOWN])
        self.assertEqual(first["drop"], {"B": [UNKNOWN]})
        self.assertEqual((first["verdicts"]["A"], first["verdicts"]["C"]), ("green", "green"))
        # The next window re-admits the remaining members: greens skip alone-tree CI entirely.
        before = len(self.gate.calls)
        events = fold.read_events(use_cache=False)
        again = self.attribute([e for e in self.entries if e[0] != "B"], events=events)
        self.assertEqual(len(self.gate.calls), before)
        self.assertEqual(sorted(again["reused"]), ["A", "C"])
        marks = window_members.readmit(self.gate, self.repo, self.base, self.entries, "w2",
                                       scope_of=self.scope_of, events=events)
        self.assertEqual(sorted(marks["green"]), ["A", "C"])
        self.assertEqual(marks["other"], ["B"])
        report("AC2", dropped=list(first["drop"]), redispatched=0, reused=again["reused"])

    def test_blame_filters_red_nodes_that_the_window_did_not_charge(self):
        self.gate.script = {"B": ("red", ["other.py::preexisting"])}
        out = self.attribute(blame=[UNKNOWN])
        self.assertEqual(out["drop"], {})
        self.assertEqual(out["verdicts"]["B"], "red")

    def test_undetermined_is_readmitted_marked_and_never_green(self):
        self.gate.script = {"A": ("undetermined", []), "B": ("red", []),
                            "C": RuntimeError("unreadable ci response")}
        out = self.attribute()
        self.assertEqual(out["verdicts"], {"A": "undetermined", "B": "undetermined", "C": "undetermined"})
        self.assertEqual(out["drop"], {})  # a red nobody can name is not a charge
        events = fold.read_events(use_cache=False)
        for entry in self.entries:
            tree = self.alone_tree(entry)
            prior = window_members.prior_result(events, tree)
            self.assertIsNone(prior)
            self.assertFalse(window_gate.covers(prior, REQUIRED))
        marks = window_members.readmit(self.gate, self.repo, self.base, self.entries, "w2",
                                       scope_of=self.scope_of, events=events)
        self.assertEqual(sorted(marks["undetermined"]), ["A", "B", "C"])
        self.assertEqual(marks["green"], [])
        # They are dispatched again next time: undetermined earns no cache privilege.
        before = len(self.gate.calls)
        self.gate.script = {}
        self.attribute(events=events)
        self.assertEqual(len(self.gate.calls) - before, 3)
        report("AC2", undetermined=marks["undetermined"], green_privileges=0)

    def test_recovery_set_is_the_escalation_default_requeue(self):
        def queued(spec, rule, nodes, minute):
            return dict(type="queue-add", subject=spec, rule=rule, nodes=nodes, ts=f"2026-11-03T10:{minute:02d}:00+00:00")
        events = [queued("A", "escalation-default", [UNKNOWN], 1), queued("B", "escalation-default", [UNKNOWN], 1),
                  queued("C", "escalation-default", [UNKNOWN], 1), queued("C", "operator", [], 2),
                  queued("D", "escalation-default", [], 1), queued("E", "escalation-default", "bad", 1)]
        self.assertEqual(window_members.recovering(events, ["A", "B", "C", "D", "E"]),
                         {"A": [UNKNOWN], "B": [UNKNOWN]})
        self.assertEqual(window_members.recovering([], ["A"]), {})

    def test_garbage_verdicts_and_cancellation_are_undetermined(self):
        original = self.gate.verify
        self.gate.verify = lambda repo, cand, required, window: {"state": "cancelled", "nodes": []}
        out = self.attribute(self.entries[:1])
        self.assertEqual(out["verdicts"], {"A": "undetermined"})
        self.gate.verify = lambda repo, cand, required, window: None
        self.assertEqual(self.attribute(self.entries[1:2])["verdicts"], {"B": "undetermined"})
        self.gate.verify = original

    def test_train_cancellation_propagates(self):
        self.gate.script = {"A": window_gate.TrainCancelled("base-moved")}
        with self.assertRaises(window_gate.TrainCancelled):
            self.attribute()


class AC3Tests(Scratch):
    def fresh(self, hours_ago, **kw):
        return self.verdict_event(self.alone_tree(self.entries[0]), hours_ago=hours_ago, **kw)

    def reusable(self, events, entry=None, hours=None, toml_path=None):
        tree = self.alone_tree(entry or self.entries[0])
        return window_gate.covers(window_members.prior_result(events, tree, now=NOW, hours=hours, toml_path=toml_path), REQUIRED)

    def test_freshness_boundary_default_24h(self):
        self.assertTrue(self.reusable([self.fresh(23.99)]))
        self.assertTrue(self.reusable([self.fresh(24)]))
        self.assertFalse(self.reusable([self.fresh(24.01)]))
        report("AC3", fresh=True, stale_at_hours=24)

    def test_configured_hours_move_the_boundary(self):
        path = pathlib.Path(self.tmp.name) / "look.toml"
        path.write_text("[merge_queue]\nverdict_fresh_hours = 2\n")
        self.assertTrue(self.reusable([self.fresh(1.9)], hours=window_members.verdict_fresh_hours(path)))
        self.assertFalse(self.reusable([self.fresh(2.1)], hours=window_members.verdict_fresh_hours(path)))

    def test_stale_dispatches_again_and_fresh_does_not(self):
        events = [self.fresh(1)]
        self.attribute(self.entries[:1], events=events, now=NOW)
        self.assertEqual(self.gate.calls, [])
        self.attribute(self.entries[:1], events=[self.fresh(30)], now=NOW)
        self.assertEqual(self.gate.calls, ["A"])

    def test_rejected_evidence_never_authorizes_reuse(self):
        tree = self.alone_tree(self.entries[0])
        cases = {
            "undetermined": self.verdict_event(tree, state="undetermined"),
            "red": self.verdict_event(tree, state="red"),
            "future": self.verdict_event(tree, hours_ago=-1),
            "no-required": self.verdict_event(tree, required=None),
            "partial-required": self.verdict_event(tree, required=["pytest"]),
            "bad-tree": self.verdict_event("not-a-tree"),
            "bad-ts": dict(self.verdict_event(tree), ts="yesterday"),
            "naive-ts": dict(self.verdict_event(tree), ts="2026-11-03T11:00:00"),
            "missing-ts": {k: v for k, v in self.verdict_event(tree).items() if k != "ts"},
            "unknown-state": self.verdict_event(tree, state="cancelled"),
            "other-tree": self.verdict_event("0" * 40),
        }
        for label, event in cases.items():
            self.assertFalse(self.reusable([event]), label)
        self.assertFalse(self.reusable([self.fresh(1), self.fresh(0.5, state="red")]), "newer red hides older green")
        self.assertTrue(self.reusable([self.fresh(2, state="red"), self.fresh(1)]), "newer green wins")

    def test_window_tree_verified_supplies_reuse_on_the_same_tree(self):
        tree = self.alone_tree(self.entries[0])
        window_event = dict(type="window-tree-verified", subject="w0", window="w0", sha="x", tree=tree, green=True,
                            required=REQUIRED, new_ids=[], ts=(NOW - timedelta(hours=3)).isoformat())
        self.assertTrue(self.reusable([window_event]))
        self.assertFalse(self.reusable([dict(window_event, ts=(NOW - timedelta(hours=25)).isoformat())]))
        self.assertFalse(self.reusable([dict(window_event, green=False)]))
        self.assertFalse(self.reusable([dict(window_event, green="true")]))

    def test_tree_equality_not_commit_identity(self):
        a = self.entries[0]
        events = [self.fresh(1)]
        tree = git(self.repo, "rev-parse", f"{a[2]}^{{tree}}")
        amended = git(self.repo, "commit-tree", tree, "-p", self.base, "-m", "amended A")
        self.assertNotEqual(amended, a[2])
        self.assertTrue(self.reusable(events, ("A", "br-A", amended)))
        changed = self.branch("A", content="different content\n")
        self.assertFalse(self.reusable(events, changed))
        # A moved base yields a different alone-tree, so the old verdict does not apply.
        (self.repo / "moved.txt").write_text("m\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "master moves")
        moved = git(self.repo, "rev-parse", "HEAD")
        self.assertNotEqual(self.alone_tree(a, base=moved), self.alone_tree(a))
        self.assertFalse(window_gate.covers(window_members.prior_result(
            events, self.alone_tree(a, base=moved), now=NOW), REQUIRED))
        report("AC3", amended_reuses=True, changed_content_invalidates=True, moved_base_invalidates=True)


class AC4Tests(Scratch):
    def test_prior_result_has_one_freshness_rule(self):
        source = inspect.getsource(window_gate.prior_result)
        self.assertIn("window_members.prior_result", source)
        self.assertNotIn("timedelta", source)
        tree = self.alone_tree(self.entries[0])
        now = datetime.now(timezone.utc)
        for hours, expected in ((23, True), (25, False)):
            event = self.verdict_event(tree, hours_ago=hours, now=now)
            self.assertEqual(window_gate.prior_result([event], tree) is not None, expected)
            self.assertEqual(window_gate.covers(window_gate.prior_result([event], tree), REQUIRED), expected)

    def test_candidate_tree_equal_to_fresh_green_skips_dispatch(self):
        cand, merged, conflicts = window_gate.build_candidate(self.repo, "w1", self.base, self.entries[:1], publish=False)
        tree = window_gate.tree_of(self.repo, cand)
        self.assertEqual(tree, self.alone_tree(self.entries[0]))
        now = datetime.now(timezone.utc)
        event = self.verdict_event(tree, hours_ago=2, now=now)
        self.assertTrue(window_gate.covers(window_gate.prior_result([event], tree), REQUIRED))
        self.assertFalse(window_gate.covers(window_gate.prior_result([self.verdict_event(tree, hours_ago=30, now=now)], tree), REQUIRED))
        self.assertFalse(window_gate.covers(window_gate.prior_result([event], tree), REQUIRED + ["extra-check"]))

    def test_base_pg_is_the_same_rule(self):
        tree = window_gate.tree_of(self.repo, self.base)
        now = datetime.now(timezone.utc)
        calls = []

        def verify(repo, sha, required, window):
            calls.append(sha)
            return dict(state="green", nodes=[], url="", doc={})
        results = {}
        for label, event in (("member-fresh", self.verdict_event(tree, hours_ago=3, now=now, required=[window_gate.PG_CHECK])),
                             ("member-stale", self.verdict_event(tree, hours_ago=30, now=now, required=[window_gate.PG_CHECK])),
                             ("window-fresh", dict(type="window-tree-verified", tree=tree, green=True, sha="x", window="w0",
                                                   required=[window_gate.PG_CHECK], new_ids=[],
                                                   ts=(now - timedelta(hours=3)).isoformat()))):
            calls.clear()
            with patch.object(window_gate, "verify", side_effect=verify):
                result, _docs = window_gate.base_pg(self.repo, self.base, [], "w1", [event])
            results[label] = (result["state"], result.get("cached_tree") == tree, len(calls))
        self.assertEqual(results["member-fresh"], ("green", True, 0))
        self.assertEqual(results["window-fresh"], ("green", True, 0))
        self.assertEqual(results["member-stale"], ("green", False, 1))
        report("AC4", base_pg=results)


class AC5Tests(Scratch):
    def test_refusal_names_itself_only_with_fresh_alone_evidence_on_the_current_base(self):
        trees = [self.alone_tree(e) for e in self.entries]
        events = [dict(self.verdict_event(t, now=datetime.now(timezone.utc)), spec=e[0], subject=e[0])
                  for t, e in zip(trees, self.entries)]
        text = window_members.refusal(self.gate, self.repo, self.base, self.entries, "w1", "NEW ids=[]",
                                      scope_of=self.scope_of, events=events)
        self.assertTrue(text.startswith("member-interaction:"))
        self.assertNotIn("base incomplete", text)
        controls = {
            "missing-one": events[:2],
            "stale": [dict(events[0], ts=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())] + events[1:],
            "undetermined": [dict(events[0], state="undetermined")] + events[1:],
            "other-base-tree": [dict(events[0], tree="1" * 40)] + events[1:],
            "uncovered-scope": [dict(e, required=["pytest"]) for e in events],
        }
        for label, evs in controls.items():
            self.assertIsNone(window_members.refusal(self.gate, self.repo, self.base, self.entries, "w1", "x",
                                                     scope_of=self.scope_of, events=evs), label)
        self.assertIsNone(window_members.refusal(self.gate, self.repo, self.base, [], "w1", "x",
                                                 scope_of=self.scope_of, events=events))
        # A base that moved recomputes different alone-trees: the old verdicts are not trusted.
        (self.repo / "moved.txt").write_text("m\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "master moves")
        moved = git(self.repo, "rev-parse", "HEAD")
        self.assertIsNone(window_members.refusal(self.gate, self.repo, moved, self.entries, "w1", "x",
                                                 scope_of=self.scope_of, events=events))
        report("AC5", refusal=text[:60], negative_controls=sorted(controls) + ["moved-base"])

    def test_gate_hook_names_a_refusal_instead_of_base_incomplete(self):
        source = inspect.getsource(window_gate._run_attempt)
        self.assertIn("window_members.refusal", source)


class AC6Tests(Scratch):
    def rows(self, *specs):
        return [dict(type="member-verdict", subject=s, spec=s, window="w", tree=t * 40, state=state, url="", nodes=[],
                     ts=(NOW - timedelta(hours=h)).isoformat()) for s, t, state, h in specs]

    def test_latest_verdict_text(self):
        events = self.rows(("L-spec-1", "a", "green", 5), ("L-spec-1", "b", "undetermined", 2), ("L-spec-2", "c", "green", 130))
        self.assertIn("bbbbbbbb undetermined", window_members.verdict_text(events, "L-spec-1", NOW))
        self.assertIn("UNDETERMINED", window_members.verdict_text(events, "L-spec-1", NOW))
        self.assertNotIn("waiting for a window", window_members.verdict_text(events, "L-spec-1", NOW))
        self.assertIn("2h ago", window_members.verdict_text(events, "L-spec-1", NOW))
        text = window_members.verdict_text(events, "L-spec-2", NOW)
        self.assertIn("cccccccc green (verified alone, waiting for a window), 5d ago", text)
        self.assertIsNone(window_members.verdict_text(events, "L-spec-3", NOW))
        self.assertIsNone(window_members.verdict_text(events + [dict(type="shipped", subject="L-spec-2",
                                                                      ts=(NOW - timedelta(hours=1)).isoformat())], "L-spec-2", NOW))
        report("AC6", latest=text)

    def test_malformed_verdicts_are_ignored(self):
        bad = self.rows(("L-spec-1", "a", "green", 1))
        for change in ({"tree": "zz"}, {"state": "maybe"}, {"ts": "never"}, {"ts": "2026-11-03T10:00:00"}):
            self.assertIsNone(window_members.verdict_text([dict(bad[0], **change)], "L-spec-1", NOW), change)

    def test_look_rows_separate_waiting_from_undetermined(self):
        events = self.rows(("L-spec-1", "a", "green", 1), ("L-spec-2", "b", "undetermined", 1), ("L-spec-3", "c", "green", 1))
        specs = {"L-spec-1": dict(state="written"), "L-spec-2": dict(state="written"), "L-spec-3": dict(state="shipped")}
        rows = window_members.waiting_rows(events, specs, NOW)
        self.assertEqual([(r[1], r[3]) for r in rows], [("waiting", "INFO"), ("undetermined", "WARN")])
        self.assertIn("L-spec-1", rows[0][2])
        self.assertNotIn("L-spec-3", rows[0][2])
        self.assertIn("L-spec-2", rows[1][2])
        self.assertEqual(window_members.waiting_rows([], specs, NOW), [])


class AC7Tests(unittest.TestCase):
    def test_gate_growth_is_hooks_only_and_within_forty_lines(self):
        text = (SRC / "window_gate.py").read_text()
        self.assertLessEqual(len(text.splitlines()), 2099)
        base = subprocess.run(["git", "-C", str(REPO_ROOT), "show", "0f3f8fd55c1100457819de6b66dc34cb5d9b4826:src/window_gate.py"],
                              capture_output=True, text=True)
        if base.returncode == 0:
            self.assertEqual(len(base.stdout.splitlines()), 2059)
            self.assertLessEqual(len(text.splitlines()) - 2059, 40)
        for behaviour in ("ThreadPoolExecutor", "as_completed", "member_verify_parallel", "tomllib.load(stream).get(\"merge_queue\")"):
            self.assertNotIn(behaviour, text)
        self.assertGreaterEqual(text.count("window_members."), 3)
        report("AC7", window_gate_lines=len(text.splitlines()), cap=2099)

    def test_config_actor_registration_and_design_line(self):
        merge = window_members._table(REPO_ROOT / "look.toml")
        self.assertEqual((merge["member_verify_parallel"], merge["verdict_fresh_hours"]), (4, 24))
        self.assertLess(list(merge).index("batch_cap"), list(merge).index("member_verify_parallel"))
        import fold_emits
        self.assertEqual(fold_emits.EMITS["member-verdict"], {"executor", "operator"})
        self.assertEqual(fold_emits.REQUIRED_ROWS["member-verdict"], ("spec", "window", "tree", "state"))
        design = (REPO_ROOT / "docs/DESIGN.md").read_text()
        self.assertEqual(sum("member-verdict" in line for line in design.splitlines()), 1)

    def test_event_envelope_carries_no_actor_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "events").mkdir()
            saved = (fold.ROOT, fold.EVENTS, os.environ.get("DOIT_LEDGER_FILE"))
            fold.ROOT, fold.EVENTS = root, root / "events"
            try:
                window_gate.ev("member-verdict", "A", spec="A", window="w", tree="a" * 40, state="green",
                               url="", nodes=[], required=REQUIRED)
                raw = [json.loads(line) for line in (root / "events/L-executor-0001.jsonl").read_text().splitlines()]
            finally:
                fold.ROOT, fold.EVENTS = saved[0], saved[1]
                if saved[2] is None:
                    os.environ.pop("DOIT_LEDGER_FILE", None)
                else:
                    os.environ["DOIT_LEDGER_FILE"] = saved[2]
        self.assertEqual(len(raw), 1)
        self.assertNotIn("actor", raw[0])
        self.assertEqual((raw[0]["type"], raw[0]["subject"], raw[0]["project"], raw[0]["v"]), ("member-verdict", "A", "albert-scott", 1))


if __name__ == "__main__":
    unittest.main()
