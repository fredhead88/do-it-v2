#!/usr/bin/env python3
"""One bounded, local scan of verified role sessions for the Stay dialog."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import tomllib
from datetime import datetime, timezone

from panes import _is_live, ledger_role

REPO = Path(__file__).resolve().parent.parent
DEFAULTS = {"dialog_text": "Background work is running", "capture_lines": 40, "settle_s": 2}
TARGET = re.compile(r"[A-Za-z0-9_.:@%\-]+")
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*?(?:\x07|\x1b\\))")
OPTION = re.compile(r"^(?:❯\s*)?(\d+)\.\s*(.+)$")
PROMPT = re.compile(r"^(?:❯(?!\s*\d+\.)|›|[$#])")


def _stamp(now=None):
    now = now() if callable(now) else now
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _date(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result


def _config():
    try:
        with (REPO / "look.toml").open("rb") as stream:
            cfg = tomllib.load(stream)["pane_watchdog"]
        if (not isinstance(cfg["dialog_text"], str) or not cfg["dialog_text"].strip()
                or type(cfg["capture_lines"]) is not int or not 1 <= cfg["capture_lines"] <= 1000
                or type(cfg["settle_s"]) not in (int, float) or not 0 <= cfg["settle_s"] <= 30):
            raise ValueError("invalid watchdog settings")
        return {k: cfg[k] for k in DEFAULTS}
    except (OSError, ValueError, KeyError, TypeError):
        return dict(DEFAULTS)


def _target(value):
    return isinstance(value, str) and TARGET.fullmatch(value) is not None


def _dialog(text, cfg):
    lines = [s.strip() for s in ANSI.sub("", text).splitlines()[-cfg["capture_lines"]:] if s.strip()][-10:]
    for index, line in enumerate(lines):
        if line.casefold() != cfg["dialog_text"].strip().casefold():
            continue
        options = [(n, OPTION.fullmatch(s)) for n, s in enumerate(lines[index + 1:], index + 1)]
        options = [(n, m) for n, m in options if m]
        if len(options) < 2:
            return None, "unresolved"
        if any(PROMPT.match(s) for s in lines[options[0][0] + 1:]):
            return None, "not-dialog"
        for _, match in options:
            if re.match(r"Stay\b", match[2], re.IGNORECASE):
                return ("3" if match[1] == "3" else "Escape"), "dialog"
        return None, "unresolved"
    return None, "not-dialog"


def scan(panes, capture):
    """Return actionable rows only. Capture errors never authorize a send."""
    rows = []
    for pane in panes:
        if not _target(pane.get("target")):
            continue
        try:
            key, _ = _dialog(capture(pane["target"]), DEFAULTS)
        except (OSError, subprocess.SubprocessError):
            continue
        if key:
            rows.append({"pane": pane["name"], "role": pane["role"], "target": pane["target"],
                         "seen_at": _stamp(), "stay_key": key})
    return rows


def resolve(row, *, send):
    """Send one bare Stay key; never Enter or any captured text."""
    if row.get("stay_key") not in ("3", "Escape") or not _target(row.get("target")):
        raise ValueError("unactionable dialog")
    resolved_at = row.get("resolved_at") or _stamp()
    send(row["target"], row["stay_key"])
    return {"pane": row["pane"], "role": row["role"], "prev_scan_at": row.get("prev_scan_at"),
            "seen_at": row["seen_at"], "resolved_at": resolved_at,
            "choice": "stay" if row["stay_key"] == "3" else "stay-esc"}


def _atomic(path, value):
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _read(path):
    return json.loads(path.read_text())


def _append(root, typ, subject, fields):
    argv = [str(REPO / "doit"), "append", typ, subject]
    argv.extend(f"{k}:={json.dumps(v)}" for k, v in fields.items())
    result = subprocess.run(argv, env={**os.environ, "DOIT_ROOT": str(root),
                                      "DOIT_LEDGER_FILE": "L-pane-watchdog-local.jsonl"},
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("watchdog append refused")


def _roster(sessions, log):
    roster, unreadable, targets = [], 0, set()
    for path in sorted(sessions.glob("*.json")):
        if not path.stem.isdecimal():
            continue
        try:
            meta = _read(path)
            role = ledger_role(meta.get("name"))
            if not role or not _is_live(int(path.stem), meta):
                continue
            name, target = meta["name"], meta.get("tmux")
            if not _target(target) or target in targets:
                unreadable += 1
                log(name, role, "unreadable", "missing-invalid-or-duplicate-target")
                continue
            targets.add(target)
            roster.append({"name": name, "role": role, "target": target})
        except (OSError, ValueError, TypeError, AttributeError):
            unreadable += 1
            log(None, None, "unreadable", "session-metadata")
    return roster, unreadable


def main(argv=None, *, root=None, now=None, capture=None, send=None, sessions_dir=None, tmux_argv=("tmux",)):
    root = Path(root or os.environ.get("DOIT_ROOT", Path.home() / ".do-it"))
    state, logs = root / "state", root / "logs"
    state.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    cfg, ts = _config(), _stamp(now)

    def log(pane, role, result, capture_status):
        with (logs / "pane-watchdog.log").open("a") as stream:
            stream.write(json.dumps({"ts": ts, "pane": pane, "role": role, "roster": pane is not None,
                                     "capture": capture_status, "result": result}) + "\n")

    def tmux(*args):
        return subprocess.run([*tmux_argv, *args], capture_output=True, text=True, check=True, timeout=5).stdout

    if capture is None:
        capture = lambda target: tmux("capture-pane", "-p", "-t", target, "-S", str(-cfg["capture_lines"]))
    if send is None:
        send = lambda target, key: tmux("send-keys", "-t", target, key)
    heartbeat, cap_path = state / "pane-watchdog.json", state / "pane-watchdog-sends.json"
    marker = state / "pane-watchdog-sends.initialized"
    # Serialize concurrent direct/cron runs, including the pre-send reservation.
    with (state / "pane-watchdog.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            prev = _read(heartbeat)["ts"]
            _date(prev)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            prev = None
        try:
            caps = _read(cap_path)
            if not isinstance(caps, dict):
                raise ValueError("invalid caps")
            for name, stamp in caps.items():
                if not ledger_role(name):
                    raise ValueError("invalid cap identity")
                _date(stamp)
        except FileNotFoundError:
            caps = None if marker.exists() else {}
        except (OSError, ValueError, TypeError, AttributeError):
            caps = None
        scanned, dialogs, unreadable, rc = 0, 0, 0, 0
        try:
            roster, unreadable = _roster(Path(sessions_dir) if sessions_dir is not None else Path.home() / ".claude/sessions", log)
            log(None, None, "scan", f"roster={len(roster)}")
            for pane in roster:
                name, role, target = pane["name"], pane["role"], pane["target"]
                try:
                    key, result = _dialog(capture(target), cfg)
                except (OSError, subprocess.SubprocessError):
                    unreadable += 1
                    log(name, role, "unreadable", "failed")
                    continue
                scanned += 1
                seen_at = _stamp(now)
                if key is None:
                    log(name, role, result, "ok")
                    continue
                dialogs += 1
                if caps is None or (name in caps and (_date(ts) - _date(caps[name])).total_seconds() < 1800):
                    log(name, role, "unresolved-cap", "ok")
                    continue
                try:
                    try:
                        key, result = _dialog(capture(target), cfg)
                    except (OSError, subprocess.SubprocessError):
                        unreadable += 1
                        scanned -= 1
                        log(name, role, "unreadable", "pre-send-failed")
                        continue
                    if key is None:
                        log(name, role, result, "pre-send")
                        continue
                    # A missing cap after an earlier write must never reset it.
                    marker.touch(exist_ok=True)
                    attempted_at = _stamp(now)
                    caps[name] = attempted_at
                    _atomic(cap_path, caps)
                    event = resolve({"pane": name, "role": role, "target": target, "stay_key": key,
                                     "prev_scan_at": prev, "seen_at": seen_at, "resolved_at": attempted_at}, send=send)
                    time.sleep(cfg["settle_s"])
                    try:
                        _, after = _dialog(capture(target), cfg)
                    except (OSError, subprocess.SubprocessError):
                        unreadable += 1
                        scanned -= 1
                        log(name, role, "unreadable", "post-send-failed")
                        continue
                    if after != "not-dialog":
                        log(name, role, "still-showing", "post-send")
                        continue
                    _append(root, "pane-dialog-resolved", name, event)
                    _append(root, "lesson", name, {"problem": "planner-exit-dialog",
                                                   "why": f"Watchdog pressed Stay ({event['choice']}) on {name} "
                                                          f"(role {role}) seen at {seen_at}."})
                    log(name, role, "resolved", "post-send")
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    log(name, role, "unreadable", "resolve-failed")
                    rc = 1
        finally:
            _atomic(heartbeat, {"ts": ts, "panes_scanned": scanned, "dialogs": dialogs, "unreadable": unreadable})
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
