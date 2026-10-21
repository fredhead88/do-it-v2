#!/usr/bin/env python3
"""Daily GitHub billing policy and its operator notifications."""
import argparse
import calendar
from datetime import date, datetime, timedelta, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tomllib

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "gh-bill.toml"
NUMBERS = ("yday", "mtd", "median7", "threshold", "limit", "pct_of_limit", "projection")
LEVELS = (50, 75, 90)


def _number(value):
    if isinstance(value, bool):
        raise ValueError("boolean amount")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite amount")
    return result


def decide(usage_items, today, limits, runner_state, healthy, shadow):
    """Return the policy decision without clock, IO, or runner imports."""
    result = dict.fromkeys(NUMBERS)
    result.update(action="undetermined", reason="")
    try:
        if usage_items is None or not isinstance(usage_items, list):
            raise ValueError("usage unreadable")
        limit = _number(limits["limit"])
        if limit <= 0:
            raise ValueError("monthly limit must be positive")
        ci = runner_state.get("CI_SELF_HOSTED")
        if ci not in ("true", "false"):
            raise ValueError("CI_SELF_HOSTED unreadable")
        daily = {}
        for item in usage_items:
            day = date.fromisoformat(item["date"][:10])
            amount = _number(item["netAmount"])
            daily[day] = daily.get(day, 0.0) + amount
        yesterday = today - timedelta(days=1)
        yday = daily.get(yesterday, 0.0)
        median7 = statistics.median(daily.get(yesterday - timedelta(days=i), 0.0) for i in range(1, 8))
        threshold = 4.0 if ci == "true" and shadow is True else max(1.5 * median7, 12.0)
        mtd = sum(amount for day, amount in daily.items()
                  if day.replace(day=1) == yesterday.replace(day=1) and day <= yesterday)
        pace = statistics.mean(daily.get(yesterday - timedelta(days=i), 0.0) for i in range(7))
        pct = mtd / limit * 100
        projection = mtd + pace * (calendar.monthrange(yesterday.year, yesterday.month)[1] - yesterday.day)
        values = dict(yday=yday, mtd=mtd, median7=median7, threshold=threshold,
                      limit=limit, pct_of_limit=pct, projection=projection)
        if not all(math.isfinite(v) for v in values.values()):
            raise ValueError("billing arithmetic overflow")
        result.update(values)
        if pct >= 90 and ci == "false":
            if healthy is True and shadow is True:
                action, reason = "flip", "90% reached; healthy runner and passed CI shadow"
            else:
                action, reason = "flip-refused", "90% reached; runner health or CI shadow not verified"
        else:
            level = next((n for n in reversed(LEVELS) if pct >= n and n not in limits.get("sent", [])), None)
            if level is not None:
                action, reason = f"alert-{level}", f"monthly spend reached {level}%"
            elif yday > threshold:
                action, reason = "alert", "yesterday exceeds the daily threshold"
            else:
                action, reason = "none", "spend within thresholds"
        result.update(action=action, reason=reason)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        result["reason"] = str(exc)
    return result


def render_telegram(decision):
    """Explain the logged decision without exposing its raw data or errors."""
    month = (date.fromisoformat(decision['date']) - timedelta(days=1)).strftime('%B')
    advice = {
        'none': 'Spending is within the current limits. No action needed.',
        'alert': 'Yesterday cost more than usual. Recommend checking recent workflow runs for avoidable costs.',
        'alert-50': 'Half the monthly limit is spent. Recommend keeping an eye on spending before adding more runs.',
        'alert-75': 'Most of the monthly limit is spent. Recommend reducing optional runs to stay within it.',
        'alert-90': 'The monthly limit is nearly used up. Recommend pausing optional runs and checking what still uses paid machines.',
        'flip': 'Builds have switched to our own verified machine to reduce costs. Recommend watching the next runs before adding paid capacity.',
        'flip-refused': 'NEEDS YOU: builds could not switch to our own machine. Recommend fixing and verifying that machine before raising the spending limit.',
        'undetermined': 'The bill could not be checked reliably. Recommend restoring the billing and build-machine checks before changing the spending limit.',
    }[decision['action']]
    amounts = ('mtd', 'pct_of_limit', 'limit', 'yday', 'projection')
    if any(decision.get(key) is None for key in amounts):
        return 'GitHub Actions bill: spending figures are unavailable.\n' + advice
    return (f"GitHub Actions bill: ${decision['mtd']:,.0f} spent so far in {month} "
            f"({decision['pct_of_limit']:.0f}% of the ${decision['limit']:,.0f} limit). "
            f"Yesterday ${decision['yday']:,.0f}; at the recent pace {month} ends near "
            f"${decision['projection']:,.0f}.\n" + advice)


def load_limits(path=CONFIG):
    try:
        cfg = tomllib.loads(Path(path).read_text())
        for key in ("default", "after_r1"):
            if _number(cfg[key]) <= 0:
                return None
        for key, value in cfg.items():
            if key not in ("default", "after_r1"):
                datetime.strptime(key, "%Y-%m")
                if _number(value) <= 0:
                    return None
        return cfg
    except (OSError, ValueError, TypeError, KeyError):
        return None


def limit_for(cfg, month, ci_self_hosted, shadow_age_days, prior):
    if cfg is None:
        return None
    try:
        value = prior if prior is not None else cfg.get(month, cfg[
            "after_r1" if ci_self_hosted in (True, "true") and shadow_age_days is not None
            and shadow_age_days >= 7 else "default"])
        return value if _number(value) > 0 else None
    except (ValueError, TypeError, KeyError):
        return None


def read_usage(today, usage_file=None):
    """A failed or malformed read is never an empty usage list."""
    try:
        if usage_file:
            payloads = [Path(usage_file).read_text()]
        else:
            import ghlimit
            yesterday = today - timedelta(days=1)
            months = [yesterday.replace(day=1)]
            if yesterday.day <= 7:
                months.append((months[0] - timedelta(days=1)).replace(day=1))
            payloads = []
            for month in months:
                endpoint = f"/users/fredhead88/settings/billing/usage?year={month.year}&month={month.month}"
                rc, out, _ = ghlimit.run(["gh", "api", endpoint], wait=False)
                if rc or not out.strip():
                    return None, f"billing usage read failed (exit {rc}, empty={not bool(out.strip())})"
                payloads.append(out)
        items = []
        for payload in payloads:
            parsed = json.loads(payload)
            if not isinstance(parsed, dict) or not isinstance(parsed.get("usageItems"), list):
                return None, "billing usage has no usageItems list"
            items.extend(parsed["usageItems"])
        return items, ""
    except (OSError, ValueError, TypeError) as exc:
        return None, f"billing usage unreadable: {exc}"


def _runner_inputs(today):
    runner = None
    try:
        if os.environ.get("GH_BILL_CI_RUNNER"):
            spec = importlib.util.spec_from_file_location("gh_bill_ci_runner", os.environ["GH_BILL_CI_RUNNER"])
            runner = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(runner)
        else:
            import ci_runner as runner
        state = runner.state()
        health = runner.healthy()
        # The merged API returns a report and the latest passed event.
        health = health.get("healthy") if isinstance(health, dict) else health
        import fold
        events = [event for history in fold.fold(fold.read_events())[3].values() for event in history]
        passed = runner.shadow_passed(events, "ci")
        shadow = passed is True or isinstance(passed, dict)
        stamps = [datetime.fromisoformat(e["ts"].replace("Z", "+00:00")).date() for e in events
                  if e.get("type") == "ci-shadow-passed" and e.get("kind") == "ci"] if shadow else []
        age = (today - min(stamps)).days if stamps else None
        return runner, state, health, shadow, age, ""
    except (ImportError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return runner, {"CI_SELF_HOSTED": None}, None, None, None, f"runner evidence unreadable: {exc}"


def _send(message):
    sender = os.environ.get("GH_BILL_TG") or str(Path.home() / ".do-it/telegram/tg-send.sh")
    result = subprocess.run([sender, message], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Telegram delivery failed")


def _append(decision):
    args = [str(REPO / "doit"), "append", "bill-check", "gh-bill"]
    for key, value in decision.items():
        args.append(f"{key}{':=' if key in NUMBERS else '='}{json.dumps(value)}")
    env = dict(os.environ, PROJECT="do-it-v2", DOIT_PROJECT="do-it-v2",
               DOIT_LEDGER_FILE="L-gh-bill-local.jsonl")
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("bill-check ledger append failed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usage-file")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    month = yesterday.strftime("%Y-%m")
    root = Path(os.environ.get("DOIT_ROOT", str(Path.home() / ".do-it")))
    path = root / "state/gh-bill.json"
    state_error = ""
    try:
        state = json.loads(path.read_text()) if path.exists() else {"months": {}}
        months = state["months"]
        saved = months.get(month, {})
        if not isinstance(months, dict) or not isinstance(saved, dict) or not isinstance(saved.get("sent", []), list):
            raise ValueError("invalid monthly state")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        state, months, saved = {"months": {}}, {}, {}
        state_error = f"billing state unreadable: {exc}"
    runner, runner_state, healthy, shadow, age, runner_error = _runner_inputs(today)
    cfg = load_limits(CONFIG)
    limit = limit_for(cfg, month, runner_state.get("CI_SELF_HOSTED"), age, saved.get("limit"))
    items, read_error = read_usage(today, args.usage_file)
    decision = decide(items, today, {"limit": limit, "sent": saved.get("sent", [])}, runner_state, healthy, shadow)
    decision["date"] = today.isoformat()
    errors = [e for e in (state_error, runner_error, read_error,
                          "monthly limit file missing or invalid" if cfg is None else "") if e]
    if errors:
        decision.update(action="undetermined", reason="; ".join(errors))
    if args.dry_run:
        print(json.dumps(decision, allow_nan=False))
        return int(decision["action"] == "undetermined")
    exit_code = int(decision["action"] == "undetermined")
    if decision["action"] == "flip":
        try:
            result = runner.set("CI_SELF_HOSTED", "true", why=decision["reason"], trigger="bill-flip")
            if not isinstance(result, dict) or result.get("ok") is not True:
                decision.update(action="flip-refused", reason=result.get("reason", "runner set refused")
                                if isinstance(result, dict) else "invalid runner set result")
        except (Exception, SystemExit) as exc:
            decision.update(action="flip-refused", reason=f"runner set failed: {exc}")
    try:
        previous_month = (yesterday.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        previous_limit = months.get(previous_month, {}).get("limit")
        if not state_error and limit is not None and month not in months and previous_limit != limit:
            _send(f"GitHub Actions spending limit for {yesterday:%B}: "
                  f"recommend setting it to ${limit:,.0f} in GitHub billing settings.")
        action = decision["action"]
        if action != "none":
            _send(render_telegram(decision))
        if not state_error and limit is not None:
            sent = list(saved.get("sent", []))
            level = 90 if action == "flip" else int(action[6:]) if action.startswith("alert-") else None
            if level is not None:
                sent = sorted(set(sent) | {n for n in LEVELS if n <= level})
            state["months"][month] = {"limit": limit, "sent": sent}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(state))
            tmp.replace(path)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        exit_code = 1
        decision["reason"] += f"; notification/state failure: {exc}"
    try:
        _append(decision)
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        exit_code = 1
    print(json.dumps(decision, allow_nan=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
