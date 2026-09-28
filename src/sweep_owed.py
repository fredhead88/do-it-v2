#!/usr/bin/env python3
"""sweep_owed — the driver that batches due owed checks into `owed-sweeper`
dispatches, and escalates the ones that expired instead (L-charter-0038 R1/R2).

  doit sweep-owed [--dry-run]

Every run, in this order (Plan SD7):

1. Append one `escalation-blocking kind=owed-expired` per spec for every
   criterion that newly reached `owed.checks()`'s `"expired"` status (SD3) —
   deduped against every criterion ANY prior `escalation-blocking
   kind=owed-expired` on that spec already named, resolved or not. This step
   always runs, whether or not step 2 below goes on to skip batching.
2. If one `owed-sweeper` spawn is already open (a `spawn-started` with no
   terminal event, younger than twice the role's own cap — 90 minutes at
   `dispatch.ROLES["owed-sweeper"]`'s 45), stop: no manifest, no dispatch.
3. Otherwise take every `due` check across every project, oldest-`due_at`
   first, EXCLUDING (a) a check carrying an `owed-failed`/`owed-unobservable`
   event in the last 12h (retry next run) and (b) a check whose latest
   `owed-unobservable` capability is not `elapsed` (SD2 — never re-batched,
   regardless of age).
4. Batch at most 8 of the OLDEST-due check's own project's checks into one
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
import argparse, json, pathlib, re, subprocess, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dispatch, fold, owed, scratch  # noqa: E402

LEDGER_NAME = "L-sweep-local.jsonl"
MAX_BATCH = 8
RETRY_WINDOW = timedelta(hours=12)

# SD3, verbatim.
EXPIRED_DEFAULT = ("leave these checks expired and unproven, off the sweep; the charter stays "
                    "held by owed<=K until an owed-met or a corrective")
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
                        and not _excluded_by_answer(specs, answered, sid, r["criterion"])})
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
    """Every `due` row across every spec, oldest-`due_at` first, minus the two
    SD2/SD7 exclusions above, plus (L-charter-0042 R6/SD13) any row whose spec
    is in `answered_specs` and whose own governing `owed-ac` does not strictly
    postdate that spec's max answering instant. One row per (spec, criterion)."""
    rows = []
    for s in specs.values():
        rows.extend(owed.checks(s["evs"], now))
    due = [r for r in rows if r["status"] == "due"
           and not _blocked_permanently(r) and not _blocked_pending_retry(r, now)]
    answered = answered_specs(specs)
    due = [r for r in due if not _excluded_by_answer(specs, answered, r["spec"], r["criterion"])]
    return sorted(due, key=lambda r: r["due_at"])


# ─────────────────────────── step 4: batch + dispatch ───────────────────────

def _batch_for(due):
    """SD7 step 4: "take one project" — the one owning the oldest due check —
    then at most `MAX_BATCH` of ITS checks, oldest-first."""
    if not due:
        return None, []
    project = due[0]["project"]
    return project, [r for r in due if r["project"] == project][:MAX_BATCH]


def _describe(batch):
    return ", ".join(f"{r['spec']}/{r['criterion']}" for r in batch)


def _manifest_text(sweep_id, project, cwd, batch):
    """The Driver→sweeper manifest `packet.parse_sweep_manifest` reads: line 1
    `# <id>`, `project:`/`cwd:` lines, then a fenced ```json array of rows,
    each carrying `spec`, `criterion`, `declared_src`, `line`, `spec_path` —
    the last resolved via `dispatch.spec_path`, the one canonical definition
    of a spec's write destination."""
    rows = [{"spec": r["spec"], "criterion": r["criterion"], "declared_src": r["declared_src"],
             "line": r["line"], "spec_path": str(dispatch.spec_path(r["spec"]))} for r in batch]
    return (f"# {sweep_id}\n\nproject: {project}\ncwd: {cwd}\n\n## Rows\n\n```json\n"
            + json.dumps(rows, indent=2) + "\n```\n")


ORPHANS = pathlib.Path.home() / ".do-it" / "state" / "sweep-owed-orphans.json"


def _load_orphans():
    try:
        return set(json.loads(ORPHANS.read_text()))
    except Exception:
        return set()


def _remember_orphan(key):
    try:
        known = _load_orphans() | {key}
        ORPHANS.parent.mkdir(parents=True, exist_ok=True)
        ORPHANS.write_text(json.dumps(sorted(known)))
    except Exception:
        pass


def dispatch_batch(project, batch, runner, *, dry_run=False):
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
    skipped = []
    while True:
        sweep_path.write_text(_manifest_text(sweep_id, project, cwd, batch))
        pr = runner.run([str(doit_bin()), "packet", "owed-sweeper", sweep_id])
        if pr.returncode == 0:
            break
        err = (pr.stderr or pr.stdout or "").strip()
        # One row whose criterion no longer exists in its spec file must not sink the whole
        # batch (first live run, 2026-09-27 12:30Z: L-spec-0205 AC6 blocked every due check).
        m = re.search(r"(L-spec-\d+)'s (\S+) is not in", err)
        keep = [r for r in batch if not (m and r["spec"] == m.group(1) and r["criterion"] == m.group(2))]
        if not m or len(keep) == len(batch) or not keep:
            return [f"sweep-owed: packet build failed for {sweep_id}: {err}"] + [
                f"sweep-owed: skipped {x} (criterion not in its spec file)" for x in skipped]
        skipped.append(f"{m.group(1)}/{m.group(2)}")
        _remember_orphan(skipped[-1])
        batch = keep
    packet_path = pr.stdout.strip()
    dr = runner.run([str(doit_bin()), "dispatch", "owed-sweeper", sweep_id, "--packet", packet_path,
                     "--cwd", cwd, "--project", project, "--detach"])
    if dr.returncode != 0:
        return [f"sweep-owed: dispatch failed for {sweep_id}: {(dr.stderr or dr.stdout or '').strip()}"]
    return [f"sweep-owed: batched {len(batch)} check(s) in project {project} as {sweep_id} "
            f"({_describe(batch)})"] + [f"sweep-owed: skipped {x} (criterion not in its spec file)"
                                        for x in skipped]


# ─────────────────────────────────── driver ──────────────────────────────────

def run(*, dry_run=False, runner=None, now=None):
    """One sweep pass, in SD7's own order. Returns the lines printed."""
    runner = runner or Runner()
    now = now or datetime.now(timezone.utc)
    all_ev = fold.read_events()
    specs = fold.fold(all_ev)[0]

    lines = sweep_expired(specs, now, dry_run=dry_run)

    if open_batch(all_ev, now):
        lines.append("sweep-owed: an owed-sweeper batch is already open; nothing dispatched this run")
    else:
        due = due_candidates(specs, now)
        if not due:
            lines.append("sweep-owed: nothing due")
        else:
            # Rows the packet builder already refused as "criterion not in spec file" are remembered
            # (ORPHANS file) and dropped BEFORE batching: 8 such rows sat at the head of the due list
            # and filled every MAX_BATCH=8 batch, so most runs on 2026-09-27/28 dispatched nothing.
            known = _load_orphans()
            orphans = [r for r in due if f"{r['spec']}/{r['criterion']}" in known]
            due = [r for r in due if f"{r['spec']}/{r['criterion']}" not in known]
            if orphans:
                lines.append(f"sweep-owed: {len(orphans)} known orphan criterion row(s) left out of the batch")
            if not due:
                lines.append("sweep-owed: nothing due")
                return lines
            project, batch = _batch_for(due)
            lines.extend(dispatch_batch(project, batch, runner, dry_run=dry_run))

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
