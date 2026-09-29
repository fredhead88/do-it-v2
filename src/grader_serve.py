#!/usr/bin/env python3
"""grader_serve — starts, reaps and mirrors sandboxed grader panes for
unclaimed grader-role seat packets (R8a, L-spec-0438, charter L-charter-0042
R8). Called once per tick, from `tick._record` — its own try/except, its own
`grader_serve_error` field on the same `tick` event (the `carry_error`
pattern) — never a second scheduler.

  run(events, *, runner=None, now=None) -> list[str]

Each call, in order:

  (0) Reap  — a mapped spawn (from every `grader-pane-started` event's own
      `pane`/`spawn_ids` CSVs, latest event wins a spawn named twice) with no
      `$R/seat/<spawn>.output.json` yet, whose newest ledger event is one of
      the terminal three (`spawn-done`/`spawn-failed`/`spawn-stale`) — the
      SAME terminal set `tick._spawn_busy` already emits — gets its mapped
      tmux window killed, best-effort. Closes the gap where `tick._spawn_busy`
      frees a capacity slot but a hung pane keeps spending.

  (1) Start — among `relay.pending_packets(events, root=None,
      served_by="grader-pane")`, claims and launches new sandboxed
      `claude --agent grader` panes for the oldest unclaimed spawns, up to
      `max_panes()` minus however many are already claimed-and-running.

  (2) Mirror + stamp — for each claimed-and-running spawn whose OWN view
      holds a schema-valid `output.json` not yet mirrored, copies it
      byte-for-byte to `$R/seat/<spawn>.output.json` and stamps it exactly
      once.

Every subprocess/tmux call goes through the injected `runner` (default
`subprocess.run`), never bare — so a test proves every call this function
makes with no real tmux, bwrap, or login anywhere.
"""
import json, os, pathlib, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch, fold, grader_view, grading_env, launch, relay, scratch, tick  # noqa: E402

# probe Q3 (Assumptions): the tmux session grader panes land in — created via
# `runner` if a first check finds it absent, never assumed to pre-exist unchecked.
TMUX_SESSION = "flow"
# The terminal set `tick._spawn_busy`/`relay.pending_packets` already use —
# named here from `relay`, never a second copy.
TERMINAL = relay.TERMINAL


def max_panes(root=None):
    """`[roles.grader].max_panes` from `launch.toml` under `root` (the repo
    root the file itself lives under — default this checkout's own, mirroring
    `launch.py`'s own `HERE.parent` convention), or 2 when the table or the
    key is absent. A DIFFERENT capacity from `models.toml`'s `[seats]`
    (builder concurrency) — this one bounds sandboxed grader panes."""
    import tomllib
    p = (pathlib.Path(root) if root else HERE.parent) / "launch.toml"
    if not p.is_file():
        return 2
    cfg = tomllib.loads(p.read_text()).get("roles", {}).get("grader") or {}
    return cfg.get("max_panes", 2)


def valid_output(path, schema_path=None):
    """True iff `path` is a non-empty, well-formed JSON file that validates
    against `schema_path` (default `agents/grader.schema.json`) — the ONE
    schema-validity check both this module's own mirror step (2) and
    `pane_end.check_and_end_grader` call; no second implementation. Any
    failure — missing file, malformed JSON, a schema miss, or `jsonschema`
    itself unimportable — reads False, never raised."""
    path = pathlib.Path(path)
    schema_path = pathlib.Path(schema_path) if schema_path else (HERE.parent / "agents" / "grader.schema.json")
    try:
        obj = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    try:
        import jsonschema
        jsonschema.validate(obj, json.loads(schema_path.read_text()))
    except Exception:
        return False
    return True


def _spawn_pane_map(events):
    """spawn -> pane, from every `grader-pane-started` event's own `pane`/
    `spawn_ids` CSVs, split by position. Ledger order is chronological, so a
    plain sequential overwrite already gives "the LATEST event wins a spawn
    named twice"."""
    out = {}
    for e in events:
        if e.get("type") != "grader-pane-started":
            continue
        panes = [p.strip() for p in str(e.get("pane") or "").split(",") if p.strip()]
        spawns = [s.strip() for s in str(e.get("spawn_ids") or "").split(",") if s.strip()]
        for pane, spawn in zip(panes, spawns):
            out[spawn] = pane
    return out


def _project_for(events, spawn):
    """The `project` a `spawn-started` recorded for this spawn — L-spec-0481:
    `grading_env.sandbox_binds`/`pane_env` need it to bind the right project's
    own `node_modules`/`venv`, and `run()` itself is handed no such argument."""
    for e in events:
        if e.get("type") == "spawn-started" and e.get("spawn") == spawn:
            return e.get("project")
    return None


def _newest_event(events, spawn):
    """The newest event (by ts) naming `spawn` in its own `spawn` field —
    never a guess when none exists."""
    newest = None
    for e in events:
        if e.get("spawn") != spawn:
            continue
        if newest is None or fold.ts(e.get("ts")) >= fold.ts(newest.get("ts")):
            newest = e
    return newest


def _start_ts_for(events, spawn):
    """The ts of the LATEST `grader-pane-started` whose `spawn_ids` split
    contains `spawn`, falling back to that spawn's own `spawn-started` ts
    otherwise. `None` when neither exists — duration_ms then stays unset."""
    best = None
    for e in events:
        if e.get("type") != "grader-pane-started":
            continue
        spawns = [s.strip() for s in str(e.get("spawn_ids") or "").split(",") if s.strip()]
        if spawn in spawns and (best is None or fold.ts(e.get("ts")) >= fold.ts(best.get("ts"))):
            best = e
    if best is not None:
        return fold.ts(best.get("ts"))
    for e in events:
        if e.get("type") == "spawn-started" and e.get("spawn") == spawn:
            return fold.ts(e.get("ts"))
    return None


def run(events, *, runner=None, now=None):
    """See module docstring for the three steps, in order. Returns the list
    of reap lines this call produced (`"reaped stale pane <pane> for
    <spawn>"`) — the reap step's only evidence beside the `runner` calls
    themselves (Assumptions: no new ledger event for step (0))."""
    events = list(events)
    runner = runner or subprocess.run
    now = now or fold.NOW
    out = []

    pane_of = _spawn_pane_map(events)
    seat_dir = fold.ROOT / "seat"

    # ── (0) Reap ─────────────────────────────────────────────────────────
    for spawn, pane in pane_of.items():
        if (seat_dir / f"{spawn}.output.json").exists():
            continue
        newest = _newest_event(events, spawn)
        if newest is None or newest.get("type") not in TERMINAL:
            continue
        try:
            runner(["tmux", "kill-window", "-t", pane])
        except Exception:
            pass
        out.append(f"reaped stale pane {pane} for {spawn}")

    # ── (1) Start ────────────────────────────────────────────────────────
    pending = relay.pending_packets(events, root=None, served_by="grader-pane")
    running, unclaimed = [], []
    for p in pending:
        spawn = p["spawn"]
        if (seat_dir / f"{spawn}.claimed").exists():
            running.append(p)
        else:
            unclaimed.append(p)
    unclaimed.sort(key=lambda p: p.get("age_min", 0), reverse=True)   # oldest (largest age_min) first

    cap = max_panes(root=None)
    started, started_panes = [], []
    session_checked = False
    for p in unclaimed:
        if len(running) >= cap:
            break
        spawn = p["spawn"]
        claim_argv = [str(HERE.parent / "scripts" / "seat" / "claim.sh"), spawn]
        claim = runner(claim_argv)
        if getattr(claim, "returncode", 1) != 0:
            continue
        if not session_checked:
            has = runner(["tmux", "has-session", "-t", TMUX_SESSION])
            if getattr(has, "returncode", 1) != 0:
                runner(["tmux", "new-session", "-d", "-s", TMUX_SESSION])
            session_checked = True
        view = scratch.sub("grade") / spawn
        cfg_dir = grader_view.config_dir()
        project = _project_for(events, spawn)
        # L-spec-0481/R12.2, R12.6: the SAME binds and environment the
        # preflight proof itself ran with — one environment for proof and
        # grade (AC8), never a second, independently-guessed set here.
        binds = grading_env.sandbox_binds(view, project) if project else []
        env = {**launch.child_env("grader"), "CLAUDE_CONFIG_DIR": str(cfg_dir),
               **grading_env.pane_env(view)}
        env_argv = [f"{k}={v}" for k, v in sorted(env.items())]
        # `-n <spawn>` (minor deviation, declared): the spec's own literal
        # argv has no room to name an addressable window target for step (0)
        # to kill later, so the window is named after its own spawn id —
        # `<session>:<spawn>` is then always a valid, unique kill-window
        # target, and every OTHER segment of the argv matches the spec's own
        # literal sequence verbatim.
        argv = (["tmux", "new-window", "-d", "-t", TMUX_SESSION, "-n", spawn, "--"]
                + grader_view.bwrap_argv(view, doit_src=HERE.parent, binds=binds)
                + ["env", "-i"] + env_argv
                + ["claude", "--agent", "grader", "--dangerously-skip-permissions"])
        runner(argv)
        running.append(p)
        started.append(spawn)
        started_panes.append(f"{TMUX_SESSION}:{spawn}")

    if started:
        unix_ts = int(now.timestamp())
        dispatch.emit(tick.tick_path(),
                      {"pane": ",".join(started_panes), "spawn_ids": ",".join(started)},
                      "grader-pane-started", subject=f"grader-panes-{unix_ts}")

    # ── (2) Mirror + stamp ───────────────────────────────────────────────
    for p in running:
        spawn = p["spawn"]
        seat_out = seat_dir / f"{spawn}.output.json"
        if seat_out.exists():
            continue
        view_out = scratch.sub("grade") / spawn / "seat" / f"{spawn}.output.json"
        if not view_out.is_file() or not valid_output(view_out):
            continue
        seat_dir.mkdir(parents=True, exist_ok=True)
        seat_out.write_bytes(view_out.read_bytes())
        model = launch.model_for("grader") or "unpinned"
        start_ts = _start_ts_for(events, spawn)
        duration_ms = round((now - start_ts).total_seconds() * 1000) if start_ts is not None else "-"
        stamp_argv = [str(HERE.parent / "scripts" / "seat" / "stamp.sh"),
                     spawn, model, spawn, "-", str(duration_ms), "0"]
        stamp_env = {**os.environ, "DOIT_CLAUDE_PROJECTS": str(grader_view.config_dir() / "projects")}
        runner(stamp_argv, env=stamp_env)

    return out
