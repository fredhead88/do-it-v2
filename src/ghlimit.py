#!/usr/bin/env python3
"""ghlimit — one shared, flock'd GitHub rate-limit state on the builder box
(charter L-charter-0037 R5, ADR SD8). Every `gh`/GitHub-API caller on the box
passes through this instead of its own ad hoc quota read, so a low quota makes
a caller WAIT for the reset instead of failing.

  gate(wait: bool = True) -> dict                 {ok, remaining, reset, waited_s}
  run(argv: list[str], wait: bool = True) -> tuple[int, str, str]   (rc, stdout, stderr)

One JSON state file at `$GH_LIMIT_STATE` (default `~/.local/state/gh-ratelimit.json`),
read/written under an `fcntl.flock` on a sibling `.lock` file — `backup.py`'s own
`_Lock` idiom, blocking (not `LOCK_NB`) here: two racing callers must SERIALIZE their
read-modify-write, never one silently skip it. No daemon.

`run()`/`gate()`'s own internal quota read always exec `$DOIT_REAL_GH` (default
`/usr/bin/gh`) BY THAT PATH — never a `PATH` search for `gh` — so a caller can never
recurse into `scripts/gh/gh` even when that shim sits on `PATH` ahead of the real
binary.
"""
import fcntl, json, os, pathlib, re, subprocess as _subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent

# Read at call time (never cached at import) — a test replaces these wholesale
# before any call reaches a real process, mirroring `intake.py`'s own idiom.
subprocess = _subprocess
sleep = time.sleep
clock = time.time

CHECK_INTERVAL_S = 60          # a cached read older than this is refreshed
WAIT_PAD_S = 5                 # sleep to (latest low reset) + this
WAIT_CAP_S = 120                # ... capped at (latest low reset) + this
DEFAULT_RETRY_WAIT_S = 60.0    # secondary-limit fallback with no Retry-After

RATE_LIMIT_PATTERNS = ("API rate limit exceeded", "secondary rate limit", "HTTP 429")
READONLY_VERBS = {"list", "view", "status", "checks"}
MUTATING_API_FLAGS = {"-f", "-F", "--input", "--raw-field"}


def state_path():
    return pathlib.Path(os.environ.get("GH_LIMIT_STATE")
                        or (pathlib.Path.home() / ".local" / "state" / "gh-ratelimit.json"))


def _lock_path():
    p = state_path()
    return p.with_name(p.name + ".lock")


def real_gh():
    return os.environ.get("DOIT_REAL_GH") or "/usr/bin/gh"


def _log(msg):
    sys.stderr.write(f"ghlimit: {msg}\n")


class _Lock:
    """One flock per state file, BLOCKING — every racing caller waits its turn
    rather than skipping the update (unlike `backup.py`'s own non-blocking
    `_Lock`, which is for a different "at most one instance" purpose)."""
    def __init__(self, path):
        self.path = path
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
        return False


def _read_state_locked():
    p = state_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return None


def _write_state_locked(state):
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(state))
    tmp.replace(p)


def _quota_read():
    """One real `$DOIT_REAL_GH api rate_limit` call (never via `PATH`), which
    does not itself consume core quota. `None` on any failure — network, auth,
    non-zero exit, or unparsable output."""
    try:
        r = subprocess.run([real_gh(), "api", "rate_limit"], capture_output=True, text=True)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    try:
        res = json.loads(r.stdout)["resources"]
        return {k: {"limit": res[k]["limit"], "remaining": res[k]["remaining"],
                    "reset": res[k]["reset"]} for k in ("core", "graphql")}
    except (ValueError, KeyError, TypeError):
        return None


def _threshold(limit):
    return max(100, limit * 0.05)


def _low_resources(state):
    """Names of every resource ("core", "graphql") currently low."""
    spent = state.get("spent_since_check", 0)
    return [r for r in ("core", "graphql")
            if r in state and (state[r]["remaining"] - spent) < _threshold(state[r]["limit"])]


def _core_view(state):
    spent = state.get("spent_since_check", 0)
    core = state.get("core") or {}
    return {"remaining": (core.get("remaining", 0) - spent) if core else None,
            "reset": core.get("reset")}


def gate(wait=True):
    """Reads/refreshes the shared state, decides whether the shared quota is
    low, and — with `wait=True` — sleeps for a low quota's reset instead of
    reporting failure. Never raises."""
    with _Lock(_lock_path()):
        state = _read_state_locked()
        stale = state is None or (clock() - state.get("checked_at", 0)) > CHECK_INTERVAL_S
        if stale:
            fresh = _quota_read()
            if fresh is None:
                _log("quota read failed; failing open (the call proceeds unchecked)")
                return {"ok": True, "remaining": None, "reset": None, "waited_s": 0}
            state = {**fresh, "checked_at": clock(), "spent_since_check": 0}
            _write_state_locked(state)
        low = _low_resources(state)
        view = _core_view(state)
        if not low:
            return {"ok": True, "remaining": view["remaining"], "reset": view["reset"], "waited_s": 0}
        if not wait:
            return {"ok": False, "remaining": view["remaining"], "reset": view["reset"], "waited_s": 0}
        latest_reset = max(state[r]["reset"] for r in low)
        target = min(latest_reset + WAIT_PAD_S, latest_reset + WAIT_CAP_S)
        waited = max(0.0, target - clock())
        _log(f"quota low, waiting {waited:.0f}s for reset (core remaining {view['remaining']})")
        sleep(waited)
        fresh = _quota_read()
        if fresh is None:
            _log("post-wait quota read failed; failing open")
            return {"ok": True, "remaining": None, "reset": None, "waited_s": waited}
        state = {**fresh, "checked_at": clock(), "spent_since_check": 0}
        _write_state_locked(state)
        low_after = _low_resources(state)
        view = _core_view(state)
        return {"ok": not low_after, "remaining": view["remaining"], "reset": view["reset"], "waited_s": waited}


def _bump_spent():
    with _Lock(_lock_path()):
        state = _read_state_locked()
        if state is None:
            return
        state["spent_since_check"] = state.get("spent_since_check", 0) + 1
        _write_state_locked(state)


def _mark_exhausted():
    """After a real rate-limit-shaped failure — push the cached state below
    its own threshold so the next `gate()` reports low without waiting for a
    fresh read to notice."""
    with _Lock(_lock_path()):
        state = _read_state_locked()
        if state is None:
            return
        state["spent_since_check"] = state.get("spent_since_check", 0) + \
            state.get("core", {}).get("limit", 0)
        _write_state_locked(state)


def _is_passthrough(argv):
    if len(argv) >= 3 and argv[0] == "gh" and argv[1:3] == ["api", "rate_limit"]:
        return True
    if len(argv) >= 2 and argv[0] == "gh" and argv[1] == "auth":
        return True
    if len(argv) >= 2 and argv[0] == "gh" and argv[1] in ("--version", "help"):
        return True
    return False


def _is_readonly(argv):
    """R1's retry-scope definition — anything not matching this is mutating."""
    if len(argv) >= 2 and argv[0] == "gh" and argv[1] == "api":
        rest = argv[2:]
        method, i = "GET", 0
        while i < len(rest):
            tok = rest[i]
            if tok in ("-X", "--method"):
                method = rest[i + 1] if i + 1 < len(rest) else method
                i += 2
                continue
            if tok in MUTATING_API_FLAGS:
                return False
            i += 1
        return method.upper() == "GET"
    if len(argv) >= 3 and argv[0] == "gh":
        return argv[2] in READONLY_VERBS
    return False


def _matches_rate_limit(err):
    return any(p in (err or "") for p in RATE_LIMIT_PATTERNS)


def _retry_wait_seconds(err, state):
    if "API rate limit exceeded" in (err or ""):
        reset = (state or {}).get("core", {}).get("reset")
        return max(0.0, reset - clock()) if reset is not None else DEFAULT_RETRY_WAIT_S
    m = re.search(r"retry-after:?\s*(\d+)", err or "", re.IGNORECASE)
    return float(m.group(1)) if m else DEFAULT_RETRY_WAIT_S


def _exec(argv):
    """Execs `$DOIT_REAL_GH` directly for `argv[0]` — never a `PATH` search for
    `gh` — even when `argv[0] == "gh"` and `PATH` would resolve to the shim."""
    full = [real_gh()] + list(argv[1:])
    try:
        r = subprocess.run(full, capture_output=True, text=True)
    except OSError as e:
        return (1, "", str(e))
    return (r.returncode, r.stdout or "", r.stderr or "")


def run(argv, wait=True):
    """Runs `argv` (a `gh`-shaped list) through the shared gate; skips/waits on
    a low quota, retries a rate-limited READ-ONLY call exactly once, never
    retries a mutating one. Never raises."""
    argv = list(argv)
    if _is_passthrough(argv):
        return _exec(argv)
    g = gate(wait=wait)
    if not g["ok"]:
        return (75, "", "ghlimit: quota low, skipped")
    _bump_spent()
    rc, out, err = _exec(argv)
    if rc != 0 and _matches_rate_limit(err):
        state = _read_state_after_lock()
        _mark_exhausted()
        if _is_readonly(argv):
            sleep(_retry_wait_seconds(err, state))
            return _exec(argv)
        return (rc, out, err)
    return (rc, out, err)


def _read_state_after_lock():
    with _Lock(_lock_path()):
        return _read_state_locked()
