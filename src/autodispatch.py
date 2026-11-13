#!/usr/bin/env python3
"""Dispatch transport, health recovery, and legacy read-only eligibility views.

Routine launches delegate to tick's Step consumer. candidates remains a legacy
read-only filesystem preparation diagnostic.
"""
import ledger_files
import datetime, json, os, pathlib, re, socket, subprocess, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dispatch, fold, panes, shape, tick  # noqa: E402

BASE_FAILURE_BUDGET = 3


def _branch_failure_streak(evs):
    """Count ancestry refusals since DONE, conservatively retaining unknown times."""
    def when(e):
        try:
            value = datetime.datetime.fromisoformat(str(e.get('ts', '')).replace('Z', '+00:00'))
            return value if value.tzinfo is not None else None
        except ValueError:
            return None

    done = max((when(e) for e in evs if e.get('type') == 'build-done'
                and e.get('status') == 'DONE' and when(e) is not None), default=None)
    failures = [e for e in evs if e.get('type') == 'spawn-failed'
                and e.get('reason') == 'branch-not-descendant'
                and (done is None or when(e) is None or when(e) >= done)]
    if not failures:
        return []
    unknown = any(when(e) is None for e in failures)
    newest = max((when(e) for e in failures if when(e) is not None), default=None)
    if not unknown and any(e.get('type') == 'decision' and e.get('rework') == 'yes'
                           and when(e) is not None and when(e) > newest for e in evs):
        return []
    # The final row supplies a stable dedup key even when its timestamp is unreadable.
    return sorted(failures, key=lambda e: (when(e) is None,
                  when(e) or datetime.datetime.max.replace(tzinfo=datetime.timezone.utc),
                  str(e.get('ts', ''))))


def _record_branch_failure_holds(events, now):
    """Existing write passes append one exhaustion receipt per newest failure."""
    by_subject = {}
    for event in events:
        by_subject.setdefault(event.get('subject'), []).append(event)
    exhausted = []
    for subject, evs in by_subject.items():
        if subject and str(subject).startswith('L-spec-'):
            failures = _branch_failure_streak(evs)
            if len(failures) >= BASE_FAILURE_BUDGET:
                exhausted.append((subject, failures))
    if not exhausted:
        return
    path = tick.tick_path()
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    persisted = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            persisted.append(row)
    for subject, failures in exhausted:
        failure_ts = failures[-1].get('ts')
        if any(e.get('type') == 'escalation-blocking'
               and e.get('kind') == 'branch-not-descendant-exhausted'
               and e.get('subject') == subject and e.get('failure_ts') == failure_ts
               for e in list(events) + persisted):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        refused = tick._committed(dispatch.emit(path, {'subject': subject}, 'escalation-blocking', strict=True,
            kind='branch-not-descendant-exhausted', owner='thinker', role='builder',
            failure_ts=failure_ts, count=len(failures),
            default='leave builder stopped pending decision rework=yes or a completed DONE build',
            deadline=(now + datetime.timedelta(hours=24)).isoformat(),
            revert='no branch was changed; nothing to revert'))
        if refused:
            raise ValueError('branch failure exhaustion record refused: ' + str(refused))


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


def _unresolved_holds(holds, spec_evs, all_evs, *, shipped=None):
    """Validate every unreleased hold; invalid history takes precedence."""
    shipped = shipped if shipped is not None else {
        e.get('subject') for e in all_evs if e.get('type') == 'shipped'}
    unresolved = []
    for hold in sorted(holds, key=_hold_order):
        if '_dependency_error' not in hold and _hold_released(hold, spec_evs):
            continue
        hold = dict(hold)
        dependency = hold.get('waits_for')
        if '_dependency_error' not in hold:
            if not fold.valid_dependency(dependency):
                hold['_dependency_error'] = 'held: invalid waits_for; unresolvable dependency-hold'
            elif dependency == hold.get('subject'):
                hold['_dependency_error'] = ('held: unresolvable dependency-hold '
                                             f'(waits_for is self: {dependency})')
        if '_dependency_error' in hold or dependency not in shipped:
            unresolved.append(hold)
    return sorted(unresolved, key=lambda e: '_dependency_error' not in e)


def _open_dependency_holds(spec_evs, all_evs, *, shipped=None):
    """One projection for ledger holds and the current text declaration epoch."""
    import dependency_holds
    own = list(spec_evs)
    holds = [e for e in own if e.get('type') == 'dependency-hold']
    declaration = dependency_holds._text_declaration(own)
    if declaration is not None:
        holds.append(declaration)
    return _unresolved_holds(holds, own, all_evs, shipped=shipped)


def open_dependencies(spec_evs, all_evs, *, shipped=None) -> list[str]:
    """Distinct validated prerequisites still awaiting shipment or release."""
    holds = _open_dependency_holds(spec_evs, all_evs, shipped=shipped)
    return list(dict.fromkeys(h['waits_for'] for h in holds if '_dependency_error' not in h))


def _dependency_reason(holds, all_evs):
    if not holds:
        return None
    hold = holds[0]
    source = f" [{hold['_src']}]" if hold.get('_src') else ''
    if '_dependency_error' in hold:
        return hold['_dependency_error'] + source
    dependency = hold['waits_for']
    killed = any(e.get('type') == 'spec-killed' and e.get('subject') == dependency for e in all_evs)
    return f"held: waits for {'killed ' if killed else ''}{dependency} ({hold.get('why', '')})" + source


def held_by_dependency(spec_evs, all_evs, *, shipped=None) -> str | None:
    """Fail closed until every ledger and declared prerequisite is resolved."""
    all_evs = list(all_evs)
    holds = _open_dependency_holds(spec_evs, all_evs, shipped=shipped)
    return _dependency_reason(holds, all_evs)


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
    failures = _branch_failure_streak(evs)
    if len(failures) >= BASE_FAILURE_BUDGET:
        return (f"held: branch-not-descendant x{len(failures)}; "
                "awaiting newer decision rework=yes or build-done DONE")
    return None


def _standing_autodispatch_failed(evs):
    """SD1 condition 4: the newest `autodispatch-failed`, when it is NEWER
    than the newest of this subject's own `spec-written`/`decision`/
    `unblocked`/`dependency-hold` — else `None` (a later marker clears it,
    AC4)."""
    # Routine failures are owned by nextstep's bounded attempt predicate. A
    # second standing transport veto would strand a due builder/reviewer retry.
    import nextstep
    fails = [e for e in evs if e.get("type") == "autodispatch-failed"
             and not nextstep._base_receipt(e)
             and (e.get('role') or fold.actor_of(str(e.get('spawn') or '')))
             not in {'builder', 'grader', 'reviewer'}]
    if not fails:
        return None
    newest_fail = max(fails, key=lambda e: str(e.get("ts", "")))
    fail_ts = fold.ts(newest_fail.get("ts"))
    clear_ts = max((fold.ts(e.get("ts")) for e in evs
                    if e.get("type") in ("spec-written", "decision", "unblocked", "dependency-hold")
                    or __import__('empty_grade').clears_failure(newest_fail, e)),
                   default=None)
    if clear_ts is not None and clear_ts > fail_ts:
        return None
    return newest_fail


def _sibling_blocks(evs, charter, wave, waves):
    """`dispatch.wave_blocker`'s test for one sibling, from that sibling's events so far."""
    raw = next((e.get("charter") for e in reversed(evs) if e.get("charter")), None)
    if raw is None or pathlib.Path(raw).stem != charter:
        return False
    written = next((e for e in reversed(evs) if e.get("type") == "spec-written" and e.get("footprint")), None)
    if not written:
        return False
    key = tuple(sorted(written["footprint"]))
    if key not in waves:
        waves[key] = dispatch.spec_wave(charter, written["footprint"])
    sib_wave = waves[key]
    if sib_wave is None or sib_wave >= wave:
        return False
    return fold.spec_state(evs, set()) not in dispatch.SETTLED_STATES


def _since(events, sid, evs, charter, wave, memo=None):
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
    # change at an event of one of those subjects. Thinker 2026-11-05: at such an event only
    # that sibling's own blocking status can change, so keep the set of blocking siblings
    # and re-test just that one. Same answer as re-running `wave_blocker` on every prefix,
    # which took `fold.py board` 30 minutes and `look` 2 hours with 61 written specs.
    if memo is None:
        memo = {}
    if charter not in memo:
        members = set()
        for e in events:
            subj = e.get("subject")
            if (subj and str(subj).startswith("L-spec-") and e.get("charter")
                    and pathlib.Path(e["charter"]).stem == charter):
                members.add(subj)
        memo[charter] = (members, sorted((e for e in events if e.get("subject") in members),
                                         key=lambda e: str(e.get("ts", ""))))
    members, ordered = memo[charter]
    was_blocked, settle_ts, seen, blocking, waves = False, None, {}, set(), {}
    for e in ordered:
        subj = e.get("subject")
        if subj == sid:
            continue
        seen.setdefault(subj, []).append(e)
        if _sibling_blocks(seen[subj], charter, wave, waves):
            blocking.add(subj)
        else:
            blocking.discard(subj)
        now_blocked = bool(blocking)
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
    since_memo = {}
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
        since = _since(events, sid, evs, charter, wave, since_memo) or now
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
    try:
        waiters = loadgate.test_waiters(now=now)
    except Exception:
        waiters = []
    for waiter in waiters:
        rows.append(dict(spec=waiter.get('spec') or 'unknown', role=waiter['role'],
                         **{'class': waiter['class']}, charter=None, project='unknown',
                         since=waiter['waiting_since'], limit_min=20,
                         status='slot-wait', reason='test-slot', _dispatch_failed=False))
    return rows


REPLAYABLE_ROLES = ("builder", "grader", "spec-writer", "spec-auditor")
TERMINAL_STATES = ("accepted", "killed", "void", "dropped", "closed-shipped", "closed-unbuilt")


def base_only_attempt(evs, *, main_sha=None):
    """Current structured block plus same-attempt admission proof, never a Git/ledger read."""
    import grader_packet
    from fold_stalls import newest_builder_outcome
    build = newest_builder_outcome(evs)
    if not build or build.get('status') != 'BLOCKED' or not build.get('spawn'):
        return None
    start = next(i for i, e in enumerate(evs) if e is build)
    later = evs[start + 1:]
    block = next((e for e in reversed(later) if e.get('type') == 'build-blocked'), None)
    if not (block and block.get('actor') == 'builder' and block.get('spawn') == build['spawn']
            and block.get('subject') == build.get('subject') and block.get('base_only') is True
            and block.get('reason') != 'handin-check:secret-scan'):
        return None
    ready, cited = block.get('ready_sha'), block.get('cited')
    if not (isinstance(ready, str) and re.fullmatch(r'[0-9a-f]{7,40}', ready)
            and grader_packet._same_sha(ready, build.get('ready_sha'))
            and isinstance(cited, list) and 0 < len(cited) <= 40
            and all(isinstance(c, str) and 0 < len(c) <= 200 for c in cited)):
        return None
    if any(e.get('type') in {'build-started', 'blocked', 'unblocked'} for e in later):
        return None
    shape = next((e for e in reversed(later) if e.get('criterion') == 'COMMIT-SHAPE'
                  and e.get('type') in {'criterion-cleared', 'rejected-criterion'}), None)
    if (not shape or shape.get('type') != 'criterion-cleared' or shape.get('actor') != 'builder'
            or shape.get('spawn') != build['spawn'] or 'COMMIT-SHAPE' in fold.standing_rejects(evs)):
        return None
    # A later receipt, including an unstructured block, supersedes this claim.
    if any(e.get('type') == 'build-blocked' and e is not block for e in
           evs[next(i for i, e in enumerate(evs) if e is block) + 1:]):
        return None
    return block, grader_packet.evidence_state(evs, build['subject'], main_sha=main_sha)


def base_only_proven(attempt):
    if not attempt:
        return False
    state = attempt[1]
    count = (state.bundle_event or {}).get('n_new')
    return state.state == 'ready' and type(count) is int and count == 0


def _grader_trigger(evs, *, main_sha=None):
    """R13a: the newest of (a) a current `build-done` (actor builder) `status=DONE`, or
    (b) a `decision regrade=yes` from `thinker`/`operator` with no `rework=yes`.
    Returns `(trigger_event, trigger_name)`, or `(None, None)` when neither
    exists. `trigger_name` for (a) is `rework` when an OLDER `rejected-criterion`
    stands, else `build-done`; for (b) it is `regrade`. A `decision regrade=yes
    rework=yes` is never itself a trigger (SD-R13-1a) — the rework's own next
    `build-done` re-triggers as (a)."""
    T = lambda e: fold.ts(e.get("ts"))
    from fold_stalls import newest_builder_outcome
    newest_bd = newest_builder_outcome(evs)
    proven = base_only_proven(base_only_attempt(evs, main_sha=main_sha))
    if newest_bd is not None and newest_bd.get("status") != "DONE" and not proven:
        newest_bd = None
    regrades = [e for e in evs if e.get("type") == "decision" and e.get("regrade") == "yes"
                and e.get("actor") in ("thinker", "operator") and e.get("rework") != "yes"]
    newest_rg = max(regrades, key=lambda e: (T(e), next(i for i, row in enumerate(evs) if row is e))) if regrades else None
    if newest_bd is None and newest_rg is None:
        return None, None
    if newest_rg is not None and (newest_bd is None or T(newest_rg) > T(newest_bd)
            or proven and T(newest_rg) == T(newest_bd)
            and next(i for i, e in enumerate(evs) if e is newest_rg)
                > next(i for i, e in enumerate(evs) if e is newest_bd)):
        return newest_rg, "regrade"
    if proven:
        return newest_bd, "base-only-proven"
    rejects = [e for e in evs if e.get("type") == "rejected-criterion"]
    older_reject = any(T(e) < T(newest_bd) for e in rejects)
    return newest_bd, ("rework" if older_reject else "build-done")


OWN_TERMINAL_TYPES = ("verdict", "review", "build-done", "spawn-done", "spawn-failed")


def _ended_spawns(events):
    return {e.get("spawn") for e in events if e.get("type") in OWN_TERMINAL_TYPES}


def _own_terminal_event(events, spawn, ended=None):
    """`ended` is `_ended_spawns(events)`, passed by a caller asking about many
    spawns so the events are scanned once rather than once per spawn."""
    if spawn in (_ended_spawns(events) if ended is None else ended):
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

    ended = _ended_spawns(events)
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
        if _own_terminal_event(events, spawn, ended):
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
        _record_branch_failure_holds(events, now)
        tick._record_held_skips(events, specs)
    # R13b: replay or escalate every dead spawn this tick has not already
    # handled (replayed or escalated) of its own.
    for row in dead_spawns(events, now):
        spawn, subject, role = row["spawn"], row["subject"], row["role"]
        if subject in handled:
            continue
        if (role in {'builder', 'grader', 'reviewer'} and subject in specs
                and any(e.get('subject') == subject and e.get('type') == 'spec-written' for e in events)):
            # These roles use the sole tick Step launcher, including when the
            # trigger is consumed or exhausted. Replay cannot mint another budget.
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
            if role == "builder":
                try:
                    import packet
                    repo = fold.ROOT / "repos" / project
                    pin = packet._effective_base(repo, project, dispatch.spec_path(subject), subject)
                    prepared = runner(["doit", "packet", "builder", subject,
                        "--repo", str(repo), "--worktree", cmd_json.get("cwd"), "--base-sha", pin])
                    if prepared.get("code", 1) or not prepared.get("stdout", "").strip():
                        raise ValueError("builder replay packet failed")
                    replay_path = pathlib.Path(prepared["stdout"].strip())
                except (ValueError, OSError, subprocess.SubprocessError):
                    _escalate("spawn-redispatch-failed")
                    continue
            else:
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
    events = events if isinstance(events, list) else list(events)
    if not dry_run:
        _record_branch_failure_holds(events, now)
    return tick.run_steps(events, specs, now, dispatcher=runner or _real_runner,
                          dry_run=dry_run)
