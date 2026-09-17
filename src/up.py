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
import importlib, json, os, pathlib, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch, fold, models, tick  # noqa: E402

DOIT = HERE.parent / "doit"


def cron_line():
    """The tick's schedule. DOIT_TICK_MIN is the same variable the fold's
    tick-stale line reads, so a line installed from here cannot disagree with the
    staleness alarm that watches it."""
    every = os.environ.get("DOIT_TICK_MIN", "5")
    return (f"*/{every} * * * * DOIT_ROOT={fold.ROOT} {DOIT} tick "
            f">> {fold.ROOT}/logs/tick.log 2>&1")


def pane_cmd(prompt=None):
    """Interactive, so no -p, no --json-schema, no --output-format: the pane's
    Output is the files and events it writes. The agent file's tools: line is the
    sandbox; the deny list is the only form a retire list has (D119).

    `prompt` is the opening turn the supervisor hands the pane — one charter id, or
    a serving pass's comma-joined spawn ids — and it goes on as a TRAILING
    POSITIONAL, after --dangerously-skip-permissions. That flag is the last option
    this builds, and a positional in front of it reads as its value."""
    cmd = ["claude", "--agent", "planner",
           "--disallowedTools", ",".join(f"Skill({s})" for s in tick.RETIRE),
           "--dangerously-skip-permissions"]
    return cmd + [prompt] if prompt else cmd


AGENTS_HOME = pathlib.Path.home() / ".claude" / "agents"


def install(contract):
    """`--agent planner` resolves through ~/.claude/agents, not through this repo —
    measured: a pane launched without this exits 1 with `--agent 'planner' not found`
    AFTER the cron line has been printed, which reads like success. The ten contracts
    are symlinked there already; this is the eleventh link, made idempotently and
    never over somebody else's file. `think.py` links the twelfth through this same
    function, because the hole is the launcher's and not the Planner's."""
    link = AGENTS_HOME / contract.name
    if link.is_symlink() and link.resolve() == contract.resolve():
        return
    if link.exists() or link.is_symlink():
        sys.exit(f"up: {link} is not this repo's contract — resolve it by hand:\n"
                 f"  ln -sfn {contract} {link}")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(contract)
    print(f"# linked {link} -> {contract}")


def main(print_only=False, max_cycles=None):
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
        cmd = pane_cmd()
        print(f"# planner pane: {ledger.stem} · {' '.join(cmd)}")
        return cmd, _child_env(ledger)
    state, cycles = {"up": None, "watermark": None, "said": None}, 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        _cycle(state)
    return cycles


# ── the supervisor: constants and the private helpers main() runs on ──────────
SUPERVISED = "DOIT_SUPERVISED"            # presence-only, on every child this starts
INTERVAL_ENV, INTERVAL_DEFAULT = "DOIT_SUPERVISOR_INTERVAL_SEC", 30.0
POLL_STEP = 0.5                           # how finely a hold notices a ledger write
RELAY_FNS = ("plannable", "waiting_lines", "ledger_changed",
             "pending_packets", "planner_attempts")


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
    is presence-only: the pane reads it to know it may end itself."""
    env = {**os.environ, "DOIT_LEDGER_FILE": ledger.name,
           "PATH": f"{HERE.parent}:{os.environ.get('PATH', '')}"}
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


def _ended(ledger):
    """True if the pane wrote its own end row. Re-read, never remembered (§9.2)."""
    for line in ledger.read_text().splitlines():
        if not line.strip().startswith("{"):
            continue
        try:
            if json.loads(line).get("type") == "planner-ended":
                return True
        except ValueError:
            continue
    return False


def _start(state, subject, mode, attempt, prompt, spawn_ids=None):
    """Start one pane and wait for it. The start row lands in the CHILD's own file
    before the child exists (L-adr-0027) — a pane that dies in its first second has
    still been recorded as started, which is the whole of planner_attempts' input.
    The end row is appended only where the child wrote none: its own clean
    `planner-ended` is the better record and is never doubled."""
    ledger = dispatch.alloc(fold.EVENTS, "L-planner-", ".jsonl")
    base, ids = {"spawn": ledger.stem}, ({"spawn_ids": spawn_ids} if spawn_ids else {})
    dispatch.emit(ledger, base, "planner-started", subject=subject, planner=ledger.stem,
                  attempt=attempt, mode=mode, **ids)
    cmd = pane_cmd(prompt)
    print(f"# planner pane: {ledger.stem} · {mode} · {subject} · attempt {attempt}")
    rc = subprocess.run(cmd, env=_child_env(ledger, supervised=True)).returncode
    if not _ended(ledger):
        dispatch.emit(ledger, base, "planner-ended", subject=subject, planner=ledger.stem,
                      mode=mode, reason=f"exit-{rc}", **ids)


def _charter_pass(state, events, charter):
    """One ready charter. False means the relay failed and the cycle holds."""
    ok, att = _guarded("planner_attempts", events, charter_id=charter)
    if not ok:
        return False
    att = att or {}
    if att.get("next") == "escalate":
        led = _launcher(state)
        dispatch.emit(led, {"spawn": led.stem}, "escalation-blocking", subject=charter,
                      last_reason=att.get("last_reason"), attempts=att.get("attempts"),
                      why=f"planner on {charter} ended {att.get('attempts')}x without landing — "
                          f"last: {att.get('last_reason')}")
        print(f"# {charter}: escalation-blocking — last {att.get('last_reason')}")
        return True                       # the charter leaves `ready` on relay's own exclusion
    _start(state, subject=charter, mode="charter",
           attempt=int(att.get("attempts") or 0) + 1, prompt=charter)
    return True


def _serving_split(events, pending):
    """Pending spawn ids, minus the ones a serving pass already took. The record is
    this spec's own `spawn_ids=` field on a serving `planner-ended` — an id served
    once is not served again, and one that is skipped is said out loud."""
    ids = sorted({(p.get("spawn") if isinstance(p, dict) else p) for p in (pending or [])})
    served = set()
    for e in events:
        if e.get("type") == "planner-ended" and e.get("mode") == "serving":
            served |= {s for s in str(e.get("spawn_ids") or "").split(",") if s}
    return [i for i in ids if i not in served], [i for i in ids if i in served]


def _hold(state, lines):
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
        ok, res = _guarded("ledger_changed", fold.ROOT, state["watermark"])
        if not ok:
            return
        changed, state["watermark"] = res
        if changed or time.monotonic() >= end:
            return
        time.sleep(min(POLL_STEP, max(0.0, end - time.monotonic())))


def _cycle(state):
    """One turn of the loop: charter work first, a serving pass only while none is
    plannable (L-adr-0029), and a hold when there is neither."""
    events = fold.read_events()
    ok, plan = _guarded("plannable", events, fold.ROOT)
    if not ok:
        return _degrade()
    ready = list(plan[0])
    ok, pending = _guarded("pending_packets", events, fold.ROOT)   # derived every cycle
    if not ok:
        return _degrade()
    if ready:
        for charter in ready:
            if not _charter_pass(state, events, charter):
                return _degrade()
        return
    todo, skipped = _serving_split(events, pending)
    for spawn in skipped:
        print(f"# serving: {spawn} already had a serving pass — skipped")
    if todo:
        ids = ",".join(todo)
        return _start(state, subject="serving:" + ids, mode="serving",
                      attempt=1, prompt=ids, spawn_ids=ids)
    ok, lines = _guarded("waiting_lines", events, fold.ROOT)
    if not ok:
        return _degrade()
    _hold(state, list(lines))


# ── the Executor's own supervising loop (L-charter-0021 R1/R2/R12/R15) ───────
# A second, INDEPENDENT loop. `main()` above is the Planner's and is untouched:
# the Executor pane had a launcher only in an operator's hands, and nothing
# re-read the ledger or restarted it when it ended.

# Wave-1 seams this unit consumes but does not own. Their module has not landed
# on this root yet (measured 2026-09-17: `pane_name` and `decide_overdue` are
# defined nowhere under src/), so they are bound late, by name, and each is
# overridable by the module attribute below — which is also how a test stubs one.
SEAM_MODULES = ("pane_identity", "panes", "board", "relay")
PANE_NAME = None                      # pane_name(ledger_file) -> str
DECIDE_OVERDUE = None                 # decide_overdue(events) -> list[dict]
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
    there is no blinking cursor waiting for an operator to type the lane in."""
    stem = _stem(ledger_file)
    return (f"{stem}: you are the Executor pane on this root, and this is your whole seed.\n"
            f"Take the next durable action on the board below, then the next. You write no "
            f"product code and run no suite: `doit dispatch` is the only build path, and your "
            f"deny list holds you to it.\n"
            f"End yourself at a quiet point — `up.quiet_point(fold.read_events(), '{stem}')` is "
            f"that predicate: your own handover (`doit append message-sent {stem} ...`) is on "
            f"your ledger file AND nothing is in flight. Ending is safe: every fact you acted on "
            f"is durable ledger state, the next pane re-derives this same board, and the loop "
            f"starts it the moment you exit — no keystroke, no wait.\n\n" + board)


def _pane_argv(ledger_file, board):
    """`claude -n <ledger stem> --agent executor ...` (R12). Interactive by
    construction: no -p, no --json-schema, no ANTHROPIC_API_KEY (spec 572, D121)."""
    name = (_seam("pane_name", "PANE_NAME") or _stem)(ledger_file)
    return ["claude", "-n", name, "--agent", "executor",
            "--disallowedTools", ",".join(executor_deny_list()),
            "--dangerously-skip-permissions", executor_prompt(ledger_file, board)]


def _from_pane(src, stem):
    """An event's `_src` (`<file>:<line>`) is this pane's own. Prefix alone would
    read L-executor-0031's handover as L-executor-0003's."""
    s = str(src or "")
    return s.startswith(stem) and s[len(stem):len(stem) + 1] in (".", ":")


def quiet_point(events, pane):
    """Is ending safe for this pane? True only once its OWN ledger file carries a
    `message-sent` handover (L-adr-0043's four event types; no fifth is invented
    here) AND nothing is in flight.

    NOT pure: `tick.in_flight` appends one `spawn-stale` into `tick.TICK` per dead
    spawn, by that function's own shipped contract. That append is idempotent
    (`in_flight` counts `spawn-stale` in its own `ended` set) and is the only write
    this predicate can cause.

    A pane is never in flight against ITSELF: the loop's own `spawn-started` for
    the running pane is dropped before `in_flight` sees the list, or the pane it
    describes could never reach a quiet point and the restart loop never turns."""
    stem = _stem(str(pane).rsplit("/", 1)[-1])
    if not any(e.get("type") == "message-sent" and _from_pane(e.get("_src"), stem) for e in events):
        return False
    return not tick.in_flight([e for e in events if e.get("spawn") != stem])


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
    install(contract)
    (root / "events").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        ledger = dispatch.alloc(root / "events", "L-executor-", ".jsonl")
        ev = fold.read_events()
        if not print_only:
            _settle_overdue(ev)
            ev = fold.read_events()          # its decisions are on the board this pane reads
        board = fold.render(ev, *fold.fold(ev))
        cmd = _pane_argv(ledger.name, board)
        env = {**os.environ, "DOIT_ROOT": str(root), "DOIT_LEDGER_FILE": ledger.name,
               "DOIT_GATE_LEDGER_FILE": ledger.name,
               "PATH": f"{HERE.parent}:{os.environ.get('PATH', '')}"}
        print(f"# executor pane: {ledger.stem} · {' '.join(cmd[:-1])}")
        if print_only:
            return cmd, env
        before = _snapshot(root)
        base, t0 = {"spawn": ledger.stem}, time.time()
        dispatch.emit(ledger, base, "spawn-started", subject=ledger.stem, role="executor",
                      backend="pane", pane=cmd[cmd.index("-n") + 1])
        try:
            code = subprocess.run(cmd, env=env, cwd=str(root)).returncode
        except OSError as e:
            dispatch.emit(ledger, base, "spawn-failed", subject=ledger.stem,
                          why=f"{type(e).__name__}: {str(e)[:200]}")
            print(f"up: the executor pane would not start ({e}) — retrying in {interval}m", file=sys.stderr)
            _SLEEP(float(interval) * 60)
            continue
        edits = _repo_edits(before, _snapshot(root), ledger, fold.read_events())
        dispatch.emit(ledger, base, "spawn-done", subject=ledger.stem, exit_code=code,
                      seconds=round(time.time() - t0, 1), repo_edits=len(edits))
        print(f"# executor pane {ledger.stem} ended (exit {code}) · {len(edits)} repo-edit — next pane now")


if __name__ == "__main__":
    main("--print-only" in sys.argv[1:])
