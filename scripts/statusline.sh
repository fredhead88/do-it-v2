#!/bin/bash
# Claude Code status line (rebuilt 2026-09-22): [model · role] ctx% · 5h used ↻reset · 7d used ↻reset · cost
python3 -c '
import sys, json, datetime as dt
d = json.load(sys.stdin)
def g(*ks, default=None):
    x = d
    for k in ks:
        x = x.get(k) if isinstance(x, dict) else None
        if x is None: return default
    return x
model = g("model","display_name") or g("model","id") or "?"
role = g("agent","name") or g("agent_type") or ""
parts = [f"[{model}" + (f" · {role}" if role else "") + "]"]
ctx = g("context_window","used_percentage")
if ctx is not None: parts.append(f"ctx {int(ctx)}%")
def win(label, key, fmt):
    used = g("rate_limits", key, "used_percentage"); rs = g("rate_limits", key, "resets_at")
    if used is None: return
    s = f"{label} {int(used)}%"
    if rs: s += " ↻" + dt.datetime.fromtimestamp(int(rs), dt.timezone.utc).strftime(fmt)
    parts.append(s)
win("5h", "five_hour", "%H:%MZ")
win("7d", "seven_day", "%a %H:%MZ")
cost = g("cost","total_cost_usd")
if cost is not None: parts.append(f"${cost:.2f}")
print(" · ".join(parts), end="")

# State capture is best effort; rendering above remains byte-for-byte unchanged.
import os, pathlib, re, tempfile

def atomic_state(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
            name = f.name
            json.dump(value, f)
        os.replace(name, path)
    finally:
        if name is not None:
            try: os.unlink(name)
            except FileNotFoundError: pass

session = g("session_id")
if isinstance(session, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session):
    root = pathlib.Path(os.environ.get("DOIT_ROOT", str(pathlib.Path.home() / ".do-it")))
    observed = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(g("rate_limits"), dict):
        usage = {key: {field: g("rate_limits", key, field)
                       for field in ("used_percentage", "resets_at")}
                 for key in ("five_hour", "seven_day")}
        usage.update(observed_at=observed, session_id=session)
        try: atomic_state(root / "state/claude-usage.json", usage)
        except Exception: pass
    if ctx is not None:
        pane = {"used_percentage": ctx, "observed_at": observed,
                "cwd": g("cwd") or g("workspace", "current_dir")}
        try: atomic_state(root / "state/pane-ctx" / (session + ".json"), pane)
        except Exception: pass
'
