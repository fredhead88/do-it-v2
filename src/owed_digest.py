#!/usr/bin/env python3
"""Daily owed digest and deduplicated operator alerts, pure over the ledger."""
import argparse
import fcntl
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone

import dispatch
import fold
import owed

LEDGER_NAME = "L-owed-digest-local.jsonl"
CORRECTING_STALE_DAYS = 7


def _time(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(value):
    return _time(value).isoformat() if value is not None else None


def _text(value):
    line = " ".join(str(value or "").split())
    return "".join(c for c in line if not unicodedata.category(c).startswith("C"))[:300]


def _span(value):
    text = _text(value)
    fence = "`" * (max([len(m) for m in re.findall(r"`+", text)] or [0]) + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _streak(all_ev, now):
    runs = sorted((e for e in all_ev if e.get("type") == "sweep-run" and _time(e["ts"]) <= now),
                  key=lambda e: _time(e["ts"]))
    failed = []
    for e in reversed(runs):
        if e.get("outcome") != "failed":
            break
        failed.append(e)
    return runs, failed


def build(all_ev, now):
    """SD8 snapshot. The clock is supplied by the caller, never read here."""
    now = _time(now)
    by_status = dict.fromkeys(owed.STATUSES, 0)
    blocked, needs, health = {}, [], []
    m1 = dict(terminal_unobservable=0, verify_unbuildable=0, sweep_failed_one_row_24h=0)
    for spec in fold.fold(all_ev)[0].values():
        evs = spec["evs"]
        for row in owed.checks(evs, now):
            status, capability = row["status"], row["capability"]
            by_status[status] += 1
            since = _iso(row["last_unobservable_at"] or row["governing_at"])
            item = dict(spec=row["spec"], criterion=row["criterion"], since=since)
            if status == "blocked":
                cause = capability or "unknown"
                blocked[cause] = blocked.get(cause, 0) + 1
                if capability == "operator-action":
                    needs.append(dict(item, ask=_text(row["ask"])))
            if status == "expired" and capability != "operator-action":
                m1["terminal_unobservable"] += 1
            if status == "quarantined" and row["criterion"] == "verify":
                m1["verify_unbuildable"] += 1
            if status in ("quarantined", "failed", "expired"):
                reason = row["quarantine_reason"] if status == "quarantined" else row["ask"]
                health.append(dict(item, kind=status, reason=_text(reason)))
            if status == "correcting":
                correctives = [e for e in evs if e.get("type") == "owed-corrective"
                               and e.get("criterion") == row["criterion"]
                               and e.get("corrective") == row["corrective"]]
                if correctives and now - _time(correctives[-1]["ts"]) > timedelta(days=CORRECTING_STALE_DAYS):
                    health.append(dict(item, kind="correcting-stale", corrective=row["corrective"],
                                       reason=f"corrective older than {CORRECTING_STALE_DAYS} days"))
    runs, failed = _streak(all_ev, now)
    recent = [e for e in runs if now - _time(e["ts"]) <= timedelta(hours=24)]
    batched = [e for e in runs if e.get("outcome") == "batched"]
    sweep = dict(runs_24h=len(recent), failed_24h=sum(e.get("outcome") == "failed" for e in recent),
                 consecutive_failed=len(failed), last_batched_at=batched[-1]["ts"] if batched else None)
    m1["sweep_failed_one_row_24h"] = sum(e.get("outcome") == "failed" and e.get("rows") == 1 for e in recent)
    if failed:
        health.append(dict(kind="sweep-failing", count=len(failed), since=failed[-1]["ts"],
                           reason=_text(failed[0].get("reason"))))
    return dict(at=now.isoformat(), counts=dict(by_status=by_status, blocked_by_capability=blocked, sweep=sweep),
                needs_you=needs, health=health, m1=m1)


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as out:
            temporary = pathlib.Path(out.name)
            os.fchmod(out.fileno(), 0o600)
            json.dump(obj, out, ensure_ascii=False, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_state(obj):
    """Replace the snapshot with one complete, private JSON file."""
    _write_json(fold.ROOT / "state/owed-digest.json", obj)


def render_md(obj):
    lines = [f"# Owed digest — {obj['at']}", "", "## Counts by status and cause", ""]
    lines.extend(f"- {name}: {count}" for name, count in obj["counts"]["by_status"].items())
    lines.append("")
    causes = obj["counts"]["blocked_by_capability"]
    lines.extend(f"- blocked / {_span(cause)}: {count}" for cause, count in sorted(causes.items()))
    if not causes:
        lines.append("Blocked causes: none")
    lines.extend(["", "Sweep: " + json.dumps(obj["counts"]["sweep"], sort_keys=True),
                  "M1: " + json.dumps(obj["m1"], sort_keys=True), "", "## NEEDS YOU", ""])
    for row in obj["needs_you"]:
        lines.append(f"- {_span(row['spec'])} / {_span(row['criterion'])}: {_span(row['ask'])}")
    if not obj["needs_you"]:
        lines.append("none")
    lines.extend(["", "## SYSTEM HEALTH", ""])
    for row in obj["health"]:
        subject = f"{_span(row['spec'])} / {_span(row['criterion'])}: " if "spec" in row else ""
        lines.append(f"- {subject}{row['kind']}: {_span(row.get('reason'))}")
    if not obj["health"]:
        lines.append("none")
    return "\n".join(lines) + "\n"


def _read_ledger():
    events = fold.EVENTS
    if (not events.is_dir() or not os.access(events, os.R_OK | os.X_OK)
            or not events.stat().st_mode & 0o444 or not events.stat().st_mode & 0o111
            or not any(events.glob("*.jsonl"))):
        raise OSError("owed digest ledger is unavailable")
    return fold.read_events()


def _alerted():
    path = fold.ROOT / "state/owed-alerted.json"
    if not path.exists():
        return dict(rows=[], streaks=[])
    obj = json.loads(path.read_text())
    if not isinstance(obj.get("rows"), list) or not isinstance(obj.get("streaks"), list):
        raise ValueError("invalid owed alert dedup state")
    return obj


def _alerts(obj, alerted):
    fresh = [r for r in obj["needs_you"] if f"{r['spec']}/{r['criterion']}/{r['since']}" not in alerted["rows"]]
    messages = []
    if fresh:
        entries = [f"{_text(r['spec'])}/{_text(r['criterion'])}: {_text(r['ask'])}" for r in fresh]
        # The cap may leave rows for the next pass; record only rows actually shown.
        text, keys = "NEEDS YOU", []
        for row, entry in zip(fresh, entries):
            if len(text) + 1 + len(entry) > 1000:
                break
            text += "\n" + entry
            keys.append(f"{row['spec']}/{row['criterion']}/{row['since']}")
        remaining = len(fresh) - len(keys)
        if remaining:
            suffix = f"\n{remaining} more new rows await the next pass (message limit)."
            while keys and len(text) + len(suffix) > 1000:
                text = text.rsplit("\n", 1)[0]
                keys.pop()
                remaining += 1
                suffix = f"\n{remaining} more new rows await the next pass (message limit)."
            text += suffix
        messages.append((text, "rows", keys))
    streak = next((r for r in obj["health"] if r["kind"] == "sweep-failing"), None)
    if streak and streak["count"] >= 3 and streak["since"] not in alerted["streaks"]:
        text = f"SYSTEM HEALTH: {streak['count']} consecutive failed sweep runs: {_text(streak['reason'])}"
        messages.append((text[:1000], "streaks", [streak["since"]]))
    return messages


def run(*, alert=False, now=None, sender=None, dry_run=False):
    """Read before writing; dry runs use the same snapshot and alert planner."""
    try:
        clock = _time(now or datetime.now(timezone.utc))
        all_ev = _read_ledger()
        obj = build(all_ev, clock)
        if dry_run:
            messages = _alerts(obj, _alerted())
            print(json.dumps(dict(digest=obj, telegrams=[m[0] for m in messages]), sort_keys=True))
            return 0
        state_dir = fold.ROOT / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        # Daily and alert invocations share dedup state: serialize their writes.
        with (state_dir / "owed-digest.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if alert:
                alerted = _alerted()
                messages = _alerts(obj, alerted)
                write_state(obj)  # The realm can read this before any message is sent.
                for text, field, keys in messages:
                    argv = [str(fold.ROOT / "telegram/tg-send.sh"), text]
                    result = sender(argv) if sender else subprocess.run(argv, capture_output=True, text=True)
                    lines = (result.stdout or "").splitlines()
                    if result.returncode != 0 or not lines or lines[-1] != "200":
                        raise OSError("owed digest Telegram send failed")
                    alerted[field].extend(keys)
                    _write_json(state_dir / "owed-alerted.json", alerted)
            else:
                date = clock.date().isoformat()
                path = fold.ROOT / "content" / f"L-owed-digest-{date}.md"
                # Fresh read under the lock prevents concurrent daily duplicate events.
                current = _read_ledger()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(render_md(obj))
                write_state(obj)
                if not any(e.get("type") == "owed-digest" and e.get("path") == str(path) for e in current):
                    refusal = dispatch.emit(fold.EVENTS / LEDGER_NAME, {}, "owed-digest",
                                            path=str(path), counts=obj["counts"])
                    if refusal:
                        raise ValueError(refusal)
        return 0
    except Exception as exc:
        print(f"owed-digest: {type(exc).__name__}: {_text(exc)}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alert", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return run(alert=args.alert, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
