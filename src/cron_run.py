#!/usr/bin/env python3
"""cron_run — wraps one cron job's command, recording start/outcome (L-charter-0042
R14a, L-spec-0485).

  cron_run.py NAME -- CMD...

Appends a `start` row `{ev:"start", ts, pid}` to `$R/state/cron-runs/<name>.jsonl`,
runs CMD with stdout INHERITED (passes straight through, untouched) and stderr
TEED — copied live to this process's own stderr, and accumulated into a bounded
buffer (last 20 lines, at most 2000 characters) that becomes `error_tail` — then
appends an `end` row `{ev:"end", ts, rc, duration_s, error_class, error_tail,
start_ts}`. Exits with CMD's own exit code; a signal death exits `128+n`.

`cron-run` must never change a job's behaviour (Constraints): a failure to
write a row (unwritable root, full disk, lock contention) is swallowed —
nothing is printed, nothing raises past this module — and CMD's exit code and
stdout/stderr pass through exactly as CMD produced them. Only alarms reach the
ledger, and only through `look` (`_check_cron_runs`); this module writes
NOTHING to `$DOIT_ROOT/events/`.
"""
import collections, json, os, pathlib, re, subprocess, sys, time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold  # noqa: E402 — fold.ROOT read live, at call time (never cached), like crons._tokens()

NAME_RE = re.compile(r'^[a-z0-9-]+$')
MAX_ROWS = 50
MAX_TAIL_LINES = 20
MAX_TAIL_CHARS = 2000

# Order matters: strip hex runs (which may themselves contain decimal digits)
# BEFORE stripping bare digits, or a run like `0xdeadbeef` would have only its
# digit characters removed, leaving stray hex letters behind.
_HEX_RE = re.compile(r'0x[0-9a-fA-F]+|[0-9a-fA-F]{7,}')
_DIGIT_RE = re.compile(r'\d+')
_WS_RE = re.compile(r'\s+')


def store_path(name, root=None):
    """`<root>/state/cron-runs/<name>.jsonl` (root defaults to `fold.ROOT`,
    read live — never cached at import, matching `crons._tokens()`'s own
    contract)."""
    root = pathlib.Path(root) if root is not None else fold.ROOT
    return root / "state" / "cron-runs" / f"{name}.jsonl"


def error_class(rc, stderr_tail):
    """`""` for `rc == 0`. Else `rc:<n>:<line>` where `<line>` is the first
    non-blank line of `stderr_tail`, with `0x…` runs and bare hex runs of 7+
    characters removed, then every remaining digit removed, then whitespace
    collapsed and the result stripped — so two tails differing only in a PID,
    a timestamp or an address compare equal."""
    if rc == 0:
        return ""
    line = ""
    for ln in (stderr_tail or "").splitlines():
        if ln.strip():
            line = ln.strip()
            break
    line = _HEX_RE.sub("", line)
    line = _DIGIT_RE.sub("", line)
    line = _WS_RE.sub(" ", line).strip()
    return f"rc:{rc}:{line}"


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_row(path, row):
    """Append one JSON line, trimmed to the newest `MAX_ROWS`. Any failure
    (unwritable parent, full disk, a concurrent writer) is swallowed entirely
    — this must never be the reason a cron job's own exit code changes."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        lines = path.read_text().splitlines() if path.exists() else []
        lines.append(json.dumps(row, default=str))
        lines = lines[-MAX_ROWS:]
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except Exception:
        pass


def run(name, cmd, root=None):
    """Runs CMD (a list), wrapped. Returns CMD's own exit code (128+n on a
    signal death) — never raises."""
    path = store_path(name, root)
    start_ts = _now_iso()
    _append_row(path, {"ev": "start", "ts": start_ts, "pid": os.getpid()})
    t0 = time.monotonic()
    tail = collections.deque(maxlen=MAX_TAIL_LINES)
    try:
        proc = subprocess.Popen(cmd, stdout=None, stderr=subprocess.PIPE, text=True, bufsize=1)
    except OSError as e:
        # CMD itself could not even be started — record it like any other
        # failure (rc 127, "command not found" shape) and surface the text on
        # our own stderr, since nothing was inherited to print it there.
        print(str(e), file=sys.stderr)
        rc, duration = 127, time.monotonic() - t0
        tail_text = str(e)[-MAX_TAIL_CHARS:]
        _append_row(path, {"ev": "end", "ts": _now_iso(), "rc": rc, "duration_s": duration,
                           "error_class": error_class(rc, tail_text), "error_tail": tail_text,
                           "start_ts": start_ts})
        return rc
    try:
        for line in proc.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()
            tail.append(line.rstrip("\n"))
    except Exception:
        pass
    proc.wait()
    duration = time.monotonic() - t0
    raw_rc = proc.returncode
    rc = raw_rc if raw_rc >= 0 else 128 - raw_rc  # a negative returncode IS -signum (POSIX)
    tail_text = "\n".join(tail)[-MAX_TAIL_CHARS:]
    _append_row(path, {"ev": "end", "ts": _now_iso(), "rc": rc, "duration_s": duration,
                       "error_class": error_class(rc, tail_text), "error_tail": tail_text,
                       "start_ts": start_ts})
    return rc


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    usage = "usage: cron_run.py NAME -- CMD..."
    if len(argv) < 2 or argv[1] != "--" or len(argv) < 3:
        print(usage, file=sys.stderr)
        return 2
    name = argv[0]
    if not NAME_RE.match(name):
        print(f"cron_run: invalid name {name!r} (must match [a-z0-9-]+)", file=sys.stderr)
        return 2
    cmd = argv[2:]
    return run(name, cmd)


if __name__ == "__main__":
    sys.exit(main())
