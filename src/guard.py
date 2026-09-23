#!/usr/bin/env python3
"""guard — registers this repo's own destructive-delete PreToolUse hook in
`~/.claude/settings.json`, globally, once, and names any live pane that
predates the registration.

`canonical_command()` is the one thing everything else here defers to: the
exact command string `install()` writes and the only string `registered()`
matches. It is resolved via `shutil.which("doit")` at CALL time, never from
this module's own `__file__` — a build runs inside a worktree
(`$DOIT_ROOT/worktrees/do-it-v2/<spec>`), and a `__file__`-derived command
would point at that worktree's own copy of the script, which `doit reap`
deletes after merge. See the module docstring in
`scripts/hooks/destructive-delete-guard.py` for the incident this all traces
back to.

`stale_panes` is read-only and injectable (`procs=`) so every buildable
acceptance criterion drives it without a real second `claude` process on the
box; the real `/proc` scan (`procs=None`) is exercised live, by an operator,
in AC10.
"""
import json
import os
import pathlib
import shutil
import sys

# Per ADR-0028-2/Q9: this pane is retired and is never asked to restart, so it
# is never named stale regardless of how far it predates a registration. A
# module-level data set, not a literal inside the staleness conditional, so a
# second exemption is a one-line addition here, not a rewrite of the check.
EXEMPT_PANE_NAMES = {"L-relay-0002"}

DEFAULT_SETTINGS_PATH = pathlib.Path.home() / ".claude" / "settings.json"


def _settings_path(settings_path=None):
    return pathlib.Path(settings_path) if settings_path else DEFAULT_SETTINGS_PATH


def canonical_command():
    """The command `install()` writes and `registered()` matches, or `None`
    when there is no safe canonical checkout to point at:

    - `shutil.which("doit")` finds nothing on PATH, or
    - its resolved directory's `pathlib.Path.parts` contains the literal
      component "worktrees" (a worktree checkout, reaped after merge), or
    - `scripts/hooks/destructive-delete-guard.py` does not exist under that
      directory.

    Never derived from this module's `__file__`."""
    found = shutil.which("doit")
    if not found:
        return None
    directory = pathlib.Path(found).resolve().parent
    if "worktrees" in directory.parts:
        return None
    script = directory / "scripts" / "hooks" / "destructive-delete-guard.py"
    if not script.is_file():
        return None
    return f"python3 {script}"


def _pretooluse_entries(data):
    return ((data.get("hooks") or {}).get("PreToolUse") or [])


def registered(settings_path=None):
    """True only when one `hooks.PreToolUse` entry's command string exactly
    equals the CURRENT `canonical_command()` — a stale or relocated command
    string reads as not registered."""
    cmd = canonical_command()
    if not cmd:
        return False
    path = _settings_path(settings_path)
    try:
        data = json.loads(path.read_text())
    except Exception:
        return False
    for entry in _pretooluse_entries(data):
        for h in entry.get("hooks") or []:
            if h.get("command") == cmd:
                return True
    return False


def install(settings_path=None):
    """Additive and idempotent. Refuses (returns False, changes nothing) when
    `canonical_command()` is None, or when the settings file exists but does
    not parse as JSON. Creates the file when absent. Never touches any key
    but the one `PreToolUse` entry this guard owns."""
    cmd = canonical_command()
    if not cmd:
        return False
    path = _settings_path(settings_path)
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            return False
    else:
        data = {}

    hooks = data.setdefault("hooks", {})
    pretooluse = hooks.setdefault("PreToolUse", [])
    for entry in pretooluse:
        for h in entry.get("hooks") or []:
            if h.get("command") == cmd:
                return True  # already registered — no rewrite needed

    pretooluse.append({
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": cmd, "timeout": 10}],
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def _comm(rec):
    argv = rec.get("argv") or []
    return os.path.basename(argv[0]) if argv else None


def _exempt_name(argv):
    for i, tok in enumerate(argv):
        if tok == "-n" and i + 1 < len(argv):
            return argv[i + 1] in EXEMPT_PANE_NAMES
    return False


def _boot_epoch():
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime"):
                    return float(line.split()[1])
    except OSError:
        pass
    return None


def _real_procs():
    """Live claude processes on this host, real `/proc` scan. Not exercised
    by any buildable AC (see Boundaries) — only AC10, live, by an operator."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import panes  # noqa: E402

    clk = os.sysconf("SC_CLK_TCK")
    boot = _boot_epoch()
    out = []
    try:
        entries = list(pathlib.Path("/proc").iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            comm = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if comm != "claude" or not panes.alive(pid):
            continue
        try:
            raw = (entry / "cmdline").read_bytes().split(b"\x00")
            argv = [a.decode(errors="replace") for a in raw if a]
        except OSError:
            argv = []
        ticks = panes.proc_start(pid)
        start_epoch = None
        if ticks is not None and boot is not None and clk:
            try:
                start_epoch = boot + (int(ticks) / clk)
            except (TypeError, ValueError, ZeroDivisionError):
                start_epoch = None
        out.append({"pid": pid, "argv": argv, "start_epoch": start_epoch})
    return out


def stale_panes(settings_path=None, procs=None):
    """Every live `claude` process (by `comm`) whose `start_epoch` precedes
    the settings file's own mtime — the registration instant — and whose `-n`
    name (if any) is not in `EXEMPT_PANE_NAMES`. `procs=None` scans `/proc`
    for real; otherwise `procs` is an iterable of
    `{"pid", "argv", "start_epoch"}` records, e.g. from `DOIT_GUARD_PROCS`."""
    path = _settings_path(settings_path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    if procs is None:
        procs = _real_procs()
    out = []
    for rec in procs:
        if _comm(rec) != "claude":
            continue
        start = rec.get("start_epoch")
        if start is None or start >= mtime:
            continue
        if _exempt_name(rec.get("argv") or []):
            continue
        out.append(rec)
    return out


def _pane_label(rec):
    argv = rec.get("argv") or []
    for i, tok in enumerate(argv):
        if tok == "-n" and i + 1 < len(argv):
            return argv[i + 1]
    return "?"


def main(argv):
    settings_path = os.environ.get("DOIT_CLAUDE_SETTINGS_PATH") or None

    if argv[:1] == ["install"]:
        ok = install(settings_path)
        if not ok:
            print("guard: install refused — no canonical `doit` checkout resolved, "
                  "or the settings file exists and does not parse as JSON")
            return 1
        print(f"guard: installed {canonical_command()} in {_settings_path(settings_path)}")
        return 0

    if argv[:1] == ["status"]:
        procs = None
        procs_file = os.environ.get("DOIT_GUARD_PROCS")
        if procs_file:
            procs = json.loads(pathlib.Path(procs_file).read_text())
        stale = stale_panes(settings_path, procs)
        if not stale:
            print("guard: clean — no live pane predates registration")
            return 0
        for rec in stale:
            print(f"guard: pid={rec.get('pid')} name={_pane_label(rec)} predates "
                  f"registration — restart it")
        return 1

    sys.exit("usage: doit guard install | status")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
