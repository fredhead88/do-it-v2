"""Ubicloud estimates from read-only inventories, never a billing API.

Jobs are charged to their UTC completion day, rounded up individually. The
monthly runner credit is shared across repositories. Resource figures assume
the current inventory runs all month at configured prices; they are estimates,
excluding deleted resources, extra storage and traffic.
"""
import calendar
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from urllib.parse import quote
import urllib.request


def amount(value):
    if isinstance(value, bool):
        raise ValueError("invalid Ubicloud price")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("invalid Ubicloud price")
    return number


def validate(cfg):
    for key in ("combined_daily_threshold", "combined_monthly_threshold", "ubicloud_monthly_credit"):
        if key in cfg:
            amount(cfg[key])
    for key in ("ubicloud_runner_prices", "ubicloud_resource_monthly"):
        if key in cfg:
            if not isinstance(cfg[key], dict):
                raise ValueError("invalid Ubicloud prices")
            for value in cfg[key].values():
                amount(value)
    if "ubicloud_repos" in cfg and (not isinstance(cfg["ubicloud_repos"], list)
            or not cfg["ubicloud_repos"] or any(not isinstance(r, str) or len(r.split('/')) != 2
                                              for r in cfg["ubicloud_repos"])):
        raise ValueError("invalid Ubicloud repositories")
    if "ubicloud_project_id" in cfg and not isinstance(cfg["ubicloud_project_id"], str):
        raise ValueError("invalid Ubicloud project")


def stamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("job timestamp lacks timezone")
    return dt.astimezone(timezone.utc)


def runner_cost(jobs, today, prices, credit):
    yesterday = today - timedelta(days=1)
    month = yesterday.replace(day=1)
    first = min(month, yesterday - timedelta(days=6))
    daily, seen = {}, set()
    if not isinstance(jobs, list):
        raise ValueError("invalid jobs fixture")
    for job in jobs:
        identity = (job.get("repo", ""), job["id"])
        if identity in seen:
            continue
        seen.add(identity)
        labels = [s for s in job["labels"] if "ubicloud" in s.lower()]
        if not labels:
            continue
        # Skipped/queued jobs without a start consumed no runner minutes.
        if not job.get("started_at") and (job.get("conclusion") == "skipped" or job.get("status") == "queued"):
            continue
        start = stamp(job["started_at"])
        if start.date() >= today:
            continue
        if not job.get("completed_at"):
            raise ValueError("Ubicloud job still unfinished at billing cutoff")
        end = stamp(job["completed_at"])
        if end.date() >= today:
            continue
        if end < start:
            raise ValueError("invalid Ubicloud job duration")
        if end.date() < first:
            continue
        matches = [amount(prices[label]) for label in labels if label in prices]
        if len(matches) != 1:
            raise ValueError("Ubicloud runner label has no unique configured price")
        minutes = math.ceil((end - start).total_seconds() / 60)
        daily[end.date()] = daily.get(end.date(), 0) + minutes * matches[0]
    gross = sum(v for d, v in daily.items() if month <= d <= yesterday)
    credit = amount(credit)
    net = max(0, gross - credit)
    before = max(0, gross - daily.get(yesterday, 0) - credit)
    days = calendar.monthrange(yesterday.year, yesterday.month)[1]
    pace = sum(daily.get(yesterday - timedelta(days=i), 0) for i in range(7)) / 7
    result = {"yday": net - before, "mtd": net, "gross_mtd": gross,
              "projection": max(0, gross + pace * (days - yesterday.day) - credit)}
    if not all(math.isfinite(v) for v in result.values()):
        raise ValueError("Ubicloud arithmetic overflow")
    return result


def _gh(endpoint):
    import ghlimit
    rc, out, _ = ghlimit.run(["gh", "api", endpoint], wait=False)
    if rc:
        raise ValueError("GitHub jobs read failed")
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        raise ValueError("GitHub jobs response invalid") from None


def _pages(endpoint, key):
    page = 1
    while True:
        payload = _gh(f"{endpoint}{'&' if '?' in endpoint else '?'}page={page}&per_page=100")
        rows = payload.get(key)
        if not isinstance(rows, list):
            raise ValueError("GitHub jobs response missing list")
        yield from rows
        if len(rows) < 100:
            break
        page += 1


def read_jobs(repos, today):
    yesterday = today - timedelta(days=1)
    first = min(yesterday.replace(day=1), yesterday - timedelta(days=6))
    jobs = []
    for repo in dict.fromkeys(repos):
        base = f"/repos/{repo}/actions/runs"
        # Enumerate run metadata without GitHub's search filters/1000-result
        # cap. An old run can be rerun this month, so created_at is unsafe.
        for run in _pages(base, "workflow_runs"):
            if stamp(run["updated_at"]).date() < first:
                continue
            for job in _pages(f"{base}/{int(run['id'])}/jobs?filter=all", "jobs"):
                jobs.append(dict(job, repo=repo))
    return jobs


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # A bearer credential must never follow an inventory redirect.
        raise ValueError("Ubicloud resources read failed")


def resource_cost(cfg, root, today):
    project = cfg.get("ubicloud_project_id")
    token_path = Path(root) / "private/ubicloud-token"
    if not project or not token_path.is_file():
        return {"status": "not configured", "yday": 0, "mtd": 0, "projection": 0}
    try:
        token = token_path.read_text().strip()
        if not token:
            raise ValueError("empty credential")
        monthly = 0
        prices = cfg.get("ubicloud_resource_monthly", {})
        # Keep the credential inside requests, never in argv, receipts or errors.
        opener = urllib.request.build_opener(_NoRedirect())
        for kind in ("vm", "postgres"):
            cursor, seen = "", set()
            while True:
                url = f"https://api.ubicloud.com/project/{quote(project, safe='')}/{kind}?page_size=100"
                if cursor:
                    url += "&start_after=" + quote(cursor, safe="")
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
                with opener.open(req, timeout=30) as response:
                    payload = json.load(response)
                rows = payload["items"]
                if not isinstance(rows, list):
                    raise ValueError("invalid inventory")
                if not rows:
                    break
                for row in rows:
                    if row["id"] in seen:
                        raise ValueError("inventory pagination repeated")
                    seen.add(row["id"])
                    size = row["size"] if kind == "vm" else row["vm_size"]
                    # VM sizes already include family; Postgres uses its flavor.
                    if kind == "postgres" and row.get("flavor") == "hobby":
                        size = "hobby-" + size.rsplit("-", 1)[-1]
                    count = row.get("target_server_count", 1) if kind == "postgres" else 1
                    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                        raise ValueError("invalid resource count")
                    monthly += amount(prices[f"{kind}.{size}"]) * count
                cursor = rows[-1]["id"]
        yesterday = today - timedelta(days=1)
        per_day = monthly / calendar.monthrange(yesterday.year, yesterday.month)[1]
        return {"status": "current inventory estimate", "yday": per_day,
                "mtd": per_day * yesterday.day, "projection": monthly}
    except Exception:
        # No exception payload: providers and HTTP clients can echo credentials.
        raise ValueError("Ubicloud resources read failed (inventory or price unavailable)") from None


def collect(cfg, root, today, jobs_file=None):
    jobs = json.loads(Path(jobs_file).read_text())["jobs"] if jobs_file else read_jobs(
        cfg.get("ubicloud_repos", ["fredhead88/albert-scott-platform", "fredhead88/do-it-v2"]), today)
    runners = runner_cost(jobs, today, cfg["ubicloud_runner_prices"], cfg.get("ubicloud_monthly_credit", 2.5))
    resources = resource_cost(cfg, root, today)
    return runners, resources


def combine(decision, cfg, root, today, jobs_file=None):
    """Attach estimates without changing the GitHub-only runner-switch policy."""
    decision.update(ubicloud_runners_yday=None, ubicloud_runners_mtd=None,
                    ubicloud_resources_yday=None, ubicloud_resources_mtd=None,
                    total_yday=None, total_mtd=None, total_projection=None, combined_alert=False)
    try:
        runners, resources = collect(cfg, root, today, jobs_file)
        for name, data in (("runners", runners), ("resources", resources)):
            for key in ("yday", "mtd"):
                decision[f"ubicloud_{name}_{key}"] = data[key]
        decision["ubicloud_resources_status"] = resources["status"]
        for key in ("yday", "mtd", "projection"):
            decision["total_" + key] = decision[key] + runners[key] + resources[key]
            if not math.isfinite(decision["total_" + key]):
                raise ValueError("combined arithmetic overflow")
        decision["combined_alert"] = (decision["total_yday"] > cfg.get("combined_daily_threshold", 8.0)
                                      or decision["total_projection"] > cfg.get("combined_monthly_threshold", 300.0))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        for key in ("total_yday", "total_mtd", "total_projection"):
            decision[key] = None
        decision["ubicloud_error"] = "combined cost unavailable: check Ubicloud inventory, jobs and configured prices"
    def money(value):
        return "unavailable" if value is None else f"${value:.2f}"
    return (f"github {money(decision['yday'])}, ubicloud runners {money(decision['ubicloud_runners_yday'])}, "
            f"ubicloud resources {money(decision['ubicloud_resources_yday'])}, total {money(decision['total_yday'])}, "
            f"month projection {money(decision['total_projection'])}; ubicloud resources: "
            + decision.get("ubicloud_resources_status", "unavailable"))


def alert_message(decision):
    return (f"Combined cloud cost estimate: yesterday ${decision['total_yday']:.2f}; "
            f"month projection ${decision['total_projection']:.2f}. "
            f"GitHub ${decision['yday']:.2f}, Ubicloud runners ${decision['ubicloud_runners_yday']:.2f}, "
            f"Ubicloud resources ${decision['ubicloud_resources_yday']:.2f} "
            f"({decision['ubicloud_resources_status']}). Check recent runs and always-on resources.")
