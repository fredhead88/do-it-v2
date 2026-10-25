#!/usr/bin/env python3
"""sweep_owed — the driver that batches due owed checks into `owed-sweeper`
dispatches, and escalates the ones that expired instead (L-charter-0038 R1/R2).

  doit sweep-owed [--dry-run]

Every run, in this order:

1. Append one `escalation-blocking kind=owed-expired` per spec for every
   criterion that newly reached `owed.checks()`'s `"expired"` status (SD3) —
   deduped against every criterion ANY prior `escalation-blocking
   kind=owed-expired` on that spec already named, resolved or not. This step
   always runs, whether or not step 2 below goes on to skip batching.
1b. Escalate new failed checks, reopen unanswered fixes after 24 hours, and
    recheck shipped correctives, via `owed_failures.escalate_failures`.
2. If one `owed-sweeper` spawn is already open (a `spawn-started` with no
   terminal event, younger than twice the role's own cap — 90 minutes at
   `dispatch.ROLES["owed-sweeper"]`'s 45), stop: no manifest, no dispatch.
3. Otherwise take every `due` check across every project, oldest-`due_at`
   first, EXCLUDING (a) a check carrying an `owed-failed`/`owed-unobservable`
   event in the last 12h (retry next run) and (b) a check whose latest
   `owed-unobservable` capability is not `elapsed` (SD2 — never re-batched,
   regardless of age).
4. Resolve all due rows, quarantine unresolved rows, and choose a project
   never batched or least recently started. Batch at most 8 of its checks into one
   `content/L-sweep-NNNN.md` manifest, and dispatch one detached
   `owed-sweeper` against it, via `doit packet` then `doit dispatch`.

`--dry-run` computes and prints every step above but appends and dispatches
nothing.

This module owns its own ledger file, `LEDGER_NAME = "L-sweep-local.jsonl"`
under `fold.EVENTS` — read and appended to directly, never through
`os.environ.get("DOIT_LEDGER_FILE", ...)` — the same convention `look.py`'s
`LEDGER_NAME`, `intake.py`'s `L-intake-local.jsonl`, and `launch.py`'s
`L-launch-local.jsonl` already use. This makes the fold's filename-derived
actor (D90: `"-".join(stem.split("-")[1:-1])`) resolve to `sweep` under any
caller's environment, including a bare cron invocation that never set
`DOIT_LEDGER_FILE` at all.
"""
import argparse, hashlib, json, os, pathlib, re, shutil, subprocess, sys, time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import provision, verifier
import build_runner, dispatch, fold, grading_env, owed, owed_failures, packet, scratch, prod_runner, prod_browser, owed_routes, sweep_provision  # noqa: E402

LEDGER_NAME = "L-sweep-local.jsonl"
MAX_BATCH = 8
READMIT_CAP = 16
RETRY_WINDOW = timedelta(hours=12)

# SD3, verbatim.
EXPIRED_DEFAULT = ("the check stays expired and listed in the daily digest; an owed-ac re-date, "
                   "an owed-waived with a reason, or a corrective puts it back")
EXPIRED_REVERT = "an owed-met (waive) or an Executor re-date puts a check back"


def ledger_path():
    """The module's own hardcoded ledger — resolved fresh on every call against
    the CURRENT `fold.EVENTS`, never cached at import (the same convention
    `tick.tick_path()` uses)."""
    return fold.EVENTS / LEDGER_NAME


def doit_bin():
    return pathlib.Path(__file__).resolve().parent.parent / "doit"


class Runner:
    """Real subprocess by default; a test swaps the whole boundary (AC3) so the
    exact argv sequences this driver hands `doit packet`/`doit dispatch` are
    provable without a real seat spawn."""
    def run(self, argv):
        return subprocess.run(argv, capture_output=True, text=True)


# ────────────────── L-charter-0042 R6: a decision answers a spec's owed-expired ──

def _answered_max_ts(evs):
    """`None` if no `escalation-blocking kind=owed-expired` event on this spec's
    own `evs` is INDIVIDUALLY answered by a later same-spec `decision`/
    `unblocked` (subject match to the spec, no `ref=` required — SD13 is
    spec-level); otherwise the MAXIMUM `ts` among the `decision`/`unblocked`
    events that answer at least one such escalation. Judged per-escalation,
    never against "whichever escalation is newest" (SD13): a spec's newest
    owed-expired escalation may itself be unanswered while an OLDER one on the
    same spec is individually answered, and that older answer still counts
    (AC12) — the naive "newest escalation's own answer" reading would drop the
    spec's answer entirely the instant a fresh, still-unanswered re-escalation
    lands. Strict `>` throughout (Assumptions §3), same as `fold.answered`."""
    spec = next((e.get("subject") for e in evs if e.get("subject")), None)
    escs = [e for e in evs if e.get("type") == "escalation-blocking" and e.get("kind") == "owed-expired"]
    if not escs:
        return None
    answerers = [e for e in evs if e.get("type") in ("decision", "unblocked") and e.get("subject") == spec]
    if not answerers:
        return None
    best = None
    for esc in escs:
        et = fold.ts(esc.get("ts"))
        qualifying = [fold.ts(a.get("ts")) for a in answerers if fold.ts(a.get("ts")) > et]
        if qualifying:
            m = max(qualifying)
            if best is None or m > best:
                best = m
    return best


def answered_specs(specs):
    """The ids of every spec carrying at least one INDIVIDUALLY-answered
    `escalation-blocking kind=owed-expired` (`fold.fold()`'s own `{sid: {"evs":
    [...], ...}}` dict) — the spec need not be answered by its own NEWEST such
    escalation; any one qualifying is enough (`_answered_max_ts` above)."""
    return {sid for sid, s in specs.items() if _answered_max_ts(s["evs"]) is not None}


def _governing_owed_ac_ts(evs, criterion):
    """The governing `owed-ac` event's own `ts` for one criterion — the same
    last-write-by-position rule `owed.checks()`'s own `last_ac` uses, re-derived
    directly from `evs` since `owed.checks()`'s row carries no such field
    (Boundaries: `src/owed.py` is out of this footprint)."""
    last = None
    for e in evs:
        if e.get("type") == "owed-ac" and e.get("criterion") == criterion:
            last = e
    return fold.ts(last.get("ts")) if last else None


def _excluded_by_answer(specs, answered, spec_id, criterion):
    """SD13: for a spec in `answered_specs`, a check is excluded unless its own
    governing `owed-ac` `ts` strictly POSTDATES the spec's max answering
    instant — a later, still-unanswered re-date "puts that one check back"
    (AC9), while every already-settled criterion stays excluded across
    repeated sweep runs (AC12) because the maximum is never re-read off
    whichever escalation happens to be newest at read time."""
    if spec_id not in answered:
        return False
    max_ts = _answered_max_ts(specs[spec_id]["evs"])
    governing_ts = _governing_owed_ac_ts(specs[spec_id]["evs"], criterion)
    return not (governing_ts is not None and max_ts is not None and governing_ts > max_ts)


# ─────────────────────────── step 1: expiry escalations (SD3) ───────────────

def _already_escalated(evs):
    """Every criterion ANY `escalation-blocking kind=owed-expired` on this
    spec's own event stream has ever named — resolved or not (SD3's own dedupe
    rule: "not re-appended while any ... lists the same criteria")."""
    out = set()
    for e in evs:
        if e.get("type") == "escalation-blocking" and e.get("kind") == "owed-expired":
            try:
                out.update(json.loads(e.get("criteria") or "[]"))
            except (ValueError, TypeError):
                pass
    return out


def sweep_expired(specs, now, *, dry_run=False):
    """SD3: one `escalation-blocking kind=owed-expired` per spec, naming only
    the criteria newly expired this run — never re-listing one a prior
    escalation on the same spec already named. L-charter-0042 R6/SD13: a
    criterion whose spec is in `answered_specs` and whose own governing
    `owed-ac` does not strictly postdate the spec's max answering instant is
    excluded here too — a decision has already answered this spec's
    owed-expired state, and re-escalating an un-re-dated criterion under it
    would re-open what the decision settled. Returns the lines printed; writes
    nothing when `dry_run`."""
    lines = []
    answered = answered_specs(specs)
    for sid, s in specs.items():
        rows = [r for r in owed.checks(s["evs"], now) if r["status"] == "expired"]
        if not rows:
            continue
        already = _already_escalated(s["evs"])
        fresh = sorted({r["criterion"] for r in rows if r["criterion"] not in already
                        and not _excluded_by_answer(specs, answered, sid, r["criterion"])
                        and not _no_runner(r) and not _attempt_in_flight(r, s["evs"], now)})
        if not fresh:
            continue
        evidence = [next((r.get("evidence") for r in rows if r["criterion"] == c), None) or ""
                    for c in fresh]
        if dry_run:
            lines.append(f"sweep-owed: DRY RUN would escalate {sid} kind=owed-expired "
                         f"criteria={fresh}")
            continue
        deadline = (now + timedelta(hours=72)).isoformat(timespec="seconds")
        reason = dispatch.emit(ledger_path(), {"subject": sid}, "escalation-blocking",
                               kind="owed-expired", criteria=json.dumps(fresh),
                               evidence=json.dumps(evidence), default=EXPIRED_DEFAULT,
                               deadline=deadline, revert=EXPIRED_REVERT)
        if reason:
            lines.append(f"sweep-owed: escalation for {sid} refused: {reason}")
        else:
            lines.append(f"sweep-owed: escalated {sid} kind=owed-expired criteria={fresh}")
    return lines


# ─────────────────────────── step 2: one open batch at a time ───────────────

def _sweep_kind(subject):
    try:
        raw = (dispatch.CONTENT / f"{subject}.md").read_text()
    except (OSError, UnicodeError):
        return "plain"
    match = re.search(r"^kind:[ \t]*(.*)$", raw, re.M)
    return match.group(1).strip() if match else "plain"


def open_batch(all_ev, now, kind=None):
    """SD7 step 2: a `spawn-started` with `role=owed-sweeper` and no terminal
    event, younger than twice the role's own cap (`dispatch.ROLES`) — the same
    aging rule `tick._spawn_busy` applies to every other role (Assumptions §6:
    computed inline here, on the driver's own observable behaviour)."""
    cap = dispatch.ROLES["owed-sweeper"][1]
    ended = {e.get("spawn") for e in all_ev if e.get("type") in ("spawn-done", "spawn-failed", "spawn-stale")}
    for e in all_ev:
        if e.get("type") != "spawn-started" or e.get("role") != "owed-sweeper":
            continue
        sid = e.get("spawn")
        if sid and sid in ended:
            continue
        if kind is not None and _sweep_kind(e.get("subject")) != kind:
            continue
        age_min = (now - fold.ts(e.get("ts"))).total_seconds() / 60
        if age_min <= 2 * cap:
            return True
    return False


# ─────────────────────────── step 3: due candidates (SD2/SD7) ───────────────

def _blocked_permanently(row):
    """SD2: the latest `owed-unobservable` on this criterion named a capability
    other than `elapsed` — never re-batched, regardless of how long ago."""
    cap = row.get("last_unobservable_capability")
    return cap is not None and cap != "elapsed"


def _blocked_pending_retry(row, now):
    """SD7: an `owed-failed`/`owed-unobservable` landed on this criterion in
    the last 12h — try again next run, not this one."""
    times = [t for t in (row.get("last_failed_at"), row.get("last_unobservable_at")) if t is not None]
    return bool(times) and (now - max(times)) < RETRY_WINDOW


def due_candidates(specs, now):
    """Due rows, oldest first, retaining the retry and capability exclusions.
    An answered expiry cannot exclude a due row (SD11)."""
    rows = []
    for s in specs.values():
        rows.extend(owed.checks(s["evs"], now))
    due = [r for r in rows if r["status"] == "due"
           and not _blocked_permanently(r) and not _blocked_pending_retry(r, now)
           and not _no_runner(r)]
    return sorted(due, key=lambda r: r["due_at"])


# ─────────────────────────── step 4: batch + dispatch ───────────────────────

def route_epochs(all_ev, project):
    """Only a landed deploy for this target and readable wrapper prove epochs."""
    deploys = [e["ts"] for e in all_ev if e.get("type") == "deploy-landed"
               and e.get("target") == project and e.get("ts")]
    epochs = {"deploy": max(deploys, key=fold.ts) if deploys else None}
    try:
        wrapper = doit_bin().parent / "scripts/sweep/droplet-read"
        epochs["droplet-wrapper"] = hashlib.sha256(wrapper.read_bytes()).hexdigest()
    except OSError:
        pass
    return epochs


def _attempt_in_flight(row, evs, now):
    attempts = [e for e in evs if e.get("type") == "owed-routed"
                and e.get("criterion") == row["criterion"]
                and e.get("route") not in ("recheck", "relabel")]
    if not attempts:
        return False
    try:
        last = datetime.fromisoformat(str(attempts[-1].get("ts")).replace("Z", "+00:00"))
        result = row.get("last_unobservable_at")
        return (result is None or last > result) and now - last < RETRY_WINDOW
    except (TypeError, ValueError):
        return True  # An undetermined attempt cannot authorize another try.


def readmitted(specs, now, epochs_for, *, exclude=frozenset(), events=None):
    """Oldest ready blocked rows, excluding transient holds before the cap."""
    rows = []
    for spec in specs.values():
        for row in owed.checks(spec["evs"], now):
            route = owed_routes.route_for(row.get("capability"))
            if (row["status"] != "blocked" or (row["spec"], row["criterion"]) in exclude
                    or route is None
                    or row.get("route_attempts", {}).get(route, 0) >= owed_routes.MAX_ATTEMPTS
                    or _attempt_in_flight(row, spec["evs"], now)
                    or not owed_routes.ready(row, now, epochs_for(row["project"]))
                    or not resolvable(row)[0]
                    or "operator" in prod_runner.record_for(row, events=events)["needs"]):
                continue
            rows.append(row)
    return sorted(rows, key=lambda r: r["last_unobservable_at"])[:READMIT_CAP]


def browser_input(specs, due, now, events=None):
    """The model-free runner consumes every ready capability, once per key."""
    rows = {(r["spec"], r["criterion"]): r for r in due}
    if events is None:
        events = fold.read_events()  # events=None default
    for row in readmitted(specs, now, lambda p: route_epochs(events, p), events=events):
        rows.setdefault((row["spec"], row["criterion"]), row)
    return [{**r, "spec_path": str(_spec_file(r))} for r in rows.values()]


def _record_attempt(row, epochs, now, *, batch=None):
    route = owed_routes.route_for(row["capability"])
    epoch = (epochs.get("deploy") or "" if route in ("ro-dsn", "not-live")
             else epochs.get("droplet-wrapper", "") if route == "droplet" else "")
    fields = dict(criterion=row["criterion"], route=route, capability=route, epoch=epoch,
                  ts=now.isoformat())
    if batch is not None:
        fields["batch"] = batch
    return _emit(row["spec"], "owed-routed", **fields)


def _batch_for(due, all_ev=()):
    """Never-batched projects first, then oldest last start and oldest due row."""
    if not due:
        return None, []
    due = sorted(due, key=lambda r: (r.get("status", "due") != "due", r["due_at"]))
    priority = [r for r in due if r.get("status", "due") == "due"] or due
    last_start = {}
    for e in all_ev:
        if e.get("type") == "spawn-started" and e.get("role") == "owed-sweeper":
            project = e.get("project")
            stamp = fold.ts(e.get("ts"))
            if project not in last_start or stamp > last_start[project]:
                last_start[project] = stamp
    oldest = {}
    for row in priority:
        oldest.setdefault(row["project"], row["due_at"])
    project = min(oldest, key=lambda p: (p in last_start,
                  last_start.get(p, datetime.min.replace(tzinfo=timezone.utc)), oldest[p]))
    return project, [r for r in due if r["project"] == project][:MAX_BATCH]


def _describe(batch):
    return ", ".join(f"{r['spec']}/{r['criterion']}" for r in batch)


def _manifest_text(sweep_id, project, cwd, batch, *, kind="plain"):
    """The Driver→sweeper manifest `packet.parse_sweep_manifest` reads: line 1
    `# <id>`, `project:`/`cwd:` lines, then a fenced ```json array of rows,
    each carrying `spec`, `criterion`, `declared_src`, `line`, `spec_path` —
    the last resolved via `dispatch.spec_path`, the one canonical definition
    of a spec's write destination."""
    rows = [{"spec": r["spec"], "criterion": r["criterion"], "declared_src": r["declared_src"],
             "line": r["line"], "spec_path": str(dispatch.spec_path(r["spec"]))} for r in batch]
    if kind == "repo":
        for row, source in zip(rows, batch):
            row.update(shipped_sha=source["shipped_sha"], base_sha=source["base_sha"], clone=cwd)
            for key in ("provisioning", "check_runner", "provisioning_record"):
                if key in source:
                    row[key] = source[key]
    return (f"# {sweep_id}\n\nproject: {project}\ncwd: {cwd}\nkind: {kind}\n\n## Rows\n\n```json\n"
            + json.dumps(rows, indent=2) + "\n```\n")


def _spec_file(row):
    return pathlib.Path(row.get("spec_path") or dispatch.spec_path(row["spec"]))


def _no_runner(row):
    """Unreadable or undetermined classification cannot authorize retirement."""
    try:
        body = _spec_file(row).read_text(encoding="utf-8")
    except Exception:
        return None
    return owed_failures.no_runner(row, body, row.get("project"))


def unverifiable_candidates(specs, now):
    """Classify only dated open rows; skip undated rows before arithmetic/sorting."""
    candidates = []
    for spec in specs.values():
        for row in owed.checks(spec["evs"], now):
            if row["status"] not in ("due", "blocked", "expired") or row["due_at"] is None:
                continue
            if now - row["due_at"] <= timedelta(days=owed_failures.UNVERIFIABLE_AFTER_DAYS):
                continue
            cause = _no_runner(row)
            if cause:
                candidates.append((row, cause))
    return sorted(candidates, key=lambda item: (item[0]["due_at"], item[0]["spec"], item[0]["criterion"]))


def close_unverifiable(all_ev, now, *, dry_run=False):
    """Close oldest established no-runner rows within the monthly allowance."""
    candidates = unverifiable_candidates(fold.fold(all_ev)[0], now)
    counts = owed_failures.month_counts(all_ev, now)
    lines = []
    for index, (row, cause) in enumerate(candidates):
        if not owed_failures.cap_allows(counts):
            lines.append(f"OWED UNVERIFIABLE CAPPED {len(candidates)-index} rows past 14 days held, closing stopped at cap")
            break
        due = row["due_at"].isoformat()
        reason = f"L-operator-0001: {cause}; due {due}; over {owed_failures.UNVERIFIABLE_AFTER_DAYS} days past due"
        if dry_run:
            lines.append(f"sweep-owed: DRY RUN would close {row['spec']}/{row['criterion']}: {reason}")
        else:
            path = fold.EVENTS / "L-prod-runner-local.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            refused = dispatch.emit(path, {"subject": row["spec"]}, "owed-unverifiable",
                                    criterion=row["criterion"], reason=reason)
            if refused:
                lines.append(f"sweep-owed: close {row['spec']}/{row['criterion']} refused: {refused}")
                continue
            lines.append(f"sweep-owed: closed unverifiable {row['spec']}/{row['criterion']}: {reason}")
        counts["unverifiable"] += 1
        counts["closed"] += 1
    return lines


def resolvable(row):
    """Resolve the row against its own spec; unreadable is never clean."""
    try:
        body = _spec_file(row).read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return False, f"spec unreadable: {exc}"[:300]
    if packet.criterion_block(body, row["criterion"]) is None:
        return False, f"{row['spec']}'s {row['criterion']} is not in its spec file"[:300]
    return True, ""


def _spec_hash(row):
    try:
        return hashlib.sha256(_spec_file(row).read_bytes()).hexdigest()
    except OSError:
        # An unavailable hash is deliberately not a hash of invented file bytes.
        return "unreadable"


def _emit(subject, type_, **fields):
    ledger_path().parent.mkdir(parents=True, exist_ok=True)
    return dispatch.emit(ledger_path(), {"subject": subject}, type_, strict=True, **fields)


def _resolve_candidates(specs, now, *, dry_run=False):
    """Retire legacy keys, recheck changed quarantines, and resolve ALL due rows.

    Cache per-row results so a legacy key also present in the due list is
    resolved and reported only once. All exclusions carry their reason.
    """
    lines, results, quarantined = [], {}, 0
    latest = {}
    for sid, spec in specs.items():
        for e in spec["evs"]:
            if e.get("type") == "owed-quarantined":
                latest[sid, e["criterion"]] = e

    def resolve(row):
        nonlocal quarantined
        key = row["spec"], row["criterion"]
        if key in results:
            return results[key]
        ok, reason = resolvable(row)
        digest = _spec_hash(row)
        if digest == "unreadable":
            ok, reason = False, (reason or "spec hash unavailable")
        results[key] = ok
        if not ok:
            reason = reason[:300]
            if dry_run:
                lines.append(f"sweep-owed: DRY RUN would quarantine {key[0]}/{key[1]}: {reason}")
            elif latest.get(key, {}).get("spec_sha256") != digest:
                refused = _emit(key[0], "owed-quarantined", criterion=key[1],
                                reason=reason, spec_sha256=digest)
                if refused:
                    lines.append(f"sweep-owed: quarantine {key[0]}/{key[1]} refused: {refused}; left out: {reason}")
                else:
                    quarantined += 1
                    latest[key] = {"spec_sha256": digest}
                    lines.append(f"sweep-owed: quarantined {key[0]}/{key[1]}: {reason}")
            else:
                lines.append(f"sweep-owed: quarantined {key[0]}/{key[1]} left out: {reason}")
        return ok

    rechecked = []
    for sid, spec in specs.items():
        for row in owed.checks(spec["evs"], now):
            key = sid, row["criterion"]
            if row["status"] != "quarantined":
                continue
            if _spec_hash(row) == latest.get(key, {}).get("spec_sha256"):
                continue
            if resolve(row):
                if dry_run:
                    lines.append(f"sweep-owed: DRY RUN would recheck {sid}/{row['criterion']}")
                    rechecked.append({"subject": sid, "type": "owed-routed", "criterion": row["criterion"],
                                      "route": "recheck", "ts": now.isoformat()})
                else:
                    refused = _emit(sid, "owed-routed", criterion=row["criterion"], route="recheck")
                    if refused:
                        lines.append(f"sweep-owed: recheck {sid}/{row['criterion']} refused: {refused}")

    if not dry_run:
        # re-read: quarantine/recheck appends above can change due status.
        specs = fold.fold(fold.read_events())[0]
    elif rechecked:
        # Simulate the same governing-instant change without writing an event.
        specs = {sid: {**spec, "evs": [*spec["evs"],
                 *(e for e in rechecked if e["subject"] == sid)]} for sid, spec in specs.items()}
    due = due_candidates(specs, now)

    legacy = dispatch.ROOT / "state" / "sweep-owed-orphans.json"
    if legacy.exists():
        try:
            keys = json.loads(legacy.read_text())
            lines.append(f"sweep-owed: retiring {len(keys)} legacy orphan key(s)")
            for key in keys:
                sid, sep, criterion = key.partition("/")
                if sep and sid in specs:
                    resolve({"spec": sid, "criterion": criterion})
            if not dry_run:
                legacy.rename(legacy.with_name("sweep-owed-orphans.retired.json"))
        except (OSError, ValueError, TypeError) as exc:
            lines.append(f"sweep-owed: orphan retirement failed: {str(exc)[:300]}")

    resolved = [row for row in due if resolve(row)]
    lines.append(f"sweep-owed: resolvable: {len(resolved)} resolved, {len(due) - len(resolved)} unresolved")
    return resolved, quarantined, lines


def make_repo_cwd(project, sha, base, sweep_id):
    """Clone shipped code privately; never modify refs or worktrees in the source."""
    parent = scratch.sub("sweeps") / sweep_id
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    tree = parent / "tree"
    repo = dispatch.ROOT / "repos" / project
    env, deadline = grading_env._clean_env(), time.monotonic() + build_runner.WALL_CAP
    try:
        build_runner._required(["git", "clone", "--shared", "--no-checkout", str(repo), str(tree)],
                               phase="repo clone", env=env, deadline=deadline)
        requested_sha = sha
        sha = build_runner._resolve(tree, sha, env, deadline)
        # The public helper also accepts the literal first-parent expression.
        if base == requested_sha + "^1":
            base = build_runner._required(["git", "-C", str(tree), "rev-parse", "--verify", sha + "^1"],
                                          phase="repo first parent", env=env, deadline=deadline)
        base = build_runner._resolve(tree, base, env, deadline)
        branch = build_runner._main_branch(tree, project, env, deadline)
        refs = build_runner._required(["git", "-C", str(tree), "for-each-ref", "--format=%(refname)",
                                       "refs/remotes/origin/"], phase="repo refs", env=env, deadline=deadline).splitlines()
        main_ref = f"refs/remotes/origin/{branch}"
        if main_ref not in refs:
            raise build_runner.Undetermined("unresolvable main branch")
        build_runner._required(["git", "-C", str(tree), "checkout", "--detach", sha],
                               phase="repo checkout", env=env, deadline=deadline)
        for ref in dict.fromkeys([main_ref, "refs/remotes/origin/main", "refs/remotes/origin/master"]):
            if ref in refs:
                build_runner._required(["git", "-C", str(tree), "update-ref", ref, base],
                                       phase="repo base ref", env=env, deadline=deadline)
        build_runner._required(["git", "-C", str(tree), "symbolic-ref", "refs/remotes/origin/HEAD", main_ref],
                               phase="repo origin HEAD", env=env, deadline=deadline)
        summary = {"node_modules": sweep_provision.dashboard(tree, dispatch.ROOT), "prod_dsns": "unset"}
        (parent / "provisioning.json").write_text(json.dumps(summary) + "\n")
        return tree
    except BaseException:
        shutil.rmtree(parent)
        raise


def reap_repo_cwds(all_ev):
    """Remove only trees whose owed-sweeper spawn has a terminal event."""
    ended = {e.get("spawn") for e in all_ev
             if e.get("type") in ("spawn-done", "spawn-failed", "spawn-stale")}
    removed = []
    for event in all_ev:
        if (event.get("type") != "spawn-started" or event.get("role") != "owed-sweeper"
                or event.get("spawn") not in ended):
            continue
        subject = event.get("subject", "")
        if not re.fullmatch(r"L-sweep-\d+", subject):
            continue
        tree = scratch.sub("sweeps") / subject / "tree"
        if tree.is_dir() and not tree.is_symlink():
            shutil.rmtree(tree)
            removed.append(str(tree))
    return removed


def dispatch_batch(project, batch, runner, *, dry_run=False, kind="plain"):
    """`doit packet owed-sweeper <id>` then `doit dispatch owed-sweeper <id>
    --packet <path> --cwd <sweep-dir> --project <name> --detach`, in that
    order, exactly once, via the injectable `runner` (AC3). `cwd` is a fresh,
    private, non-git directory under the shared scratch root
    (`scratch.sub("sweeps") / <sweep-id>`, mode 0o700) — never
    `$R/repos/<project>` and never a builder/grader worktree (L-spec-9004
    OC1) — created before the manifest is written, so the manifest's own
    `cwd:` field and the `--cwd` argv agree, as they did before this change.
    For kind=repo only, cwd is a clone at the shipped sha instead, provisioned
    by make_repo_cwd before the manifest or packet is written."""
    if dry_run:
        return [f"sweep-owed: DRY RUN would batch {len(batch)} check(s) in project {project} "
                f"({_describe(batch)}) as one detached owed-sweeper dispatch"]
    sweep_path = dispatch.alloc(dispatch.CONTENT, "L-sweep-", ".md")
    sweep_id = sweep_path.stem
    if kind == "repo":
        try:
            cwd = str(make_repo_cwd(project, batch[0]["shipped_sha"], batch[0]["base_sha"], sweep_id))
            # Resolve the first-parent expression to the actual base for the manifest.
            base = build_runner._required(["git", "-C", cwd, "rev-parse", "HEAD^1"],
                                          phase="repo base", env=os.environ.copy(),
                                          deadline=time.monotonic() + 30)
            summary = json.loads((pathlib.Path(cwd).parent / "provisioning.json").read_text())
            provisioned = []
            for index, row in enumerate(batch):
                record = pathlib.Path(cwd).parent / f"check-{index}.provisioning.json"
                record.write_text(json.dumps(summary) + "\n")
                provisioned.append({**row, "base_sha": base, "provisioning": summary,
                    "check_runner": sweep_provision.check_command(cwd, base, record),
                    "provisioning_record": str(record)})
            batch = provisioned
        except (build_runner.Undetermined, OSError, subprocess.SubprocessError):
            sweep_path.unlink(missing_ok=True)
            parent = scratch.sub("sweeps") / sweep_id
            if parent.exists():
                shutil.rmtree(parent)
            return [f"sweep-owed: repo clone failed for {sweep_id}"]
    else:
        cwd_dir = scratch.sub("sweeps") / sweep_id
        cwd_dir.mkdir(parents=True, exist_ok=True)
        cwd_dir.chmod(0o700)
        cwd = str(cwd_dir)
    # Provision the batch's read-only DSN BEFORE the packet is built: packet.p_owed_sweeper
    # decides "credential available" by checking <cwd>/.env exists, and dispatch's own
    # provisioning runs only after the packet is already written. In the old order every
    # sweeper since 2026-09-27 was told "not available (capability=ro-dsn)", recorded the
    # check unobservable, and _blocked_permanently retired it until it expired (2026-10-11).
    if kind != "repo":
        dispatch.provision_sweep_env(cwd, dispatch.ROOT / "repos" / project)
    sweep_path.write_text(_manifest_text(sweep_id, project, cwd, batch, kind=kind))
    pr = runner.run([str(doit_bin()), "packet", "owed-sweeper", sweep_id])
    if pr.returncode != 0:
        err = (pr.stderr or pr.stdout or "").strip()
        return [f"sweep-owed: packet build failed for {sweep_id}: {err}"]
    packet_path = pr.stdout.strip()
    browser_args = ["--mcp-config", str(pathlib.Path(__file__).resolve().parent.parent / "mcp/chrome-devtools.json")] if kind == "browser" else []
    dr = runner.run([str(doit_bin()), "dispatch", "owed-sweeper", sweep_id, "--packet", packet_path,
                     "--cwd", cwd, "--project", project, "--detach", *browser_args])
    if dr.returncode != 0:
        return [f"sweep-owed: dispatch failed for {sweep_id}: {(dr.stderr or dr.stdout or '').strip()}"]
    return [f"sweep-owed: batched {len(batch)} check(s) in project {project} as {sweep_id} "
            f"({_describe(batch)})"]


def judgment_rows(rows, observations, events=None):
    """Partition using the runner's single returned hand-off channel."""
    selected, excluded = [], {}
    browser = {(r["spec"], r["criterion"]) for r in prod_runner.batch_for_browser(rows, events=events)}
    for row in rows:
        key = row["spec"], row["criterion"]
        record = prod_runner.record_for(row, events=events)
        if key in observations["held"]:
            reason = "runner held"
        elif row.get("capability") in ("operator-action", "ro-dsn-grant") or "operator" in record["needs"]:
            reason = "operator or grant required"
        elif key in browser:
            reason = "browser hand-off"
        elif record["runner"] == "judgment":
            selected.append(row)
            continue
        elif record["runner"] == "build" and key in observations["bundle_rows"]:
            selected.append({**row, "line": f"{row.get('line') or ''}\nevidence bundle: {observations['bundle_rows'][key]}"})
            continue
        elif record["runner"] == "prod" and key in observations["to_judgment"]:
            selected.append(row)
            continue
        else:
            reason = "script observation"
        excluded[reason] = excluded.get(reason, 0) + 1
    lines = [f"sweep-owed: {count} row(s) excluded from plain batch: {reason}"
             for reason, count in sorted(excluded.items())]
    return selected, lines


# ─────────────────────────────────── driver ──────────────────────────────────

def run(*, dry_run=False, runner=None, now=None):
    """One pass: expiry, resolution, project choice, dispatch and run record."""
    runner = runner or Runner()
    now = now or datetime.now(timezone.utc)
    all_ev = fold.read_events()  # events=None default: initial sweep snapshot
    specs = fold.fold(all_ev)[0]
    if not dry_run:
        reap_repo_cwds(all_ev)

    lines = sweep_expired(specs, now, dry_run=dry_run)
    lines.extend(owed_failures.escalate_failures(specs, now, dry_run=dry_run))
    # Preserve pre-observation rows: a runner verdict may change their status.
    retry_specs = specs
    epochs_for = lambda project: route_epochs(all_ev, project)
    runner_keys = {(r["spec"], r["criterion"]) for r in readmitted(specs, now, epochs_for, events=all_ev)}
    retry_epochs = {r["project"]: epochs_for(r["project"])
                    for r in readmitted(specs, now, epochs_for, events=all_ev)}
    if not dry_run and any(line.startswith(("sweep-owed: escalated ", "sweep-owed: reopened ",
                                            "sweep-owed: recheck ")) for line in lines):
        # re-read: expiry/failure escalation or recheck appended before observations.
        all_ev = fold.read_events()
    observations = prod_runner.run(now, dry_run=dry_run, events=all_ev)
    lines.extend(observations["lines"])
    if not dry_run:
        # re-read: runner verdicts must govern routing and open-batch decisions.
        all_ev = fold.read_events()
    specs = fold.fold(all_ev)[0]
    lines.extend(close_unverifiable(all_ev, now, dry_run=dry_run))
    no_runner_counts = {}
    for spec in specs.values():
        for row in owed.checks(spec["evs"], now):
            if row["status"] in ("due", "blocked", "expired"):
                cause = _no_runner(row)
                if cause:
                    no_runner_counts[cause] = no_runner_counts.get(cause, 0) + 1
    for cause, count in sorted(no_runner_counts.items()):
        lines.append(f"sweep-owed: {count} no-runner row(s) excluded from escalation and batching: {cause}; see owed digest")

    due, quarantined, resolution_lines = _resolve_candidates(specs, now, dry_run=dry_run)
    lines.extend(resolution_lines)
    # Re-admit stale failure hand-offs only under the same guards as observation,
    # using the fresh fold in case a corrective landed during the runner pass.
    routed = set(observations["bundle_rows"]) | observations["to_judgment"]
    present = {(r["spec"], r["criterion"]) for r in due}
    for spec in specs.values():
        for row in owed.checks(spec["evs"], now):
            key = row["spec"], row["criterion"]
            if key in routed - present and prod_runner.stale_failure_ready(row, spec["evs"], now):
                ok, reason = resolvable(row)
                if ok:
                    due.append(row)
                else:
                    lines.append(f"sweep-owed: 1 routed row excluded: {key[0]}/{key[1]}: {reason}")
    retries = readmitted(retry_specs, now, lambda p: retry_epochs.get(p, route_epochs(all_ev, p)),
                         exclude=observations["held"], events=all_ev)
    retry_epochs.update({r["project"]: route_epochs(all_ev, r["project"]) for r in retries
                         if r["project"] not in retry_epochs})
    retry_keys = {(r["spec"], r["criterion"]) for r in retries}
    due_keys = {(r["spec"], r["criterion"]) for r in due}
    due.extend(r for r in retries if (r["spec"], r["criterion"]) not in due_keys)
    due, script_lines = provision.script_rows(due, all_ev, fold.EVENTS / "L-owed-sweeper-script.jsonl",
                                              lane="sweep", dry_run=dry_run)
    lines.extend(script_lines)
    # Capability browser always goes to the browser seat, even for judgment rows.
    handed = prod_runner.batch_for_browser(due, events=all_ev)
    handed_keys = {(r["spec"], r["criterion"]) for r in handed}
    handed.extend(r for r in retries if owed_routes.route_for(r["capability"]) == "browser"
                  and (r["spec"], r["criterion"]) not in handed_keys)
    e2e, browser_seat = prod_browser.split(handed, events=all_ev)
    if e2e:
        lines.append(f"sweep-owed: {len(e2e)} browser row(s) assigned to committed e2e observations")
    seat_keys = {(r["spec"], r["criterion"]) for r in browser_seat}
    for row in retries:
        key = row["spec"], row["criterion"]
        if (key in runner_keys and key not in seat_keys
                and key not in observations["bundle_rows"] and key not in observations["to_judgment"]
                and prod_runner.record_for(row, events=all_ev)["runner"] in ("build", "prod", "prod-browser")):
            if not dry_run:
                refused = _record_attempt(row, retry_epochs[row["project"]], now)
                if refused:
                    lines.append(f"sweep-owed: attempt for {key[0]}/{key[1]} refused: {refused}")
    due = [r for r in due if (r["spec"], r["criterion"]) not in seat_keys]
    due, partition_lines = judgment_rows(due, observations, events=all_ev)
    lines.extend(partition_lines)
    repo_rows, plain_rows = [], []
    for row in due:
        if (prod_runner.record_for(row, events=all_ev)["runner"] == "judgment"
                and owed_routes.ROUTES.get(owed_routes.route_for(row.get("capability"))) == "repo"):
            sha = next((e.get("sha") for e in reversed(specs[row["spec"]]["evs"])
                        if e.get("type") == "shipped"), None)
            if not sha:
                lines.append(f"sweep-owed: 1 repo row excluded: {row['spec']}/{row['criterion']}: shipped sha undetermined")
                continue
            repo_rows.append({**row, "shipped_sha": sha, "base_sha": sha + "^1"})
        else:
            plain_rows.append(row)
    due = plain_rows
    plain_project, _ = _batch_for(due, all_ev)
    project, _ = _batch_for([*due, *browser_seat], all_ev)
    kind = "browser" if project is not None and project != plain_project else "plain"
    candidates = browser_seat if kind == "browser" else due
    record = {"quarantined": quarantined}
    plain_open = open_batch(all_ev, now, "plain") or open_batch(all_ev, now, "browser")
    repo_open = open_batch(all_ev, now, "repo")
    if (not candidates or plain_open) and repo_rows and not repo_open:
        candidates, kind = repo_rows, "repo"
    if (repo_open if kind == "repo" else plain_open) or (not candidates and repo_rows and repo_open):
        record["outcome"] = "open"
        lines.append("sweep-owed: an owed-sweeper batch is already open; nothing dispatched this run")
    elif not candidates:
        record["outcome"] = "nothing-due"
        lines.append("sweep-owed: nothing due to batch")
    else:
        project, batch = _batch_for(candidates, all_ev)
        if kind == "repo":
            sha = batch[0]["shipped_sha"]
            batch = sorted([row for row in candidates if row["project"] == project and row["shipped_sha"] == sha],
                           key=lambda r: (r.get("status", "due") != "due", r["due_at"]))[:MAX_BATCH]
            deferred = sum(row["project"] == project for row in candidates) - len(batch)
            if deferred:
                lines.append(f"sweep-owed: {deferred} repo row(s) deferred: one shipped sha and at most {MAX_BATCH} rows per batch")
        try:
            batch_lines = dispatch_batch(project, batch, runner, dry_run=dry_run, kind=kind)
        except (OSError, subprocess.SubprocessError) as exc:
            batch_lines = [f"sweep-owed: batch failed: {str(exc)[:300]}"]
        lines.extend(batch_lines)
        failure = next((line for line in batch_lines if "failed" in line), None)
        if failure:
            record.update(outcome="failed", reason=failure[:300])
        else:
            record.update(outcome="batched", kind=kind, rows=len(batch))
            if not dry_run:
                record["batch"] = batch_lines[0].split(" as ", 1)[1].split()[0]
                for row in batch:
                    if (row["spec"], row["criterion"]) in retry_keys:
                        refused = _record_attempt(row, retry_epochs[row["project"]], now, batch=record["batch"])
                        if refused:
                            lines.append(f"sweep-owed: attempt for {row['spec']}/{row['criterion']} refused: {refused}")
    if not dry_run:
        refused = _emit("sweep-owed", "sweep-run", **record)
        if refused:
            lines.append(f"sweep-owed: sweep-run refused: {refused}")

    lines.extend(verifier.run(dry_run=dry_run, now=now, runner=runner))
    for line in lines:
        print(line)
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(prog="doit sweep-owed", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print what would happen; write nothing")
    a = ap.parse_args(argv)
    run(dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
