"""Read-only, allowlisted Codex presence for the realm. Never export rollout bodies."""
import datetime as dt
import json
import pathlib
import re
import subprocess
import time

TERMINAL = {"build-done", "spawn-done", "spawn-failed", "spawn-stale"}


def text(value, limit=80):
    value = str(value or "")
    value = re.sub(r"(?:sk-|ghp_|github_pat_)[\w-]+", "[redacted]", value)
    value = re.sub(r"(?:https?://\S+|(?:~?/)[\w./-]+)", "[path]", value)
    return " ".join(value.split())[:limit]


def stamp(seconds):
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat(timespec="seconds")


def worktree_name(cwd, home):
    try:
        path = pathlib.Path(cwd).resolve().relative_to((home / ".do-it/worktrees").resolve())
        return text(path.name) if path.parts else None
    except (ValueError, OSError, TypeError):
        return None


def ledger_sessions(rows):
    sessions = {}
    for row in sorted(rows, key=lambda r: str(r.get("ts", ""))):
        ident, kind = row.get("spawn"), row.get("type")
        if not ident:
            continue
        if kind in {"build-started", "spawn-started"} and row.get("backend") == "codex":
            sessions.setdefault(ident, {"id": text(ident), "kind": "doit-build", "subject": text(row.get("subject")),
                                       "role": text(row.get("role") or "-".join(ident.split("-")[1:-1])),
                                       "state": "live", "started": row.get("ts"), "last": row.get("ts"), "tokens": 0, "turns": 0})
        session = sessions.get(ident)
        if not session:
            continue
        if kind == "backend-fallback":
            session.update(state="fallback", last=row.get("ts"), fell_back="fell back: " + text(row.get("why"), 120), fallback_at=row.get("ts"))
        elif kind in TERMINAL:
            session.update(state="ended", last=row.get("ts"), tokens=number(row.get("subagent_tokens"), session["tokens"]))
    return list(sessions.values())


def number(value, default=0):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def process_sessions(files):
    """Match a codex process's resume id or open rollout fd, without exporting argv."""
    live = set()
    by_path = {str(p.resolve()): p for p in files}
    ids = {}
    for p in files:
        match = re.search(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", p.name)
        if match:
            ids[match[1]] = p
    for proc in pathlib.Path("/proc").glob("[0-9]*"):
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
            if not argv or pathlib.Path(argv[0].decode(errors="replace")).name != "codex":
                continue
            for arg in argv[1:]:
                ident = arg.decode(errors="replace")
                if ident in ids:
                    live.add(str(ids[ident].resolve()))
            for fd in (proc / "fd").iterdir():
                try:
                    target = str(fd.resolve())
                    if target in by_path:
                        live.add(target)
                except OSError:
                    pass
        except (OSError, ValueError):
            continue
    return live


def tmux_windows():
    try:
        result = subprocess.run(["tmux", "list-windows", "-a", "-F", "#{window_name}\t#{pane_current_path}"],
                                capture_output=True, text=True, timeout=2)
        windows = {}
        for line in result.stdout.splitlines():
            name, sep, cwd = line.partition("\t")
            if sep and name.startswith("codex-"):
                windows[cwd] = text(name)
        return windows
    except (OSError, subprocess.TimeoutExpired):
        return {}


def quota_from(limits):
    quota = {}
    for key in ("primary", "secondary"):
        window = limits.get(key) or {}
        if window.get("window_minutes") == 10080:
            quota.update(weekly_used_pct=number(window.get("used_percent")), resets_at=number(window.get("resets_at")))
    credits = limits.get("credits") or {}
    balance = credits.get("balance")
    if isinstance(balance, (int, float)) or (isinstance(balance, str) and re.fullmatch(r"\d+(?:\.\d+)?", balance)):
        quota["credits_balance"] = balance
    return quota


def read_worker(path, home, now, processes, windows):
    session = {"id": "", "kind": "worker", "role": "worker", "subject": "Codex worker", "tokens": 0, "turns": 0}
    turns, first_user, quota, quota_time = set(), False, {}, ""
    cwd = ""
    modified = path.stat().st_mtime
    with path.open(errors="replace") as source:
        for line in source:
            try:
                row = json.loads(line)
                payload = row.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                ty = row.get("type")
                if ty == "session_meta":
                    session["id"] = text(payload.get("id") or payload.get("session_id"))
                    session["started"] = payload.get("timestamp") or row.get("timestamp")
                    cwd = payload.get("cwd") or ""
                elif ty == "turn_context" or (ty == "event_msg" and payload.get("type") == "task_started"):
                    if payload.get("turn_id"):
                        turns.add(payload["turn_id"])
                elif ty == "response_item" and payload.get("type") == "message" and payload.get("role") == "user" and not first_user:
                    first_user = True
                    for part in payload.get("content") or []:
                        if not isinstance(part, dict) or part.get("type") != "input_text":
                            continue
                        heading = re.search(r"^#\s+(.+)$", part.get("text") or "", re.M)
                        if heading:
                            session["subject"] = text(heading[1])
                            break
                elif ty == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    session["tokens"] = number((info.get("total_token_usage") or {}).get("total_tokens"), session["tokens"])
                    latest = quota_from(payload.get("rate_limits") or {})
                    if latest:
                        quota.update(latest)
                        quota_time = row.get("timestamp") or stamp(modified)
            except (ValueError, TypeError, AttributeError):
                continue  # partial last JSON line while a worker writes
    if not session["id"]:
        return None, {}, ""
    age = now - modified
    live = session["id"] in processes or str(path.resolve()) in processes
    session.update(state="live" if age < 180 and live else "idle" if age < 1800 else "ended",
                   last=stamp(modified), turns=len(turns))
    session.setdefault("started", session["last"])
    short = worktree_name(cwd, home)
    if short:
        session["cwd"] = short
    if cwd in windows:
        session["tmux"] = text(windows[cwd])
    return session, quota, quota_time


def payload(rows, *, home=None, now=None, processes=None, windows=None):
    home = pathlib.Path(home) if home is not None else pathlib.Path.home()
    now = time.time() if now is None else now
    files = []
    for path in (home / ".codex/sessions").glob("*/*/*/rollout-*.jsonl"):
        try:
            if now - path.stat().st_mtime < 6 * 3600:
                files.append(path)
        except OSError:
            continue
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    processes = process_sessions(files) if processes is None else processes
    windows = tmux_windows() if windows is None else windows
    sessions, quota, quota_at = ledger_sessions(rows), {}, ""
    for path in files:
        try:
            session, limits, at = read_worker(path, home, now, processes, windows)
        except OSError:
            continue
        if session:
            sessions.append(session)
        if at >= quota_at and limits:
            quota.update(limits)
            quota_at = at
    return {"sessions": sessions, "quota": quota}


def page_bytes(page):
    """Assemble the module at the local page's explicit inline seam."""
    return page.read_bytes().replace(b"/*__CODEX_JS__*/", (page.parent / "realm_codex.js").read_bytes())
