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
import dispatch, fold, launch, models, tick  # noqa: E402

DOIT = HERE.parent / "doit"


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
           "--dangerously-skip-permissions"]
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
        name = (_seam("pane_name", "PANE_NAME") or _stem)(ledger.name)
        cmd = pane_cmd(name=name)
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
    name = (_seam("pane_name", "PANE_NAME") or _stem)(ledger.name)
    mp = models.load(fold.ROOT / "models.toml")
    model = launch.model_for("planner")
    model_configured = models.resolve("planner", mp=mp).get("model") if mp is not None else None
    cmd = pane_cmd(prompt, name=name, model=model)
    print(f"# planner pane: {ledger.stem} · {mode} · {subject} · attempt {attempt}")
    env = launch.child_env("planner", base=_child_env(ledger, supervised=True))
    proc = subprocess.Popen(cmd, env=env)
    launch.record("planner", ledger.stem, proc.pid, model or "unpinned",
                  str(dispatch.AGENTS / "planner.md"), model_configured=model_configured)
    rc = proc.wait()
    if not _ended(ledger):
        dispatch.emit(ledger, base, "planner-ended", subject=subject, planner=ledger.stem,
                      mode=mode, reason=f"exit-{rc}", **ids)
    launch.ended(ledger.stem, rc)


def _charter_pass(state, events, charter):
    """One ready charter. False means the relay failed and the cycle holds."""
    ok, att = _guarded("planner_attempts", events, charter_id=charter)
    if not ok:
        return False
    att = att or {}
    if att.get("next") == "escalate":
        led = _launcher(state)
        # R3/L-spec-0192: `escalation_ok` now gates this write (via `emit()`'s
        # `required_reason` door) — mirroring dispatch.py's own post-0192 fix for
        # the identical gap, this names the irreversible act rather than inventing
        # a default/deadline/revert for a retry that R7's at-most-one-restart rule
        # already forbids from happening on its own (measured: this call site was
        # silently refused, and no escalation-blocking row has landed from it,
        # since L-spec-0192 shipped — an unrelated, pre-existing bug fixed here
        # because it otherwise blocks this file from ever reaching exit 0).
        dispatch.emit(led, {"spawn": led.stem}, "escalation-blocking", subject=charter,
                      last_reason=att.get("last_reason"), attempts=att.get("attempts"),
                      why=f"planner on {charter} ended {att.get('attempts')}x without landing — "
                          f"last: {att.get('last_reason')}",
                      irreversible=f"{att.get('attempts')} planner attempt(s) on {charter} already ran "
                                   f"and drew their budget; R7's at-most-one-restart rule means this "
                                   f"charter is not retried automatically — an operator must record a "
                                   f"decision or unblocked on {charter} to resume it")
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


# ── the relay: a second, standing loop, independent of the charter/serving
# priority above (L-spec-0270 R1) ─────────────────────────────────────────────
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
    `OSError` starting it)."""
    import relay
    contract = dispatch.AGENTS / "relay.md"
    if not contract.exists():
        sys.exit(f"up: no relay contract at {contract} — the pane is the contract (D116)")
    install(contract)
    (fold.ROOT / "events").mkdir(parents=True, exist_ok=True)
    mp = models.load(fold.ROOT / "models.toml")
    state, cycles = {"watermark": None, "said": None}, 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        pending = relay.pending_packets(fold.read_events(), fold.ROOT)
        todo = _unclaimed_pending(pending, fold.ROOT)
        if not todo:
            if print_only:
                print(RELAY_DRY)
                return None
            _hold(state, [RELAY_DRY])
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
        env = launch.child_env("relay", base=_child_env(ledger, supervised=True))
        dispatch.emit(ledger, base, "spawn-started", subject=ledger.stem, role="relay")
        print(f"# relay pane: {ledger.stem} · serving {ids}")
        try:
            proc = subprocess.Popen(cmd, env=env)
        except OSError as e:
            dispatch.emit(ledger, base, "spawn-failed", subject=ledger.stem, role="relay",
                          why=f"{type(e).__name__}: {str(e)[:200]}")
            continue
        launch.record("relay", ledger.stem, proc.pid, model or "unpinned",
                      str(dispatch.AGENTS / "relay.md"), model_configured=model_configured)
        rc = proc.wait()
        launch.ended(ledger.stem, rc)
        dispatch.emit(ledger, base, "spawn-done", subject=ledger.stem, role="relay", exit_code=rc)
    return None


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
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        ledger = dispatch.alloc(root / "events", "L-executor-", ".jsonl")
        ev = fold.read_events()
        if not print_only:
            _settle_overdue(ev)
            ev = fold.read_events()          # its decisions are on the board this pane reads
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
                      backend="pane", pane=cmd[cmd.index("-n") + 1])
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
        edits = _repo_edits(before, _snapshot(root), ledger, fold.read_events())
        dispatch.emit(ledger, base, "spawn-done", subject=ledger.stem, exit_code=code,
                      seconds=round(time.time() - t0, 1), repo_edits=len(edits))
        print(f"# executor pane {ledger.stem} ended (exit {code}) · {len(edits)} repo-edit — next pane now")


if __name__ == "__main__":
    if "--relay" in sys.argv[1:]:
        relay_main(print_only="--print-only" in sys.argv[1:])
    else:
        main("--print-only" in sys.argv[1:])
