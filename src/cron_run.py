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
A run stopped at its crons.toml `kill_after_s` limit exits 124 when SIGTERM
sufficed and 137 when SIGKILL was sent (as timeout(1)). A run with such a
limit that is stopped because cron-run itself received signal n exits `128+n`.
On a run with such a limit, cron-run stops reading CMD's stderr at most one
`kill_grace_s` after its last signal, or at once when nothing in CMD's group
is left to signal, and records `stderr_abandoned` on the `end` row.

Takes a non-blocking exclusive flock on `<name>.lock` before any row, held
through the end row. The descriptor is close-on-exec. A contending invocation
runs nothing, prints nothing, returns 0, and records a `skip` row with reason
`already-running` in its own newest-50-row `<name>.skips.jsonl` ring. Skips
never evict start/end rows. Any other lock error runs CMD as usual.

`cron-run` must never change a job's behaviour (Constraints), with one exception:
a run whose crons.toml row declares `kill_after_s`, while kills are not switched
off, runs in its own session and is stopped once that limit passes (SIGTERM to
its process group, then SIGKILL only if a member survives the grace), exits as
above, and ends its stderr with one `cron-run:` line. A failure to write a row
(unwritable root, full disk, concurrent writer) is swallowed — nothing is
printed, nothing raises past this module — and, apart from that exception,
CMD's exit code and stdout/stderr pass through exactly as CMD produced them.
Only alarms reach the ledger, and only through `look` (`_check_cron_runs`);
this module writes NOTHING to `$DOIT_ROOT/events/`.
"""
import codecs, collections, errno, fcntl, json, locale, os, pathlib, re, select, signal, subprocess, sys, time
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


def skip_store_path(name, root=None):
    """The separate skip ring, with the same live-root default as store_path."""
    return store_path(name, root).with_name(f"{name}.skips.jsonl")


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


def _limit(name, path, manifest):
    """Fail-open raw NAME lookup, after the lock and switch, independent of crons."""
    if os.environ.get("DOIT_CRON_KILL") == "off":
        return {}
    try:
        try:
            path.with_name("kill.off").stat()
        except FileNotFoundError:
            pass
        else:
            return {}
        import tomllib
        manifest = pathlib.Path(manifest) if manifest is not None else pathlib.Path(__file__).resolve().parent.parent / "crons.toml"
        doc = tomllib.loads(manifest.read_text())
        row = next((r for r in doc.get("row", []) if r.get("name") == name), {})
        if "kill_after_s" not in row:
            return {}
        after, grace = row["kill_after_s"], row.get("kill_grace_s", 10)
        if type(after) is not int or not 1 <= after <= 86400 or type(grace) is not int or not 1 <= grace <= 300:
            return {"kill_config": "invalid"}
        return {"kill_after_s": after, "kill_grace_s": grace}
    except Exception:
        return {}


def _killpg(pgid, sig):
    """The single seam for group probes and delivered signals."""
    os.killpg(pgid, sig)


def run(name, cmd, root=None, manifest=None):
    """Runs CMD (a list), wrapped. Returns CMD's own exit code (128+n on a
    signal death), or 0 without running CMD when this job already holds its lock."""
    path = store_path(name, root)
    lock = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_suffix(".lock").open("a")
        os.set_inheritable(lock.fileno(), False)
    except OSError:
        if lock is not None:
            lock.close()
        lock = None
    if lock is not None:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock.close()
            lock = None
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                _append_row(skip_store_path(name, root), {"ev": "skip", "ts": _now_iso(),
                            "pid": os.getpid(), "reason": "already-running"})
                return 0
    try:
        config = _limit(name, path, manifest)
        if "kill_after_s" in config:
            return _run_bounded(name, path, cmd, config)
        return _run_command(path, cmd, config)
    finally:
        if lock is not None:
            lock.close()


def _run_command(path, cmd, config=None):
    """Existing command execution and start/end bookkeeping, under run's lock."""
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
                           "start_ts": start_ts, **(config or {})})
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
                       "start_ts": start_ts, **(config or {})})
    return rc


def _run_bounded(name, path, cmd, config):
    """Poll/tee in this thread; TERM and KILL bound both CMD and its open pipe.

    A gone group is never contacted again. A refused probe/signal abandons the
    bound and waits naturally, retaining only signals actually delivered.
    """
    received = []
    previous = {}

    def flag(signum, frame):
        if not received:
            received.append(signum)

    try:
        try:
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                handler = signal.getsignal(sig)
                if handler != signal.SIG_IGN:
                    signal.signal(sig, flag)
                    previous[sig] = handler
        except ValueError:  # Python forbids handler installation in worker threads.
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            previous.clear()
        return _poll_command(name, path, cmd, config, received)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _poll_command(name, path, cmd, config, received):
    start_ts = _now_iso()
    _append_row(path, {"ev": "start", "ts": start_ts, "pid": os.getpid()})
    t0 = time.monotonic()
    extra = dict(config)
    try:
        proc = subprocess.Popen(cmd, stdout=None, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        tail_text = str(exc)[-MAX_TAIL_CHARS:]
        _append_row(path, {"ev": "end", "ts": _now_iso(), "rc": 127, "duration_s": time.monotonic() - t0,
                           "error_class": error_class(127, tail_text), "error_tail": tail_text,
                           "start_ts": start_ts, **extra})
        return 127

    deadline = t0 + config["kill_after_s"]
    grace = config["kill_grace_s"]
    decoder = codecs.getincrementaldecoder(locale.getpreferredencoding(False))(errors="replace")
    tail_text = ""
    eof = False
    sequence = None
    last_signal = None
    group_gone = False
    kill_error = None
    wrapper_signal = None

    def tail_add(text):
        nonlocal tail_text
        # Keep both bounds even for an arbitrarily long, unterminated line.
        tail_text = (tail_text + text)[-MAX_TAIL_CHARS:]
        lines = tail_text.split("\n")
        tail_text = "\n".join(lines[-(MAX_TAIL_LINES + (not lines[-1])):])

    def contact(sig):
        nonlocal group_gone, kill_error, last_signal
        if group_gone:
            return False
        try:
            _killpg(proc.pid, sig)
        except ProcessLookupError:
            group_gone = True
            return False
        except Exception as exc:
            kill_error = f"{type(exc).__name__}: {exc}"[:200]
            return False
        if sig:
            extra["kill_signals"].append("TERM" if sig == signal.SIGTERM else "KILL")
            last_signal = time.monotonic()
        return True

    try:
        while True:
            proc.poll()  # Reap before sending TERM: an exited CMD must not keep the group alive.
            now = time.monotonic()
            if sequence is None:
                if proc.returncode is not None and eof:
                    break
                if received or now >= deadline:
                    wrapper_signal = received[0] if received else None
                    sequence = now if wrapper_signal else deadline
                    extra.update(kill_reason="wrapper-signal" if wrapper_signal else "deadline",
                                 kill_pgid=proc.pid, kill_elapsed_s=now - t0, kill_signals=[])
                    contact(signal.SIGTERM)
            if sequence is not None and kill_error is None:
                if not group_gone:
                    contact(0)
                if kill_error is None:
                    if group_gone and not extra["kill_signals"]:
                        break  # Only an outside-group process holds the pipe.
                    if not group_gone and now >= sequence + grace and "KILL" not in extra["kill_signals"]:
                        contact(signal.SIGKILL)
                    # Do not finish on CMD/EOF alone while a descendant survives TERM.
                    if group_gone and proc.returncode is not None and eof:
                        break
                    if "KILL" in extra["kill_signals"] and proc.returncode is not None and eof:
                        break
                    if kill_error is None and last_signal is not None and now >= last_signal + grace:
                        break
            # Signal failure deliberately has no deadline: continue the same polling tee.
            timeout = .1
            if kill_error is None:
                next_check = deadline if sequence is None else sequence + grace
                if sequence is not None and (group_gone or "KILL" in extra["kill_signals"]):
                    next_check = last_signal + grace if last_signal is not None else now
                timeout = max(0, min(timeout, next_check - now))
            if eof:
                if kill_error is not None and proc.returncode is not None:
                    break
                time.sleep(timeout)
            elif select.select([proc.stderr], [], [], timeout)[0]:
                chunk = os.read(proc.stderr.fileno(), 65536)
                if not chunk:
                    eof = True
                    decoded = decoder.decode(b"", final=True)
                    tail_add(decoded)
                    if decoded and not hasattr(sys.stderr, "buffer"):
                        try:
                            sys.stderr.write(decoded)
                            sys.stderr.flush()
                        except Exception:
                            pass
                else:
                    decoded = decoder.decode(chunk)
                    tail_add(decoded)
                    try:
                        sys.stderr.flush()
                        if hasattr(sys.stderr, "buffer"):
                            sys.stderr.buffer.write(chunk)
                            sys.stderr.buffer.flush()
                        else:
                            sys.stderr.write(decoded)
                            sys.stderr.flush()
                    except Exception:
                        pass  # As today, stderr output failures do not alter CMD's outcome.
    finally:
        proc.stderr.close()

    proc.poll()
    cmd_rc = None if proc.returncode is None else (proc.returncode if proc.returncode >= 0 else 128 - proc.returncode)
    rc = cmd_rc
    if sequence is not None:
        extra.update(cmd_reaped=cmd_rc is not None, cmd_rc=cmd_rc)
    if not eof:
        extra["stderr_abandoned"] = True
    if kill_error is not None:
        extra["kill_error"] = kill_error
    elif sequence is not None and extra["kill_signals"]:
        extra["killed"] = True
        rc = 128 + wrapper_signal if wrapper_signal else (137 if "KILL" in extra["kill_signals"] else 124)
        line = (f"cron-run: {name} killed after {time.monotonic() - t0:.3f}s "
                f"(kill_after_s={config['kill_after_s']}, signal={extra['kill_signals'][-1]})")
        try:
            print(line, file=sys.stderr)
        except Exception:
            pass
        tail_add(("\n" if tail_text and not tail_text.endswith("\n") else "") + line)
    tail_text = tail_text.removesuffix("\n")[-MAX_TAIL_CHARS:]
    _append_row(path, {"ev": "end", "ts": _now_iso(), "rc": rc, "duration_s": time.monotonic() - t0,
                       "error_class": "killed" if extra.get("killed") else error_class(rc, tail_text),
                       "error_tail": tail_text, "start_ts": start_ts, **extra})
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
    manifest = os.environ.get("DOIT_CRON_RUN_MANIFEST") if os.environ.get("DOIT_CRON_RUN_TEST") == "1" else None
    return run(name, cmd, manifest=manifest)


if __name__ == "__main__":
    sys.exit(main())
