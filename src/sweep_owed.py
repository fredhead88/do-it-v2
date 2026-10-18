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
import argparse, hashlib, json, pathlib, re, subprocess, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dispatch, fold, owed, owed_failures, packet, scratch, prod_runner, prod_browser, owed_routes  # noqa: E402

LEDGER_NAME = "L-sweep-local.jsonl"
MAX_BATCH = 8
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
                        and not _no_runner(r)})
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

def open_batch(all_ev, now):
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

def browser_input(specs, due, now):
    """Include retry-ready legacy browser rows once, retaining the due rows."""
    rows = {(r["spec"], r["criterion"]): r for r in due}
    for spec in specs.values():
        for row in owed.checks(spec["evs"], now):
            if (row["status"] == "blocked"
                    and owed_routes.route_for(row.get("capability")) == "browser"
                    and row.get("route_attempts", {}).get("browser", 0) < owed_routes.MAX_ATTEMPTS
                    and not _blocked_pending_retry(row, now) and resolvable(row)[0]):
                rows.setdefault((row["spec"], row["criterion"]), row)
    return [{**r, "spec_path": str(_spec_file(r))} for r in rows.values()]


def _batch_for(due, all_ev=()):
    """Never-batched projects first, then oldest last start and oldest due row."""
    if not due:
        return None, []
    due = sorted(due, key=lambda r: r["due_at"])
    last_start = {}
    for e in all_ev:
        if e.get("type") == "spawn-started" and e.get("role") == "owed-sweeper":
            project = e.get("project")
            stamp = fold.ts(e.get("ts"))
            if project not in last_start or stamp > last_start[project]:
                last_start[project] = stamp
    oldest = {}
    for row in due:
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
    return dispatch.emit(ledger_path(), {"subject": subject}, type_, **fields)


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


def dispatch_batch(project, batch, runner, *, dry_run=False, kind="plain"):
    """`doit packet owed-sweeper <id>` then `doit dispatch owed-sweeper <id>
    --packet <path> --cwd <sweep-dir> --project <name> --detach`, in that
    order, exactly once, via the injectable `runner` (AC3). `cwd` is a fresh,
    private, non-git directory under the shared scratch root
    (`scratch.sub("sweeps") / <sweep-id>`, mode 0o700) — never
    `$R/repos/<project>` and never a builder/grader worktree (L-spec-9004
    OC1) — created before the manifest is written, so the manifest's own
    `cwd:` field and the `--cwd` argv agree, as they did before this change."""
    if dry_run:
        return [f"sweep-owed: DRY RUN would batch {len(batch)} check(s) in project {project} "
                f"({_describe(batch)}) as one detached owed-sweeper dispatch"]
    sweep_path = dispatch.alloc(dispatch.CONTENT, "L-sweep-", ".md")
    sweep_id = sweep_path.stem
    cwd_dir = scratch.sub("sweeps") / sweep_id
    cwd_dir.mkdir(parents=True, exist_ok=True)
    cwd_dir.chmod(0o700)
    cwd = str(cwd_dir)
    # Provision the batch's read-only DSN BEFORE the packet is built: packet.p_owed_sweeper
    # decides "credential available" by checking <cwd>/.env exists, and dispatch's own
    # provisioning runs only after the packet is already written. In the old order every
    # sweeper since 2026-09-27 was told "not available (capability=ro-dsn)", recorded the
    # check unobservable, and _blocked_permanently retired it until it expired (2026-10-11).
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


def judgment_rows(rows, observations):
    """Partition using the runner's single returned hand-off channel."""
    selected, excluded = [], {}
    browser = {(r["spec"], r["criterion"]) for r in prod_runner.batch_for_browser(rows)}
    for row in rows:
        key = row["spec"], row["criterion"]
        record = prod_runner.record_for(row)
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
    all_ev = fold.read_events()
    specs = fold.fold(all_ev)[0]

    lines = sweep_expired(specs, now, dry_run=dry_run)
    lines.extend(owed_failures.escalate_failures(specs, now, dry_run=dry_run))
    observations = prod_runner.run(now, dry_run=dry_run)
    lines.extend(observations["lines"])
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
    # Stale failures are runner candidates even though due_candidates excludes
    # failed status. Preserve their explicit evidence/judgment hand-off too.
    routed = set(observations["bundle_rows"]) | observations["to_judgment"]
    present = {(r["spec"], r["criterion"]) for r in due}
    for spec in specs.values():
        for row in owed.checks(spec["evs"], now):
            key = row["spec"], row["criterion"]
            if key in routed - present and row["status"] == "failed":
                ok, reason = resolvable(row)
                if ok:
                    due.append(row)
                else:
                    lines.append(f"sweep-owed: 1 routed row excluded: {key[0]}/{key[1]}: {reason}")
    handed = prod_runner.batch_for_browser(browser_input(specs, due, now))
    e2e, browser_seat = prod_browser.split(handed)
    if e2e:
        lines.append(f"sweep-owed: {len(e2e)} browser row(s) assigned to committed e2e observations")
    due, partition_lines = judgment_rows(due, observations)
    lines.extend(partition_lines)
    plain_project, _ = _batch_for(due, all_ev)
    project, _ = _batch_for([*due, *browser_seat], all_ev)
    kind = "browser" if project is not None and project != plain_project else "plain"
    candidates = browser_seat if kind == "browser" else due
    record = {"quarantined": quarantined}
    if open_batch(all_ev, now):
        record["outcome"] = "open"
        lines.append("sweep-owed: an owed-sweeper batch is already open; nothing dispatched this run")
    elif not candidates:
        record["outcome"] = "nothing-due"
        lines.append("sweep-owed: nothing due to batch")
    else:
        project, batch = _batch_for(candidates, all_ev)
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
    if not dry_run:
        refused = _emit("sweep-owed", "sweep-run", **record)
        if refused:
            lines.append(f"sweep-owed: sweep-run refused: {refused}")

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
