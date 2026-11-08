"""Recognize transient CI setup failures without hiding candidate failures."""
import json
from pathlib import Path
import re
import subprocess


def setup_only(jobs: list | None) -> tuple[bool, str]:
    if not isinstance(jobs, list):
        return False, ""
    reasons = []
    for job in jobs:
        if not isinstance(job, dict):
            return False, ""
        if job.get("conclusion") != "failure":
            continue
        name = job.get("name", "")
        if name == "pytest":
            continue
        if not isinstance(name, str) or not name.startswith("pytest — shard"):
            return False, ""
        steps = job.get("steps")
        if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
            return False, ""
        first = next((i for i, step in enumerate(steps) if step.get("conclusion") == "failure"), None)
        tests = [i for i, step in enumerate(steps) if step.get("name") == "Run tests"]
        if first is None or len(tests) != 1 or tests[0] <= first or steps[tests[0]].get("conclusion") != "skipped":
            return False, ""
        failed = steps[first].get("name", "")
        if not isinstance(failed, str) or not (failed == "Install uv" or failed.startswith((
                "Run actions/setup-python", "Run actions/checkout", "Run actions/setup-node",
                "Run actions/download-artifact"))):
            return False, ""
        reasons.append(f"{name}: {failed}")
    return bool(reasons), "; ".join(reasons)


def jobs_for(repo: Path, url: str) -> list | None:
    if not isinstance(url, str):
        return None
    match = re.fullmatch(r"https?://[^/\s]+/[^/\s]+/[^/\s]+/actions/runs/(\d+)(?:[/?#][^\s]*)?", url)
    if not match:
        return None
    try:
        proc = subprocess.run(["gh", "run", "view", match[1], "--json", "jobs"],
                              cwd=repo, capture_output=True, text=True, timeout=60)
        if proc.returncode:
            return None
        doc = json.loads(proc.stdout)
        jobs = doc.get("jobs") if isinstance(doc, dict) else None
        return jobs if isinstance(jobs, list) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def verify_with_rerun(once, ev, repo, cand, required, window, *, rehearsal=False):
    """Retry a transient setup-only CI failure once on the same candidate."""
    def setup_reason(result):
        if result.get("state") == "green" or result.get("nodes") or not result.get("url"):
            return ""
        checks = result.get("doc", {}).get("checks", {})
        runs = {str(c["run_id"]) for c in checks.values()
                if isinstance(c, dict) and c.get("run_id") is not None} if isinstance(checks, dict) else set()
        if len(runs) > 1:
            return ""
        matched, why = setup_only(jobs_for(repo, result["url"]))
        return why if matched else ""

    first = once(repo, cand, required, window, rehearsal=rehearsal)
    why = setup_reason(first)
    if not why:
        return first
    ev("ci-setup-rerun", window, window=window, candidate=cand, run_url=first["url"], why=why)
    second = once(repo, cand, required, window, rehearsal=rehearsal)
    second = {**second, "setup_rerun_of": first["url"]}
    why = setup_reason(second)
    if why:
        second.update(state="undetermined", why="infra: CI setup failed twice: " + why)
    return second
