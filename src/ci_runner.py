#!/usr/bin/env python3
"""Read runner health and change the CI switch through the shadow gate."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import ghlimit

VARIABLES = {"CI_SELF_HOSTED": "ci", "CI_PG_SELF_HOSTED": "pg"}
TRIGGERS = ("manual", "bill-flip", "rollback")
DOIT = Path(__file__).resolve().parents[1] / "doit"


def _repo():
    return os.environ.get("CI_RUNNER_REPO", "fredhead88/albert-scott-platform")


def _gh(*args):
    if os.environ.get("CI_RUNNER_GH"):
        os.environ["DOIT_REAL_GH"] = os.environ["CI_RUNNER_GH"]
    return ghlimit.run(["gh", *args], wait=False)


def _state(variable):
    rc, out, err = _gh("api", f"repos/{_repo()}/actions/variables/{variable}", "--jq", ".value")
    if rc == 0:
        value = out.strip()
        return value if value in ("true", "false") else None
    if rc != 75 and re.search(r"\bHTTP 404\b", err):
        # A private/unreadable repository can also return 404; confirm access.
        repo_rc, repo_out, _ = _gh("api", f"repos/{_repo()}", "--jq", ".full_name")
        if repo_rc == 0 and repo_out.strip() == _repo():
            return "false"
    return None


def state():
    return {variable: _state(variable) for variable in VARIABLES}


def _rows(path, key):
    rc, out, _ = _gh("api", path, "--paginate")
    if rc:
        raise ValueError(f"{key} API unreadable (exit {rc})")
    try:
        source = out.lstrip()
        decoder, pages = json.JSONDecoder(), []
        while source:
            page, end = decoder.raw_decode(source)
            pages.append(page)
            source = source[end:].lstrip()
        if not pages:
            raise ValueError("missing pages")
        rows = []
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get(key), list):
                raise ValueError("missing list")
            for row in page[key]:
                if not isinstance(row, dict):
                    raise ValueError("invalid row")
                rows.append(row)
        return rows
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{key} API returned invalid JSON data") from exc


def _timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return result


def healthy(now=None):
    now = now if now is not None else datetime.now(timezone.utc)
    try:
        online = 0
        runners = []
        for runner in _rows(f"repos/{_repo()}/actions/runners?per_page=100", "runners"):
            labels = runner["labels"]
            if (not isinstance(labels, list) or not isinstance(runner["name"], str)
                    or not isinstance(runner["status"], str) or not isinstance(runner["busy"], bool)
                    or any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                           for label in labels)):
                raise ValueError("invalid runner name, labels, status or busy")
            if any(label["name"] == "as-ci" for label in labels):
                runners.append({key: runner[key] for key in ("name", "status", "busy")})
                online += runner["status"] == "online"
        stuck = 0
        runs = _rows(f"repos/{_repo()}/actions/runs?status=queued&per_page=100", "workflow_runs")
        for run in runs:
            run_id = run["id"]
            if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
                raise ValueError("invalid queued run id")
            for job in _rows(f"repos/{_repo()}/actions/runs/{run_id}/jobs?per_page=100", "jobs"):
                if job["status"] != "queued":
                    continue
                labels = job["labels"]
                if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
                    raise ValueError("invalid queued job labels")
                if any(label in ("self-hosted", "as-ci") for label in labels):
                    if now - _timestamp(job["created_at"]) > timedelta(minutes=15):
                        stuck += 1
        reasons = []
        if not online:
            reasons.append("no as-ci runners online (offline or absent)")
        if stuck:
            reasons.append(f"{stuck} self-hosted job(s) queued over 15 minutes")
        return {"healthy": bool(online and not stuck), "online": online, "stuck_queued": stuck,
                "reason": "; ".join(reasons) or "as-ci runner online; no stuck queued self-hosted jobs",
                "runners": runners}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return {"healthy": None, "online": 0, "stuck_queued": 0,
                "reason": f"runner health undetermined: {exc}", "runners": []}


def shadow_passed(events, kind):
    matching = [event for event in events
                if event.get("type") == "ci-shadow-passed" and event.get("kind") == kind]
    return max(matching, key=lambda event: _timestamp(event["ts"]), default=None)


def _kept_events():
    import fold
    history = fold.fold(fold.read_events())[3]
    return [event for events in history.values() for event in events]


def set(variable, value, *, why, trigger):
    if variable not in VARIABLES or value not in ("true", "false") or trigger not in TRIGGERS:
        raise SystemExit(2)
    previous = _state(variable)
    result = {"ok": False, "variable": variable, "from": previous or "unset", "to": value, "reason": ""}
    if value == "true":
        kind = VARIABLES[variable]
        try:
            passed = shadow_passed(_kept_events(), kind)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            result["reason"] = f"{kind} shadow evidence unreadable: {exc}"
            return result
        if passed is None:
            result["reason"] = f"missing ci-shadow-passed for kind {kind}"
            return result
    rc, _, _ = _gh("variable", "set", variable, "--repo", _repo(), "--body", value)
    if rc:
        result["reason"] = f"variable set failed (exit {rc}); change not verified"
        return result
    if _state(variable) != value:
        result["reason"] = "variable re-read did not verify requested value; no event appended"
        return result
    fields = {"variable": variable, "from": result["from"], "to": value, "why": why, "trigger": trigger}
    # Encode every value as JSON so fold.append preserves strings like "true".
    argv = [str(DOIT), "append", "ci-runner-set", "ci-runner"]
    argv.extend(f"{key}={json.dumps(item)}" for key, item in fields.items())
    try:
        appended = subprocess.run(argv, env=dict(os.environ, DOIT_LEDGER_FILE="L-ci-runner-local.jsonl"),
                                  capture_output=True, text=True)
    except OSError:
        result["reason"] = "variable changed and verified, but ledger append could not run"
        return result
    if appended.returncode:
        result["reason"] = "variable changed and verified, but ledger append failed"
        return result
    result.update(ok=True, reason="change verified and recorded")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true")
    change = commands.add_parser("set")
    change.add_argument("variable", choices=VARIABLES)
    change.add_argument("value", choices=("true", "false"))
    change.add_argument("--why", required=True)
    args = parser.parse_args(argv)
    if args.command == "status":
        result = {"state": state(), "health": healthy()}
        if args.json:
            print(json.dumps(result))
        else:
            for variable, value in result["state"].items():
                print(f"{variable}: {value if value is not None else 'undetermined'}")
            health = result["health"]
            label = "undetermined" if health["healthy"] is None else str(health["healthy"]).lower()
            print(f"healthy: {label}; online: {health['online']}; stuck_queued: {health['stuck_queued']}; {health['reason']}")
            for runner in health["runners"]:
                print(f"runner {runner['name']} {runner['status']} busy={runner['busy']}")
        return 0
    result = set(args.variable, args.value, why=args.why, trigger="manual")
    print(json.dumps(result), file=sys.stdout if result["ok"] else sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
