#!/usr/bin/env python3
"""Read-only owed observations, with bounded execution and expiring strikes."""
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
import pathlib
import re
import shlex
import subprocess
import time

import build_runner
import checks
import dispatch
import fold
import owed
import sweep_wrappers

SOURCE = pathlib.Path(__file__).resolve().parent.parent
LEDGER_NAME = "L-prod-runner-local.jsonl"
CMD_TIMEOUT_S = 60
BUILD_TIMEOUT_S = 2100
MAX_BUILD_ROWS = 5
PASS_WALL_S = 1500
PASSES = []
NEED_ROUTES = {"ro-prod-sql": "ro-dsn", "droplet-read": "droplet",
               "deployed-site": "not-live", "browser-review-account": "browser",
               "elapsed-time": "elapsed", "repo": "repo", "operator": "operator-action"}


def redact(value):
    text = str(value or "")
    text = re.sub(r"(?i)postgres(?:ql)?://[^\s\"'<>]+", "[redacted database URL]", text)
    text = re.sub(r"(https?://)[^\s/@]+@", r"\1[redacted]@", text, flags=re.I)
    text = re.sub(r"(?i)\b(password|token|secret|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+",
                  r"\1=[redacted]", text)
    return text[:600]


def _instant(value):
    try:
        instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return instant if instant.tzinfo else None
    except (ValueError, TypeError):
        return None


def _capability(record):
    return next((NEED_ROUTES[n] for n in record.get("needs", []) if n in NEED_ROUTES), "repo")


def _curl(args):
    index, urls = 0, 0
    while index < len(args):
        word = args[index]
        if re.fullmatch(r"-[sSfIL]+", word):
            index += 1
        elif word in ("-o", "-w", "-H", "--max-time") and index + 1 < len(args):
            value = args[index + 1]
            if (word == "-o" and value != "/dev/null"
                    or word in ("-w", "-H") and value.startswith("@")
                    or word == "-w" and "%output{" in value
                    or word == "--max-time" and not re.fullmatch(r"\d+(?:\.\d+)?", value)):
                raise ValueError("curl argument refused")
            index += 2
        elif word.startswith(("http://", "https://")):
            urls += 1
            index += 1
        else:
            raise ValueError("curl argument refused")
    if not urls:
        raise ValueError("curl URL required")
    # -q disables implicit ~/.curlrc; input cannot inject config or protocols.
    return ["curl", "-q", "--proto", "=http,https", *args]


def _argv(command):
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    if not argv:
        raise ValueError("empty command")
    head = pathlib.Path(argv[0]).name
    if head == "curl":
        return _curl(argv[1:])
    if head == "ro-sql":
        if len(argv) != 3 or sweep_wrappers.sql_statement_refusal(argv[2]):
            raise ValueError("SQL refused")
    elif head == "droplet-read":
        if len(argv) < 3:
            raise ValueError("droplet arguments required")
        refusal = (sweep_wrappers.named_read_refusal(argv[2], argv[3:])
                   if argv[2] in sweep_wrappers.NAMED_DROPLET_READS
                   else sweep_wrappers.droplet_command_refusal(argv[2], argv[3:]))
        if refusal:
            raise ValueError("droplet command refused")
    else:
        raise ValueError("command head refused")
    return [str(SOURCE / "scripts" / "sweep" / head), *argv[1:]]


def observe(row, record, *, now=None):
    now = now or datetime.now(timezone.utc)
    result = {"observed_at": now.isoformat(),
              "valid_until": (now + timedelta(hours=record.get("validity_h", 24))).isoformat(),
              "evidence": ""}

    def unavailable(capability, why):
        return {**result, "outcome": "unobservable", "capability": capability, "why": redact(why)}

    commands = record.get("commands", [])
    if not commands:
        return unavailable("repo", "no commands; Thinker must choose an observation route")
    try:
        argvs = [_argv(command) for command in commands]
    except (ValueError, TypeError):
        return unavailable("repo", "read-only command refused; Thinker must review the observation route")
    import provision
    evidence, failed = [], False
    for index, argv in enumerate(argvs):
        # Legacy endpoint observations retain their read-only curl contract.
        check = checks.CheckRecord("observe:check", row.get("spec", "observation"),
            row.get("criterion", "check"), (shlex.split(commands[index]) if isinstance(commands[index], str)
                                          else list(commands[index])), "", "production", False,
            "repo-venv" if argv[0] == "curl" else "prod-readonly", "post-deploy", "script")
        with provision.context(tree=SOURCE):
            prepared = provision.prepare(check, lane="production")
            try:
                verdict = provision.run_script_check(check, prepared)
            finally:
                prepared.cleanup()
        if verdict.outcome == "unobservable":
            return unavailable(verdict.capability if verdict.capability == "ro-dsn-grant" else _capability(record), verdict.reason)
        failed |= verdict.outcome == "failed"
        evidence.append(verdict.evidence)
    return {**result, "outcome": "failed" if failed else "met", "evidence": redact("; ".join(evidence))}


def record_for(row, events=None):
    """Adapt the authoritative declaration for existing model-lane routing."""
    if events is None:
        events = fold.read_events()  # events=None default
    record = checks.record_for(events, row["spec"], row["criterion"])
    if record is None:
        browser = row.get("capability") == "browser"
        return {"runner": "prod-browser" if browser else "judgment",
                "needs": ["browser-review-account"] if browser else [], "commands": [],
                "validity_h": 24, "unknown": "data-source-unknown"}
    needs = (["ro-prod-sql"] if record.profile == "prod-readonly" else
             ["browser-review-account"] if record.profile == "browser-review" else ["repo"])
    return {"runner": "script" if record.answered_by == "script" else
            "prod-browser" if record.profile == "browser-review" else "judgment",
            "needs": needs, "commands": [record.argv] if record.argv else [], "validity_h": 24}


def batch_for_browser(rows, events=None):
    return [row for row in rows if record_for(row, events=events)["runner"] == "prod-browser"]


def stale_failure_ready(row, evs, now):
    """Retry expired failure evidence only after 12h and without an owned fix.

    A corrective anywhere after this row's governing declaration/recheck owns
    the fix, even when a later failure makes the folded status failed again.
    Missing or malformed validity never establishes expired evidence.
    """
    import sweep_owed
    failed_at = row.get("last_failed_at")
    governing_at = row.get("governing_at")
    if (row["status"] != "failed" or failed_at is None or governing_at is None
            or now - failed_at < sweep_owed.RETRY_WINDOW):
        return False
    related = [e for e in evs if e.get("criterion") == row["criterion"]]
    failure = next((e for e in reversed(related) if e.get("type") == "owed-failed"), {})
    until = _instant(failure.get("valid_until"))
    return (until is not None and until < now
            and not any(e.get("type") == "owed-corrective"
                        and fold.ts(e.get("ts")) > governing_at for e in related))


def _selected(specs, now):
    """Due rows plus retry-ready stale failures without a standing corrective."""
    import sweep_owed
    rows = {(r["spec"], r["criterion"]): r for r in sweep_owed.due_candidates(specs, now)}
    for spec in specs.values():
        for row in owed.checks(spec["evs"], now):
            if stale_failure_ready(row, spec["evs"], now):
                rows[row["spec"], row["criterion"]] = row
    return sorted(rows.values(), key=lambda r: r["due_at"])


def _load_state(path, now):
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            return {}
        return {key: value for key, value in state.items() if isinstance(value, dict)
                and _instant(value.get("observed_at")) is not None
                and _instant(value.get("valid_until")) is not None
                and _instant(value["valid_until"]) > now
                and _instant(value["observed_at"]) <= now
                and value.get("outcome") in ("failed", "unobservable")}
    except (OSError, ValueError, TypeError):
        return {}


def _build(row, record, specs, now):
    sha = next((e.get("sha") for e in reversed(specs[row["spec"]]["evs"])
                if e.get("type") == "shipped"), None)
    if not sha:
        return None, None
    argv = [str(SOURCE / "doit"), "run-checks", row["spec"], "--sha", sha,
            "--criteria", row["criterion"]]
    try:
        process = subprocess.run(argv, capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)
        if (process.returncode == 2 and (process.stderr or "").strip()
                == "run-checks: " + build_runner.PROD_DATA_UNAVAILABLE):
            return {"outcome": "unobservable", "capability": "ro-dsn",
                    "observed_at": now.isoformat(),
                    "valid_until": (now + timedelta(hours=record.get("validity_h", 24))).isoformat(),
                    "evidence": "", "why": build_runner.PROD_DATA_UNAVAILABLE,
                    "env": dict(build_runner.NO_PROD_ENV)}, None
        if process.returncode != 0:
            return None, None
        path = pathlib.Path(process.stdout.strip())
        bundle = json.loads(path.read_text())
        commands = bundle["commands"]
        # Consume 1108's published schema, including identity and nonempty commands.
        if (bundle.get("spec") != row["spec"] or bundle.get("sha") != sha
                or not commands or not isinstance(commands, list)
                or any(type(c.get("exit")) is not int or c.get("criterion") != row["criterion"]
                       for c in commands)):
            return None, None
        if any(c["exit"] != 0 for c in commands):
            return None, str(path)
        return {"outcome": "met", "observed_at": now.isoformat(),
                "valid_until": (now + timedelta(hours=record.get("validity_h", 24))).isoformat(),
                "evidence": redact(f"evidence bundle: {path}")}, None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        return None, None


def run(now, *, dry_run=False, events=None):
    if events is None:
        events = fold.read_events()  # events=None default
    specs = fold.fold(events)[0]
    import prod_browser
    import sweep_owed
    rows = sweep_owed.browser_input(specs, _selected(specs, now), now, events=events)
    total_rows = len(rows)
    rows = [r for r in rows if not ((declared := checks.record_for(events, r["spec"], r["criterion"]))
                                  and declared.answered_by == "script")]
    records = {(r["spec"], r["criterion"]): record_for(r, events=events) for r in rows}
    result = {"lines": [], "bundle_rows": {}, "held": set(), "to_judgment": set()}
    started = time.monotonic()
    state_path = pathlib.Path(os.environ.get("DOIT_ROOT", dispatch.ROOT)) / "state" / "prod-runner.json"

    def line(message):
        message = f"prod-runner: {message}"
        result["lines"].append(message)
        print(message)

    def emit(type_, subject, **fields):
        if dry_run:
            line(f"DRY RUN would append {type_} for {subject}")
            return None
        path = fold.EVENTS / LEDGER_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        return dispatch.emit(path, {"subject": subject}, type_, **fields)

    if total_rows != len(rows):
        line(f"{total_rows - len(rows)} script-answered row(s) delegated to inline sweep/verifier checks")
    active = {key for key, record in records.items() if record["runner"] in ("prod", "build", "prod-browser")}
    if dry_run:
        for key in sorted(active):
            line(f"DRY RUN would observe {key[0]}/{key[1]}")
        result["held"].update(active)
        for callback in PASSES:
            try:
                for message in callback(now, True, emit) or []:
                    line(message)
            except Exception:
                line(f"pass {getattr(callback, '__name__', 'unnamed')} raised")
        return result

    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result["held"].update(active)
            line(f"lock busy; held {len(active)} build/prod row(s)")
            return result
        state = _load_state(state_path, now)
        browser_rows = [{**r, "validity_h": records[r["spec"], r["criterion"]].get("validity_h", 24),
                         "spec_path": str(r.get("spec_path") or dispatch.spec_path(r["spec"]))}
                        for r in rows if records[r["spec"], r["criterion"]]["runner"] == "prod-browser"]
        browser_results = {(o["spec"], o["criterion"]): o
                           for o in prod_browser.run_batch(browser_rows, now, events=events)}
        builds = 0
        for row in rows:
            key = row["spec"], row["criterion"]
            record = records[key]
            if record["runner"] == "build":
                if builds >= MAX_BUILD_ROWS or time.monotonic() - started >= PASS_WALL_S:
                    result["held"].add(key)
                    line(f"held {key[0]}/{key[1]}: build pass budget")
                    continue
                builds += 1
                observation, bundle = _build(row, record, specs, now)
                if bundle:
                    result["bundle_rows"][key] = bundle
                    line(f"judgment {key[0]}/{key[1]}: evidence bundle {redact(bundle)}")
                    continue
                if observation is None:
                    result["held"].add(key)
                    line(f"held {key[0]}/{key[1]}: build undetermined")
                    continue
            elif record["runner"] == "prod":
                if not record.get("commands"):
                    result["to_judgment"].add(key)
                    line(f"judgment {key[0]}/{key[1]}: no commands")
                    continue
                observation = observe(row, record, now=now)
            elif record["runner"] == "prod-browser":
                observation = browser_results.get(key)
                if observation is None:
                    continue
                observation = {k: v for k, v in observation.items() if k not in ("spec", "criterion")}
            else:
                continue
            state_key = "/".join(key)
            prior = state.get(state_key)
            outcome = observation["outcome"]
            deterministic = observation.get("capability") in ("ro-dsn-grant", "operator-action")
            fields = {"criterion": key[1], **{k: v for k, v in observation.items() if k != "outcome"}}
            if outcome == "met" or deterministic:
                state.pop(state_key, None)
            else:
                # An unobservable cannot cancel a pending failure. A failed/met
                # reading cancels a pending transient-unobservable strike.
                preserved = prior if prior and prior["outcome"] == "failed" and outcome == "unobservable" else None
                if preserved:
                    prior = preserved.get("transient")
                    if not (isinstance(prior, dict) and prior.get("outcome") == "unobservable"
                            and _instant(prior.get("valid_until")) is not None
                            and _instant(prior["valid_until"]) > now
                            and _instant(prior.get("observed_at")) is not None
                            and _instant(prior["observed_at"]) <= now):
                        prior = None
                elif prior:
                    prior.pop("transient", None)
                if not prior or prior["outcome"] != outcome:
                    if preserved:
                        preserved["transient"] = observation
                    else:
                        state[state_key] = observation
                    line(f"waiting {key[0]}/{key[1]}: first {outcome} observation")
                    continue
                if now - _instant(prior["observed_at"]) < timedelta(minutes=5):
                    line(f"waiting {key[0]}/{key[1]}: recheck before five minutes")
                    continue
                fields["observations"] = [prior["observed_at"], observation["observed_at"]]
                if preserved:
                    preserved.pop("transient", None)
                else:
                    state.pop(state_key, None)
            if outcome == "failed":
                fields["kind"] = "stale" if any(e.get("type") == "owed-met" and e.get("criterion") == key[1]
                    for e in specs[key[0]]["evs"]) else "unmet"
            refused = emit("owed-" + outcome, key[0], **fields)
            line(f"{key[0]}/{key[1]}: {'event refused' if refused else outcome}")
        temporary = state_path.with_suffix(".pending")
        temporary.write_text(json.dumps(state, sort_keys=True) + "\n")
        temporary.replace(state_path)
        for callback in PASSES:
            try:
                for message in callback(now, False, emit) or []:
                    line(message)
            except Exception:
                line(f"pass {getattr(callback, '__name__', 'unnamed')} raised")
    return result
