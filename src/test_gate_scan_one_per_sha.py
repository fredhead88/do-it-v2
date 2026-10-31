"""Gate outcome accounting regressions, using disposable local state only."""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import realm
from test_realm_codex import signed


class GateAccountingTests(unittest.TestCase):
    def row(self, kind, timestamp, subject="L-spec-0001", **fields):
        return realm.compact(dict(type=kind, ts=timestamp, subject=subject, **fields), "executor")

    def metrics(self, rows, timestamp, states=()):
        result = realm.metrics_at(rows, states, {}, realm._parse(timestamp))
        return tuple(result[key] for key in ("gate", "gate_undetermined", "conflict"))

    def test_ac5_latest_outcome_and_past_snapshots(self):
        for reason in ("lock-busy", "deadline", "timeout", "worktree", "tool", None,
                       "synthetic diagnostic " * 20):
            with self.subTest(reason=reason):
                fields = {} if reason is None else {"reason": reason}
                row = self.row("merge-gate-undetermined", "2026-10-29T00:01:00+00:00",
                               **fields)
                self.assertIsNotNone(row)
                self.assertEqual({key: row[key] for key in ("t", "ty", "s", "a", "why")},
                                 dict(t="2026-10-29T00:01:00+00:00",
                                      ty="merge-gate-undetermined", s="L-spec-0001",
                                      a="executor", why=realm.trunc(reason, 160)))
                self.assertLessEqual(len(row["why"]), 160)
                if reason and len(reason) > 160:
                    self.assertEqual(row["why"], reason[:159] + "…")
        rows = [
            self.row("merge-gate-rework", "2026-10-29T00:00:00+00:00"),
            self.row("merge-gate-undetermined", "2026-10-29T00:01:00+00:00",
                     reason="secret-scan", findings=["scanner unavailable"]),
            self.row("merge-gate-clean", "2026-10-29T00:02:00+00:00"),
        ]
        self.assertTrue(all(rows), "compact must retain every gate outcome")
        self.assertEqual(rows[1]["why"], "secret-scan")
        for timestamp, expected in (
            ("2026-10-29T00:00:30Z", (1, 0, 0)),
            ("2026-10-29T00:01:30Z", (0, 1, 0)),
            ("2026-10-29T00:02:30Z", (0, 0, 0)),
            ("2026-10-29T12:02:30Z", (0, 0, 0)),
        ):
            with self.subTest(at=timestamp):
                self.assertEqual(self.metrics(rows, timestamp), expected)
                print("AC1 snapshot", timestamp, expected)
        self.assertEqual([row["ty"] for row in rows], [
            "merge-gate-rework", "merge-gate-undetermined", "merge-gate-clean"])
        signed(dict(criterion="AC1", compact=rows, snapshots=[
            dict(at=at, metrics=self.metrics(rows, at)) for at in (
                "2026-10-29T00:00:30Z", "2026-10-29T00:01:30Z",
                "2026-10-29T00:02:30Z", "2026-10-29T12:02:30Z")]))

    def test_ac5_distinct_subjects_cutoff_and_eligibility(self):
        stamp = "2026-10-29T00:01:00+00:00"
        rows = [self.row("merge-gate-undetermined", stamp, reason="secret-scan"),
                self.row("merge-gate-undetermined", "2026-10-29T00:02:00+00:00",
                         reason="secret-scan")]
        self.assertTrue(all(rows))
        self.assertEqual(self.metrics(rows, "2026-10-29T00:02:30Z"), (0, 1, 0))
        self.assertEqual(self.metrics(rows, "2026-10-29T00:00:59Z"), (0, 0, 0))
        rows.append(self.row("merge-gate-undetermined", "2026-10-29T00:03:00+00:00",
                             "L-spec-0002", reason="gate-tooling"))
        self.assertEqual(self.metrics(rows, "2026-10-29T00:03:30Z"), (0, 2, 0))
        for state in ("killed", "void", "dropped"):
            with self.subTest(state=state):
                self.assertEqual(self.metrics(rows, "2026-10-29T00:03:30Z",
                                              [("L-spec-0001", state)]), (0, 1, 0))
        self.assertEqual(self.metrics(rows[:1], "2026-10-29T12:00:59Z"), (0, 1, 0))
        self.assertEqual(self.metrics(rows[:1], "2026-10-29T12:01:00Z"), (0, 0, 0))
        self.assertEqual(self.metrics(rows[:1], "2026-10-29T12:01:01Z"), (0, 0, 0))
        unrelated = self.row("merge-gate-undetermined", stamp, "L-charter-0001",
                             reason="secret-scan")
        self.assertEqual(self.metrics([unrelated], "2026-10-29T00:02:00Z"), (0, 0, 0))
        self.assertEqual(self.metrics([], "2026-10-29T00:02:00Z"), (0, 0, 0))
        # The existing H24 activity filter still suppresses stale unfinished churn.
        stale = [self.row("verdict", f"2026-10-28T00:00:0{i}+00:00", confirmed=False)
                 for i in range(3)]
        at = realm._parse("2026-10-29T00:02:00Z")
        self.assertEqual(realm.metrics_at(stale, [], {}, at)["churn"], 0)
        active = stale + [self.row("merge-gate-undetermined", stamp, reason="lock-busy")]
        self.assertEqual(realm.metrics_at(active, [], {}, at)["churn"], 1)

    def test_ac5_rework_cutoff_and_repeated_conflict_policy(self):
        rework = self.row("merge-gate-rework", "2026-10-29T00:00:00+00:00",
                          why="actual merge conflict")
        self.assertEqual(self.metrics([rework], "2026-10-29T11:59:59Z"), (1, 0, 0))
        self.assertEqual(self.metrics([rework], "2026-10-29T12:00:00Z"), (0, 0, 0))
        self.assertEqual(self.metrics([rework], "2026-10-29T12:00:01Z"), (0, 0, 0))
        rows = [rework, self.row("merge-gate-rework", "2026-10-29T00:01:00+00:00",
                                 why="actual merge conflict"),
                self.row("merge-gate-undetermined", "2026-10-29T00:02:00+00:00",
                         reason="conflict-diagnostic")]
        self.assertTrue(all(rows))
        self.assertEqual(self.metrics(rows, "2026-10-29T00:02:30Z"), (0, 1, 1))
        self.assertEqual(self.metrics(rows, "2026-10-29T23:59:59Z"), (0, 0, 1))
        self.assertEqual(self.metrics(rows, "2026-10-30T00:00:00Z"), (0, 0, 0))
        self.assertEqual(self.metrics(rows, "2026-10-30T00:00:01Z"), (0, 0, 0))
        # Diagnostic text mentioning conflict cannot create a real conflict.
        self.assertEqual(self.metrics(rows[2:] * 2, "2026-10-29T00:02:30Z"), (0, 1, 0))

    def test_ac5_existing_regression_scripts(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        python = root / ".venv" / "bin" / "python"
        # Worktrees need not carry the project's ignored venv. Use its existing
        # interpreter when present; all these scripts also support stdlib Python.
        interpreter = str(python) if python.is_file() else sys.executable
        for name in ("test_realm.py", "test_realm_codex.py"):
            with self.subTest(script=name), tempfile.TemporaryDirectory() as temp:
                env = dict(os.environ, DOIT_ROOT=str(pathlib.Path(temp) / "root"),
                           DOIT_SCRATCH=str(pathlib.Path(temp) / "scratch"), TMPDIR=temp)
                env.pop("DOIT_LEDGER_FILE", None)
                result = subprocess.run([interpreter, str(root / "src" / name)], cwd=root,
                                        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, timeout=240)
                self.assertEqual(result.returncode, 0, result.stdout[-6000:])
                print("AC1 regression", name, "exit", result.returncode)

    def test_ac1_churn_ignores_undetermined(self):
        rows = [self.row("verdict", f"2026-10-29T00:00:0{i}+00:00", confirmed=False)
                for i in range(3)]
        rows += [self.row(kind, f"2026-10-29T00:0{i}:00+00:00", reason="lock-busy")
                 for i, kind in enumerate(("merge-gate-clean", "merge-gate-undetermined",
                                           "merge-gate-rework", "merge-gate-undetermined",
                                           "merge-gate-clean"), 1)]
        self.assertTrue(all(rows))
        for minute, expected in enumerate(((0, 0, 0), (0, 1, 0), (1, 0, 1),
                                            (0, 1, 1), (0, 0, 0)), 1):
            at = f"2026-10-29T00:0{minute}:30Z"
            with self.subTest(at=at):
                metrics = realm.metrics_at(rows, [("L-spec-0001", "reviewing")], {},
                                           realm._parse(at))
                self.assertEqual(tuple(metrics[k] for k in ("gate", "gate_undetermined", "churn")),
                                 expected)
                print("AC1 churn snapshot", at, expected)
        # Existing verdict and terminal-state eligibility still governs churn.
        for state in ("shipped", "accepted", "killed", "void", "dropped"):
            self.assertEqual(realm.metrics_at(rows, [("L-spec-0001", state)], {},
                                             realm._parse("2026-10-29T00:03:30Z"))["churn"], 0)
        for prefix in (rows[1:], [dict(r, ok=True) if r["ty"] == "verdict" else r for r in rows]):
            self.assertEqual(realm.metrics_at(prefix, [], {},
                                             realm._parse("2026-10-29T00:03:30Z"))["churn"], 0)

    def test_ac2_page_gate_matches_server(self):
        rows = [self.row(kind, f"2026-10-29T00:0{i}:00+00:00", reason="lock-busy")
                for i, kind in enumerate(("merge-gate-rework", "merge-gate-undetermined",
                                          "merge-gate-clean", "merge-gate-rework"))]
        self.assertTrue(all(rows), "compact must retain every page fixture outcome")
        fixtures = []
        for at, expected in (("00:00:30", (1, 0)), ("00:01:30", (0, 1)),
                             ("00:02:30", (0, 0)), ("00:03:30", (1, 0)),
                             ("12:02:59", (1, 0)), ("12:03:00", (0, 0)),
                             ("12:03:01", (0, 0))):
            fixtures.append(dict(at=f"2026-10-29T{at}Z", rows=rows, states=[], expected=expected))
        for state in ("killed", "void", "dropped"):
            fixtures.append(dict(at="2026-10-29T00:03:30Z", rows=rows,
                                 states=[("L-spec-0001", state)], expected=(0, 0)))
        fixtures.append(dict(at="2026-10-29T00:03:30Z",
                             rows=[dict(r, s="L-charter-0001") for r in rows],
                             states=[], expected=(0, 0)))
        # Two subjects exercise the alarm's aggregate count as well as membership.
        fixtures.append(dict(at="2026-10-29T00:03:30Z",
                             rows=sorted(rows + [dict(rows[-1], s="L-spec-0002")], key=lambda r: r["t"]),
                             states=[], expected=(2, 0)))
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, DOIT_ROOT=temp), \
                mock.patch.object(tempfile, "tempdir", temp):
            from test_realm import _gate_page_snapshots
            page_results = _gate_page_snapshots(fixtures)
        self.assertEqual(len(page_results), len(fixtures))
        observed = []
        for fixture, subjects in zip(fixtures, page_results):
            with self.subTest(at=fixture["at"], states=fixture["states"]):
                metrics = realm.metrics_at(fixture["rows"], fixture["states"], {},
                                           realm._parse(fixture["at"]))
                self.assertEqual((metrics["gate"], metrics["gate_undetermined"]), fixture["expected"])
                self.assertEqual(metrics["gate"], len(subjects))
                expected_subjects = (["L-spec-0001", "L-spec-0002"] if metrics["gate"] == 2
                                     else ["L-spec-0001"] if metrics["gate"] else [])
                self.assertEqual(subjects, expected_subjects)
                print("AC2 page/server", fixture["at"], fixture["states"], fixture["expected"], subjects)
                observed.append(dict(at=fixture["at"], states=fixture["states"],
                                     gate=metrics["gate"], gate_undetermined=metrics["gate_undetermined"],
                                     subjects=subjects))
        signed(dict(criterion="AC2", observations=observed))


if __name__ == "__main__":
    unittest.main()
