#!/usr/bin/env python3
"""autodispatch — R1 (L-charter-0042): the tick dispatches.

Classifies every `written` spec as `dispatchable` / `seat-wait` / `blocked`
(`candidates`), and cuts a worktree + dispatches `builder` for each
`dispatchable` row, oldest `since` first, up to seat capacity (`run`) — the
SAME recipe the Executor's `agents/executor.md` "Cutting a worktree" ran by
hand (`git worktree add`, `doit packet builder`, `doit dispatch --detach
builder`), issued through one injectable `runner` so no new dispatch code
path exists. `tick._record()` calls `run()` once per tick, last, right before
`todo = lane(...)`. `look.py`'s `_check_dispatchable_stale` calls
`candidates()` to alarm on anything left dispatchable (or standing-failed)
past 30 minutes.
"""
import datetime, json, pathlib, socket, subprocess, sys
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


def _standing_autodispatch_failed(evs):
    """SD1 condition 4: the newest `autodispatch-failed`, when it is NEWER
    than the newest of this subject's own `spec-written`/`decision`/
    `unblocked` — else `None` (a later `decision`/`unblocked` clears it,
    AC4)."""
    fails = [e for e in evs if e.get("type") == "autodispatch-failed"]
    if not fails:
        return None
    newest_fail = max(fails, key=lambda e: str(e.get("ts", "")))
    fail_ts = fold.ts(newest_fail.get("ts"))
    clear_ts = _newest_marker_ts(evs)
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


def _blocking_reason(sid, evs, events, project, charter, wave, busy):
    """The five-condition `dispatchable` gate (SD1), checked in a fixed
    priority order — each acceptance criterion exercises exactly one
    condition at a time, so the order only matters when more than one is
    true at once. Returns `(reason_text_or_None, is_standing_autodispatch_failed)`."""
    af = _standing_autodispatch_failed(evs)
    if af is not None:
        return (af.get("reason") or "autodispatch-failed"), True
    try:
        text = dispatch.spec_path(sid).read_text()
    except OSError:
        return "spec file unreadable", False
    shp = shape.check(text)
    # SD9 (L-spec-0478): a missing "In plain English:" line does not make an
    # already-written spec unbuildable — the pre-build gate's one exception.
    # `spec_shape` itself stays caller-unaware and reports the finding always;
    # only this caller strips it before deciding blocked vs dispatchable.
    block = [b for b in shp["block"] if not b.startswith("Plain English:")]
    if block:
        return "shape: " + "; ".join(block), False
    if wave is not None and charter is not None:
        blocker = dispatch.wave_blocker(events, charter, sid, wave)
        if blocker:
            return f"wave-blocked by {blocker}", False
    if sid in busy:
        return "busy (open escalation or spawn)", False
    if not (fold.ROOT / "repos" / project).exists():
        return f"no $R/repos/{project} symlink", False
    return None, False


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


def candidates(events, specs, now):
    """One row per `written` spec: `{spec, charter, project, since, status,
    reason}` — `status` `dispatchable`/`seat-wait`/`blocked`, oldest `since`
    first (SD1/SD2)."""
    events = list(events)
    by_subject = {}
    for e in events:
        sid = e.get("subject")
        if sid:
            by_subject.setdefault(sid, []).append(e)
    busy = tick.in_flight(events)
    written = [s for s in specs.values() if s.get("state") == "written"]
    rows = []
    for s in written:
        sid = s["id"]
        evs = s.get("evs") or by_subject.get(sid, [])
        charter = dispatch.resolve_charter(events, sid, None)
        own_footprint = next((e.get("footprint") for e in reversed(evs)
                              if e.get("type") == "spec-written" and e.get("footprint")), None)
        project = next((e.get("project") for e in reversed(evs) if e.get("project")), None) or "unknown"
        wave = dispatch.spec_wave(charter, own_footprint) if (charter and own_footprint) else None
        reason, is_fail = _blocking_reason(sid, evs, events, project, charter, wave, busy)
        since = _since(events, sid, evs, charter, wave) or now
        rows.append({"spec": sid, "charter": charter, "project": project,
                     "since": since.isoformat(timespec="seconds"),
                     "status": "blocked" if reason else None, "reason": reason,
                     "_dispatch_failed": is_fail, "_since_dt": since})
    rows.sort(key=lambda r: r["_since_dt"])
    slots = max(0, capacity() - _builder_in_flight_count(events))
    for r in rows:
        if r["status"] == "blocked":
            continue
        if slots > 0:
            r["status"], slots = "dispatchable", slots - 1
        else:
            r["status"] = "seat-wait"
    for r in rows:
        del r["_since_dt"]
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


def grader_candidates(events, specs, now):
    """R13a: one row `{spec, charter, project, trigger, since}` per spec whose
    newest trigger (see `_grader_trigger`) has no grader dispatch/refusal of
    its own since, is not held (`fold.held_specs`), not killed, not shipped."""
    events = list(events)
    by_subject = {}
    for e in events:
        sid = e.get("subject")
        if sid:
            by_subject.setdefault(sid, []).append(e)
    held = fold.held_specs(events)
    rows = []
    for sid, evs in by_subject.items():
        if not str(sid).startswith("L-spec-"):
            continue
        if any(e.get("type") == "spec-killed" for e in evs):
            continue
        if any(e.get("type") == "shipped" for e in evs):
            continue
        if sid in held:
            continue
        trig_event, trigger = _grader_trigger(evs)
        if trig_event is None:
            continue
        t_ts = fold.ts(trig_event.get("ts"))
        suppressed = any(
            fold.ts(e.get("ts")) > t_ts and e.get("role") == "grader" and e.get("type") in (
                "spawn-started", "spawn-failed", "autodispatched", "autodispatch-failed")
            for e in evs)
        if suppressed:
            continue
        charter = dispatch.resolve_charter(events, sid, None)
        project = next((e.get("project") for e in reversed(evs) if e.get("project")), None) or "unknown"
        rows.append({"spec": sid, "charter": charter, "project": project,
                     "trigger": trigger, "since": t_ts.isoformat(timespec="seconds")})
    rows.sort(key=lambda r: r["since"])
    return rows


def _own_terminal_event(events, spawn):
    return any(e.get("spawn") == spawn and e.get("type") in
               ("verdict", "review", "build-done", "spawn-done", "spawn-failed") for e in events)


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


def _real_runner(argv):
    # cron's PATH has no `doit`: a bare "doit" raised FileNotFoundError after the
    # worktree was already added, stranding it (09-28 04:30-05:08Z, 0400/0423/9002).
    if argv and argv[0] == "doit":
        argv = [DOIT_BIN] + list(argv[1:])
    r = subprocess.run(argv, capture_output=True, text=True)
    return {"code": r.returncode, "stdout": r.stdout, "stderr": r.stderr}


def run(events, specs, now, *, runner=None, dry_run=False):
    """One line per action. `dry_run=True` never calls `runner` and appends
    no `autodispatched`/`autodispatch-failed` event (a `spawn-stale` may
    still land, from `candidates()`'s own `tick.spawn_in_flight` call —
    independent of `dry_run`, AC14)."""
    events = list(events)
    runner = runner or _real_runner
    rows = candidates(events, specs, now)
    out = []
    for row in rows:
        if row["status"] != "dispatchable":
            continue
        spec, charter, project, since = row["spec"], row["charter"], row["project"], row["since"]
        if dry_run:
            out.append(f"would dispatch {spec} (charter={charter}, project={project})")
            continue
        repo = fold.ROOT / "repos" / project
        base = {"subject": spec}

        def _fail(reason):
            dispatch.emit(tick.tick_path(), base, "autodispatch-failed", reason=reason)
            out.append(f"failed {spec}: {reason}")

        r1 = runner(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"])
        if r1.get("code", 1) != 0:
            _fail((r1.get("stderr") or r1.get("stdout") or "symbolic-ref failed").strip())
            continue
        main_branch = (r1.get("stdout") or "").strip()
        worktree = fold.ROOT / "worktrees" / project / spec.lower()
        if worktree.is_dir():
            # A worktree left by an earlier failed attempt is reused only while it
            # holds no commits above main; otherwise it is real work and we stop.
            r2 = runner(["git", "-C", str(worktree), "rev-list", "--count", f"{main_branch}..HEAD"])
            if r2.get("code", 1) != 0 or (r2.get("stdout") or "").strip() != "0":
                _fail(f"worktree {worktree} exists with commits above {main_branch}; not reusing")
                continue
            r2 = runner(["git", "-C", str(worktree), "merge", "--ff-only", main_branch])
        else:
            r2 = runner(["git", "-C", str(repo), "worktree", "add", str(worktree), "-b", spec.lower(), main_branch])
        if r2.get("code", 1) != 0:
            _fail((r2.get("stderr") or r2.get("stdout") or "worktree add failed").strip())
            continue
        r3 = runner(["doit", "packet", "builder", spec, "--worktree", str(worktree), "--repo", str(repo)])
        if r3.get("code", 1) != 0:
            _fail((r3.get("stderr") or r3.get("stdout") or "doit packet builder failed").strip())
            continue
        packet_path = (r3.get("stdout") or "").strip()
        r4 = runner(["doit", "dispatch", "--detach", "builder", spec, "--packet", packet_path,
                     "--cwd", str(worktree), "--charter", charter or "", "--project", project])
        if r4.get("code", 1) != 0:
            _fail((r4.get("stderr") or r4.get("stdout") or "doit dispatch --detach failed").strip())
            continue
        try:
            detach = json.loads(r4.get("stdout") or "")
        except ValueError:
            _fail(f"doit dispatch --detach printed non-JSON: {(r4.get('stdout') or '')!r}")
            continue
        dispatch.emit(tick.tick_path(), base, "autodispatched", spawn=str(detach.get("detached")),
                      since=since, worktree=str(worktree), log=detach.get("log"))
        out.append(f"dispatched {spec} spawn={detach.get('detached')} worktree={worktree}")

    # R13a: dispatch the grader for every candidate this tick has not already
    # dispatched/refused a grader for.
    for row in grader_candidates(events, specs, now):
        spec, charter, project = row["spec"], row["charter"], row["project"]
        trigger, since = row["trigger"], row["since"]
        if dry_run:
            out.append(f"would dispatch grader {spec} (trigger={trigger})")
            continue
        base = {"subject": spec}

        def _gfail(reason):
            dispatch.emit(tick.tick_path(), base, "autodispatch-failed", role="grader", reason=reason)
            out.append(f"failed grader {spec}: {reason}")

        worktree = fold.ROOT / "worktrees" / project / spec.lower()
        if not worktree.is_dir():
            _gfail(f"worktree {worktree} missing — never created for a grader dispatch")
            continue
        r1 = runner(["doit", "packet", "grader", spec, "--worktree", str(worktree)])
        if r1.get("code", 1) != 0:
            _gfail((r1.get("stderr") or r1.get("stdout") or "doit packet grader failed").strip())
            continue
        packet_path = (r1.get("stdout") or "").strip()
        # --timeout 30: the grader cap counts relay-queue wait, which alone ate the
        # 15-min default on 6+ graders on 2026-09-29/30 (Thinker standing rule).
        r2 = runner(["doit", "dispatch", "--detach", "grader", spec, "--packet", packet_path,
                     "--cwd", str(worktree), "--charter", charter or "", "--project", project,
                     "--timeout", "30"])
        if r2.get("code", 1) != 0:
            _gfail((r2.get("stderr") or r2.get("stdout") or "doit dispatch --detach failed").strip())
            continue
        try:
            detach = json.loads(r2.get("stdout") or "")
        except ValueError:
            _gfail(f"doit dispatch --detach printed non-JSON: {(r2.get('stdout') or '')!r}")
            continue
        dispatch.emit(tick.tick_path(), base, "autodispatched", spawn=str(detach.get("detached")),
                      role="grader", trigger=trigger, since=since)
        out.append(f"dispatched grader {spec} spawn={detach.get('detached')} trigger={trigger}")

    # R13b: replay or escalate every dead spawn this tick has not already
    # handled (replayed or escalated) of its own.
    for row in dead_spawns(events, now):
        spawn, subject, role = row["spawn"], row["subject"], row["role"]
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
