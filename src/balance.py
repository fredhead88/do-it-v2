#!/usr/bin/env python3
"""Move the six managed model roles using local weekly quota observations."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib

import models

STATES = ("default", "claude-pressed", "codex-pressed")
MANAGED = ("builder", "spec-writer", "probe", "research", "reuse-scout", "charter-reviewer")
JUDGES = ("grader", "reviewer", "spec-auditor", "plan-auditor", "charter-reviewer")
KEYS = ("backend", "model", "effort", "fallback")


def _seat(model):
    return {"backend": "seat", "model": model}


def _codex(role):
    fallback = "claude-haiku-4-5-20251001" if role == "research" else "claude-sonnet-5-5"
    return {"backend": "codex", "model": "gpt-6.1-sol",
            "effort": "high" if role == "builder" else "medium", "fallback": _seat(fallback)}


TARGETS = {state: {role: (_codex(role) if role in MANAGED[:4] else
                         _seat("claude-opus-5-5" if role == "charter-reviewer" else "claude-sonnet-5-5"))
                   for role in MANAGED} for state in STATES}
TARGETS["claude-pressed"]["reuse-scout"] = _codex("reuse-scout")
TARGETS["claude-pressed"]["charter-reviewer"] = _seat("claude-sonnet-5-5")
TARGETS["codex-pressed"] = {role: _seat("claude-opus-5-5" if role == "charter-reviewer" else
                                             "claude-haiku-4-5-20251001" if role == "research" else
                                             "claude-sonnet-5-5") for role in MANAGED}


def _percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and 0 <= value <= 100 else None
    except (TypeError, ValueError, OverflowError):
        return None


def choose(codex_pct, claude_7d_pct, current_state):
    """Pure SD11 transition function; missing readings never imply spare capacity."""
    if current_state not in STATES:
        raise ValueError(f"unknown balance state: {current_state}")
    c, s = _percent(codex_pct), _percent(claude_7d_pct)
    if s is not None and s >= 70:
        return "claude-pressed"
    if current_state == "codex-pressed" and (c is None or c >= 50):
        return current_state
    if current_state == "claude-pressed" and (s is None or s >= 60):
        return current_state
    if current_state == "codex-pressed" and s is None:
        return "default"
    if s is None or (s < 50 and c is None):
        return current_state
    return "codex-pressed" if s < 50 and c >= 70 else "default"


def _time(value):
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value) if math.isfinite(value) else None
        if isinstance(value, datetime):
            return value.timestamp() if value.tzinfo else None
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def read_inputs(events, now, quota):
    """Read the real quota callback and latest observed seat usage, not append order."""
    stamp = _time(now)
    codex = quota(stamp)
    c = _percent(codex.get("used_percent")) if isinstance(codex, dict) else None
    observations = [e for e in events if e.get("type") == "seat-usage"]
    # A malformed observation cannot safely be ordered against the others.
    if not observations or any(_time(e.get("observed_at")) is None for e in observations):
        return c, None
    newest = max(enumerate(observations), key=lambda pair: (_time(pair[1]["observed_at"]), pair[0]))[1]
    age = stamp - _time(newest["observed_at"])
    s = _percent(newest.get("seven_day_pct")) if -300 <= age < 1800 else None
    return c, s


def _check_judges(document):
    default = document.get("defaults", {}).get("backend", "claude-p")
    for role in JUDGES:
        contract = document.get("contracts", {}).get(role)
        if contract is not None and contract.get("backend", default) == "codex":
            raise ValueError(f"refused: judging role {role} is on codex")


def _comment(line):
    """Find a TOML trailing comment without mistaking a quoted # for one."""
    quote, escaped = None, False
    for i, char in enumerate(line):
        if escaped:
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#":
            start = i
            while start and line[start - 1] in " \t":
                start -= 1
            return line[start:].rstrip("\r\n")
    return ""


def _assignment(key, value):
    if key == "fallback":
        return 'fallback = { backend = ' + json.dumps(value["backend"]) + ', model = ' + json.dumps(value["model"]) + ' }'
    return f"{key} = {json.dumps(value)}"


def rewrite(text, state):
    """Edit only managed key lines, retaining the rest of the document verbatim."""
    target = TARGETS[state]
    original = tomllib.loads(text)
    # An unmanaged judge cannot be repaired within this function's write grant.
    for role in JUDGES:
        if role not in MANAGED:
            _check_judges({"defaults": original.get("defaults", {}),
                           "contracts": {role: original.get("contracts", {}).get(role, {})}})
    _check_judges({"contracts": target})
    out, seen, keys = [], set(), set()
    current = None

    def finish():
        if current in target:
            for key, value in target[current].items():
                if key not in keys:
                    if out and not out[-1].endswith("\n"):
                        out[-1] += "\n"
                    out.append(_assignment(key, value) + "\n")

    for line in text.splitlines(keepends=True):
        if re.match(r"\s*\[", line):
            finish()
            match = re.match(r"\s*\[contracts\.([\w-]+)\]\s*(?:#.*)?$", line.rstrip("\r\n"))
            current = match[1] if match else None
            keys = set()
            if current in target:
                seen.add(current)
        match = re.match(r"\s*(backend|model|effort|fallback)\s*=", line)
        if current in target and match:
            key = match[1]
            keys.add(key)
            comment = _comment(line)
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            if key in target[current]:
                out.append(_assignment(key, target[current][key]) + comment + ending)
            elif comment:
                out.append(comment + ending)
        else:
            out.append(line)
    finish()
    for role in MANAGED:
        if role not in seen:
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
            out.append(f"\n[contracts.{role}]\n")
            out.extend(_assignment(key, value) + "\n" for key, value in target[role].items())
    result = "".join(out)
    _check_judges(tomllib.loads(result))
    return result


def _atomic(path, text, validate=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if validate:
            validate(Path(name))
        if path.exists():
            os.chmod(name, path.stat().st_mode & 0o777)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def apply(text, state, path=None, dry_run=False):
    """Validate beside the destination before installing a rewritten model map."""
    result = rewrite(text, state)
    path = Path(path or models.PATH)
    if not dry_run and result != text:
        _atomic(path, result, models.load)
    return result


def _state(path):
    if not path.exists():
        return {"state": "default", "held": False, "held_at": None,
                "resumed_at": None, "updated_at": None}
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or data.get("state") not in STATES or
            type(data.get("held")) is not bool or
            any(k not in data for k in ("held_at", "resumed_at", "updated_at")) or
            type(data.get("resumed_position", -1)) is not int or data.get("resumed_position", -1) < -1 or
            any(data[k] is not None and _time(data[k]) is None
                for k in ("held_at", "resumed_at", "updated_at"))):
        raise ValueError("state unreadable")
    return data


def _operator_hold(events, data):
    balances = [(_time(e.get("ts")), i) for i, e in enumerate(events) if e.get("type") == "balance"]
    anchors = [(ts, i) for ts, i in balances if ts is not None]
    resumed = _time(data.get("resumed_at"))
    if resumed is not None:
        anchors.append((resumed, data.get("resumed_position", -1)))
    anchor = max(anchors, default=(float("-inf"), -1))
    newer = [e for i, e in enumerate(events) if e.get("type") == "models-changed" and
             (_time(e.get("ts")) is None or (_time(e["ts"]), i) > anchor)]
    return newer[-1] if newer else None


def _pair(contract):
    return {key: contract.get(key) for key in ("backend", "model")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("hold", "resume"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.action and args.dry_run:
        parser.error("--dry-run applies to a balance run")
    import fold
    import dispatch
    path = Path(os.environ.get("DOIT_ROOT", Path.home() / ".do-it")) / "state/balance.json"
    try:
        data = _state(path)
    except (OSError, ValueError):
        print("held: state unreadable")
        return 0 if not args.action else 1
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    try:
        if args.action:
            data["held"] = args.action == "hold"
            data["held_at" if data["held"] else "resumed_at"] = stamp
            if not data["held"]:
                data["resumed_position"] = max(
                    (i for i, event in enumerate(fold.read_events())
                     if _time(event.get("ts")) is not None and _time(event["ts"]) <= now.timestamp()),
                    default=-1)
            data["updated_at"] = stamp
            _atomic(path, json.dumps(data, sort_keys=True) + "\n")
            print("held" if args.action == "hold" else "resumed")
            return 0
        if data["held"]:
            print(f"held: manual {data['held_at']}")
            return 0
        events = fold.read_events()
        operator = _operator_hold(events, data)
        if operator is not None:
            print(f"held: operator profile {operator.get('ts')}")
            return 0
        text = models.PATH.read_bytes().decode("utf-8")
        before = models.load()
        if before is None:
            raise ValueError("models.toml unreadable")
        c, s = read_inputs(events, now, dispatch.codex_quota)
        state = choose(c, s, data["state"])
        result = rewrite(text, state)
        changes = [{"role": role, "from": _pair(before.get(role, {})), "to": _pair(TARGETS[state][role])}
                   for role in MANAGED if _pair(before.get(role, {})) != _pair(TARGETS[state][role])]
        moved = state != data["state"] or result != text
        if args.dry_run:
            print(f"dry-run: {data['state']} -> {state}; changes={json.dumps(changes)}; file_changed={result != text}")
            return 0
        if moved:
            apply(text, state)
            fields = ["balance", "models", f"state_from={data['state']}", f"state_to={state}",
                      "codex_pct=" + (format(c, "g") if c is not None else "unknown"),
                      "claude_7d_pct=" + (format(s, "g") if s is not None else "unknown"),
                      "changes=" + json.dumps(changes),
                      "models_sha256=" + hashlib.sha256(result.encode()).hexdigest()]
            prior = os.environ.get("DOIT_LEDGER_FILE")
            try:
                os.environ["DOIT_LEDGER_FILE"] = "L-balance-local.jsonl"
                fold.append(fields, strict=True)
            finally:
                if prior is None:
                    os.environ.pop("DOIT_LEDGER_FILE", None)
                else:
                    os.environ["DOIT_LEDGER_FILE"] = prior
            data.update(state=state, updated_at=stamp)
            _atomic(path, json.dumps(data, sort_keys=True) + "\n")
        print(f"balance: {state}; codex={c if c is not None else 'unknown'}; claude={s if s is not None else 'unknown'}; changed={moved}")
        return 0
    except (OSError, ValueError, SystemExit) as exc:
        print(f"held: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
