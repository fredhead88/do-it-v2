#!/usr/bin/env python3
"""Offline regressions for GitHub's unexpanded skipped-shard job names."""
import unittest

import test_master_full as fixtures
import master_full


# Run 43616937139's four executed and four skipped jobs. Expression bodies
# are abbreviated, as in the captured job listing; the collector never evals them.
RAW_PLAN = "${{ (((((vars.CI_PYTEST_SHARDS ... }}"


def run_jobs():
    return [dict(name="pytest", conclusion="success"),
            dict(name="pg-parity (Postgres)", conclusion="success", steps=[])] + [
        dict(name=f"pytest — shard {n} of {4 if n <= 4 else RAW_PLAN}",
             conclusion="success" if n <= 4 else "skipped")
        for n in range(1, 9)]


class ShardNameTests(unittest.TestCase):
    receipt = fixtures.MasterFixture.receipt

    def assert_all_required(self, jobs):
        self.assertEqual(master_full._active_shards(jobs), jobs[2:])

    def test_real_four_shard_plan(self):
        jobs = run_jobs()
        active = master_full._active_shards(jobs)
        self.assertEqual(active, jobs[2:6])
        self.receipt("AC1-plan", dict(active=[j["name"] for j in active],
                                     inactive=4, reason="skipped above expanded plan 4"))

    def test_expanded_and_mixed_inactive_names(self):
        for expanded in ((5, 6, 7, 8), (5, 7)):
            with self.subTest(expanded=expanded):
                jobs = run_jobs()
                for n in expanded:
                    jobs[n + 1]["name"] = f"pytest — shard {n} of 4"
                self.assertEqual(master_full._active_shards(jobs), jobs[2:6])

    def test_no_expanded_name_proves_plan(self):
        jobs = run_jobs()
        for n, job in enumerate(jobs[2:], 1):
            job["name"] = f"pytest — shard {n} of {RAW_PLAN}"
        self.assert_all_required(jobs)

    def test_unexpanded_job_must_be_proven_inactive(self):
        conclusions = ["cancelled", "failure", "timed_out", "success", None, "missing"]
        for conclusion in conclusions:
            with self.subTest(conclusion=conclusion):
                jobs = run_jobs()
                if conclusion == "missing":
                    jobs[7].pop("conclusion")
                else:
                    jobs[7]["conclusion"] = conclusion
                self.assert_all_required(jobs)
        self.receipt("AC1-required", dict(conclusions=conclusions, required=8))

    def test_unexpanded_required_number_keeps_every_shard(self):
        jobs = run_jobs()
        jobs[5] = dict(name=f"pytest — shard 4 of {RAW_PLAN}", conclusion="skipped")
        self.assert_all_required(jobs)

    def test_unknown_and_multiline_names_fail_closed(self):
        names = ["pytest — shard api-2", "pytest — shard 6 of ${{ unfinished",
                 f"pytest — shard 6 of {RAW_PLAN}\n",
                 "pytest — shard 6 of ${{ vars.CI_PYTEST_SHARDS\n }}",
                 "pytest — shard 6 of ${{ vars.CI_PYTEST_SHARDS\r }}",
                 f"pytest — shard \u0666 of {RAW_PLAN}",
                 f"pytest — shard 6 of {RAW_PLAN} suffix"]
        for name in names:
            with self.subTest(name=name):
                jobs = run_jobs()
                jobs[7]["name"] = name
                self.assert_all_required(jobs)

    def test_plan_validation_still_fails_closed(self):
        for plan in (0, 8, 9):
            with self.subTest(plan=plan):
                jobs = run_jobs()
                jobs[2]["name"] = f"pytest — shard 1 of {plan}"
                self.assert_all_required(jobs)
        jobs = run_jobs()
        del jobs[4]  # Required shard 3 is absent, despite the expanded plan.
        self.assert_all_required(jobs)
        self.assertEqual(master_full._active_shards([]), [])


class ShardCollectionTests(fixtures.MasterFixture):
    def collect(self, rid, *, failing=False, cancelled=False):
        ids = [fixtures.PY] if failing else []
        self.ready(rid=rid, ids=ids, conclusion="failure" if failing else "success")
        self.jobs[1] = run_jobs()
        if failing:
            self.jobs[1][0]["conclusion"] = "failure"
            self.jobs[1][2]["conclusion"] = "failure"
        if cancelled:
            self.jobs[1][4]["conclusion"] = "cancelled"
        self.tick()
        return self.rows("master-full-run")[-1]

    def test_full_green_and_red_rows_complete(self):
        results = []
        for rid, failing in enumerate((False, True), 1):
            with self.subTest(failing=failing):
                row = self.collect(rid, failing=failing)
                self.assertTrue(row["complete"], row)
                self.assertEqual(row["green"], not failing)
                self.assertEqual(row["ids"], [fixtures.PY] if failing else [])
                self.assertEqual(row["covered"], ["pg-parity", "pytest"])
                self.assertEqual(row["undetermined"], [])
                results.append(dict(complete=row["complete"], green=row["green"],
                                    ids=row["ids"], undetermined=row["undetermined"]))
        self.receipt("AC2-complete", results)

    def test_cancelled_required_shard_stays_incomplete(self):
        row = self.collect(1, cancelled=True)
        self.assertFalse(row["complete"])
        self.assertFalse(row["green"])
        self.assertIn("1 pytest shards did not execute completely: "
                      "pytest — shard 3 of 4: cancelled", row["undetermined"])
        self.assertFalse(any("${{" in why for why in row["undetermined"]))
        self.receipt("AC2-cancelled", dict(complete=row["complete"], green=row["green"],
                                         undetermined=row["undetermined"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
