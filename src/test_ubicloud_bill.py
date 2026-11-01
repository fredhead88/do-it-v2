"""Cost fixtures: no GitHub, Ubicloud, Telegram or live ledger calls."""
from datetime import date, timedelta
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gh_bill


def job(identity, day, seconds=61, label="ubicloud-standard-2"):
    return {"id": identity, "labels": ["self-hosted", label], "status": "completed",
            "started_at": f"{day}T00:00:00Z",
            "completed_at": f"{day}T00:{seconds // 60:02}:{seconds % 60:02}Z"}


class Costs(unittest.TestCase):
    def module(self):
        return importlib.import_module("ubicloud_bill")

    def test_minutes_credit_month_boundary_and_duplicates(self):
        m = self.module()
        jobs = [job(1, "2026-10-30", 60), job(2, "2026-10-31"),
                job(3, "2026-10-31", 1), job(4, "2026-10-31", label="ubuntu-latest")]
        # A dollar/minute fixture makes credit consumption and per-job rounding visible.
        result = m.runner_cost(jobs + jobs[:1], date(2026, 11, 1),
                               {"ubicloud-standard-2": 1}, 2.5)
        self.assertEqual(result["mtd"], 1.5)
        self.assertEqual(result["yday"], 1.5)
        self.assertEqual(result["gross_mtd"], 4)
        self.assertEqual(result["projection"], 1.5)
        result = m.runner_cost(jobs, date(2026, 10, 31), {"ubicloud-standard-2": 1}, 2.5)
        self.assertEqual(result["yday"], 0)
        self.assertEqual(result["projection"], 0)
        with self.assertRaises(ValueError):
            m.runner_cost([job(1, "2026-10-31", label="ubicloud-new-8")],
                          date(2026, 11, 1), {"ubicloud-standard-2": 1}, 2.5)
        with self.assertRaises(ValueError):
            m.runner_cost([dict(jobs[0], completed_at=None)], date(2026, 11, 1),
                          {"ubicloud-standard-2": 1}, 2.5)

    def test_paginated_runs_jobs_and_old_rerun(self):
        m = self.module()
        calls = []
        runs = [{"id": n, "updated_at": "2026-10-31T01:00:00Z"} for n in range(100)]
        old = {"id": 999, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-10-31T02:00:00Z"}
        def api(argv, **kwargs):
            calls.append(argv[-1])
            endpoint = argv[-1]
            if "/actions/runs?" in endpoint:
                return 0, json.dumps({"workflow_runs": runs if "page=1&" in endpoint else [old]}), ""
            self.assertIn("filter=all", endpoint)
            return 0, json.dumps({"jobs": [job(int(endpoint.split('/runs/')[1].split('/')[0]), "2026-10-31")]}), ""
        with patch("ghlimit.run", side_effect=api):
            jobs = m.read_jobs(["owner/repo"], date(2026, 11, 1))
        self.assertEqual(len(jobs), 101)
        self.assertIn(999, [j["id"] for j in jobs])
        self.assertTrue(all(a.startswith("/repos/owner/repo/actions/runs") for a in calls))
        with patch("ghlimit.run", return_value=(1, "secret fixture", "private error")):
            with self.assertRaisesRegex(ValueError, "GitHub jobs read failed"):
                m.read_jobs(["owner/repo"], date(2026, 11, 1))

    def test_resources_get_pagination_prices_and_secret_redaction(self):
        m = self.module()
        calls = []
        class Reply(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        def get(request, timeout):
            calls.append(request)
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Authorization"), "Bearer fixture-token")
            if "/vm?" in request.full_url:
                rows = [] if "start_after" in request.full_url else [{"id": "v1", "size": "standard-2"}]
            else:
                rows = [] if "start_after" in request.full_url else [{"id": "p1", "vm_size": "standard-2", "flavor": "standard", "target_server_count": 2}]
            return Reply(json.dumps({"count": len(rows), "items": rows}).encode())
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = {"ubicloud_project_id": "project-1", "ubicloud_resource_monthly":
                   {"vm.standard-2": 31, "postgres.standard-2": 62}}
            self.assertEqual(m.resource_cost(cfg, root, date(2026, 11, 1))["status"], "not configured")
            (root / "private").mkdir()
            (root / "private/ubicloud-token").write_text("fixture-token\n")
            with patch("urllib.request.OpenerDirector.open", side_effect=get):
                result = m.resource_cost(cfg, root, date(2026, 11, 1))
            self.assertEqual((result["yday"], result["mtd"], result["projection"]), (5, 155, 155))
            self.assertEqual(len(calls), 4)
            with patch("urllib.request.OpenerDirector.open", side_effect=OSError("fixture-token")):
                with self.assertRaisesRegex(ValueError, "Ubicloud resources read failed") as exc:
                    m.resource_cost(cfg, root, date(2026, 11, 1))
            self.assertNotIn("fixture-token", str(exc.exception))

    def test_command_combined_alert_and_no_network(self):
        # This command-level regression also fails on the old CLI's unknown --jobs-file.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = root / "bill.toml"
            cfg.write_text('default=450\nafter_r1=120\ncombined_daily_threshold=8.0\n'
                           'combined_monthly_threshold=300.0\nubicloud_monthly_credit=2.5\n'
                           'ubicloud_repos=["owner/repo"]\n[ubicloud_runner_prices]\nubicloud-standard-2=1.0\n')
            today = date.today()
            yesterday = today - timedelta(days=1)
            jobs_file = root / "jobs.json"
            jobs_file.write_text(json.dumps({"jobs": [job(n, yesterday.isoformat()) for n in range(10)]}))
            usage_file = root / "usage.json"
            usage_file.write_text(json.dumps({"usageItems": [{"date": yesterday.isoformat(), "netAmount": 1}]}))
            output, log, messages = io.StringIO(), io.StringIO(), []
            with patch.dict(os.environ, DOIT_ROOT=td), patch.object(gh_bill, "CONFIG", cfg), \
                 patch.object(gh_bill, "_runner_inputs", return_value=(None, {"CI_SELF_HOSTED": "false"}, True, True, 8, "")), \
                 patch.object(gh_bill, "_send", side_effect=messages.append), \
                 patch("ghlimit.run", side_effect=AssertionError("network forbidden")), \
                 patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden")), \
                 patch("sys.stdout", output), patch("sys.stderr", log):
                rc = gh_bill.main(["--usage-file", str(usage_file), "--jobs-file", str(jobs_file)])
            self.assertEqual(rc, 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["total_yday"], 18.5)
            self.assertTrue(result["combined_alert"])
            self.assertIn("ubicloud resources: not configured", log.getvalue())
            self.assertIn("github $1.00, ubicloud runners $17.50", log.getvalue())
            self.assertIn("Combined", messages[-1])
            self.assertIn("18.50", messages[-1])
            recorded = json.loads((root / "events/L-gh-bill-local.jsonl").read_text().splitlines()[-1])
            self.assertEqual(recorded["total_yday"], 18.5)
            self.assertIs(recorded["combined_alert"], True)
            # The combined daily policy also covers a cheap day with an expensive month projection.
            projected = dict(result, total_yday=1, total_projection=301)
            self.assertIn("301", self.module().alert_message(projected))

    def test_projection_alert_and_unavailable_totals(self):
        m = self.module()
        decision = {"yday": 1, "mtd": 299, "projection": 301}
        zero = {"yday": 0, "mtd": 0, "projection": 0, "status": "not configured"}
        with patch.object(m, "collect", return_value=(zero, zero)):
            m.combine(decision, {}, Path("/unused"), date(2026, 11, 1))
        self.assertTrue(decision["combined_alert"])
        self.assertEqual(decision["total_yday"], 1)
        with patch.object(m, "collect", side_effect=ValueError("secret-token")):
            log = m.combine(decision, {}, Path("/unused"), date(2026, 11, 1))
        self.assertFalse(decision["combined_alert"])
        self.assertIsNone(decision["total_yday"])
        self.assertIsNone(decision["total_projection"])
        self.assertNotIn("secret-token", log + json.dumps(decision))
        self.assertIn("unavailable", log)

    def test_invalid_prices_fail_config_validation(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bill.toml"
            for invalid in ('-1', 'true', 'nan'):
                path.write_text('default=450\nafter_r1=120\n[ubicloud_runner_prices]\nubicloud-standard-2=' + invalid)
                self.assertIsNone(gh_bill.load_limits(path))


if __name__ == "__main__":
    unittest.main()
