#!/usr/bin/env python3
"""up — start the Planner pane, and PRINT the cron line for the tick (D117, D95).

  up.py [--print-only]

One pane and one job. The pane is this script's business: `claude --agent planner`
with §10.5's RETIRE list denied by name (D119) and its own ledger file, so what it
appends lands as the actor `planner` (D90).

**The job is cron's, and cron is the operator's.** The line is printed, never
installed: editing the operator's cron table is an irreversible act on a thing outside this
system, and §4.9 routes those to a human rather than to an unattended process.
`test_up.py` holds that as a check, because it is exactly the rule a later edit
would helpfully break.
"""
import ledger_files
import fcntl, importlib, json, math, os, pathlib, re, shlex, signal, stat, subprocess, sys, time
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ledger_cache
import cli_path, ctx_guard, dispatch, fold, launch, models, panes, scratch, tick  # noqa: E402

DOIT = HERE.parent / "doit"

_EXECV = os.execv                     # the seam `maybe_reexec` calls — stubbable, `up._SLEEP`'s own shape
_LAST_REEXEC_FAILURE = None           # the last `current` sha this process already failed to exec into


def code_sha():
    """R2: `git -C <checkout root> rev-parse HEAD`, stripped. `None` on any
    failure — SD5's "undetermined": never a false "unchanged," the loop keeps
    running regardless. `HERE.parent` is the checkout root `DOIT`/`install()`
    already resolve against — read as a module global at call time, so a test
    that repoints `up.HERE` sees it immediately."""
    try:
        out = subprocess.run(["git", "-C", str(HERE.parent), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    sha = out.stdout.strip()
    return sha or None


def maybe_reexec(kind, started_sha, *, events_dir=None):
    """R2: re-derive `code_sha()` and, on a real change, replace this OS process
    with a fresh interpreter running the checkout as it now stands — never mid-
    pane, only immediately before the next one starts (every call site is at a
    pane boundary, never inside one). `up._EXECV` is the sole name this calls,
    so a test can prove the branch fires without actually replacing the test
    process.

    A no-op — no write, no exec — when `code_sha()` comes back `None` (SD5:
    undetermined is never treated as unchanged, but nor is it acted on with
    nothing to compare against), when it equals `started_sha` (nothing moved),
    or when it equals `up._LAST_REEXEC_FAILURE` (this process already failed to
    exec into this exact sha; retried only once `code_sha()` differs again — a
    closed retry storm). Also a no-op, before any write, when `sys.argv` is
    empty or `sys.argv[0] == "-c"` — a `python3 -c "..."` start's shape, which
    would otherwise replay into a bare interpreter.

    Otherwise: one fresh `L-up-NNNN.jsonl` in `events_dir` (default
    `fold.EVENTS`), one `supervisor-code reason="reexec"` row naming the new
    sha, then `up._EXECV(sys.executable, [sys.executable] + sys.argv)`. Success
    never returns (the image is replaced). A real `OSError` — the call failed,
    this process is unchanged — is caught: `up._LAST_REEXEC_FAILURE` is set to
    `current`, one more `supervisor-code reason="reexec-failed"` row lands on
    the SAME file, and this returns — the loop keeps cycling on its old code;
    point 7's `doit look` check is what eventually alarms, never a crash or a
    retry storm."""
    global _LAST_REEXEC_FAILURE
    current = code_sha()
    if current is None or current == started_sha or current == _LAST_REEXEC_FAILURE:
        return
    if not sys.argv or sys.argv[0] == "-c":
        return
    events_dir = events_dir if events_dir is not None else fold.EVENTS
    ledger = dispatch.alloc(events_dir, "L-up-", ".jsonl")
    dispatch.emit(ledger, {}, "supervisor-code", subject=kind, kind=kind,
                  sha=current, pid=os.getpid(), reason="reexec")
    try:
        _EXECV(sys.executable, [sys.executable] + sys.argv)
    except OSError as e:
        _LAST_REEXEC_FAILURE = current
        dispatch.emit(ledger, {}, "supervisor-code", subject=kind, kind=kind,
                      sha=current, pid=os.getpid(), reason="reexec-failed",
                      why=f"{type(e).__name__}: {str(e)[:200]}")
        return


def cron_line():
    """The tick's schedule. DOIT_TICK_MIN is the same variable the fold's
    tick-stale line reads, so a line installed from here cannot disagree with the
    staleness alarm that watches it."""
    every = os.environ.get("DOIT_TICK_MIN", "5")
    return (f"*/{every} * * * * DOIT_ROOT={fold.ROOT} {DOIT} tick "
            f">> {fold.ROOT}/logs/tick.log 2>&1")


def pane_cmd(prompt=None, name=None, model=None):
    """Interactive, so no -p, no --json-schema, no --output-format: the pane's
    Output is the files and events it writes. The agent file's tools: line is the
    sandbox; the deny list is the only form a retire list has (D119).

    `prompt` is the opening turn the supervisor hands the pane — one charter id, or
    a serving pass's comma-joined spawn ids — and it goes on as a TRAILING
    POSITIONAL, after --dangerously-skip-permissions. That flag is the last option
    this builds, and a positional in front of it reads as its value.

    `name` is the pane's own ledger stem (R12, mirroring the Executor's own
    `_pane_argv`): given, it is `-n <name>` as the first two tokens after
    `claude`. Omitted (the default), the argv is byte-identical to before this
    parameter existed — no `-n` anywhere.

    `model` (L-spec-0320 R2), truthy, inserts `["--model", model]` right after
    `--agent planner`; falsy (every call site before this parameter existed)
    leaves the argv byte-identical to before it."""
    cmd = ["claude"] + (["-n", name] if name else []) + [
           "--agent", "planner"] + (["--model", model] if model else []) + [
           "--disallowedTools", ",".join(f"Skill({s})" for s in tick.RETIRE),
           *ctx_guard.hook_args("planner"), "--dangerously-skip-permissions"]
    return cmd + [prompt] if prompt else cmd


def relay_pane_cmd(prompt=None, name=None, model=None):
    """`up.pane_cmd()`'s byte-identical twin for the relay contract (L-spec-0270
    R1): same shape, same RETIRE deny list, same interactive flags — never `-p`,
    `--json-schema` or `--output-format` — with `--agent relay` in place of
    `--agent planner`. `prompt` is the comma-joined unclaimed spawn ids, oldest
    first; `name` is the pane's own ledger stem, exactly as `pane_cmd` uses it;
    `model` is `pane_cmd`'s own new keyword, same insertion point."""
    cmd = ["claude"] + (["-n", name] if name else []) + [
           "--agent", "relay"] + (["--model", model] if model else []) + [
           "--disallowedTools", ",".join(f"Skill({s})" for s in tick.RETIRE),
           *ctx_guard.hook_args("relay"), "--dangerously-skip-permissions"]
    return cmd + [prompt] if prompt else cmd


AGENTS_HOME = pathlib.Path.home() / ".claude" / "agents"  # deprecated alias, kept for one release (R8.4) — up.agents_home() is live; this is computed once, at import time, and a caller that reassigns it no longer redirects anything


def agents_home():
    """`~/.claude/agents`, HOME read at CALL time (R8.4) — never import time, so a
    test (or a pane) that changes HOME after this module is imported is honoured
    immediately, and a test's own temp HOME can never leak into whatever HOME this
    process started with."""
    return pathlib.Path(os.environ["HOME"]) / ".claude" / "agents"


def install(contract):
    """`--agent planner` resolves through ~/.claude/agents, not through this repo —
    measured: a pane launched without this exits 1 with `--agent 'planner' not found`
    AFTER the cron line has been printed, which reads like success. The ten contracts
    are symlinked there already; this is the eleventh link, made idempotently and
    never over somebody else's file. `think.py` links the twelfth through this same
    function, because the hole is the launcher's and not the Planner's."""
    link = agents_home() / contract.name
    if link.is_symlink() and link.resolve() == contract.resolve():
        return
    if link.exists() or link.is_symlink():
        sys.exit(f"up: {link} is not this repo's contract — resolve it by hand:\n"
                 f"  ln -sfn {contract} {link}")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(contract)
    print(f"# linked {link} -> {contract}")


def main(print_only=False, max_cycles=None):
    """`doit up` no longer supervises. The tick (cron -> `doit tick`) owns Planner
    scheduling through `ensure_planner`; this prints where Planners run and returns,
    so the exit is 0 and nothing is launched, slept on or escalated here (L-spec-2310).

    `print_only` is the old one-shot dry run, unchanged in every observable way:
    no relay call, no child, no event, no launcher file, and no sleep. `max_cycles`
    is accepted for the old signature and ignored."""
    if print_only:
        return _supervisor_main(print_only=True)
    contract = dispatch.AGENTS / "planner.md"
    if not contract.exists():
        sys.exit(f"up: no planner contract at {contract} — the pane is the contract (D116)")
    install(contract)
    (fold.ROOT / "events").mkdir(parents=True, exist_ok=True)
    (fold.ROOT / "logs").mkdir(parents=True, exist_ok=True)
    print("# the tick is cron's, and cron is yours — install this line yourself:")
    print(cron_line())
    print("# Planners are started by the tick (doit tick) in tmux session do-it-relay;"
          " doit up starts nothing")
    return 0


def _supervisor_main(print_only=False, max_cycles=None):
    """One `doit up` is one FOREGROUND SUPERVISOR: derive the next plannable
    charter, run a Planner pane on it as a child, wait for it to exit, record the
    outcome, repeat — with no keystroke in between. The launcher never replaces
    itself with the pane (L-adr-0026): a process that has been replaced is not
    there to notice the pane end, and the operator becomes the relay.

    `max_cycles` is the fixtures' bound and nothing else's. Unset, the loop is
    unbounded and a dry queue HOLDS rather than ends it (R6) — `doit up` stops
    when the operator stops it.

    `print_only` is the old one-shot dry run, unchanged in every observable way:
    no relay call, no child, no event, no launcher file, and no sleep."""
    contract = dispatch.AGENTS / "planner.md"
    if not contract.exists():
        sys.exit(f"up: no planner contract at {contract} — the pane is the contract (D116)")
    install(contract)
    (fold.ROOT / "events").mkdir(parents=True, exist_ok=True)
    (fold.ROOT / "logs").mkdir(parents=True, exist_ok=True)
    print("# the tick is cron's, and cron is yours — install this line yourself:")
    print(cron_line())
    if print_only:
        ledger = dispatch.alloc(fold.EVENTS, "L-planner-", ".jsonl")
        name = (_seam("pane_name", "PANE_NAME") or _stem)(ledger.name)
        cmd = pane_cmd(name=name)
        print(f"# planner pane: {ledger.stem} · {' '.join(cmd)}")
        return cmd, _child_env(ledger)
    started_sha = code_sha()
    state, cycles = {"up": None, "watermark": None, "said": None}, 0
    led = _launcher(state)
    dispatch.emit(led, {}, "supervisor-code", subject="planner", kind="planner",
                  sha=started_sha or "undetermined", pid=os.getpid(), reason="start")
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        _cycle(state, started_sha)
    return cycles


# ── the supervisor: constants and the private helpers main() runs on ──────────
SUPERVISED = "DOIT_SUPERVISED"            # presence-only, on every child this starts
INTERVAL_ENV, INTERVAL_DEFAULT = "DOIT_SUPERVISOR_INTERVAL_SEC", 30.0
POLL_STEP = 0.5                           # how finely a hold notices a ledger write
RELAY_FNS = ("plannable", "waiting_lines", "ledger_changed",
             "planner_attempts")


def _interval():
    """Seconds between re-derivations while the queue is dry. Env-overridable in the
    pattern DOIT_TICK_MIN already sets, so a fixture can drive the loop at zero."""
    try:
        return float(os.environ.get(INTERVAL_ENV, INTERVAL_DEFAULT))
    except (TypeError, ValueError):
        return INTERVAL_DEFAULT


def _guarded(fn, *a, **kw):
    """EVERY relay call, without exception. relay-queries lands after this unit, so
    `import relay` failing is itself one of the failures R5 names — and a launcher
    that dies on an absent sibling is a launcher nobody can run. Degrade and say so
    (L-adr-0033): one line naming the exception, then the cycle holds."""
    try:
        import relay                                          # noqa: PLC0415 — see docstring
        return True, getattr(relay, fn)(*a, **kw)
    except Exception as e:
        print(f"# relay.{fn} unavailable — {type(e).__name__}: {e}")
        return False, None


def _degrade():
    """A relay that failed has already printed its one line. Start nothing, append
    nothing, and wait out the interval so a missing sibling cannot spin the box."""
    time.sleep(_interval())


def _child_env(ledger, supervised=False):
    """The pane writes as itself (D90) and resolves `doit` from this repo. The marker
    is presence-only: the pane reads it to know it may end itself. CLI resolution
    is optional here; a rejected pin is reported by the relay spawn path."""
    env = {**os.environ, "DOIT_LEDGER_FILE": ledger.name,
           "PATH": f"{HERE.parent}:{os.environ.get('PATH', '')}"}
    binary = cli_path.claude_bin()
    if binary is not None:
        # Keep the launcher directory, not a versioned symlink target.
        directory = os.path.dirname(os.path.abspath(binary))
        parts = env["PATH"].split(os.pathsep)
        if "DOIT_CLAUDE_BIN" in os.environ:
            parts = [parts[0], directory, *(p for p in parts[1:] if p != directory)]
        elif directory not in parts:
            parts.append(directory)
        env["PATH"] = os.pathsep.join(parts)
    if supervised:
        env[SUPERVISED] = "1"
    return env


def _launcher(state):
    """ONE L-up-NNNN.jsonl per run, allocated lazily on the run's first
    launcher-authored event. The actor is the filename and never a field (D90,
    fold.read_events), so the launcher's own rows need a file of the launcher's own;
    `L-up-0001` folds to the actor `up`."""
    if state["up"] is None:
        state["up"] = dispatch.alloc(fold.EVENTS, "L-up-", ".jsonl")
    return state["up"]


def _planner_own_rows(ledger, *, strict=True):
    """One own-file parser for wrapper completion and reaper read-back."""
    rows = []
    for line in ledger_files.read_text(ledger.parent, ledger.name).splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Planner own row must be an object")
            rows.append(row)
        except ValueError:
            if strict:
                raise
    return rows


def _ended(ledger):
    """True if the pane wrote its own end row. Re-read, never remembered (§9.2)."""
    return any(row.get("type") == "planner-ended" for row in _planner_own_rows(ledger, strict=False))


PLANNER_OCCUPIED = "planner-occupied"


def _planner_lock():
    """Claim the common lifetime lock; only this parent retains its descriptor."""
    fd = os.open(fold.ROOT / "planner.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.set_inheritable(fd, False)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _start(state, subject, mode, attempt, prompt, spawn_ids=None, started_sha=None,
           *, wrapper_owned=False):
    fd = _planner_lock()
    if fd is None:
        return PLANNER_OCCUPIED
    try:
        return _run_planner(subject, mode, attempt, prompt, spawn_ids, started_sha,
                            wrapper_owned=wrapper_owned)
    finally:
        os.close(fd)


def _run_planner(subject, mode, attempt, prompt, spawn_ids, started_sha, *, wrapper_owned=False):
    """Start one pane and wait for it. The start row lands in the CHILD's own file
    before the child exists (L-adr-0027) — a pane that dies in its first second has
    still been recorded as started, which is the whole of planner_attempts' input.
    The end row is appended only where the child wrote none: its own clean
    `planner-ended` is the better record and is never doubled.

    `started_sha` (R2): the SAME value `main()`'s own boot `supervisor-code
    reason="start"` row carries — never a fresh `code_sha()` call here.
    `maybe_reexec` has already resynced immediately before every call site that
    reaches this, so `started_sha` is current at the moment this row lands."""
    ledger = dispatch.alloc(fold.EVENTS, "L-planner-", ".jsonl")
    base, ids = {"spawn": ledger.stem}, ({"spawn_ids": spawn_ids} if spawn_ids else {})
    refusal = dispatch.emit(ledger, base, "planner-started", strict=True, subject=subject, planner=ledger.stem,
                  attempt=attempt, mode=mode, code_sha=started_sha or "undetermined",
                  **({"wrapper_pid": os.getpid()} if wrapper_owned else {}), **ids)
    if refusal:
        raise ValueError(refusal)
    try:
        name = (_seam("pane_name", "PANE_NAME") or _stem)(ledger.name)
        mp = models.load(fold.ROOT / "models.toml")
        model = launch.model_for("planner")
        model_configured = models.resolve("planner", mp=mp).get("model") if mp is not None else None
        cmd = pane_cmd(prompt, name=name, model=model)
        print(f"# planner pane: {ledger.stem} · {mode} · {subject} · attempt {attempt}")
        env = launch.child_env("planner", base={**_child_env(ledger, supervised=True),
                                               "DOIT_ROOT": str(fold.ROOT)})
        proc = subprocess.Popen(cmd, env=env, cwd=str(HERE.parent))
    except Exception as exc:
        dispatch.emit(ledger, base, "planner-ended", subject=subject, planner=ledger.stem,
                      mode=mode, reason="launch-failed", failure_class=type(exc).__name__, **ids)
        raise
    try:
        launch.record("planner", ledger.stem, proc.pid, model or "unpinned",
                      str(dispatch.AGENTS / "planner.md"), model_configured=model_configured)
    finally:
        # Even a record failure cannot abandon a running child or release its lock early.
        rc = proc.wait()
        if not _ended(ledger):
            dispatch.emit(ledger, base, "planner-ended", subject=subject, planner=ledger.stem,
                          mode=mode, reason=f"exit-{rc}", **ids)
        launch.ended(ledger.stem, rc)


def ctx_handover(events, charter):
    """Only the latest end for this charter may supply a restart handover."""
    latest = next((event for event in reversed(events)
                   if event.get("type") == "planner-ended"
                   and event.get("subject") == charter), None)
    if latest is None or latest.get("reason") not in ("ctx", "held"):
        return None
    try:
        handover = latest.get("handover")
        if not isinstance(handover, str) or not handover:
            return None
        path = pathlib.Path(handover)
        if path.is_file():
            with path.open("rb") as stream:
                if stream.read(1):
                    return handover
    except (OSError, ValueError):
        pass
    return None


def _charter_pass(state, events, charter, started_sha):
    """One ready charter. False means the relay failed and the cycle holds."""
    ok, att = _guarded("planner_attempts", events, charter_id=charter)
    if not ok:
        return False
    att = att or {}
    if att.get("next") == "held":
        # planner-attempts-hold (L-charter-0042 R6): reachable only if a caller hands
        # this a charter `relay.plannable` itself would not offer (its own held check
        # runs ahead of everything else) — a defensive branch, not the normal path.
        # No pane, no new escalation-blocking: the charter is already held by an
        # answer that did not carry replan=yes, and re-escalating it here would be
        # exactly the bug this spec fixes. The cycle continues to the next charter.
        print(f"# {charter}: held — known-bug escalation answered by {att.get('held_by')} without replan=yes")
        return True
    if att.get("next") == "escalate":
        # ensure_planner is the only writer of the attempt-limit escalation (L-spec-2310).
        print(f"# {charter}: attempt limit — the tick's ensure_planner escalates it")
        return True
    maybe_reexec("planner", started_sha)  # R2 point 5(b): immediately before THIS charter's pane
    handover = ctx_handover(events, charter)
    prompt = f"{charter}\nRead the handover file {handover} before continuing." if handover else charter
    result = _start(state, subject=charter, mode="charter",
           attempt=int(att.get("attempts") or 0) + 1, prompt=prompt, started_sha=started_sha)
    return PLANNER_OCCUPIED if result == PLANNER_OCCUPIED else True


def _hold(state, lines, *, relay_only=False):
    """A dry queue holds the loop open (R6). The waiting text is the board's own
    (L-adr-0032) and is re-printed only when it changes, so an idle night does not
    scroll the pane. A ledger write ends the hold immediately; otherwise the
    interval does."""
    if lines != state["said"]:
        for line in lines:
            print(line)
        state["said"] = lines
    end = time.monotonic() + _interval()
    while True:
        if relay_only:
            _relay_heartbeat()
        ok, res = _guarded("ledger_changed", fold.ROOT, state["watermark"])
        if not ok:
            return
        changed, state["watermark"] = res
        if changed or time.monotonic() >= end:
            return
        time.sleep(min(POLL_STEP, max(0.0, end - time.monotonic())))


def _cycle(state, started_sha):
    """Plan every ready charter, otherwise hold; seat serving belongs to the relay."""
    maybe_reexec("planner", started_sha)
    try:
        events = fold.read_events()
    except ledger_cache.LockTimeout as exc:
        print(str(exc), file=sys.stderr)
        return _degrade()
    ok, plan = _guarded("plannable", events, fold.ROOT)
    if not ok:
        return _degrade()
    ready = list(plan[0])
    if ready:
        for charter in ready:
            result = _charter_pass(state, events, charter, started_sha)
            if result == PLANNER_OCCUPIED:
                # Unlike a dry-queue hold, ledger writes cannot shorten this wait.
                _SLEEP(_interval())
                return
            if not result:
                return _degrade()
        return
    ok, lines = _guarded("waiting_lines", events, fold.ROOT)
    if not ok:
        return _degrade()
    _hold(state, list(lines))


# ── the relay: the sole seat-serving loop (L-spec-1497) ─────────────────────────────────────────────
RELAY_DRY = "RELAY WAITING: no pending packets"


def _unclaimed_pending(pending, root):
    """`relay.pending_packets` minus whatever a claim file already served.
    `pending_packets` itself does not apply this filter (its own docstring says
    so); this is the relay's own gate, run once here so `relay_main` and
    `check_and_end_relay` (src/pane_end.py) can never disagree about what
    "unclaimed" means."""
    root = pathlib.Path(root)
    seat = root / "seat"
    out = []
    for p in pending or ():
        sid = p.get("spawn") if isinstance(p, dict) else p
        if not (seat / f"{sid}.claimed").exists():
            out.append(p)
    return out


def _relay_heartbeat():
    """Only relay_main and its hold/poll paths may call this writer."""
    (fold.ROOT / "relay.heartbeat").touch()


def default_launcher(argv, *, runner=None):
    """Give cron's relay a terminal in a dedicated tmux session."""
    runner = runner or subprocess.run
    session = "do-it-relay"
    present = runner(["tmux", "has-session", "-t", session], capture_output=True)
    if present.returncode == 1:
        runner(["tmux", "new-session", "-d", "-s", session], check=True, capture_output=True)
    elif present.returncode != 0:
        raise OSError("tmux session state undetermined")
    runner(["tmux", "new-window", "-d", "-t", session, *argv], check=True, capture_output=True)


def _planner_args(charter, mode, attempt):
    if not isinstance(charter, str) or not re.fullmatch(r"L-charter-[0-9]+", charter):
        raise ValueError("invalid charter")
    if mode not in ("charter", "ctx-restart"):
        raise ValueError("invalid Planner mode")
    if type(attempt) is not int or attempt <= 0:
        raise ValueError("attempt must be a positive integer")


def planner_alive() -> bool | None:
    """Read both ownership and pane identity; uncertainty never admits a launch."""
    try:
        ledger_files._accessible(fold.ROOT, directory=True)
        lock = fold.ROOT / "planner.lock"
        held = False
        try:
            ledger_files._accessible(lock)
            fd = os.open(lock, os.O_RDONLY)
        except FileNotFoundError:
            fd = None
        if fd is not None:
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held = True
            finally:
                os.close(fd)
        directory = panes.SESSIONS
        # A not-yet-created sessions directory under a readable HOME has no panes.
        try:
            ledger_files._accessible(directory, directory=True)
            files = list(directory.iterdir())
        except FileNotFoundError:
            ledger_files._accessible(pathlib.Path(os.environ["HOME"]), directory=True)
            files = []
        live = False
        for path in files:
            if path.suffix != ".json" or not path.stem.isdigit():
                continue
            ledger_files._accessible(path)
            meta = json.loads(path.read_text())
            if not isinstance(meta, dict) or not isinstance(meta.get("name"), str) or not meta["name"]:
                raise ValueError("pane identity unreadable")
            if panes._is_live(int(path.stem), meta) and panes.ledger_role(meta["name"]) == "planner":
                live = True
        # A named Planner CLI without session JSON is still a live Planner; an
        # unreadable or hidden process table is never "no Planner".
        found = _planner_processes()
        if found is None:
            return None
        return held or live or bool(found)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _planner_host_namespace():
    """Fail closed outside Linux's initial PID namespace, including nested procfs.

    Linux reserves this namespace inode for init_pid_ns (PROC_PID_INIT_INO).
    Merely seeing PID 1 or a one-element NSpid does not establish host visibility.
    """
    try:
        namespace = os.readlink("/proc/self/ns/pid")
        if namespace != "pid:[4026531836]" or os.readlink("/proc/1/ns/pid") != namespace:
            return None
        mounts = pathlib.Path("/proc/mounts").read_text().splitlines()
        if not any(row.split()[1:3] == ["/proc", "proc"]
                   and not any(option.startswith("hidepid=") and option != "hidepid=0"
                               for option in row.split()[3].split(",")) for row in mounts):
            return None
        return namespace
    except (OSError, ValueError, IndexError):
        return None


def _proc_identity(pid):
    """Read one kernel identity; never return or log unrelated argv tokens."""
    directory = pathlib.Path("/proc") / str(pid)
    try:
        raw = (directory / "stat").read_text()
        fields = raw[raw.rindex(")") + 2:].split()
        ticks = int(fields[19])
        if ticks < 0:
            raise ValueError("invalid process start")
        status = dict(line.split(":", 1) for line in (directory / "status").read_text().splitlines()
                      if ":" in line)
        uid = int(status["Uid"].split()[1])
        uptime = float(pathlib.Path("/proc/uptime").read_text().split()[0])
        started = time.time() - uptime + ticks / os.sysconf("SC_CLK_TCK")
        args = (directory / "cmdline").read_bytes().split(b"\0")
        args = [os.fsdecode(token) for token in args if token]
        cli = cli_path.claude_bin()
        stem = None
        # A shebang script has its interpreter first and the script second.
        executable = pathlib.Path(cli).resolve() if cli else None
        prefix = 1
        if args and executable is not None:
            if pathlib.Path(args[0]).resolve() == executable:
                prefix = 1
            elif len(args) > 1 and args[0].startswith("/") and pathlib.Path(args[1]).resolve() == executable:
                prefix = 2
            else:
                prefix = 0
            if prefix and len(args) > prefix+1 and args[prefix] == "-n":
                if re.fullmatch(r"L-planner-[0-9]+", args[prefix+1]):
                    stem = args[prefix+1]
        namespace = os.readlink(directory / "ns/pid") if stem else None
        if not math.isfinite(started):
            raise ValueError("invalid process age")
        return dict(pid=pid, uid=uid, start_ticks=ticks, pid_namespace=namespace,
                    started_at=started, planner=stem, zombie=fields[0] in ("Z", "X"))
    except FileNotFoundError:
        # A process disappearing during a multi-file read is not a malformed identity.
        if not directory.exists():
            return None
        raise


def _scan_planner_processes():
    if _planner_host_namespace() is None or cli_path.claude_bin() is None:
        return None
    try:
        found = []
        for path in pathlib.Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            identity = _proc_identity(int(path.name))
            if identity and identity["planner"] and not identity["zombie"]:
                found.append(identity)
        return found
    except (OSError, ValueError, KeyError, IndexError):
        return None


# Kept as a separate probe so process-fixture tests can isolate the host scan.
def _planner_processes():
    return _scan_planner_processes()


def _planner_pid_dead(pid, start_ticks=None):
    if type(pid) is not int or pid <= 0:
        return None
    try:
        identity = _proc_identity(pid)
        if identity is None:
            return True
        if start_ticks is not None and str(identity["start_ticks"]) != str(start_ticks):
            return None  # reused PID does not establish this run's death
        return identity["zombie"]
    except (OSError, ValueError, KeyError, IndexError):
        return None


def _unmatched_planners(events):
    starts, ended = {}, set()
    for event in events:
        stem = event.get("planner") or event.get("spawn")
        if not isinstance(stem, str) or not re.fullmatch(r"L-planner-[0-9]+", stem):
            continue
        if event.get("type") == "planner-started" and event.get("mode") != "serving":
            starts.setdefault(stem, []).append(event)
        elif event.get("type") == "planner-ended":
            ended.add(stem)
    return [{**rows[0], **({"_reap_ambiguous": True} if len(rows) != 1 else {})}
            for stem, rows in starts.items() if stem not in ended]


def _planner_reap_approved(starts, namespace):
    legacy = [e for e in starts if "wrapper_pid" not in e]
    if not legacy:
        return True
    if namespace is None:
        return False
    path = fold.ROOT / "state/planner-reap-approval.json"
    try:
        if path.is_symlink() or path.resolve().parent != (fold.ROOT / "state").resolve():
            return False
        if (fold.ROOT / "state").is_symlink():
            return False
        meta = path.stat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o022:
            return False
        ledger_files._accessible(path)
        approval = json.loads(path.read_text())
        if not isinstance(approval, dict) or approval.get("pid_namespace") != namespace:
            return False
        rows = approval.get("starts")
        if not isinstance(rows, list) or any(not isinstance(e, dict) or set(e) != {"planner", "started_at"}
                                           or not isinstance(e["planner"], str)
                                           or not isinstance(e["started_at"], str) for e in rows):
            return False
        approved = {(e["planner"], e["started_at"]) for e in rows}
        return all((e.get("planner") or e.get("spawn"), e.get("ts")) in approved for e in legacy)
    except (OSError, ValueError, TypeError):
        return False


def reap_planners(events, now):
    """Persist proven dead ends under the lifetime lock; no scheduling or full read.

    Legacy starts are report-only until the operator acknowledges their identities
    in the current host namespace. The caller's snapshot is never modified.
    """
    closed, report = [], []
    fd = None
    try:
        # Probe this BEFORE reading sessions, processes, or migration approval.
        fd = _planner_lock()
        if fd is None:
            return []
        namespace = _planner_host_namespace()
        processes = _planner_processes()
        starts = _unmatched_planners(events)
        approved = _planner_reap_approved(starts, namespace)
        launched = {}
        for event in events:
            if event.get("type") == "role-launched" and event.get("pane"):
                launched.setdefault(event["pane"], []).append(event)
        for start in starts:
            stem = start.get("planner") or start["spawn"]
            matching = [p for p in processes or [] if p["planner"] == stem]
            reason = "vanished" if "wrapper_pid" in start or launched.get(stem) else "never-started"
            item = dict(planner=stem, started_at=start.get("ts"), reason=reason,
                        processes=matching, action="left", uncertainty=None)
            report.append(item)
            if start.get("_reap_ambiguous"):
                item["uncertainty"] = "multiple starts for one Planner identity"
                continue
            if namespace is None or processes is None or not approved:
                item["uncertainty"] = "host/process visibility or migration acknowledgment unavailable"
                continue
            path = fold.EVENTS / (stem + ".jsonl")
            if start.get("_src", "").split(":")[0] != path.name or path.is_symlink():
                item["uncertainty"] = "start does not identify its existing own file"
                continue
            own = _planner_own_rows(path)
            own_starts = [e for e in own if e.get("type") == "planner-started"]
            raw_start = {key: value for key, value in start.items() if key not in ("actor", "_src")}
            if (len(own_starts) != 1 or own_starts[0] != raw_start
                    or any(e.get("type") == "planner-ended" for e in own)):
                item["uncertainty"] = "own rows changed or already ended"
                continue
            pids = [(e.get("pid"), e.get("proc_start")) for e in launched.get(stem, [])]
            if "wrapper_pid" in start:
                pids.append((start["wrapper_pid"], None))
            if any(_planner_pid_dead(pid, ticks) is not True for pid, ticks in pids):
                item["uncertainty"] = "wrapper/child alive or identity undetermined"
                continue
            # Session metadata is corroboration; malformed or unreadable input blocks.
            session_live = False
            try:
                files = list(panes.SESSIONS.iterdir())
            except FileNotFoundError:
                ledger_files._accessible(pathlib.Path(os.environ["HOME"]), directory=True)
                files = []
            for session in files:
                if session.suffix == ".json" and session.stem.isdigit():
                    ledger_files._accessible(session)
                    meta = json.loads(session.read_text())
                    if (not isinstance(meta, dict) or not isinstance(meta.get("name"), str)
                            or not meta["name"]):
                        raise ValueError("unreadable session identity")
                    if meta["name"] == stem and _planner_pid_dead(int(session.stem), meta.get("procStart")) is not True:
                        session_live = True
            if session_live:
                item["uncertainty"] = "live/uncertain session"
                continue
            if matching:
                item["uncertainty"] = "live named process"
                if reason != "never-started" or len(own) != 1 or len(matching) != 1:
                    continue
                process = matching[0]
                row_time = datetime.fromisoformat(start["ts"].replace("Z", "+00:00"))
                if (row_time.tzinfo is None or now.tzinfo is None
                        or (now-row_time).total_seconds() <= 3600
                        or now.timestamp()-process["started_at"] <= 3600
                        or process["uid"] != os.geteuid() or process["pid_namespace"] != namespace):
                    continue
                # Re-read kernel identity immediately before the only destructive act.
                current = _proc_identity(process["pid"])
                keys = ("pid", "uid", "start_ticks", "pid_namespace", "planner", "zombie")
                if current is None or any(current[k] != process[k] for k in keys):
                    continue
                print("planner-reap signal " + json.dumps(dict(planner=stem, own_row_count=len(own),
                      pid=current["pid"], start_ticks=current["start_ticks"])), file=sys.stderr)
                os.kill(current["pid"], signal.SIGTERM)
                item["action"] = "signaled"
                item["signaled"] = True
                deadline = time.monotonic() + .25
                while _planner_pid_dead(current["pid"], current["start_ticks"]) is not True:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(.01)
                else:
                    item["uncertainty"] = None
                if item["uncertainty"]:
                    continue
            refusal = dispatch.emit(path, {"spawn": stem}, "planner-ended", strict=True,
                                    subject=start.get("subject"), planner=stem,
                                    mode=start.get("mode"), reason=reason)
            if refusal:
                raise ValueError(refusal)
            own = _planner_own_rows(path)
            end = own[-1]
            closed.append({**end, "actor": "planner", "_src": f"{path.name}:{len(own)}"})
            item["action"] = "closed"
            item["uncertainty"] = None
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        report.append(dict(action="left", uncertainty=type(exc).__name__))
    finally:
        if fd is not None:
            os.close(fd)
        if report:
            print("planner-reap " + json.dumps(dict(closed=len(closed),
                  signaled=sum(bool(e.get("signaled")) for e in report),
                  left=sum(e["action"] == "left" for e in report), starts=report)), file=sys.stderr)
    return closed


def planner_launcher(argv, *, runner=None):
    """Only Planner windows get the trusted checkout cwd; quote the tmux shell boundary."""
    runner = runner or subprocess.run
    session = "do-it-relay"
    present = runner(["tmux", "has-session", "-t", session], capture_output=True)
    if present.returncode == 1:
        runner(["tmux", "new-session", "-d", "-s", session, "-c", str(HERE.parent)],
               check=True, capture_output=True)
    elif present.returncode != 0:
        raise OSError("tmux session state undetermined")
    runner(["tmux", "new-window", "-d", "-t", session, "-c", str(HERE.parent), shlex.join(argv)],
           check=True, capture_output=True)


def launch_planner(charter, mode, attempt, *, launcher=None, now=None):
    """Admit one wrapper using an exclusive, bounded start window."""
    try:
        _planner_args(charter, mode, attempt)
        alive = planner_alive()
        if alive is True:
            return "alive"
        if alive is None:
            return "undetermined"
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            return "undetermined"
        window = float(os.environ.get("DOIT_TICK_MIN", "5")) * 60
        stamp = now.timestamp()
        if not math.isfinite(window) or window <= 0 or not math.isfinite(stamp):
            return "undetermined"
        marker = fold.ROOT / "planner.starting"
        for _ in range(2):
            try:
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                # Serialize stale recovery on the old inode, then verify it is still current.
                ledger_files._accessible(marker)
                with marker.open("rb") as stream:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return "starting"
                    stat = os.fstat(stream.fileno())
                    if (stat.st_dev, stat.st_ino) != (marker.stat().st_dev, marker.stat().st_ino):
                        continue
                    age = stamp - stat.st_mtime
                    if not math.isfinite(age) or age < 0:
                        return "undetermined"
                    if age <= window:
                        return "starting"
                    marker.unlink()
            else:
                try:
                    os.utime(fd, (stamp, stamp))
                finally:
                    os.close(fd)
                (launcher or planner_launcher)(["env", f"DOIT_ROOT={fold.ROOT}", str(DOIT),
                    "planner-run", charter, "--mode", mode, "--attempt", str(attempt)])
                return "started"
        return "starting"
    except Exception:
        return "undetermined"


def _attempt_escalation(charter, att):
    """The attempt-limit escalation fields, shared by nothing else (L-spec-2310)."""
    return dict(subject=charter, last_reason=att.get("last_reason"), attempts=att.get("attempts"),
        why=f"planner on {charter} ended {att.get('attempts')}x without landing — "
            f"last: {att.get('last_reason')}",
        irreversible=f"{att.get('attempts')} planner attempt(s) on {charter} already ran "
                     f"and drew their budget; R7's at-most-one-restart rule means this "
                     f"charter is not retried automatically — an operator must record a "
                     f"decision or unblocked on {charter} carrying replan=yes to resume it "
                     f"— one without replan=yes leaves {charter} held")


def _escalation_written(charter, snapshot):
    """Ledger-based idempotency: an actor-up escalation for this charter at or after
    its newest end already exists on disk, whatever the caller's snapshot says."""
    ends = [e.get("ts") for e in snapshot
            if e.get("type") == "planner-ended" and e.get("subject") == charter]
    newest = max((t for t in ends if isinstance(t, str)), default="")
    for name in sorted(ledger_files.names(fold.EVENTS)):
        if not re.fullmatch(r"L-up-[0-9]+\.jsonl", name):
            continue
        for line in ledger_files.read_text(fold.EVENTS, name).splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if (isinstance(row, dict) and row.get("type") == "escalation-blocking"
                    and row.get("subject") == charter and str(row.get("ts", "")) >= newest):
                return True
    return False


LAST_PLANNER_READY = None


def ensure_planner(events, now, *, launcher=None):
    """The tick's one Planner scheduler: reap, decide, launch ready[0] through
    launch_planner. The ordered ready snapshot lands in LAST_PLANNER_READY (None when
    unknown) for the tick row; undetermined never reads as idle."""
    global LAST_PLANNER_READY
    LAST_PLANNER_READY = None
    try:
        if events is None:
            return "undetermined"
        import relay
        snapshot = list(events)
        snapshot.extend(reap_planners(events, now))
        alive = planner_alive()
        if alive is None:
            return "undetermined"
        if alive:
            LAST_PLANNER_READY = []
            return "alive"
        if _unmatched_planners(snapshot):
            return "undetermined"   # report-only migration or unexplained start
        ready = list(relay.plannable(snapshot, fold.ROOT)[0])
        LAST_PLANNER_READY = ready
        if not ready:
            return "idle"
        charter = ready[0]
        att = relay.planner_attempts(snapshot, charter) or {}
        if att.get("next") == "held":
            return "idle"
        if att.get("next") == "escalate":
            if not _escalation_written(charter, snapshot):
                led = dispatch.alloc(fold.EVENTS, "L-up-", ".jsonl")
                refusal = dispatch.emit(led, {"spawn": led.stem}, "escalation-blocking",
                                        **_attempt_escalation(charter, att))
                if refusal:
                    raise ValueError(refusal)
            return "escalated"
        return launch_planner(charter, "charter", int(att.get("attempts") or 0) + 1,
                              launcher=launcher, now=now)
    except Exception:
        return "undetermined"


def ensure_relay(events, now, *, launcher=None):
    """Restart only with readable inputs and an unclaimed packet; deduplicate ticks."""
    import relay
    try:
        if events is None:
            return "undetermined"
        # fold.read_events tolerates an absent directory; a restart decision cannot.
        ledger_files.assert_readable(fold.EVENTS)
        fd = os.open(fold.ROOT / "relay.lock", os.O_RDONLY | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                try:
                    minutes = float(os.environ.get("DOIT_RELAY_WEDGED_MIN", "30"))
                    if not math.isfinite(minutes) or minutes <= 0:
                        minutes = 30
                except ValueError:
                    minutes = 30
                try:
                    age = now.timestamp() - (fold.ROOT / "relay.heartbeat").stat().st_mtime
                except OSError:
                    return "serving"
                return "wedged" if age > minutes * 60 else "serving"
        finally:
            os.close(fd)
        pending = relay.pending_packets(events, fold.ROOT, served_by="relay")
        if not _unclaimed_pending(pending, fold.ROOT):
            return "idle"
        window = float(os.environ.get("DOIT_TICK_MIN", "5")) * 60
        lock = fold.ROOT / "relay.starting"
        # O_EXCL admits one launcher. Retry once if a concurrent tick won the race.
        for _ in range(2):
            try:
                fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                age = now.timestamp() - lock.stat().st_mtime
                if age < 0:
                    return "undetermined"
                if age <= window:
                    return "serving"
                lock.unlink()
            else:
                os.close(fd)
                os.utime(lock, (now.timestamp(), now.timestamp()))
                (launcher or default_launcher)([str(DOIT), "relay"])
                return "started"
        return "serving"
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return "undetermined"


def relay_main(print_only=False, max_cycles=None):
    """The relay's own standing loop (R1), alongside `main()` (Planner) and
    `executor_loop` (Executor) — a fourth member of this module's family of
    loop functions, never a branch inside `_cycle()` (see the spec's own
    Boundaries: merging it there would collapse this module's one-pane-per-tick
    shape into a second thing per tick).

    Every cycle: `relay.pending_packets` then `_unclaimed_pending`. Nothing
    unclaimed -> print `RELAY_DRY`; print_only returns None here, having done
    no write at all; otherwise `_hold` (this module's own, already used by the
    Planner) waits out `_interval()` or a ledger change, then the loop
    continues. Something unclaimed -> allocate one fresh `L-relay-<NNNN>.jsonl`
    (D90), build the pane command naming every unclaimed spawn id, oldest
    first, as the opening prompt; print_only prints and returns `(cmd, env)`
    with no subprocess and no event (the allocation itself is the one file
    write, still empty); otherwise run the pane in the foreground and wait for
    it — it ends ITSELF via `doit pane-end --relay`, this loop never signals
    it — recording `spawn-started`/`spawn-done` (or `spawn-failed` on an
    environment or resolution error starting it). One lifetime lock covers all
    holds, pane waits and failure backoffs; every exit releases ownership."""
    fd = None
    try:
        if not print_only:
            fold.ROOT.mkdir(parents=True, exist_ok=True)
            fd = os.open(fold.ROOT / "relay.lock", os.O_RDONLY | os.O_CREAT, 0o644)
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        print("relay: another relay holds relay.lock; exiting")
                        return None
                    time.sleep(0.1)
            _relay_heartbeat()
        import relay
        contract = dispatch.AGENTS / "relay.md"
        if not contract.exists():
            sys.exit(f"up: no relay contract at {contract} — the pane is the contract (D116)")
        install(contract)
        (fold.ROOT / "events").mkdir(parents=True, exist_ok=True)
        mp = models.load(fold.ROOT / "models.toml")
        started_sha = None
        if not print_only:
            started_sha = code_sha()
            led = dispatch.alloc(fold.EVENTS, "L-up-", ".jsonl")
            dispatch.emit(led, {}, "supervisor-code", subject="relay", kind="relay",
                          sha=started_sha or "undetermined", pid=os.getpid(), reason="start")
        failure_class, failures = None, 0
        state, cycles = {"watermark": None, "said": None}, 0
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            if not print_only:
                _relay_heartbeat()
                maybe_reexec("relay", started_sha)   # one call per cycle: this loop starts at
                                                      # most one pane per iteration (R2 point 5)
            try:
                pending = relay.pending_packets(fold.read_events(), fold.ROOT)
            except ledger_cache.LockTimeout as exc:
                print(str(exc), file=sys.stderr)
                if print_only:
                    return None
                _SLEEP(_interval())
                continue
            todo = _unclaimed_pending(pending, fold.ROOT)
            if not todo:
                if print_only:
                    print(RELAY_DRY)
                    return None
                _hold(state, [RELAY_DRY], relay_only=True)
                continue
            ledger = dispatch.alloc(fold.EVENTS, "L-relay-", ".jsonl")
            ids = ",".join(str(p.get("spawn") if isinstance(p, dict) else p) for p in todo)
            model = launch.model_for("relay")
            model_configured = models.resolve("relay", mp=mp).get("model") if mp is not None else None
            cmd = relay_pane_cmd(prompt=ids, name=ledger.stem, model=model)
            if print_only:
                print(f"# relay pane: {ledger.stem} · {' '.join(cmd)}")
                return cmd, _child_env(ledger)
            base = {"spawn": ledger.stem}
            print(f"# relay pane: {ledger.stem} · serving {ids}")
            try:
                dispatch.emit(ledger, base, "spawn-started", subject=ledger.stem, role="relay",
                              code_sha=started_sha or "undetermined")
                env = launch.child_env("relay", base=_child_env(ledger, supervised=True))
                cli_path.claude_bin_or_raise()
                proc = subprocess.Popen(cmd, env=env)
            except (OSError, ValueError, scratch.ScratchRootError) as e:
                dispatch.emit(ledger, base, "spawn-failed", subject=ledger.stem, role="relay",
                              why=f"{type(e).__name__}: {str(e)[:200]}")
                failures = failures + 1 if failure_class is type(e) else 1
                failure_class = type(e)
                # The 30-second floor reaches the cap by the third failure.
                remaining = min(max(_interval(), 30) * 2 ** min(failures - 1, 2), 120)
                while remaining > 0:
                    _relay_heartbeat()
                    step = min(POLL_STEP, remaining)
                    _SLEEP(step)
                    remaining -= step
                continue
            failure_class, failures = None, 0
            launch.record("relay", ledger.stem, proc.pid, model or "unpinned",
                          str(dispatch.AGENTS / "relay.md"), model_configured=model_configured)
            while True:
                try:
                    rc = proc.wait(timeout=max(0.1, min(_interval(), 60)))
                    break
                except subprocess.TimeoutExpired:
                    _relay_heartbeat()
            launch.ended(ledger.stem, rc)
            dispatch.emit(ledger, base, "spawn-done", subject=ledger.stem, role="relay", exit_code=rc)
        return None

    finally:
        if fd is not None:
            os.close(fd)


# ── the Executor's own supervising loop (L-charter-0021 R1/R2/R12/R15) ───────
# A second, INDEPENDENT loop. `main()` above is the Planner's and is untouched:
# the Executor pane had a launcher only in an operator's hands, and nothing
# re-read the ledger or restarted it when it ended.

# Wave-1 seams this unit consumes but does not own. Their module has not landed
# on this root yet (measured 2026-09-17: `pane_name` and `decide_overdue` are
# defined nowhere under src/), so they are bound late, by name, and each is
# overridable by the module attribute below — which is also how a test stubs one.
SEAM_MODULES = ("pane_identity", "panes", "board", "relay", "guard")
PANE_NAME = None                      # pane_name(ledger_file) -> str
DECIDE_OVERDUE = None                 # decide_overdue(events) -> list[dict]
GUARD_REGISTERED = None               # guard.registered(settings_path=None) -> bool
_SLEEP = time.sleep                   # the backoff, stubbable — never a real wait in a test


def _seam(name, hook):
    """Late-bind a wave-1 seam. This module's own `hook` attribute wins (a test, or
    a wiring line); otherwise the first module on sys.path that produces it. None
    when nothing does yet: wave 1 may land after this unit, and a launcher that
    dies on a missing sibling is worse than one that says the sibling is missing."""
    fn = globals().get(hook)
    if callable(fn):
        return fn
    for mod in SEAM_MODULES:
        try:
            m = importlib.import_module(mod)
        except Exception:
            continue
        fn = getattr(m, name, None)
        if callable(fn):
            globals()[hook] = fn
            return fn
    return None


def _stem(ledger_file):
    """`L-executor-0031.jsonl` -> `L-executor-0031`. The FALLBACK for `pane_name`
    only — this is not that seam and must never be read as it (AC1): it is the
    same `ledger.stem` `dispatch.alloc` already returned, so the pane's name and
    its ledger actor cannot drift apart (D90) while wave 1 is in flight."""
    s = str(ledger_file)
    return s[:-len(".jsonl")] if s.endswith(".jsonl") else s


def executor_deny_list():
    """The Executor pane's `--disallowedTools`: §10.5's RETIRE by name (D119), plus
    the suite runs and the commit its contract says it must not perform by hand
    (R15a). Every Bash entry is the `Bash(<cmd>:*)` PREFIX form `dispatch.BUILDER_DENY`
    already ships — the only shape measured to fire (docs/handoffs/prompt-tier-
    contracts.md, 2026-09-08). A mid-path glob nobody has watched fire would be
    decoration; a spelling a prefix cannot reach is R15(b)'s porcelain backstop's
    job, not a pattern's. Prevention here is best-effort and says so."""
    return [f"Skill({s})" for s in tick.RETIRE] + [
        "Bash(pytest:*)", "Bash(python3 -m pytest:*)", "Bash(python -m pytest:*)",
        "Bash(npm test:*)", "Bash(npm run test:*)", "Bash(doit test:*)",
        "Bash(./doit test:*)", "Bash(node:*)", "Bash(git commit:*)"]


def executor_prompt(ledger_file, board):
    """The pane is SELF-SEEDED: this is passed as claude's positional prompt, so
    there is no blinking cursor waiting for an operator to type the lane in.

    R3: names BOTH halves of unattended operation — the background wake (`doit
    wait --max 300` at the end of every turn) and the real end command (`doit
    pane-end --executor --handover <file>`, never a bare stop)."""
    stem = _stem(ledger_file)
    return (f"{stem}: you are the Executor pane on this root, and this is your whole seed.\n"
            f"Take the next durable action on the board below, then the next. You write no "
            f"product code and run no suite: `doit dispatch` is the only build path, and your "
            f"deny list holds you to it.\n"
            f"At the end of every turn, start `doit wait --max 300` as a background task — it "
            f"blocks on the ledger or up to five minutes, so a real change wakes you inside five "
            f"minutes whether or not anyone types.\n"
            f"End yourself at a quiet point — `up.quiet_point(fold.read_events(), '{stem}')` is "
            f"that predicate: your own handover (`doit append message-sent {stem} ...`) is on "
            f"your ledger file AND nothing is in flight. Then end the OS process itself with "
            f"`doit pane-end --executor --handover <the handover file>` — never a bare stop; a "
            f"pane that only prints and idles is never replaced. Ending is safe: every fact you "
            f"acted on is durable ledger state, the next pane re-derives this same board, and the "
            f"loop starts it the moment you exit — no keystroke, no wait.\n\n" + board)


def _pane_argv(ledger_file, board, model=None):
    """`claude -n <ledger stem> --agent executor ...` (R12). Interactive by
    construction: no -p, no --json-schema, no ANTHROPIC_API_KEY (spec 572, D121).
    `model`, truthy, inserts `["--model", model]` right after `--agent executor`."""
    name = (_seam("pane_name", "PANE_NAME") or _stem)(ledger_file)
    return (["claude", "-n", name, "--agent", "executor"] + (["--model", model] if model else []) +
            ["--disallowedTools", ",".join(executor_deny_list()),
             *ctx_guard.hook_args("executor"),
             "--dangerously-skip-permissions", executor_prompt(ledger_file, board)])


def _from_pane(src, stem):
    """An event's `_src` (`<file>:<line>`) is this pane's own. Prefix alone would
    read L-executor-0031's handover as L-executor-0003's."""
    s = str(src or "")
    return s.startswith(stem) and s[len(stem):len(stem) + 1] in (".", ":")


def quiet_point(events, pane):
    """Is ending safe for this pane? True only once its OWN ledger file carries a
    `message-sent` handover (L-adr-0043's four event types; no fifth is invented
    here) AND nothing REAL is in flight.

    `tick.spawn_in_flight` (0187), not `tick.in_flight`: an open
    `escalation-blocking` on an unrelated subject elsewhere is the OPERATOR's,
    never a reason to stall this pane's own end (measured: `in_flight`'s merged
    busy set otherwise stalls every Executor pane behind any one open operator
    escalation, anywhere — 0187 Assumption 7, AC6(d)). `tick.lane`'s own use of
    `in_flight` is unchanged; only this predicate switches.

    NOT pure: `tick.spawn_in_flight` appends one `spawn-stale` into
    `tick.tick_path()` per dead spawn, by that function's own shipped contract.
    That append is idempotent (`ended` already counts `spawn-stale`) and is the
    only write this predicate can cause.

    A pane is never in flight against ITSELF: the loop's own `spawn-started` for
    the running pane is dropped before `spawn_in_flight` sees the list, or the
    pane it describes could never reach a quiet point and the restart loop never
    turns."""
    stem = _stem(str(pane).rsplit("/", 1)[-1])
    if not any(e.get("type") == "message-sent" and _from_pane(e.get("_src"), stem) for e in events):
        return False
    return not tick.spawn_in_flight([e for e in events if e.get("spawn") != stem])


def _open_builds(events):
    """Lowercased subjects with a `build-started` and no terminal event — the
    worktrees a detached builder is legitimately writing into right now. Derived
    here rather than via `tick.in_flight` on purpose: R15(b) must cause no write,
    and `in_flight` emits."""
    ended = {e.get("spawn") for e in events
             if e.get("type") in ("build-done", "spawn-done", "spawn-failed", "spawn-stale")
             and e.get("spawn")}
    return {str(e.get("subject")).lower() for e in events
            if e.get("type") == "build-started" and e.get("spawn") not in ended and e.get("subject")}


def _repo_dirs(root):
    """R15(b)'s two globs, exactly: `repos/*` (the master checkouts the Executor
    contract forbids it to edit) and `worktrees/*/*` (that contract's own
    `WT="$R/worktrees/<project>/<spec, lowercased>"` layout)."""
    root = pathlib.Path(root)
    return sorted(set(root.glob("repos/*")) | set(root.glob("worktrees/*/*")))


def _porcelain(p):
    try:
        return dispatch.porcelain(p)
    except OSError:
        return None                    # gone or unreadable: undetermined, and undetermined is never clean


def _snapshot(root, paths=None):
    """path -> porcelain, for each watched repo. NOT_A_REPO (a plain project
    directory) and None (undetermined) are kept as themselves and can never
    produce a diff: a difference you could not establish is not an edit."""
    return {str(p): _porcelain(p) for p in (_repo_dirs(root) if paths is None else paths)}


def _repo_edits(before, after, ledger, events):
    """R15(b), the detective half. A path in one snapshot and not the other (a
    `git worktree add`, a `doit reap`) is not a diff — only paths in both are
    compared. A worktree with an OPEN `build-started` is skipped: a detached
    builder writes there by grant, and including it would drown this signal in
    builder work. Everything left is a hand on a repo the Executor was told not
    to touch, and gets one `repo-edit` on that pane's own ledger file."""
    open_builds, out = _open_builds(events), []
    for path in sorted(set(before) & set(after)):
        b, a = before[path], after[path]
        if b is None or a is None or dispatch.NOT_A_REPO in (b, a) or b == a:
            continue
        p = pathlib.Path(path)
        if p.parent.parent.name == "worktrees" and p.name.lower() in open_builds:
            continue
        changed = sorted(set(a.splitlines()) ^ set(b.splitlines()))
        dispatch.emit(ledger, {}, "repo-edit", subject=ledger.stem, ledger=ledger.name,
                      path=path, changed=changed[:20])
        out.append(path)
    return out


def _settle_overdue(events):
    """A6: once per cycle, immediately before this cycle's pane is launched, and
    never under print_only (the seam writes, by its producer's contract)."""
    fn = _seam("decide_overdue", "DECIDE_OVERDUE")
    if fn is None:
        print("up: decide_overdue has not landed on this root (wave 1) — 0 of this cycle's "
              "overdue questions were settled; the pane starts on an unsettled board", file=sys.stderr)
        return []
    return list(fn(events) or [])


def _check_guard():
    """R3's own guard check, at the loop's start: `guard.registered()` late-bound
    exactly as `pane_name` already is (`_seam("registered", "GUARD_REGISTERED")`).
    A missing seam (wave 1 not merged on this root) or a False registration each
    print ONE stderr line and let the loop proceed — this launcher never blocks
    on a guard it does not own and cannot install (ADR-0028-7)."""
    fn = _seam("registered", "GUARD_REGISTERED")
    if fn is None:
        print("up: guard.registered has not landed on this root yet — proceeding unguarded",
              file=sys.stderr)
        return
    try:
        ok = fn()
    except Exception as e:
        print(f"up: guard.registered raised {type(e).__name__}: {e} — proceeding unguarded",
              file=sys.stderr)
        return
    if not ok:
        print("up: the destructive-delete-guard is not registered for this user "
              "(doit guard install) — proceeding unguarded", file=sys.stderr)


def executor_loop(root, interval, print_only=False, max_cycles=None):
    """Forever: settle what a default now settles, derive the board, launch a named,
    self-seeded, tool-restricted Executor pane, wait for it to end itself, record
    any repo it touched, and start the next one — with no keystroke between.

    `interval` is the backoff when the pane will not START (and AC4's bound), never
    a sleep between a healthy pane's exit and the next one's launch. `max_cycles`
    is a test-only seam; no caller outside this file passes it."""
    root = pathlib.Path(root)
    contract = dispatch.AGENTS / "executor.md"
    if not contract.exists():
        sys.exit(f"up: no executor contract at {contract} — the pane is the contract (D116)")
    # The root's ruling first, and loudly (mirrors tick.main's own guard): a root whose
    # map says claude-p is the TICK's to drive, and a second driver here would duplicate it.
    mp = models.load(root / "models.toml")
    if mp is not None and models.backend_of("executor", mp) != "pane":
        why = (f"executor backend is {models.backend_of('executor', mp)!r} under "
               f"{root / 'models.toml'}; executor_loop supervises a pane and nothing else (L-adr-0035)")
        print(f"up: refused — {why}", file=sys.stderr)
        sys.exit(2)
    _check_guard()
    install(contract)
    (root / "events").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    events_dir = root / "events"
    started_sha = None
    if not print_only:
        started_sha = code_sha()
        led = dispatch.alloc(events_dir, "L-up-", ".jsonl")
        dispatch.emit(led, {}, "supervisor-code", subject="executor", kind="executor",
                      sha=started_sha or "undetermined", pid=os.getpid(), reason="start")
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        if not print_only:
            maybe_reexec("executor", started_sha, events_dir=events_dir)   # one call per
                                                  # cycle: at most one pane per iteration
        ledger = dispatch.alloc(root / "events", "L-executor-", ".jsonl")
        try:
            ev = fold.read_events()
            if not print_only:
                _settle_overdue(ev)
                ev = fold.read_events()      # decisions are on the board this pane reads
        except ledger_cache.LockTimeout as exc:
            print(str(exc), file=sys.stderr)
            _SLEEP(float(interval) * 60)
            continue
        board = fold.render(ev, *fold.fold(ev))
        model = launch.model_for("executor")
        model_configured = models.resolve("executor", mp=mp).get("model") if mp is not None else None
        cmd = _pane_argv(ledger.name, board, model=model)
        # R3 gap 1: built through `_child_env(..., supervised=True)` — a hand-built
        # dict here never set DOIT_SUPERVISED, so `pane_end.supervised()`'s
        # precondition refused for every pane this loop ever started.
        env = {**launch.child_env("executor", base=_child_env(ledger, supervised=True)),
               "DOIT_ROOT": str(root), "DOIT_GATE_LEDGER_FILE": ledger.name}
        print(f"# executor pane: {ledger.stem} · {' '.join(cmd[:-1])}")
        if print_only:
            return cmd, env
        before = _snapshot(root)
        base, t0 = {"spawn": ledger.stem}, time.time()
        dispatch.emit(ledger, base, "spawn-started", subject=ledger.stem, role="executor",
                      backend="pane", pane=cmd[cmd.index("-n") + 1],
                      code_sha=started_sha or "undetermined")
        try:
            proc = subprocess.Popen(cmd, env=env, cwd=str(root))
        except OSError as e:
            dispatch.emit(ledger, base, "spawn-failed", subject=ledger.stem,
                          why=f"{type(e).__name__}: {str(e)[:200]}")
            print(f"up: the executor pane would not start ({e}) — retrying in {interval}m", file=sys.stderr)
            _SLEEP(float(interval) * 60)
            continue
        launch.record("executor", ledger.stem, proc.pid, model or "unpinned",
                      str(dispatch.AGENTS / "executor.md"), model_configured=model_configured,
                      events_dir=events_dir)
        code = proc.wait()
        launch.ended(ledger.stem, code, events_dir=events_dir)
        try:
            edits = _repo_edits(before, _snapshot(root), ledger, fold.read_events())
        except ledger_cache.LockTimeout as exc:
            print(str(exc), file=sys.stderr)
            edits = None
        counts = {} if edits is None else {"repo_edits": len(edits)}
        dispatch.emit(ledger, base, "spawn-done", subject=ledger.stem, exit_code=code,
                      seconds=round(time.time() - t0, 1), **counts)
        count = "unknown" if edits is None else str(len(edits))
        print(f"# executor pane {ledger.stem} ended (exit {code}) · {count} repo-edit — next pane now")


if __name__ == "__main__":
    if "--relay" in sys.argv[1:]:
        relay_main(print_only="--print-only" in sys.argv[1:])
    else:
        main("--print-only" in sys.argv[1:])
