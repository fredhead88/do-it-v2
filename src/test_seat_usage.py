#!/usr/bin/env python3
"""Hermetic seat usage regression checks, runnable directly or at collection."""
import copy
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
from unittest import mock

_TEMP = tempfile.TemporaryDirectory(prefix="seat-usage-test-")
TMP = pathlib.Path(_TEMP.name)
os.environ["HOME"] = str(TMP / "home")
os.environ["DOIT_ROOT"] = str(TMP / "root")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold
import seat_usage

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/statusline.sh"
OLD = REPO / "src/fixtures/statusline_old.sh"
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
STAMP = NOW.isoformat().replace("+00:00", "Z")
PAYLOAD = {
    "model": {"display_name": "Opus 4.5"}, "agent": {"name": "planner"},
    "context_window": {"used_percentage": 41},
    "rate_limits": {"five_hour": {"used_percentage": 37.5, "resets_at": 1800000000},
                    "seven_day": {"used_percentage": 62, "resets_at": 1800200000}},
    "cost": {"total_cost_usd": 1.25}, "session_id": "session_1", "cwd": "/workspace",
}


def run_line(payload, root, script=SCRIPT):
    result = subprocess.run([str(script)], input=json.dumps(payload).encode(),
                            capture_output=True, env={**os.environ, "DOIT_ROOT": str(root)})
    assert result.returncode == 0 and result.stderr == b"", result
    return result.stdout


def state(root, **changes):
    value = {**copy.deepcopy(PAYLOAD["rate_limits"]),
             "observed_at": STAMP, "session_id": PAYLOAD["session_id"]}
    value.update(changes)
    path = root / "state/claude-usage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def cli(root, *args, expected=0):
    result = subprocess.run([str(REPO / "doit"), "seat-usage", *args],
                            capture_output=True, text=True,
                            env={**os.environ, "DOIT_ROOT": str(root),
                                 "DOIT_LEDGER_FILE": "L-operator-local.jsonl"})
    assert result.returncode == expected, (result.stdout, result.stderr, result.returncode)
    assert not result.stderr, result.stderr
    return result.stdout


def main():
    # stdout is compared to the frozen original, including its lack of newline.
    for label, omitted in (("all", None), ("no-rate", "rate_limits"),
                           ("no-context", "context_window"), ("no-agent", "agent"),
                           ("no-cost", "cost"), ("seven-only", "five_hour")):
        payload = copy.deepcopy(PAYLOAD)
        if omitted == "five_hour":
            del payload["rate_limits"][omitted]
        elif omitted:
            del payload[omitted]
        before = run_line(payload, TMP / label, OLD)
        assert before == run_line(payload, TMP / label) and not before.endswith(b"\n")
        print("1129-AC1", label, "same")
    mode = subprocess.check_output(["git", "ls-files", "-s", "scripts/statusline.sh"], cwd=REPO, text=True).split()[0]
    assert mode == "100755", mode
    print("1129-AC1 mode", mode)

    root = TMP / "writes"
    rendered = run_line(PAYLOAD, root)
    usage_path = root / "state/claude-usage.json"
    pane_path = root / "state/pane-ctx/session_1.json"
    usage, pane = json.loads(usage_path.read_text()), json.loads(pane_path.read_text())
    assert set(usage) == {"five_hour", "seven_day", "observed_at", "session_id"}
    for window in ("five_hour", "seven_day"):
        assert usage[window] == PAYLOAD["rate_limits"][window]
        assert set(usage[window]) == {"used_percentage", "resets_at"}
    assert usage["session_id"] == PAYLOAD["session_id"]
    assert usage["observed_at"].endswith("Z")
    assert abs((dt.datetime.fromisoformat(usage["observed_at"].replace("Z", "+00:00")) - NOW).total_seconds()) < 60
    assert set(pane) == {"used_percentage", "observed_at", "cwd"}
    assert pane == {"used_percentage": 41, "observed_at": usage["observed_at"], "cwd": "/workspace"}
    print("1129-AC2 usage keys", sorted(usage), "pane keys", sorted(pane))
    no_rate = {k: v for k, v in PAYLOAD.items() if k != "rate_limits"}
    run_line(no_rate, TMP / "skip-rate")
    assert not (TMP / "skip-rate/state/claude-usage.json").exists()
    print("1129-AC2 no-rate skipped")
    before = usage_path.read_bytes()
    run_line(no_rate, root)
    assert usage_path.read_bytes() == before
    print("1129-AC2 existing unchanged")
    no_context = {**PAYLOAD, "context_window": {}}
    run_line(no_context, TMP / "skip-context")
    assert not (TMP / "skip-context/state/pane-ctx").exists()
    print("1129-AC2 no-context skipped")

    stop, errors = threading.Event(), []
    def reader():
        while not stop.is_set():
            try:
                for path in (usage_path, pane_path):
                    assert isinstance(json.loads(path.read_text()), dict)
                stop.wait(0.001)
            except BaseException as error:
                errors.append(error)
                return
    worker = threading.Thread(target=reader)
    worker.start()
    try:
        for _ in range(200):
            assert run_line(PAYLOAD, root) == rendered
    finally:
        stop.set()
        worker.join()
    assert not errors, errors
    print("1129-AC3 reader-clean 200/200")
    for i, session in enumerate(("../x", "a/b", "", "a" * 65)):
        unsafe_root = TMP / f"unsafe-{i}"
        assert run_line({**PAYLOAD, "session_id": session}, unsafe_root) == rendered
        assert not (unsafe_root / "state/pane-ctx").exists()
        print("1129-AC3 unsafe-id skipped")
    blocked = TMP / "blocked"
    blocked.mkdir()
    (blocked / "state").write_text("not a directory")
    assert not os.path.isdir(blocked / "state")
    assert run_line(PAYLOAD, blocked) == rendered
    print("1129-AC3 unwritable ok")

    fresh_root = TMP / "fresh"
    path = state(fresh_root)
    fresh = seat_usage.latest(fresh_root, NOW)
    assert fresh == {
        "five_hour_pct": 37.5, "seven_day_pct": 62,
        "five_hour_resets_at": dt.datetime.fromtimestamp(1800000000, dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "seven_day_resets_at": dt.datetime.fromtimestamp(1800200000, dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "observed_at": STAMP, "session": "session_1",
    }, fresh
    print("1129-AC4 a", fresh)
    assert seat_usage.latest(TMP / "absent", NOW) is None
    print("1129-AC4 b None")
    cases = {
        "c": {"observed_at": (NOW - dt.timedelta(seconds=900)).isoformat()},
        "d": {"observed_at": (NOW + dt.timedelta(seconds=61)).isoformat()},
        "e": "broken", "f": "five_hour", "g": "seven_day",
        "h": {"five_hour": {"used_percentage": "bad", "resets_at": 1800000000}},
    }
    for label, change in cases.items():
        path = state(fresh_root)
        if change == "broken":
            path.write_text("{")
        elif isinstance(change, str):
            data = json.loads(path.read_text())
            del data[change]
            path.write_text(json.dumps(data))
        else:
            state(fresh_root, **change)
        assert seat_usage.latest(fresh_root, NOW) is None, label
        print("1129-AC4", label, "None")

    # Production append, fixture ledger and controlled clock; no write mock.
    ledger_root = pathlib.Path(os.environ["DOIT_ROOT"])
    path = state(ledger_root)
    with mock.patch.object(fold, "NOW", NOW):
        first = seat_usage.record(now=NOW)
        assert first["action"] == "appended", first
        print("1129-AC5 a appended")
        assert seat_usage.record(now=NOW + dt.timedelta(seconds=60))["action"] == "none"
        print("1129-AC5 b none")
        original = json.loads(path.read_text())
        for window, field in (("five_hour", "used_percentage"), ("seven_day", "used_percentage"),
                              ("five_hour", "resets_at"), ("seven_day", "resets_at")):
            data = json.loads(path.read_text())
            data[window][field] += 1
            path.write_text(json.dumps(data))
            assert seat_usage.record(now=NOW)["action"] == "appended"
        print("1129-AC5 c appended (both percentages and both reset times)")
        assert seat_usage.record(now=NOW + dt.timedelta(seconds=839))["action"] == "none"
        assert seat_usage.record(now=NOW + dt.timedelta(seconds=840))["action"] == "appended"
        print("1129-AC5 d appended")
        result = seat_usage.record(now=NOW + dt.timedelta(seconds=1200))
        assert result["action"] == "none" and result["reason"] == "stale"
        print("1129-AC5 e none stale")
        for window in ("five_hour", "seven_day"):
            original[window]["used_percentage"] = 0
        path.write_text(json.dumps(original))
        zero = seat_usage.record(now=NOW)["event"]
        assert zero["five_hour_pct"] == zero["seven_day_pct"] == "0"
        print("1129-AC5 f appended strings 0")

    # Ledger string coercion must also preserve boolean-looking session ids.
    state(ledger_root, session_id="true")
    with mock.patch.object(fold, "NOW", NOW + dt.timedelta(seconds=840)):
        result = seat_usage.record(now=NOW + dt.timedelta(seconds=840))
    assert result["event"]["session"] == "true"

    roundtrip = TMP / "roundtrip"
    state(roundtrip)
    cli(roundtrip)
    read_code = '''import json, fold
rows = [e for e in fold.read_events() if e["type"] == "seat-usage"]
assert len(rows) == 1
print(json.dumps(rows[0]))'''
    result = subprocess.run(["/usr/bin/python3", "-c", read_code], cwd=REPO / "src",
                            capture_output=True, text=True,
                            env={**os.environ, "DOIT_ROOT": str(roundtrip)})
    assert result.returncode == 0, result.stderr
    event = json.loads(result.stdout)
    assert event["subject"] == "seat-claude" and event["actor"] == "seat-usage"
    assert all(event[key] == str(value) for key, value in fresh.items())
    assert isinstance(event["five_hour_pct"], str) and isinstance(event["seven_day_pct"], str)
    print("1129-AC6 roundtrip kept")
    ledger = roundtrip / "events/L-seat-usage-local.jsonl"
    assert "actor" not in json.loads(ledger.read_text())
    assert not (roundtrip / "events/L-operator-local.jsonl").exists()
    print("1129-AC6 no-actor")
    before = ledger.read_bytes()
    state(roundtrip, five_hour={"used_percentage": 12, "resets_at": 1800000000})
    assert "12" in cli(roundtrip, "--dry-run")
    assert ledger.read_bytes() == before
    print("1129-AC7 dry-run unchanged")
    dry = json.loads(cli(roundtrip, "--dry-run", "--json"))
    assert set(dry) == {"action", "reason", "event"} and dry["event"]["five_hour_pct"] == "12"
    result = json.loads(cli(roundtrip, "--json"))
    assert result["action"] == "appended" and result["event"]["five_hour_pct"] == "12"
    assert json.loads(cli(roundtrip, "--json"))["action"] == "none"
    print("1129-AC7 json ok")
    for malformed in ("{", "{}"):
        (roundtrip / "state/claude-usage.json").write_text(malformed)
        result = json.loads(cli(roundtrip, "--json", expected=1))
        assert result["action"] == "none" and result["reason"]
    print("1129-AC7 corrupt exit 1")
    for idle in (TMP / "idle", roundtrip):
        if idle == roundtrip:
            state(idle, observed_at=(NOW - dt.timedelta(seconds=1200)).isoformat())
        assert json.loads(cli(idle, "--json"))["action"] == "none"
    print("1129-AC7 idle exit 0")

    home = TMP / "link-home"
    dest = home / ".claude/statusline.sh"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"original\n")
    settings = dest.parent / "settings.json"
    settings.write_bytes(b"not JSON -- must never be read")
    def link():
        result = subprocess.run(["/usr/bin/python3", str(REPO / "src/seat_usage.py"), "link-statusline"],
                                env={**os.environ, "HOME": str(home)}, capture_output=True)
        assert result.returncode == 0, result.stderr
    link()
    backups = list(dest.parent.glob("statusline.sh.bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"original\n"
    assert backups[0].name == "statusline.sh.bak-" + dt.datetime.now().strftime("%Y%m%d")
    assert dest.is_symlink() and dest.resolve() == SCRIPT
    print("1129-AC8 a ok")
    before_target = os.readlink(dest)
    before_stat = dest.lstat()
    link()
    after_stat = dest.lstat()
    assert os.readlink(dest) == before_target
    assert (after_stat.st_dev, after_stat.st_ino) == (before_stat.st_dev, before_stat.st_ino)
    assert list(dest.parent.glob("statusline.sh.bak-*")) == backups
    print("1129-AC8 b ok")
    dest.unlink()
    dest.symlink_to(home / "different")
    link()
    extra = [p for p in dest.parent.glob("statusline.sh.bak-*") if p not in backups]
    assert len(extra) == 1 and extra[0].is_symlink() and os.readlink(extra[0]) == str(home / "different")
    print("1129-AC8 c ok")
    dest.unlink()
    link()
    assert dest.resolve() == SCRIPT
    print("1129-AC8 d ok")
    assert backups[0].read_bytes() == b"original\n" and extra[0].name.endswith(".1")
    print("1129-AC8 e ok")
    assert settings.read_bytes() == b"not JSON -- must never be read"
    assert "settings.json" not in (REPO / "src/seat_usage.py").read_text()
    print("1129-AC8 f ok")
    install = (REPO / "install.sh").read_text()
    assert install.count("seat_usage.py link-statusline") == 1
    assert install.index("seat_usage.py") > install.index("link-agents")
    cli(TMP / "dispatch", "--dry-run")
    print("1129-AC9 dispatch ok")
    assert "seat-usage" in subprocess.check_output([str(REPO / "doit"), "help"], text=True)
    print("1129-AC9 help ok")


def test_seat_usage():
    main()



def usage_view():
    import dispatch
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        path = state(root)
        before = {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()}
        with mock.patch.object(dispatch, 'codex_quota', return_value=dict(used_percent=42)):
            assert seat_usage.usage_view(root, NOW) == dict(codex=dict(used=42, limit=100), claude=dict(used=62, limit=100))
        after = {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()}
        assert before == after
        with mock.patch.object(dispatch, 'codex_quota', return_value=None):
            assert seat_usage.usage_view(root, NOW)['codex']['used'] is None
            assert seat_usage.usage_view(root, NOW + dt.timedelta(seconds=900))['claude']['used'] is None
            path.write_text('{')
            assert seat_usage.usage_view(root, NOW)['claude']['used'] is None
            path.unlink()
            assert seat_usage.usage_view(root, NOW)['claude']['used'] is None


def nullable_windows():
    """Strict production append and independent cold-reader proof of unknowns."""
    root = TMP / 'nullable'
    cases = {
        'null-block': None,
        'null-members': dict(used_percentage=None, resets_at=None),
        'null-percentage': dict(used_percentage=None, resets_at=1800000000),
        'null-reset': dict(used_percentage=37.5, resets_at=None),
    }
    for name, block in cases.items():
        state(root, five_hour=block)
        reading = seat_usage.latest(root, NOW)
        assert reading is not None, name
        assert reading['five_hour_pct'] is reading['five_hour_resets_at'] is None
        assert reading['seven_day_pct'] == 62
        print('2179-AC1', name, 'weekly=62 five-hour=null')
    for name, change in {
        'null-weekly': dict(seven_day=None),
        'null-weekly-member': dict(seven_day=dict(used_percentage=None, resets_at=1800000000)),
        'bool': dict(five_hour=dict(used_percentage=True, resets_at=1800000000)),
        'infinite': dict(five_hour=dict(used_percentage=float('inf'), resets_at=1800000000)),
        'nan': dict(seven_day=dict(used_percentage=float('nan'), resets_at=1800000000)),
        'partial-null-malformed': dict(five_hour=dict(used_percentage=None, resets_at='bad')),
        'partial-null-missing': dict(five_hour=dict(used_percentage=None)),
    }.items():
        state(root, **change)
        assert seat_usage.latest(root, NOW) is None, name
        print('2179-AC1 rejected', name)
    state(root, five_hour=None)
    assert json.loads(cli(root, '--dry-run', '--json'))['event']['five_hour_pct'] == 'unknown'
    assert not (root / 'events').exists()
    result = json.loads(cli(root, '--json'))
    assert result['action'] == 'appended', result
    code = "import fold,json; print(json.dumps(fold.read_events(use_cache=False)))"
    rows = json.loads(subprocess.check_output(['/usr/bin/python3', '-c', code],
        cwd=REPO/'src', env={**os.environ, 'DOIT_ROOT': str(root)}, text=True))
    event, = rows
    assert event['actor'] == 'seat-usage' and event['subject'] == 'seat-claude'
    assert event['five_hour_pct'] == event['five_hour_resets_at'] == 'unknown'
    assert event['seven_day_pct'] == '62'
    before = (root / 'events' / seat_usage.LEDGER).read_bytes()
    assert json.loads(cli(root, '--json'))['reason'] == 'unchanged'
    assert (root / 'events' / seat_usage.LEDGER).read_bytes() == before
    state(root)
    assert json.loads(cli(root, '--json'))['reason'] == 'changed'
    state(root, five_hour=None)
    assert json.loads(cli(root, '--json'))['reason'] == 'changed'
    with mock.patch.dict(os.environ, DOIT_ROOT=str(root)), mock.patch.object(fold, 'ROOT', root), \
            mock.patch.object(fold, 'EVENTS', root/'events'):
        prior = seat_usage._prior_event()
        at = seat_usage._time(prior['ts'])
        state(root, five_hour=None, observed_at=seat_usage._iso(at))
        assert seat_usage.record(now=at+dt.timedelta(seconds=839))['reason'] == 'unchanged'
        with mock.patch.object(fold, 'NOW', at+dt.timedelta(seconds=840)):
            assert seat_usage.record(now=at+dt.timedelta(seconds=840))['reason'] == 'heartbeat'
    print('2179-AC1 strict CLI append; cold read-back; transitions; dedupe; 839/840 heartbeat; dry-run')


def prior_event_equality():
    import hashlib
    import ledger_files
    import ledger_cache
    base = dict(type='seat-usage', subject='seat-claude', project='do-it-v2',
                ts=STAMP, five_hour_pct='37.5', seven_day_pct='62',
                five_hour_resets_at='2027-01-15T08:00:00Z',
                seven_day_resets_at='2027-01-17T15:33:20Z')
    cases = {
        'missing': None, 'empty': '',
        'timestamp-order': [base, dict(base, ts=seat_usage._iso(NOW-dt.timedelta(seconds=100)), seven_day_pct='10')],
        'equal-timestamps-forged-actor': [base, dict(base, actor='forged', seven_day_pct='61')],
        'unrelated-type-subject': [base, dict(base, type='other'), dict(base, subject='other')],
        'unknown': [dict(base, five_hour_pct='unknown', five_hour_resets_at='unknown')],
        'projected-and-malformed': [dict(base, type='tick', recorded_reap=['x']),
                                    dict(base, type='tree-reaped', retained=['x']), base],
        'archived-virtual': [base],
    }
    for name, seed in cases.items():
        root = TMP / ('prior-' + name)
        events = root / 'events'
        events.mkdir(parents=True)
        text = '' if not seed else '\n'.join(json.dumps(row) for row in seed)+'\n'
        if name == 'projected-and-malformed':
            text = '{broken\n\n' + text + '{torn'
        if name == 'archived-virtual':
            data = text.encode()
            segments = events/'segments'
            segments.mkdir()
            (segments/'seed.jsonl').write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            (segments/'seed.idx.json').write_text(json.dumps(dict(version=1, segment='seed.jsonl',
                bytes=len(data), sha256=digest, members=[dict(name=seat_usage.LEDGER, offset=0,
                length=len(data), lines=1, sha256=digest, ends_nl=True)])))
        elif seed is not None:
            (events/seat_usage.LEDGER).write_text(text)
        # A different filename cannot impersonate this actor, even with a forged payload.
        (events/'L-builder-fixture.jsonl').write_text(json.dumps(dict(base, actor='seat-usage',
            ts=seat_usage._iso(NOW+dt.timedelta(seconds=1))))+'\n')
        with mock.patch.object(fold, 'ROOT', root), mock.patch.object(fold, 'EVENTS', events), \
                mock.patch.dict(os.environ, DOIT_ROOT=str(root)):
            oracle = next((e for e in reversed(fold.read_events(use_cache=False))
                if e.get('type') == 'seat-usage' and e.get('subject') == 'seat-claude'
                and e.get('actor') == 'seat-usage'), None)
            with mock.patch.object(ledger_files, 'read_text', wraps=ledger_files.read_text) as reads, \
                    mock.patch.object(fold, 'read_events', side_effect=AssertionError('whole-ledger lookup')), \
                    mock.patch.object(ledger_cache, 'read', side_effect=AssertionError('cache lookup')), \
                    mock.patch.object(ledger_cache, 'tail_refresh', side_effect=AssertionError('tail lookup')):
                assert seat_usage._prior_event() == oracle, name
                assert reads.call_count == 1
                assert reads.call_args.args == (events, seat_usage.LEDGER)
                for block in (None, PAYLOAD['rate_limits']['five_hour']):
                    for elapsed in (839, 840):
                        at = NOW + dt.timedelta(seconds=elapsed)
                        state(root, five_hour=block, observed_at=seat_usage._iso(at))
                        reading = seat_usage.latest(root, at)
                        expected_fields = {k: 'unknown' if reading[k] is None else str(reading[k])
                                           for k in seat_usage.READING_FIELDS}
                        changed = oracle is None or any(expected_fields[k] != oracle.get(k)
                                                       for k in seat_usage.READING_FIELDS)
                        reason = 'changed' if changed else ('heartbeat' if
                            (at-seat_usage._time(oracle['ts'])).total_seconds() >= 840 else 'unchanged')
                        result = seat_usage.record(dry_run=True, now=at)
                        assert result['reason'] == ('unchanged' if reason == 'unchanged' else 'dry-run: '+reason), (name, result)
                print('2179-AC2', name, 'prior/content/actor/_src equal; own-file reads=', reads.call_count,
                      'full/cache/tail lookup=0; numeric/unknown decisions 839/840 equal')


def run_groups():
    import argparse, hashlib
    parser = argparse.ArgumentParser()
    parser.add_argument('-k')
    args = parser.parse_args()
    groups = dict(legacy=test_seat_usage, usage_view=usage_view,
                  nullable_windows=nullable_windows, prior_event_equality=prior_event_equality)
    selected = [name for name in groups if args.k is None or args.k in name]
    if not selected:
        parser.exit(1, 'no group matches: ' + args.k + '\n')
    for name in selected:
        groups[name]()
        body = name + ': passed'
        print(json.dumps(dict(url='cli://seat-usage/' + name, status=0,
                             body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))


if __name__ == '__main__':
    run_groups()
