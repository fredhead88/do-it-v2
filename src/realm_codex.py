"""Read-only, allowlisted Codex presence for the realm. Never export rollout bodies."""
import copy
import datetime as dt
import math
import socket
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


def number(value, default=0):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else default


def usage(row):
    """Cached input and reasoning are subsets, never additional billed tokens."""
    structured = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}
    incoming = number(row.get("input_tokens"), None)
    outgoing = number(row.get("output_tokens"), None)
    if incoming is None:
        incoming = number(structured.get("input"), None)
    if outgoing is None:
        outgoing = number(structured.get("output"), None)
    total = number(incoming + outgoing, None) if incoming is not None and outgoing is not None else number(row.get("tokens"), number(row.get("subagent_tokens"), None))
    result = dict(input_tokens=incoming, output_tokens=outgoing, tokens=total)
    breakdown = {key: number(structured[key], None) for key in ("input", "output", "cached_input", "reasoning") if number(structured.get(key), None) is not None}
    if breakdown:
        result["token_breakdown"] = breakdown
    return result


def canonical(value):
    if not isinstance(value, str) or not pathlib.Path(value).is_absolute():
        return None
    try:
        return str(pathlib.Path(value).resolve())
    except (OSError, ValueError):
        return None


def moment(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def ledger_sessions(rows):
    sessions = {}
    rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("type"), str)]
    ordered = sorted(rows, key=lambda r: (str(r.get("ts", "")), str(r.get("type", ""))))
    # Index starts first so completion observations arriving out of order survive.
    for row in ordered:
        ident, kind = row.get("spawn"), row.get("type")
        if not isinstance(ident, str) or not ident:
            continue
        if kind in {"build-started", "spawn-started"} and row.get("backend") == "codex":
            session = sessions.setdefault(ident, dict(id=text(ident), spawn=text(ident), worker_id=None, session=None,
                kind="doit-build", subject=text(row.get("subject")), role=text(row.get("role") or "-".join(ident.split("-")[1:-1])),
                state="live", started=row.get("ts"), last=row.get("ts"), turns=0, outcome=None, reason=None, **usage({})))
            path = canonical(row.get("worktree"))
            if path:
                session["_worktree"] = path
            host, pid, ticks = row.get("waiter_host"), row.get("waiter_pid"), row.get("waiter_proc_start")
            if host == socket.gethostname() and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and isinstance(ticks, str) and ticks.isdigit():
                session["_waiter_identity"] = (host, pid, ticks)
    for row in ordered:
        kind = row.get("type")
        targets = [sessions[row["spawn"]]] if isinstance(row.get("spawn"), str) and row["spawn"] in sessions else []
        if kind == "spec-written" and not targets:
            targets = [s for s in sessions.values() if s["role"] == "spec-writer" and s["subject"] == row.get("subject") and (moment(row.get("ts")) or 0) >= (moment(s["started"]) or float("inf"))]
            targets = sorted(targets, key=lambda s: str(s["started"]), reverse=True)[:1]
        for session in targets:
            if kind == "spawn-done":
                if row.get("session"):
                    session["session"] = text(row["session"])
                normalized = usage(row)
                if normalized["tokens"] is not None or normalized["input_tokens"] is not None or normalized["output_tokens"] is not None:
                    session.update(normalized)
            outcome = None
            if kind == "backend-fallback":
                outcome = "fallback"
                session.update(fell_back="fell back: " + text(row.get("why") or row.get("reason") or "reason unavailable",120), fallback_at=row.get("ts"))
            elif kind == "spec-written":
                outcome = "written"
            elif kind == "build-blocked" or (kind in {"build-done", "spawn-done"} and row.get("status") in {"BLOCKED", "NEEDS_CONTEXT"}):
                outcome = "blocked"
            elif kind in {"spawn-failed", "spawn-stale"}:
                outcome = "failed" if kind == "spawn-failed" else "stale"
            elif kind in {"build-done", "spawn-done"}:
                outcome = "done" if row.get("status") == "DONE" else None
                if session["outcome"] in {"blocked", "failed", "stale", "fallback", "written"}:
                    outcome = None
            if outcome:
                session.update(outcome=outcome, reason=text(row.get("reason") or row.get("why"),160) or None)
            if kind in TERMINAL | {"build-blocked", "spec-written", "backend-fallback"}:
                session.update(state="fallback" if kind == "backend-fallback" else "ended", last=row.get("ts"))
    return list(sessions.values())


class ProcessEvidence(set):
    """Presence is compatible with sets; verified ancestry is additional evidence."""
    def __init__(self):
        super().__init__()
        self.ancestors = {}


def _proc_stat(proc):
    fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[1]), fields[19]


def _read_process_tree(files):
    """Snapshot local /proc identities; no argv or paths leave this reader."""
    tree = {}
    paths = {str(p.resolve()) for p in files}
    ids = {}
    for path in files:
        match = re.search(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", path.name)
        if match:
            ids[match[1]] = str(path.resolve())
    for proc in pathlib.Path("/proc").glob("[0-9]*"):
        try:
            parent, ticks = _proc_stat(proc)
            record = dict(ppid=parent, start=ticks, sessions=[])
            argv = (proc / "cmdline").read_bytes().split(b"\0")
            if argv and pathlib.Path(argv[0].decode(errors="replace")).name == "codex":
                record["sessions"].extend(ids[arg.decode(errors="replace")] for arg in argv[1:] if arg.decode(errors="replace") in ids)
                for fd in (proc / "fd").iterdir():
                    try:
                        target = str(fd.resolve())
                        if target in paths:
                            record["sessions"].append(target)
                    except OSError:
                        pass
            if _proc_stat(proc) == (parent, ticks):
                tree[int(proc.name)] = record
        except (OSError, ValueError, IndexError):
            continue
    # Revalidate the snapshot: PID reuse/death during traversal gives no evidence.
    for pid in list(tree):
        try:
            if _proc_stat(pathlib.Path("/proc") / str(pid)) != (tree[pid]["ppid"], tree[pid]["start"]):
                del tree[pid]
        except (OSError, ValueError, IndexError):
            del tree[pid]
    return tree


def process_sessions(files):
    tree, evidence = _read_process_tree(files), ProcessEvidence()
    for pid, record in tree.items():
        if not record.get("sessions"):
            continue
        ancestors, seen, parent = [], {pid}, record.get("ppid")
        while parent in tree and parent not in seen:
            seen.add(parent)
            ancestor = tree[parent]
            ancestors.append((socket.gethostname(), parent, str(ancestor["start"])))
            parent = ancestor.get("ppid")
        for identity in record["sessions"]:
            evidence.add(identity)
            evidence.ancestors.setdefault(identity, []).extend(ancestors)
    return evidence


def join(ledger_sessions, workers):
    """Prefer explicit identity, then unique ancestry, then unique path/time."""
    def unique(entries):
        selected = {}
        for entry in entries:
            key = entry.get("id")
            candidate = copy.deepcopy(entry)
            if key not in selected or str(candidate.get("last", "")) > str(selected[key].get("last", "")):
                selected[key] = candidate
        return [selected[key] for key in sorted(selected)]
    ledgers, workers = unique(ledger_sessions), unique(workers)
    matches, used = {}, set()
    def in_time(l, w):
        a, b = moment(l.get("started")), moment(w.get("started"))
        return a is not None and b is not None and 0 <= b-a <= 300
    def eligible(l, w):
        return not l.get("session") and l.get("state") == "live" and w.get("state") in {"live", "idle"} and in_time(l,w)
    for mode in ("session", "ancestry", "worktree"):
        candidates = {}
        for i,l in enumerate(ledgers):
            if i in matches:
                continue
            for j,w in enumerate(workers):
                if j in used:
                    continue
                if mode == "session":
                    match = bool(l.get("session")) and l["session"] == w.get("id")
                elif mode == "ancestry":
                    match = eligible(l,w) and l.get("_waiter_identity") is not None and l["_waiter_identity"] in w.get("_ancestor_identities", [])
                else:
                    match = eligible(l,w) and l.get("_worktree") is not None and l["_worktree"] == w.get("_worktree")
                if match:
                    candidates.setdefault(i, []).append(j)
        for i,js in candidates.items():
            if len(js) == 1 and sum(js[0] in others for others in candidates.values()) == 1:
                matches[i] = js[0]; used.add(js[0])
    result = []
    for i,l in enumerate(ledgers):
        l.setdefault("spawn",l["id"]); l.setdefault("worker_id",None)
        if i in matches:
            w = workers[matches[i]]
            for key in ("tmux", "cwd", "turns"):
                if key in w:
                    l[key] = w[key]
            l["worker_id"] = w["id"]
            if l.get("state") == "live":
                l["state"] = w["state"] if w["state"] != "ended" else "idle"
                l["last"] = max(str(l.get("last") or ""),str(w.get("last") or ""))
            if l.get("tokens") is None:
                for key in ("tokens", "input_tokens", "output_tokens", "token_breakdown"):
                    if key in w:
                        l[key] = w[key]
        result.append(l)
    result.extend(w for j,w in enumerate(workers) if j not in used)
    return result


def activity(entry, now):
    outcome, state = entry.get("outcome"), entry.get("state")
    reason = entry.get("reason") or "reason unavailable"
    if state == "fallback" or outcome == "fallback":
        return "fell back: " + reason
    if outcome in {"blocked", "failed", "stale"}:
        return ("blocked" if outcome == "blocked" else "failed") + ": " + reason
    if outcome in {"done", "written"}:
        return "finished and departing"
    if state == "ended":
        return "inactive; outcome unavailable"
    last = moment(entry.get("last"))
    if state == "idle" or (last is not None and now.timestamp()-last >= 180):
        return "waiting on the model"
    return {"builder":"building", "spec-writer":"writing the spec", "spec-auditor":"auditing the spec", "grader":"grading", "reviewer":"reviewing", "probe":"probing"}.get(entry.get("role"), "working")


def tmux_windows():
    try:
        result = subprocess.run(["tmux", "list-windows", "-a", "-F", "#{window_name}\t#{pane_current_path}"],
                                capture_output=True, text=True, timeout=2)
        windows = {}
        for line in result.stdout.splitlines():
            name, sep, cwd = line.partition("\t")
            if sep and name.startswith("codex-"):
                windows.setdefault(cwd, []).append(text(name))
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
    session = {"id": "", "kind": "worker", "role": "worker", "subject": "Codex worker", "spawn": None, "worker_id": None, "session": None, "outcome": None, "reason": None, "turns": 0, **usage({})}
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
                    counts = info.get("total_token_usage") or {}
                    normalized = usage(dict(input_tokens=counts.get("input_tokens"), output_tokens=counts.get("output_tokens"), tokens=counts.get("total_tokens")))
                    if normalized["tokens"] is not None:
                        session.update(normalized)
                    subsets = {k:number(counts[k],None) for k in ("cached_input_tokens", "reasoning_output_tokens") if number(counts.get(k),None) is not None}
                    if subsets:
                        session["token_breakdown"] = subsets
                    latest = quota_from(payload.get("rate_limits") or {})
                    if latest:
                        quota.update(latest)
                        quota_time = row.get("timestamp") or stamp(modified)
            except (ValueError, TypeError, AttributeError):
                continue  # partial last JSON line while a worker writes
    if not session["id"]:
        return None, {}, ""
    session["worker_id"] = session["id"]
    session["session"] = session["id"]
    session["_worktree"] = canonical(cwd)
    session["_ancestor_identities"] = list(getattr(processes, "ancestors", {}).get(session["id"], [])) + list(getattr(processes, "ancestors", {}).get(str(path.resolve()), []))
    age = now - modified
    live = session["id"] in processes or str(path.resolve()) in processes
    session.update(state="live" if age < 180 and live else "idle" if age < 1800 else "ended",
                   last=stamp(modified), turns=len(turns))
    session.setdefault("started", session["last"])
    short = worktree_name(cwd, home)
    if short:
        session["cwd"] = short
    names = windows.get(cwd, [])
    names = [names] if isinstance(names, str) else names
    if isinstance(names, list) and len(names) == 1:
        session["tmux"] = text(names[0])
    return session, quota, quota_time


def payload(rows, *, home=None, now=None, processes=None, windows=None, ledger_projection=None):
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
    sessions = [dict(s) for s in ledger_projection] if ledger_projection is not None else ledger_sessions(rows)
    quota, quota_at, workers = {}, "", []
    for path in files:
        try:
            session, limits, at = read_worker(path, home, now, processes, windows)
        except OSError:
            continue
        if session:
            workers.append(session)
        if at >= quota_at and limits:
            quota.update(limits)
            quota_at = at
    # cwd alone is evidence only for a single observed worker and window.
    for worker in workers:
        if sum(other.get("_worktree") == worker.get("_worktree") for other in workers) != 1:
            worker.pop("tmux", None)
    projected = join(sessions, workers)
    utc_now = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    for entry in projected:
        entry["activity"] = activity(entry, utc_now)
    public = {"id", "kind", "subject", "role", "state", "started", "last", "tokens", "turns", "spawn", "worker_id", "session", "input_tokens", "output_tokens", "token_breakdown", "activity", "outcome", "reason", "cwd", "tmux", "fell_back", "fallback_at"}
    for entry in projected:
        for key in ("started", "last", "fallback_at"):
            if key in entry and moment(entry[key]) is None:
                entry[key] = None
    return {"sessions": [{key:value for key,value in entry.items() if key in public} for entry in projected], "quota": quota}


def page_bytes(page):
    """Assemble the module at the local page's explicit inline seam."""
    return page.read_bytes().replace(b"/*__CODEX_JS__*/", (page.parent / "realm_codex.js").read_bytes())
