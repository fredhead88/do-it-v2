#!/usr/bin/env python3
"""Health alarms, pure evaluation and bounded ledger/Telegram delivery."""
import ledger_files
import argparse
import fcntl
import tempfile
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
import notification_policy
import scratch
import tg_say

LOCK_STALE_S = 4200
ALARMS = ("mirror-stale", "release-failed", "deploy-lock-stale",
          "sweeper-unobservable", "disk-low", "disk-used", "preflight-refusals", "ledger-segment-invalid", "flow-health", "memory-low")
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


MEMORY_LIMIT = 3 * 1024**3


def mem_available(text):
    """Linux meminfo uses KiB despite its kB unit spelling."""
    if not isinstance(text, str):
        return None
    rows = re.findall(r"^MemAvailable:\s+(\d+) kB\s*$", text, re.M)
    return int(rows[0]) * 1024 if len(rows) == 1 else None


def memory_observation(available, prior, now):
    """Advance only elapsed time bracketed by valid low samples."""
    valid = isinstance(available, int) and not isinstance(available, bool) and available >= 0
    prior = _dict(prior)
    def aware(value):
        try:
            instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return _time(instant) if instant.tzinfo is not None else None
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
    measured = aware(prior.get('measured_at'))
    since = aware(prior.get('low_since'))
    previous = prior.get('available_bytes')
    continuous = (valid and available < MEMORY_LIMIT and isinstance(previous, int) and not isinstance(previous, bool)
                  and 0 <= previous < MEMORY_LIMIT and measured is not None and since is not None
                  and since <= measured <= now and prior.get('episode') == since.isoformat())
    low_since = since if continuous else now if valid and available < MEMORY_LIMIT else None
    return dict(measured_at=now.isoformat(), available_bytes=available if valid else None,
                low_since=low_since.isoformat() if low_since else None,
                episode=low_since.isoformat() if low_since else None)


def evaluate(events, now, *, probes, observation_state=None):
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

    memory = memory_observation(p.get('memory_available_bytes'), _dict(observation_state).get('memory'), now)
    if memory['available_bytes'] is None:
        add('memory-low', dict(available_bytes=None), 'Available memory undetermined', unknown=('memory_available_bytes',))
    elif memory['low_since'] is not None:
        duration = (now - _time(memory['low_since'])).total_seconds()
        add('memory-low', dict(memory, sampled_low_seconds=duration),
            f"Available memory below 3 GiB across {int(duration//60)} minutes of low samples", duration >= 900)

    issues = p.get("ledger_segments", [])
    if issues:
        add("ledger-segment-invalid", dict(count=len(issues), problems=issues[:5]),
            f"ledger segment problems={len(issues)}: {issues[:5]}", True)

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
        add("release-failed", dict(key=newest.get("ts", "ongoing"), failures=len(all_failures),
                                   production_down=any(e.get("production_down") is True for e in releases)),
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
    """Newest firing per alarm (flow-health per measure/day) in 24h, then the newest complete base-red per project."""
    now = _time(now)
    alarms, bases = {}, {}
    for e in sorted(_events(events, now), key=lambda e: _time(e["ts"])):
        if e["type"] == "health-alarm" and e.get("alarm") in ALARMS and (now - _time(e["ts"])).total_seconds() <= 86400:
            key = (e["alarm"], e.get("measure", ""), e.get("day", "")) if e["alarm"] == "flow-health" else (e["alarm"], "", "")
            alarms[key] = e
        if e["type"] == "base-red" and e.get("complete") is True and isinstance(e.get("project"), str):
            bases[e["project"]] = e
    lines = [f"HEALTH {name} {_text(e.get('severity'))}: {_text(e.get('line'))}" for (name, _, _), e in sorted(alarms.items())]
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
    issues = ledger_files.problems(fold.EVENTS) + ledger_files.verify_segments(fold.EVENTS, fold.ROOT / "state")
    try:
        available = mem_available(pathlib.Path('/proc/meminfo').read_text())
    except (OSError, UnicodeError, ValueError):
        available = None
    return dict(memory_available_bytes=available, ledger_segments=issues, used_fraction=used_fraction, backup=pushed, mirror_ahead_s=mirror_ahead(now), release_log_failed=failed,
                locks=dict(deploy=dict(held=lock_held(fold.ROOT / "deploy.lock"), since=deploy_start),
                           release=dict(held=lock_held("/var/lock/albert-scott-daily-release.lock"), since=release_start)),
                disk_free_gb=disk)


def _read_ledger():
    # Empty is readable; missing or inaccessible is not a clean ledger.
    directory = fold.EVENTS
    stat = directory.stat()
    if not directory.is_dir() or not stat.st_mode & 0o444 or not stat.st_mode & 0o111:
        raise OSError("health alarms ledger is unreadable")
    ledger_files.assert_readable(directory)
    return fold.read_events()


def _plan(alarm, events, now):
    window = 86400 if alarm["alarm"] in ("release-failed", "disk-used") else 21600
    prior = [e for e in _events(events, now, window) if e["type"] == "health-alarm" and e.get("alarm") == alarm["alarm"]]
    key = alarm["reading"].get("key", "ongoing")
    append = not any(e.get("severity") == alarm["severity"] and _dict(e.get("reading")).get("key", "ongoing") == key for e in prior)
    send = alarm["severity"] == "immediate" and (notification_policy.health_interrupt(alarm) or
           (alarm['alarm'] == 'memory-low' and _number(alarm['reading'].get('sampled_low_seconds'))
            and alarm['reading']['sampled_low_seconds'] >= 900 and
            isinstance(alarm['reading'].get('available_bytes'), int) and
            not isinstance(alarm['reading']['available_bytes'], bool) and
            0 <= alarm['reading']['available_bytes'] < MEMORY_LIMIT)) and not any(e.get("telegram") == "sent" for e in prior)
    failed = [e for e in prior if e.get("telegram") == "failed"]
    if failed and (now - max(_time(e["ts"]) for e in failed)).total_seconds() < 3600:
        send = False
    return append, send


def message(alarm):
    descriptions = {
        'memory-low': ('Available memory is low.', 'Low available memory has persisted across at least 15 minutes of samples.'),
        'release-failed': ('A release needs attention.', 'A failed release has confirmed service impact.'),
        'ledger-segment-invalid': ('The event ledger needs attention.', 'Ledger checks found a confirmed operational impact.'),
    }
    what, real = descriptions.get(alarm['alarm'], ('A machine health check needs attention.',
                                                'A health check found confirmed service impact or ongoing cost.'))
    return notification_policy.format_message('[DOWN]', what, real,
                'Inspect the local health report and restore the affected service.', None,
                'Keeping diagnostic details in the local health ledger.')


def _watchdog_state(path):
    try:
        state = json.loads(path.read_text())
        if state.get('version') == 1 and isinstance(state.get('memory'), dict) and isinstance(state.get('deliveries'), dict):
            return state
    except (OSError, UnicodeError, ValueError, AttributeError):
        pass
    return dict(version=1, memory={}, deliveries={})


def _write_watchdog(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix=path.name+'.', delete=False) as out:
            tmp = pathlib.Path(out.name)
            json.dump(state, out, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        tmp.replace(path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _run_locked(now, probes, sender, dry_run):
    events = _read_ledger()
    path = fold.ROOT / 'state/health-watchdog.json'
    state = _watchdog_state(path)
    probes = live_probes(now) if probes is None else probes
    alarms = evaluate(events, now, probes=probes, observation_state=state)
    memory = memory_observation(_dict(probes).get('memory_available_bytes'), state['memory'], now)
    deliveries = {}
    plans = []
    for alarm in alarms:
        name = alarm['alarm']
        prior = _dict(state['deliveries'].get(name))
        episode = memory['episode'] if name == 'memory-low' else prior.get('episode') or now.isoformat()
        if prior.get('episode') != episode:
            prior = {}
        record = dict(prior, episode=episode)
        append, send = _plan(alarm, events, now)
        attempted = _time(prior.get('attempted_at'))
        if prior.get('disposition') == 'held':
            send = False
        if (prior.get('disposition') in ('failed', 'refused') and attempted is not None
                and 0 <= (now-attempted).total_seconds() < 3600):
            send = False
        plans.append((alarm, append, send, record))
        deliveries[name] = record
    if dry_run:
        print(json.dumps(dict(alarms=alarms, telegrams=[message(a) for a, _, send, _ in plans if send]), sort_keys=True))
        return 0
    for alarm, append, send, record in plans:
        telegram = 'none'
        if send:
            try:
                if sender is not None:
                    # Compatibility only: production delivery always uses tg_say.
                    result = sender([str(fold.ROOT / 'telegram/tg-send.sh'), message(alarm)])
                else:
                    result = tg_say.deliver('health-alarms', message(alarm), label='[DOWN]', root=fold.ROOT)
                telegram = result.state if result.state in ('sent', 'held', 'failed', 'refused') else 'failed'
                if telegram == 'sent' and result.http != 200:
                    telegram = 'failed'
            except Exception:
                telegram = 'failed'
            record.update(disposition=telegram, attempted_at=now.isoformat())
        if append or send:
            fields = dict(alarm, telegram=telegram, ts=now.isoformat())
            if not append:
                fields['retry'] = True
            previous = os.environ.get('DOIT_LEDGER_FILE')
            os.environ['DOIT_LEDGER_FILE'] = LEDGER_NAME
            try:
                fold.append(['health-alarm', 'health-' + alarm['alarm']] +
                            [f'{k}:={json.dumps(v)}' for k, v in fields.items()], strict=True)
            finally:
                if previous is None:
                    os.environ.pop('DOIT_LEDGER_FILE', None)
                else:
                    os.environ['DOIT_LEDGER_FILE'] = previous
    _write_watchdog(path, dict(version=1, memory=memory, deliveries=deliveries))
    print(f'HEALTH CHECK {now.isoformat()}: alarms={len(alarms)}')
    return 0


def run(*, now=None, probes=None, sender=None, dry_run=False):
    try:
        now = _time(now) if now is not None else datetime.now(timezone.utc)
        if dry_run:
            return _run_locked(now, probes, sender, True)
        directory = fold.ROOT / 'state'
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / 'health-watchdog.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return _run_locked(now, probes, sender, False)
    except (Exception, SystemExit) as exc:
        print(f'health-alarms: {type(exc).__name__}: {_text(exc)}', file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
