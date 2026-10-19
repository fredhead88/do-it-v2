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
'
