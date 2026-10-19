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


if __name__ == "__main__":
    test_seat_usage()
