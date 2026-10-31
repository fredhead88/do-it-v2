#!/usr/bin/env python3
"""Dispatch transport, health recovery, and legacy read-only eligibility views.

Routine launches delegate to tick's Step consumer. candidates remains a legacy
read-only filesystem preparation diagnostic.
"""
import ledger_files
import datetime, json, os, pathlib, re, socket, subprocess, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dispatch, fold, panes, shape, tick  # noqa: E402


def capacity(root=None):
    """Builder seats: `[seats] builder` in `$R/models.toml`; `4` when the
    table, the key, or the file itself is absent or unparsable (SD2)."""
    root = pathlib.Path(root) if root is not None else fold.ROOT
    path = root / "models.toml"
    try:
        import tomllib
        with open(path, "rb") as f:
            doc = tomllib.load(f)
        return int(doc["seats"]["builder"])
    except (OSError, ValueError, KeyError, TypeError):
        return 4


def _newest_marker_ts(evs):
    times = [fold.ts(e.get("ts")) for e in evs if e.get("type") in ("spec-written", "decision", "unblocked")]
    return max(times) if times else None


def _hold_released(hold, evs):
    """Only an explicit, strictly newer authorized release clears this hold."""
    return any(e.get('subject') == hold.get('subject')
               and e.get('actor') in ('thinker', 'operator')
               and fold.ts(e.get('ts')) > fold.ts(hold.get('ts'))
               and (e.get('type') == 'unblocked'
                    or e.get('type') == 'decision' and bool(hold.get('_src'))
                    and bool(e.get('ref')) and e['ref'] == hold['_src']) for e in evs)


def _hold_order(e):
    source, _, line = str(e.get('_src') or '').rpartition(':')
    return fold.ts(e.get('ts')), source, int(line) if line.isdigit() else 0


def held_by_dependency(spec_evs, all_evs, *, shipped=None) -> str | None:
    """The newest hold stands until its dependency ships or an own clearer wins.

    Invalid dependencies fail closed. Thinker/operator unblocked or a decision
    referring to the hold releases it; answers to other questions do not.
    A dependency shipped before the hold is already satisfied.
    """
    spec_evs = list(spec_evs)
    holds = [e for e in spec_evs if e.get("type") == "dependency-hold"]
    if not holds:
        return None
    hold = max(holds, key=_hold_order)
    if _hold_released(hold, spec_evs):
        return None
    source = f" [{hold['_src']}]" if hold.get('_src') else ''
    dependency = hold.get("waits_for")
    if not fold.valid_dependency(dependency):
        return "held: invalid waits_for; unresolvable dependency-hold" + source
    if dependency == hold.get("subject"):
        return f"held: unresolvable dependency-hold (waits_for is self: {dependency})" + source
    if (dependency in shipped if shipped is not None else
            any(e.get("type") == "shipped" and e.get("subject") == dependency for e in all_evs)):
        return None
    return f"held: waits for {dependency} ({hold.get('why', '')})" + source


def _standing_ruling(spec_evs):
    """SD10 builder veto, separate from the dependency-only reader contract."""
    evs = list(spec_evs)
    last_build = max((fold.ts(e.get('ts')) for e in evs
                      if e.get('type') == 'build-started'), default=None)
    holds = [e for e in evs if e.get('type') == 'decision'
             and str(e.get('subject') or '').startswith('L-spec-')
             and e.get('actor') in ('thinker', 'operator')
             and e.get('action') in ('hold', 'recut')
             and (last_build is None or fold.ts(e.get('ts')) > last_build)]
    hold = max(holds, key=_hold_order, default=None)
    return hold if hold is not None and not _hold_released(hold, evs) else None


def _automatic_hold(spec_evs, all_evs, *, shipped=None):
    evs = list(spec_evs)
    dependency = held_by_dependency(evs, all_evs, shipped=shipped)
    if dependency:
        return dependency
    ruling = _standing_ruling(evs)
    if ruling is not None:
        return f"held: ruling {ruling['action']} ({ruling.get('_src') or 'source missing'})"
    return None


def _standing_autodispatch_failed(evs):
    """SD1 condition 4: the newest `autodispatch-failed`, when it is NEWER
    than the newest of this subject's own `spec-written`/`decision`/
    `unblocked`/`dependency-hold` — else `None` (a later marker clears it,
    AC4)."""
    fails = [e for e in evs if e.get("type") == "autodispatch-failed"]
    if not fails:
        return None
    newest_fail = max(fails, key=lambda e: str(e.get("ts", "")))
    fail_ts = fold.ts(newest_fail.get("ts"))
    clear_ts = max((fold.ts(e.get("ts")) for e in evs
                    if e.get("type") in ("spec-written", "decision", "unblocked", "dependency-hold")),
                   default=None)
    if clear_ts is not None and clear_ts > fail_ts:
        return None
    return newest_fail


def _since(events, sid, evs, charter, wave):
    """SD1's three-part `since` (Assumption 6): later of `spec-written`/
    `decision`/`unblocked`, plus — only if this subject was EVER wave-blocked
    — the ts `wave_blocker` last stopped returning non-`None` for it,
    reproduced by re-evaluating `wave_blocker` against ledger history
    (never a new event type). Never wave-blocked (or no charter/wave to
    check) -> the first component alone (AC10)."""
    marker_ts = _newest_marker_ts(evs)
    if wave is None or charter is None:
        return marker_ts
    # Fast path (Thinker 2026-09-29, operator-approved build-speed fix): `wave_blocker` only
    # ever counts L-spec subjects whose charter stem is `charter`, and its answer can only
    # change at an event of one of those subjects. So replay just those subjects' events and
    # re-evaluate only there. Same answer as the full replay; O(sibling events^2) instead of
    # O(all events^2) per spec (the full replay made each tick take 8+ minutes).
    siblings = set()
    for e in events:
        subj = e.get("subject")
        if (subj and subj != sid and str(subj).startswith("L-spec-") and e.get("charter")
                and pathlib.Path(e["charter"]).stem == charter):
            siblings.add(subj)
    ordered = sorted(events, key=lambda e: str(e.get("ts", "")))
    was_blocked, settle_ts, prefix = False, None, []
    for e in ordered:
        if e.get("subject") not in siblings:
            continue
        prefix.append(e)
        now_blocked = dispatch.wave_blocker(prefix, charter, sid, wave) is not None
        if was_blocked and not now_blocked:
            settle_ts = fold.ts(e.get("ts"))
        was_blocked = now_blocked
    if settle_ts is None:
        return marker_ts
    return max(marker_ts, settle_ts) if marker_ts is not None else settle_ts


def _head_of(project, *, runner=None):
    """Read the repository's symbolic HEAD; a failed or detached read is unknown."""
    argv = ["git", "-C", str(fold.ROOT / "repos" / project), "symbolic-ref", "--short", "HEAD"]
    try:
        if runner is None:
            result = subprocess.run(argv, capture_output=True, text=True)
            code, stdout = result.returncode, result.stdout
        else:
            result = runner(argv)
            code, stdout = result.get("code", 1), result.get("stdout")
        return ((stdout or "").strip() or None) if code == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _builder_in_flight_count(events):
    """Seats occupied right now: a `build-started` (role=builder — the only
    role that ever writes one) with no terminal event, on a subject
    `tick.spawn_in_flight` still counts busy (its own aging/`spawn-stale`
    side effect applies unchanged, AC16)."""
    busy = tick.spawn_in_flight(events)
    ended = {e.get("spawn") for e in events if e.get("type") in ("spawn-done", "spawn-failed", "spawn-stale")}
    active = set()
    for e in events:
        if e.get("type") != "build-started" or e.get("subject") not in busy:
            continue
        sid = e.get("spawn")
        if sid and sid in ended:
            continue
        active.add(e.get("subject"))
    return len(active)


def _lane_limit(rule):
    import lane_rules
    try:
        return lane_rules.load()[rule]["limit_min"]
    except ValueError:
        return {"build": 30, "grade": 15}[rule]


def candidates(events, specs, now, *, head_of=None):
    """One row per `written` spec: `{spec, charter, project, since, status,
    reason}` — `status` `dispatchable`/`seat-wait`/`blocked`, oldest `since`
    first (SD1/SD2). `head_of(project)` is read only for a single-token Base
    declaration, once per project, and defaults to the repository's symbolic HEAD.
    Fresh resource-held written rows seat-wait without consuming capacity;
    held grades in other unsettled states get trailing resource-wait rows."""
    reader = head_of if head_of is not None else _head_of
    heads = {}

    def head_for(project):
        if project not in heads:
            heads[project] = reader(project)
        return heads[project]

    events = list(events)
    by_subject = {}
    for e in events:
        sid = e.get("subject")
        if sid:
            by_subject.setdefault(sid, []).append(e)
    busy = tick.in_flight(events)
    written = [s for s in specs.values() if s.get("state") == "written"]
    rows = []
    limit_min = _lane_limit("build")
    for s in written:
        sid = s["id"]
        evs = s.get("evs") or by_subject.get(sid, [])
        charter = dispatch.resolve_charter(events, sid, None)
        own_footprint = next((e.get("footprint") for e in reversed(evs)
                              if e.get("type") == "spec-written" and e.get("footprint")), None)
        project = next((e.get("project") for e in reversed(evs) if e.get("project")), None) or "unknown"
        wave = dispatch.spec_wave(charter, own_footprint) if (charter and own_footprint) else None
        # This diagnostic still reports filesystem preparation failures; all
        # routine launch eligibility belongs to nextstep.
        reason = _automatic_hold(evs, events)
        failure = _standing_autodispatch_failed(evs) if reason is None else None
        is_fail = failure is not None
        if failure is not None:
            reason = failure.get('reason') or 'autodispatch-failed'
        if reason is None:
            try:
                text = dispatch.spec_path(sid).read_text()
            except OSError:
                reason = 'spec file unreadable'
            else:
                base = re.search(r'^Base:\s+(\S+)\s*$', text, re.MULTILINE)
                if base and base[1] != head_for(project):
                    reason = f"base {base[1]}: the Executor cuts this worktree by hand"
                if reason is None:
                    block = [b for b in shape.check(text)['block'] if not b.startswith('Plain English:')]
                    if block:
                        reason = 'shape: ' + '; '.join(block)
        if reason is None and wave is not None and charter is not None:
            blocker = dispatch.wave_blocker(events, charter, sid, wave)
            if blocker:
                reason = f'wave-blocked by {blocker}'
        if reason is None and sid in busy:
            reason = 'busy (open escalation or spawn)'
        if reason is None and not (fold.ROOT / 'repos' / project).exists():
            reason = f'no $R/repos/{project} symlink'
        if reason is None:
            reason = dispatch.unaudited(evs)
        since = _since(events, sid, evs, charter, wave) or now
        rows.append({"spec": sid, "charter": charter, "project": project,
                     "since": since.isoformat(timespec="seconds"), "limit_min": limit_min,
                     "status": "blocked" if reason else None, "reason": reason,
                     "_dispatch_failed": is_fail, "_since_dt": since})
    rows.sort(key=lambda r: r["_since_dt"])
    try:
        import loadgate
        held = loadgate.held_items(now=now)
    except Exception:
        held = frozenset()
    slots = max(0, capacity() - _builder_in_flight_count(events))
    for r in rows:
        if r["status"] == "blocked":
            continue
        if held and loadgate.hold_key(r['spec']) in held:
            r['status'], r['reason'] = 'seat-wait', 'resource-wait'
        elif slots > 0:
            r["status"], slots = "dispatchable", slots - 1
        else:
            r["status"] = "seat-wait"
    for r in rows:
        del r["_since_dt"]
    if held:
        for s in specs.values():
            sid = s['id']
            if s.get('state') == 'written' or s.get('state') in dispatch.SETTLED_STATES:
                continue
            if loadgate.hold_key(sid) not in held:
                continue
            evs = s.get('evs') or by_subject.get(sid, [])
            project = next((e.get('project') for e in reversed(evs) if e.get('project')), None) or 'unknown'
            rows.append(dict(spec=sid, charter=dispatch.resolve_charter(events, sid, None), project=project,
                             since=now.isoformat(timespec='seconds'), limit_min=limit_min,
                             status='seat-wait', reason='resource-wait', _dispatch_failed=False))
    return rows


REPLAYABLE_ROLES = ("builder", "grader", "spec-writer", "spec-auditor")
TERMINAL_STATES = ("accepted", "killed", "void", "dropped", "closed-shipped", "closed-unbuilt")


def _grader_trigger(evs):
    """R13a: the newest of (a) a `build-done` (actor builder) `status=DONE`, or
    (b) a `decision regrade=yes` from `thinker`/`operator` with no `rework=yes`.
    Returns `(trigger_event, trigger_name)`, or `(None, None)` when neither
    exists. `trigger_name` for (a) is `rework` when an OLDER `rejected-criterion`
    stands, else `build-done`; for (b) it is `regrade`. A `decision regrade=yes
    rework=yes` is never itself a trigger (SD-R13-1a) — the rework's own next
    `build-done` re-triggers as (a)."""
    T = lambda e: fold.ts(e.get("ts"))
    build_dones = [e for e in evs if e.get("type") == "build-done" and e.get("status") == "DONE"
                   and e.get("actor") == "builder"]
    newest_bd = max(build_dones, key=T) if build_dones else None
    regrades = [e for e in evs if e.get("type") == "decision" and e.get("regrade") == "yes"
                and e.get("actor") in ("thinker", "operator") and e.get("rework") != "yes"]
    newest_rg = max(regrades, key=T) if regrades else None
    if newest_bd is None and newest_rg is None:
        return None, None
    if newest_rg is not None and (newest_bd is None or T(newest_rg) > T(newest_bd)):
        return newest_rg, "regrade"
    rejects = [e for e in evs if e.get("type") == "rejected-criterion"]
    older_reject = any(T(e) < T(newest_bd) for e in rejects)
    return newest_bd, ("rework" if older_reject else "build-done")


def _own_terminal_event(events, spawn):
    if any(e.get("spawn") == spawn and e.get("type") in
           ("verdict", "review", "build-done", "spawn-done", "spawn-failed") for e in events):
        return True
    # Re-read the spawn's own ledger file: a waiter that wrote spawn-done and exited
    # after `events` was read looked dead and was escalated as spawn-redispatch-lapsed
    # (L-builder-0902, L-builder-0914, 2026-10-04).
    try:
        text = ledger_files.read_text(fold.EVENTS, f"{spawn}.jsonl")
    except (OSError, TypeError):
        return False
    return any(f'"{t}"' in text for t in ("spawn-done", "spawn-failed", "build-done", "verdict"))


def dead_spawns(events, now):
    """R13b: `{spawn, subject, role, visible_at, replaced, replacement_of}` for
    every dead, unverdicted, recent, non-terminal-subject, replayable-role spawn
    with no prior handling (replay or escalation) of its own. Dead: a
    `spawn-stale` names it, or its own `spawn-started`/`build-started` carries a
    same-host `waiter_pid` `panes._is_live` reports dead."""
    events = list(events)
    by_subject = {}
    for e in events:
        subj = e.get("subject")
        if subj:
            by_subject.setdefault(subj, []).append(e)
    replaced_spawns = {e.get("replaces") for e in events
                       if e.get("type") == "autodispatched" and e.get("replaces")}
    escalated_spawns = {e.get("dead_spawn") for e in events
                        if e.get("type") == "escalation-blocking" and e.get("dead_spawn")}
    autodispatched_by_spawn = {e.get("spawn"): e for e in events
                               if e.get("type") == "autodispatched" and e.get("spawn")}
    stale_by_spawn = {}
    for e in events:
        if e.get("type") == "spawn-stale" and e.get("spawn"):
            cur = stale_by_spawn.get(e["spawn"])
            if cur is None or fold.ts(e.get("ts")) > fold.ts(cur.get("ts")):
                stale_by_spawn[e["spawn"]] = e
    starts_by_spawn = {}
    for e in events:
        if e.get("type") in ("spawn-started", "build-started") and e.get("spawn"):
            starts_by_spawn.setdefault(e["spawn"], e)

    rows = []
    for spawn in set(stale_by_spawn) | set(starts_by_spawn):
        stale = stale_by_spawn.get(spawn)
        start = starts_by_spawn.get(spawn)
        if stale is not None:
            visible_at, subject = fold.ts(stale.get("ts")), stale.get("subject")
            role = stale.get("role") or ""
        elif start is not None:
            waiter_pid = start.get("waiter_pid")
            if not (waiter_pid and start.get("waiter_host") == socket.gethostname()
                    and not panes._is_live(waiter_pid, {"procStart": start.get("waiter_proc_start")})):
                continue
            visible_at, subject = fold.ts(start.get("ts")), start.get("subject")
            role = start.get("role") or ("builder" if start.get("type") == "build-started" else "")
        else:
            continue
        if not subject:
            continue
        if _own_terminal_event(events, spawn):
            continue
        if (now - visible_at).total_seconds() > 2 * 3600:
            continue
        subj_evs = by_subject.get(subject, [])
        if any(e.get("type") == "shipped" for e in subj_evs):
            continue
        if fold.spec_state(subj_evs, set()) in TERMINAL_STATES:
            continue
        if role not in REPLAYABLE_ROLES:
            continue
        if (role == "builder" and _automatic_hold(subj_evs, events)
                or role == "grader" and held_by_dependency(subj_evs, events)):
            continue
        # A grader is only ever dispatched after a build-done (R13a); a dead
        # `role=grader` spawn on a subject with no build-done of its own is not
        # a real grading step to recover — never a `dead_spawns` row.
        if role == "grader" and not any(e.get("type") == "build-done" for e in subj_evs):
            continue
        if spawn in replaced_spawns or spawn in escalated_spawns:
            continue
        replacement_of = autodispatched_by_spawn.get(spawn, {}).get("replaces")
        rows.append({"spawn": spawn, "subject": subject, "role": role,
                     "visible_at": visible_at.isoformat(timespec="seconds"),
                     "replaced": False, "replacement_of": replacement_of})
    rows.sort(key=lambda r: r["visible_at"])
    return rows


def _strip_replay_packet(text, spawn):
    """R13b/AC9: the dead packet, minus a leading `WRITE PATH: ...` line (when
    present) and the trailing `spawn_id: <spawn>` line with the blank line
    before it — byte-identical otherwise."""
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith("WRITE PATH:"):
        lines = lines[1:]
    out = "".join(lines)
    trailer = f"\n\nspawn_id: {spawn}\n"
    if out.endswith(trailer):
        out = out[: -len(trailer)]
    return out


DOIT_BIN = str(pathlib.Path(__file__).resolve().parent.parent / "doit")


def _real_runner(argv, *, cwd=None, env=None):
    # cron's PATH has no `doit`: a bare "doit" raised FileNotFoundError after the
    # worktree was already added, stranding it (09-28 04:30-05:08Z, 0400/0423/9002).
    if argv and argv[0] == "doit":
        argv = [DOIT_BIN] + list(argv[1:])
    env = dict(os.environ if env is None else env, DOIT_LEDGER_FILE=str(tick.tick_path()))
    r = subprocess.run(argv, capture_output=True, text=True, cwd=cwd, env=env)
    return {"code": r.returncode, "stdout": r.stdout, "stderr": r.stderr}


def add_builder_worktree(repo, worktree, branch, base, *, runner=None, reuse_branch=True):
    """Create a builder checkout, preserving an existing branch on retry."""
    runner = runner or _real_runner
    prefix = ["git", "-C", str(repo)]
    # Remove only this missing checkout's stale registration, never another
    # checkout or a branch. git refuses removal of a directory still present.
    if reuse_branch and not pathlib.Path(worktree).exists():
        listed = runner(prefix + ["worktree", "list", "--porcelain"])
        registered = [line[9:] for line in listed.get("stdout", "").splitlines()
                      if line.startswith("worktree ")]
        if str(pathlib.Path(worktree).resolve()) in registered:
            removed = runner(prefix + ["worktree", "remove", str(worktree)])
            if removed.get("code", 1):
                return removed
    if pathlib.Path(worktree).is_dir():
        return dict(code=0, stdout='', stderr='')
    created = runner(prefix + ["worktree", "add", str(worktree), "-b", branch, base])
    def recorded(result):
        if result.get("code", 1) == 0:
            import resources
            subject = "L-spec-" + branch.removeprefix("l-spec-")
            resources.record("worktree", str(worktree), lane="build", subject=subject)
        return result

    if created.get("code", 1) == 0 or not reuse_branch:
        return recorded(created)
    exists = runner(prefix + ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
    if exists.get("code", 1) == 0:
        return recorded(runner(prefix + ["worktree", "add", str(worktree), branch]))
    return created


def redispatch_dead(events, specs, now, *, runner=None, dry_run=False, handled=frozenset()):
    """Recover dead spawns without classifying routine item eligibility."""
    runner = runner or _real_runner
    out, handled = [], set(handled)
    if not dry_run:
        tick._record_held_skips(events, specs)
    # R13b: replay or escalate every dead spawn this tick has not already
    # handled (replayed or escalated) of its own.
    for row in dead_spawns(events, now):
        spawn, subject, role = row["spawn"], row["subject"], row["role"]
        if subject in handled:
            continue
        handled.add(subject)
        replacement_of, visible_at = row["replacement_of"], fold.ts(row["visible_at"])
        if dry_run:
            out.append(f"would handle dead spawn {spawn} ({role}) on {subject}")
            continue
        base = {"subject": subject}
        deadline = (now + datetime.timedelta(hours=24)).isoformat(timespec="seconds")

        def _escalate(kind):
            dispatch.emit(tick.tick_path(), base, "escalation-blocking", kind=kind, owner="thinker",
                          role=role, dead_spawn=spawn,
                          default=(f"leave {subject} as it stands; an operator decides the next "
                                   f"step for the dead {role} spawn {spawn}"),
                          deadline=deadline,
                          revert=f"no automatic action was taken on {subject} — nothing to revert")
            out.append(f"escalated {subject} dead_spawn={spawn} kind={kind}")

        if replacement_of:
            _escalate("spawn-died-twice")
            continue
        cmd_json_path, packet_path_seat = dispatch.SEAT / f"{spawn}.cmd.json", dispatch.SEAT / f"{spawn}.packet.md"
        if not cmd_json_path.is_file() or not packet_path_seat.is_file():
            _escalate("spawn-died-twice")
            continue
        if (now - visible_at).total_seconds() > 600:
            _escalate("spawn-redispatch-lapsed")
            continue
        charter = dispatch.resolve_charter(events, subject, None)
        subj_evs = [e for e in events if e.get("subject") == subject]
        project = next((e.get("project") for e in reversed(subj_evs) if e.get("project")), None) or "unknown"
        if role == "grader":
            if subject in fold.held_specs(events):
                handled.discard(subject)
                out.append(f"no action: {subject} held")
                continue
            worktree = fold.ROOT / "worktrees" / project / subject.lower()
            if not worktree.is_dir():
                _escalate("spawn-redispatch-failed")
                continue
            r1 = runner(["doit", "packet", "grader", subject, "--worktree", str(worktree)])
            if r1.get("code", 1) != 0:
                _escalate("spawn-redispatch-failed")
                continue
            packet_path = (r1.get("stdout") or "").strip()
            r2 = runner(["doit", "dispatch", "--detach", "grader", subject, "--packet", packet_path,
                         "--cwd", str(worktree), "--charter", charter or "", "--project", project,
                         "--timeout", "30"])
            if r2.get("code", 1) != 0:
                _escalate("spawn-redispatch-failed")
                continue
            try:
                detach = json.loads(r2.get("stdout") or "")
            except ValueError:
                _escalate("spawn-redispatch-failed")
                continue
        else:
            cmd_json = json.loads(cmd_json_path.read_text())
            replay_path = fold.ROOT / "content" / f"replay-{spawn}.md"
            replay_path.write_text(_strip_replay_packet(packet_path_seat.read_text(), spawn))
            argv = ["doit", "dispatch", "--detach", role, subject, "--packet", str(replay_path),
                    "--cwd", cmd_json.get("cwd")]
            if cmd_json.get("path"):
                argv += ["--path", cmd_json["path"]]
            argv += ["--charter", charter or "", "--project", project]
            if role == "grader":  # same 30-min cap as the fresh-dispatch path
                argv += ["--timeout", "30"]
            r = runner(argv)
            if r.get("code", 1) != 0:
                _escalate("spawn-redispatch-failed")
                continue
            try:
                detach = json.loads(r.get("stdout") or "")
            except ValueError:
                _escalate("spawn-redispatch-failed")
                continue
        dispatch.emit(tick.tick_path(), base, "autodispatched", spawn=str(detach.get("detached")),
                      role=role, trigger="redispatch", replaces=spawn, since=row["visible_at"])
        out.append(f"replayed {spawn} as {detach.get('detached')}")
    return out


def run(events, specs, now, *, runner=None, dry_run=False):
    """Compatibility entry point; tick owns routine Step launches."""
    return tick.run_steps(events, specs, now, dispatcher=runner or _real_runner,
                          dry_run=dry_run)
