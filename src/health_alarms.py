#!/usr/bin/env python3
"""Health alarms, pure evaluation and bounded ledger/Telegram delivery."""
import argparse
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import backup
import fold
import scratch

LOCK_STALE_S = 4200
ALARMS = ("mirror-stale", "release-failed", "deploy-lock-stale",
          "sweeper-unobservable", "disk-low", "disk-used", "preflight-refusals")
LEDGER_NAME = "L-health-local.jsonl"
RELEASE_LOG = pathlib.Path("/var/log/albert-scott/daily-release.log")
_PROC = object()


def _time(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def _text(value, cap=200):
    return " ".join(str(value).split())[:cap]


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _events(events, now, seconds=None):
    for e in events:
        if not isinstance(e, dict) or not isinstance(e.get("type"), str):
            continue
        ts = _time(e.get("ts"))
        if ts is not None and ts <= now and (seconds is None or (now - ts).total_seconds() <= seconds):
            yield e


def disk_alarm(used_fraction):
    valid = _number(used_fraction) and 0 <= used_fraction <= 1
    if valid and used_fraction < .90:
        return None
    reading = {"used_fraction": used_fraction if valid else None}
    if not valid:
        reading["undetermined"] = ["used_fraction"]
    return dict(alarm="disk-used", severity="immediate" if valid else "summary",
                reading=reading, line=f"disk used fraction={used_fraction}" if valid else "disk usage undetermined")


def evaluate(events, now, *, probes):
    """Evaluate only supplied state; missing readings remain visible as summary alarms."""
    now = _time(now)
    p = _dict(probes)
    evs = list(_events(events, now))
    recent = lambda seconds, types: [e for e in _events(evs, now, seconds) if e["type"] in types]
    result = []

    def add(name, reading, line, immediate=False, unknown=()):
        if unknown:
            reading["undetermined"] = list(unknown)
        result.append(dict(alarm=name, severity="immediate" if immediate and not unknown else "summary",
                           reading=reading, line=_text(line)))

    age = _dict(p.get("backup")).get("age_s")
    ahead = p.get("mirror_ahead_s")
    failures = len(recent(3600, {"backup-failed"}))
    unknown = [name for name, value in (("backup.age_s", age), ("mirror_ahead_s", ahead)) if not _number(value)]
    triggers = (["backup-failed"] if failures else [])
    triggers += [name for name, value in (("age_s", age), ("mirror_ahead_s", ahead)) if _number(value) and value > 3600]
    if unknown or triggers:
        reading = dict(age_s=age, mirror_ahead_s=ahead, backup_failed_1h=failures, trigger=triggers)
        add("mirror-stale", reading, f"mirror age_s={age}, ahead_s={ahead}, backup failures/1h={failures}" + ("; undetermined " + ", ".join(unknown) if unknown else ""),
            any(_number(v) and v > 21600 for v in (age, ahead)), unknown)

    log = p.get("release_log_failed")
    unknown = ["release_log_failed"] if not isinstance(log, list) else []
    releases = recent(86400, {"deploy-failed"})
    records = [e for e in log if isinstance(e, dict) and _time(e.get("ts")) is not None
               and 0 <= (now - _time(e["ts"])).total_seconds() <= 86400] if isinstance(log, list) else []
    all_failures = releases + records
    if unknown or all_failures:
        newest = max(all_failures, key=lambda e: _time(e["ts"])) if all_failures else {}
        immediate = any(e.get("rollback") in ("ran", "failed") or
                        any(word in str(e.get("why", "")).lower() for word in ("timeout", "killed", "-9")) for e in releases)
        add("release-failed", dict(key=newest.get("ts", "ongoing"), failures=len(all_failures)),
            f"release failures/24h={len(all_failures)}" + ("; undetermined release_log_failed" if unknown else ""), immediate, unknown)

    locks = _dict(p.get("locks"))
    unknown, stale = [], []
    for name in ("deploy", "release"):
        lock = _dict(locks.get(name))
        held = lock.get("held")
        if held is False:
            continue
        since = _time(lock.get("since"))
        if held is not True:
            unknown.append(f"locks.{name}.held")
        elif since is None:
            unknown.append(f"locks.{name}.since")
        elif (now - since).total_seconds() > LOCK_STALE_S:
            stale.append(name)
    if stale or unknown:
        add("deploy-lock-stale", dict(locks=locks, stale=stale),
            "locks stale=" + (", ".join(stale) or "none") + ("; undetermined " + ", ".join(unknown) if unknown else ""), bool(stale), unknown)

    verdicts = recent(86400, {"owed-met", "owed-failed", "owed-unobservable"})
    total = len(verdicts)
    unobservable = sum(e["type"] == "owed-unobservable" for e in verdicts)
    share = unobservable / total if total else 0
    if total >= 5 and share > .30:
        add("sweeper-unobservable", dict(share=share, unobservable=unobservable, total=total),
            f"sweeper unobservable={unobservable}/{total} ({share:.0%}) in 24h")

    disk = _dict(p.get("disk_free_gb"))
    values = {name: disk.get(name) for name in ("/", "scratch")}
    unknown = [f"disk_free_gb.{name}" for name, value in values.items() if not _number(value)]
    if unknown or any(_number(v) and v < 15 for v in values.values()):
        add("disk-low", dict(values), f"disk free GB: /={values['/']}, scratch={values['scratch']}" +
            ("; undetermined " + ", ".join(unknown) if unknown else ""),
            any(_number(v) and v < 5 for v in values.values()), unknown)

    if alarm := disk_alarm(p.get("used_fraction")):
        result.append(alarm)

    refusals = recent(3600, {"grading-preflight-failed", "evidence-bundle-failed"})
    refusals = [e for e in refusals if not any(str(e.get(k, "")).startswith("bundle-pending") for k in ("capability", "reason", "why"))]
    if len(refusals) > 3:
        tally = {}
        for e in refusals:
            capability = str(e.get("capability") or e["type"])
            tally[capability] = tally.get(capability, 0) + 1
        add("preflight-refusals", dict(count=len(refusals), capabilities=tally), f"grading refusals/1h={len(refusals)}")
    return result


def summary_lines(events, now):
    """Newest firing per alarm in 24h, then the newest complete base-red per project."""
    now = _time(now)
    alarms, bases = {}, {}
    for e in sorted(_events(events, now), key=lambda e: _time(e["ts"])):
        if e["type"] == "health-alarm" and e.get("alarm") in ALARMS and (now - _time(e["ts"])).total_seconds() <= 86400:
            alarms[e["alarm"]] = e
        if e["type"] == "base-red" and e.get("complete") is True and isinstance(e.get("project"), str):
            bases[e["project"]] = e
    lines = [f"HEALTH {name} {_text(e.get('severity'))}: {_text(e.get('line'))}" for name, e in sorted(alarms.items())]
    for project, e in sorted(bases.items()):
        ids = e.get("ids", [])
        if isinstance(ids, dict):
            ids = [item for value in ids.values() if isinstance(value, list) for item in value]
        if isinstance(ids, list) and ids:
            shown = ", ".join(_text(i) for i in ids[:5])
            if len(ids) > 5:
                shown += f" +{len(ids) - 5} more"
            lines.append(f"HEALTH BASE-RED {_text(project)} {_text(e.get('sha', ''))[:12]}: {shown}")
    return lines


def lock_held(path, proc_text=_PROC):
    """Inspect kernel inode records without changing either watched lock."""
    try:
        stat = pathlib.Path(path).stat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    if proc_text is _PROC:
        try:
            proc_text = pathlib.Path("/proc/locks").read_text()
        except OSError:
            return None
    if not isinstance(proc_text, str):
        return None
    wanted = (os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino)
    for line in proc_text.splitlines():
        if "FLOCK" not in line.split():
            continue
        for major, minor, inode in re.findall(r"\b([0-9a-fA-F]+):([0-9a-fA-F]+):(\d+)\b", line):
            if (int(major, 16), int(minor, 16), int(inode)) == wanted:
                return True
    return False


def release_log(path, now):
    try:
        lines = pathlib.Path(path).read_text().splitlines()
    except OSError:
        return None, None
    failed, starts = [], []
    for line in lines:
        match = re.match(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\] (FAILED: |START\b)", line)
        if not match:
            continue
        ts = _time(match[1])
        if ts is None or ts > now:
            continue
        if match[2].startswith("START"):
            starts.append(match[1])
        elif (now - ts).total_seconds() <= 86400:
            failed.append(dict(ts=match[1]))
    return failed, max(starts, default=None)


def mirror_ahead(now):
    try:
        proc = subprocess.run(["git", "-C", str(fold.ROOT), "rev-list", "--timestamp", "HEAD", "--not", "--remotes"],
                              capture_output=True, text=True, timeout=15)
        if proc.returncode:
            return None
        stamps = [int(line.split()[0]) for line in proc.stdout.splitlines()]
        return max(0, now.timestamp() - min(stamps)) if stamps else 0
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def live_probes(now):
    """Read local backup/git/log/kernel/disk state; failed probes stay unknown."""
    now = _time(now)
    try:
        pushed = backup.last_push(fold.ROOT)
    except Exception:
        pushed = None
    failed, release_start = release_log(RELEASE_LOG, now)
    try:
        starts = [e for e in _events(fold.read_events(), now) if e["type"] == "deploy-started"]
        deploy_start = max(starts, key=lambda e: _time(e["ts"]))["ts"] if starts else None
    except Exception:
        deploy_start = None
    disk = {}
    for name in ("/", "scratch"):
        try:
            disk[name] = shutil.disk_usage("/" if name == "/" else scratch.root()).free / 1024**3
        except Exception:
            disk[name] = None
    try:
        usage = shutil.disk_usage("/")
        used_fraction = usage.used / usage.total
    except Exception:
        used_fraction = None
    return dict(used_fraction=used_fraction, backup=pushed, mirror_ahead_s=mirror_ahead(now), release_log_failed=failed,
                locks=dict(deploy=dict(held=lock_held(fold.ROOT / "deploy.lock"), since=deploy_start),
                           release=dict(held=lock_held("/var/lock/albert-scott-daily-release.lock"), since=release_start)),
                disk_free_gb=disk)


def _read_ledger():
    # Empty is readable; missing or inaccessible is not a clean ledger.
    directory = fold.EVENTS
    stat = directory.stat()
    if not directory.is_dir() or not stat.st_mode & 0o444 or not stat.st_mode & 0o111:
        raise OSError("health alarms ledger is unreadable")
    for path in directory.glob("*.jsonl"):
        if not path.stat().st_mode & 0o444:
            raise OSError("health alarms ledger is unreadable")
    return fold.read_events()


def _plan(alarm, events, now):
    window = 86400 if alarm["alarm"] in ("release-failed", "disk-used") else 21600
    prior = [e for e in _events(events, now, window) if e["type"] == "health-alarm" and e.get("alarm") == alarm["alarm"]]
    key = alarm["reading"].get("key", "ongoing")
    append = not any(e.get("severity") == alarm["severity"] and _dict(e.get("reading")).get("key", "ongoing") == key for e in prior)
    send = alarm["severity"] == "immediate" and not any(e.get("telegram") == "sent" for e in prior)
    failed = [e for e in prior if e.get("telegram") == "failed"]
    if failed and (now - max(_time(e["ts"]) for e in failed)).total_seconds() < 3600:
        send = False
    return append, send


def run(*, now=None, probes=None, sender=None, dry_run=False):
    try:
        now = _time(now) if now is not None else datetime.now(timezone.utc)
        events = _read_ledger()
        alarms = evaluate(events, now, probes=live_probes(now) if probes is None else probes)
        plans = [(alarm, *_plan(alarm, events, now)) for alarm in alarms]
        message = lambda alarm: f"HEALTH {alarm['alarm']} {alarm['severity']}: {alarm['line']}"[:1000]
        if dry_run:
            print(json.dumps(dict(alarms=alarms, telegrams=[message(a) for a, _, send in plans if send]), sort_keys=True))
            return 0
        for alarm, append, send in plans:
            telegram = "none"
            if send:
                try:
                    argv = [str(fold.ROOT / "telegram/tg-send.sh"), message(alarm)]
                    result = sender(argv) if sender else subprocess.run(argv, capture_output=True, text=True,
                        env={**os.environ, "TG_SENDER": "health-alarms"}, timeout=60)
                    lines = (result.stdout or "").splitlines()
                    telegram = "sent" if result.returncode == 0 and lines and lines[-1] == "200" else "failed"
                except Exception:
                    telegram = "failed"
            if append or send:
                fields = dict(alarm, telegram=telegram, ts=now.isoformat())
                if not append:
                    fields["retry"] = True
                previous = os.environ.get("DOIT_LEDGER_FILE")
                os.environ["DOIT_LEDGER_FILE"] = LEDGER_NAME
                try:
                    fold.append(["health-alarm", "health-" + alarm["alarm"]] +
                                [f"{k}:={json.dumps(v)}" for k, v in fields.items()], strict=True)
                finally:
                    if previous is None:
                        os.environ.pop("DOIT_LEDGER_FILE", None)
                    else:
                        os.environ["DOIT_LEDGER_FILE"] = previous
        print(f"HEALTH CHECK {now.isoformat()}: alarms={len(alarms)}")
        return 0
    except (Exception, SystemExit) as exc:
        print(f"health-alarms: {type(exc).__name__}: {_text(exc)}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
