#!/usr/bin/env python3
"""Read the local seat meter and append fresh readings through the ledger guard."""
import argparse
import datetime as dt
import json
import math
import os
import pathlib
import sys

UTC = dt.timezone.utc
LEDGER = "L-seat-usage-local.jsonl"
READING_FIELDS = ("five_hour_pct", "seven_day_pct", "five_hour_resets_at",
                  "seven_day_resets_at")


def _root(root=None):
    return pathlib.Path(root if root is not None else
                        os.environ.get("DOIT_ROOT", pathlib.Path.home() / ".do-it"))


def _time(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC)


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("meter values must be finite numbers")
    return value


def _reading(root=None, now=None, max_age_s=900):
    try:
        data = json.loads((_root(root) / "state/claude-usage.json").read_text())
    except FileNotFoundError:
        return None, "absent"
    except (OSError, ValueError) as error:
        return None, f"invalid state: {error}"
    try:
        observed = _time(data["observed_at"])
        reading = {"observed_at": _iso(observed), "session": data["session_id"]}
        if not isinstance(reading["session"], str) or not reading["session"]:
            raise ValueError("session_id missing")
        for window in ("five_hour", "seven_day"):
            block = data[window]
            reading[window + "_pct"] = _number(block["used_percentage"])
            reading[window + "_resets_at"] = _iso(dt.datetime.fromtimestamp(
                _number(block["resets_at"]), UTC))
        age = ((now if now is not None else dt.datetime.now(UTC)) - observed).total_seconds()
        if age >= max_age_s or age < -60:
            return None, "stale"
        return reading, "fresh"
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError) as error:
        return None, f"invalid state: {error}"


def latest(root=None, now=None, max_age_s=900):
    """Return a validated SD2 reading, or None for absent, stale or invalid state."""
    return _reading(root, now, max_age_s)[0]


def record(dry_run=False, now=None):
    """Append changes immediately, or an unchanged reading after 14 minutes."""
    now = now if now is not None else dt.datetime.now(UTC)
    reading, reason = _reading(now=now)
    if reading is None:
        return {"action": "none", "reason": reason, "event": None}
    import fold
    prior = next((event for event in reversed(fold.read_events())
                  if event.get("type") == "seat-usage" and event.get("subject") == "seat-claude"
                  and event.get("actor") == "seat-usage"), None)
    event = {"type": "seat-usage", "subject": "seat-claude",
             **{key: str(value) for key, value in reading.items()}}
    changed = prior is None or any(event[key] != prior.get(key) for key in READING_FIELDS)
    if not changed:
        try:
            elapsed = (now - _time(prior["ts"])).total_seconds()
        except (KeyError, ValueError, AttributeError, TypeError):
            return {"action": "none", "reason": "invalid prior event timestamp", "event": None}
        if elapsed < 840:
            return {"action": "none", "reason": "unchanged", "event": None}
    reason = "changed" if changed else "heartbeat"
    if dry_run:
        return {"action": "none", "reason": "dry-run: " + reason, "event": event}
    previous = os.environ.get("DOIT_LEDGER_FILE")
    os.environ["DOIT_LEDGER_FILE"] = LEDGER
    try:
        stored = fold.append(["seat-usage", "seat-claude", "project=do-it-v2",
                              *[f"{key}={json.dumps(str(value))}" for key, value in reading.items()]], strict=True)
    except (SystemExit, OSError, AssertionError) as error:
        return {"action": "none", "reason": f"append failed: {error}", "event": None}
    finally:
        if previous is None:
            os.environ.pop("DOIT_LEDGER_FILE", None)
        else:
            os.environ["DOIT_LEDGER_FILE"] = previous
    return {"action": "appended", "reason": reason, "event": stored}


def link_statusline():
    """Back up a displaced file/link without overwriting any earlier backup."""
    source = pathlib.Path(__file__).resolve().parent.parent / "scripts/statusline.sh"
    target = pathlib.Path.home() / ".claude/statusline.sh"
    if target.is_symlink() and target.resolve() == source:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        backup = target.with_name(target.name + ".bak-" + dt.datetime.now().strftime("%Y%m%d"))
        candidate, suffix = backup, 0
        while candidate.exists() or candidate.is_symlink():
            suffix += 1
            candidate = backup.with_name(backup.name + f".{suffix}")
        target.rename(candidate)
    target.symlink_to(source)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["link-statusline"]:
        link_statusline()
        return 0
    parser = argparse.ArgumentParser(prog="doit seat-usage")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = record(dry_run=args.dry_run)
    except (OSError, ValueError) as error:
        result = {"action": "none", "reason": f"ledger read failed: {error}", "event": None}
    if args.json:
        print(json.dumps(result))
    else:
        print(f"seat-usage: {result['action']} ({result['reason']})")
        if result["event"] is not None:
            print(json.dumps(result["event"]))
    return int(result["action"] == "none" and result["reason"] not in
               ("absent", "stale", "unchanged") and not result["reason"].startswith("dry-run:"))


if __name__ == "__main__":
    sys.exit(main())
