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
import json, pathlib, subprocess, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dispatch, fold, shape, tick  # noqa: E402


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
    if shp["block"]:
        return "shape: " + "; ".join(shp["block"]), False
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
    return out
