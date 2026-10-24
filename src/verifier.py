#!/usr/bin/env python3
"""verifier — the scheduled, full-environment owed-check lane (L-spec-0682 R1).

  doit verify-owed [--dry-run] [--check SPEC/CRITERION ...]   one pass
  doit verify-owed --preflight                                the stack, one line per part

The restricted `owed-sweeper` has no git, no verify-chain scripts, no local
stack and no browser, so a check needing one of them ends `owed-unobservable`
and `sweep_owed` never re-batches it. This lane is the path for those: a
standing local stack plus the read-only prod DSN, run every two hours, and every
check it takes ends in a real verdict (met / failed, with evidence) or an
explicit, plain-English operator ask (`owed_asks`) — never silently dropped.

One pass, in this order:

 1. Timed readings (`owed_timed.run_pass`): store the first reading of a check
    declaring two, and date its second.
 2. Pick candidates: every due check whose latest word is NOT an elapsed-time wait
    and which carries no failed verdict yet, minus the six L-spec-0163 checks the
    operator excused (ruling L-operator-0001.jsonl:17, 2026-09-23). A check with an
    open operator ask is left to its ask unless named with `--check`.
 3. Observe each (`observe(row)` -> {verdict: met|failed|blocked, evidence, ...}) and
    append exactly one `owed-met` / `owed-failed` / `owed-unobservable` per check,
    evidence attached, through `dispatch.emit` — the ledger's production append path.
    With no `observe` handed in, the checks are batched into one detached
    `owed-verifier` dispatch (the seat-billed sub-agent), whose verdicts the
    dispatch wrapper appends the same way.
 4. File ONE rework `brief` per failed check (deduped by criterion and the failed verdict's
    timestamp, so a second pass over the same failure files nothing).

The only event types this module ever appends are `owed-met`, `owed-failed`,
`owed-unobservable` and `brief` (`ALLOWED_TYPES`, enforced at the append and by
a test): nothing waives, drops or auto-closes a check. It is read-only against
production (reads go only through `scripts/sweep/ro-sql` / `droplet-read`) and
writes nothing to any repository.
"""
import argparse, json, os, pathlib, re, shutil, socket, subprocess, sys
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch, fold, owed, owed_timed, scratch, provision  # noqa: E402

LEDGER_NAME = "L-verifier-local.jsonl"
ALLOWED_TYPES = frozenset({"owed-met", "owed-failed", "owed-unobservable", "brief"})
RETRY = timedelta(hours=6)
CAPABILITIES = ("ro-dsn", "droplet", "browser", "operator-action", "elapsed", "other")
EXCUSED = frozenset(("L-spec-0163", c) for c in ("AC1", "AC2", "AC5b", "AC11a", "AC11b", "AC13"))
PROJECT = "albert-scott"
COMPONENTS = ("git", "verify-chain scripts", "local Postgres from genesis", "local uvicorn",
              "dashboard dev server", "chrome-devtools browser", "read-only prod DSN")


def ledger_path():
    """This module's own ledger — filename-derived actor `verifier` (D90) —
    resolved fresh against the CURRENT `fold.EVENTS`, never cached at import."""
    return fold.EVENTS / LEDGER_NAME


def _emit(type_, spec, **kv):
    """THE append path of this module. A type outside `ALLOWED_TYPES` is a bug."""
    if type_ not in ALLOWED_TYPES:
        raise AssertionError(f"verifier may not append {type_!r}")
    fold.EVENTS.mkdir(parents=True, exist_ok=True)
    return dispatch.emit(ledger_path(), {"subject": spec}, type_, **kv)


# ───────────────────────────────── preflight ─────────────────────────────────

_SECRET_NAME = re.compile(r"PASSWORD|TOKEN|SECRET|DB_URL|DSN|API_KEY", re.I)


def _secrets(env, root):
    vals = {v for k, v in env.items() if _SECRET_NAME.search(k) and v and len(v) >= 4}
    env_file = pathlib.Path(root) / "repos" / PROJECT / ".env"
    if env_file.is_file():
        for k, v in dispatch._parse_env(env_file).items():
            if _SECRET_NAME.search(k) and v:
                vals.add(v)
    out = set(vals)
    for v in vals:                                   # a DSN's password on its own
        m = re.match(r"^[a-z+]+://[^:@/]*:([^@/]+)@", v)
        if m:
            out.add(m.group(1))
    return sorted(out, key=len, reverse=True)


def _redact(text, secrets):
    for s in secrets:
        text = text.replace(s, "[redacted]")
    return text


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return None
    except OSError:
        return f"nothing is listening on 127.0.0.1:{port}"


def _probe_git(ctx):
    return None if shutil.which("git") else "no git on PATH"


def _probe_scripts(ctx):
    repo = HERE.parent
    need = ["doit", "src/merge_gate.py", "scripts/private-pg.sh", "scripts/sweep/ro-sql", "scripts/sweep/droplet-read"]
    miss = [n for n in need if not (repo / n).exists() or (n.startswith(("scripts/", "doit")) and not os.access(repo / n, os.X_OK))]
    return f"missing or not executable: {', '.join(miss)}" if miss else None


def _probe_postgres(ctx):
    pgbin = pathlib.Path("/usr/lib/postgresql/17/bin")
    if not (pgbin / "pg_ctl").exists():
        return "postgresql 17 is not installed"
    genesis = pathlib.Path(ctx["root"]) / "repos" / PROJECT / "api" / "alembic_supabase" / "genesis_schema.sql"
    if not genesis.is_file():
        return "the genesis schema sidecar is not in the project checkout"
    try:
        d = scratch.sub("verifier-pg")
    except Exception as x:  # noqa: BLE001
        return f"no scratch root: {type(x).__name__}"
    sock = next((s for s in d.glob(".s.PGSQL.*") if s.is_socket()), None)
    if sock is None:
        return f"no standing cluster in {d} (start it with scripts/private-pg.sh, then load the genesis schema)"
    r = subprocess.run([str(pgbin / "psql"), "-X", "-At", "-h", str(d), "-p", sock.name.rsplit(".", 1)[1], "-U", "albert",
                        "-d", "scratch", "-c", "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"],
                       capture_output=True, text=True, timeout=20, env={"PATH": os.environ.get("PATH", "")})
    if r.returncode != 0:
        return "the standing cluster does not answer"
    return None if (r.stdout.strip() or "0") != "0" else "the standing cluster holds no tables — genesis has not been loaded"


def _probe_uvicorn(ctx):
    return _port_open(int(ctx["env"].get("DOIT_VERIFIER_API_PORT") or 8000))


def _probe_dashboard(ctx):
    return _port_open(int(ctx["env"].get("DOIT_VERIFIER_DASHBOARD_PORT") or 3000))


def _probe_browser(ctx):
    """Registered only — a worktree cannot drive it (spec A7)."""
    home = pathlib.Path(ctx["env"].get("HOME") or pathlib.Path.home())
    for p in (home / ".claude.json", pathlib.Path(ctx["root"]) / "mcp.json", HERE.parent / ".mcp.json"):
        try:
            if "chrome-devtools" in p.read_text():
                return None
        except OSError:
            continue
    return "no chrome-devtools server is registered in any MCP config"


def _probe_dsn(ctx):
    import sweep_wrappers
    dsn, refusal = sweep_wrappers.resolve_ro_dsn(pathlib.Path(ctx["root"]) / "repos" / PROJECT)
    return refusal if (dsn is None or refusal) else None


PROBES = dict(zip(COMPONENTS, (_probe_git, _probe_scripts, _probe_postgres, _probe_uvicorn,
                               _probe_dashboard, _probe_browser, _probe_dsn)))


def preflight(root=None, env=None, probes=None):
    """`(lines, all_present)` — one `<component>: present` or `<component>: absent:
    <why>` per component, in `COMPONENTS` order. Every line is scrubbed of any
    secret the environment or the project's `.env` holds before it is returned."""
    env = dict(os.environ if env is None else env)
    root = pathlib.Path(root or fold.ROOT)
    ctx, secrets, lines, ok = {"root": root, "env": env}, _secrets(env, root), [], True
    for name in COMPONENTS:
        try:
            why = (probes or PROBES)[name](ctx)
        except Exception as x:  # noqa: BLE001
            why = f"probe failed: {type(x).__name__}: {x}"
        ok = ok and why is None
        lines.append(f"{name}: present" if why is None else _redact(f"{name}: absent: {why}", secrets))
    return lines, ok


# ─────────────────────────────── the pass ───────────────────────────────────

def candidates(by, now, only=None):
    """The checks this pass observes, oldest-due first (see the module docstring)."""
    rows = []
    for sid, evs in by.items():
        for r in owed.checks(evs, now):
            key = f"{r['spec']}/{r['criterion']}"
            if (r["spec"], r["criterion"]) in EXCUSED:
                continue
            if only is not None:
                if key in only and r["status"] not in ("met", "dropped", "waived", "unshipped"):
                    rows.append(r)
                continue
            cap = r.get("last_unobservable_capability")
            if r["status"] not in ("due", "blocked", "expired") or r["failures"] >= 1 or cap in ("operator-action", "elapsed"):
                continue
            recent = r.get("last_unobservable_at")
            if recent is not None and now - recent < RETRY:
                continue
            rows.append(r)
    return sorted(rows, key=lambda r: (r["due_at"] or now, r["spec"], r["criterion"]))


def record(row, obs):
    """Append the ONE verdict `obs` is for `row`. `obs` is `{verdict, evidence, ...}`;
    an unusable one (no verdict, or no evidence) is recorded as `owed-unobservable`
    saying so — a check is never skipped without a word. Returns the event type."""
    spec, crit = row["spec"], row["criterion"]
    verdict = (obs or {}).get("verdict")
    evidence = str((obs or {}).get("evidence") or "").strip()[:600]
    if verdict in ("met", "failed") and evidence:
        t = "owed-met" if verdict == "met" else "owed-failed"
        kv = {"evidence": evidence, "verifier": "verify-owed"}
        if verdict == "failed":
            kv["kind"] = (obs or {}).get("kind") if (obs or {}).get("kind") in ("unmet", "stale") else "unmet"
        why = _emit(t, spec, criterion=crit, **kv)
    else:
        cap = (obs or {}).get("capability")
        cap = cap if cap in CAPABILITIES else "other"
        why_text = str((obs or {}).get("why") or evidence or "the verifier could not reach a verdict and gave no reason")[:400]
        t = "owed-unobservable"
        why = _emit(t, spec, criterion=crit, capability=cap, why=why_text, evidence=evidence or why_text,
                    verifier="verify-owed")
    if why:
        raise RuntimeError(f"{spec} {crit}: ledger refused the {t}: {why}")
    return t


def file_rework_briefs(by, now, events, dry_run=False):
    """ONE `brief` per current failed check, deduped on (spec, criterion, the
    failed verdict's own timestamp): a failure observed again after a rework is a NEW one."""
    seen = {(e.get("subject"), e.get("key")) for e in events if e.get("type") == "brief"}
    out = []
    for sid, evs in sorted(by.items()):
        for r in owed.checks(evs, now):
            if r["failures"] < 1 or r["status"] in ("met", "dropped", "waived") or (sid, r["criterion"]) in EXCUSED:
                continue
            key = f"{sid}/{r['criterion']}@{r['last_failed_at'].isoformat(timespec='seconds')}"
            if (sid, key) in seen:
                continue
            seen.add((sid, key))
            out.append(f"filed a rework brief for {sid} {r['criterion']}")
            if dry_run:
                continue
            ev = r.get("evidence") or "no evidence recorded"
            why = _emit("brief", sid, condition="owed-check-failed", key=key, criterion=r["criterion"],
                        owner="executor", problem="owed-check-failed", evidence=ev,
                        why=f"{sid} {r['criterion']} failed its post-deploy check: {ev}"[:600],
                        measured_at=now.isoformat(timespec="seconds"))
            if why:
                out[-1] = f"brief for {sid} {r['criterion']} refused: {why}"
    return out


def _open_batch(events, now):
    cap = dispatch.ROLES["owed-verifier"][1]
    ended = {e.get("spawn") for e in events if e.get("type") in ("spawn-done", "spawn-failed", "spawn-stale")}
    return any(e.get("type") == "spawn-started" and e.get("role") == "owed-verifier" and e.get("spawn") not in ended
               and (now - fold.ts(e.get("ts"))).total_seconds() / 60 <= 2 * cap for e in events)


def dispatch_batch(batch, runner, now):
    """One detached `owed-verifier` dispatch over up to eight checks of ONE project
    (the manifest and packet shapes are the sweeper's own, `packet.parse_sweep_manifest`).
    A row whose criterion is not in its spec file is recorded `owed-unobservable`
    instead of sinking the batch. Returns the lines to print."""
    import sweep_owed
    lines, project = [], batch[0]["project"] or PROJECT
    batch = [r for r in batch if (r["project"] or PROJECT) == project][:sweep_owed.MAX_BATCH]
    path = dispatch.alloc(dispatch.CONTENT, "L-sweep-", ".md")
    sid = path.stem
    cwd = scratch.sub("verifies") / sid
    cwd.mkdir(parents=True, exist_ok=True)
    cwd.chmod(0o700)
    doit = str(HERE.parent / "doit")
    while batch:
        path.write_text(sweep_owed._manifest_text(sid, project, str(cwd), batch))
        pr = runner.run([doit, "packet", "owed-verifier", sid])
        if pr.returncode == 0:
            break
        err = (pr.stderr or pr.stdout or "").strip()
        m = re.search(r"(L-spec-\d+)'s (\S+) is not in", err)
        bad = [r for r in batch if m and r["spec"] == m.group(1) and r["criterion"] == m.group(2)]
        if not bad or len(bad) == len(batch):
            return [f"verify-owed: packet build failed for {sid}: {err}"]
        for r in bad:
            record(r, {"verdict": "blocked", "capability": "other",
                       "why": "the criterion is not in its spec file, so there is nothing to observe"})
            lines.append(f"verify-owed: {r['spec']} {r['criterion']} is not in its spec file; recorded unobservable")
        batch = [r for r in batch if r not in bad]
    if not batch:
        return lines
    dr = runner.run([doit, "dispatch", "owed-verifier", sid, "--packet", pr.stdout.strip(), "--cwd", str(cwd),
                     "--project", project, "--detach"])
    if dr.returncode != 0:
        return lines + [f"verify-owed: dispatch failed for {sid}: {(dr.stderr or dr.stdout or '').strip()}"]
    return lines + [f"verify-owed: dispatched {len(batch)} check(s) in {project} as {sid}"]


def run(*, dry_run=False, observe=None, now=None, only=None, runner=None, reader=None):
    """One verifier pass. Returns the lines printed. `observe`, `runner` and
    `reader` are the test seams (an in-process observer, the `doit packet|dispatch`
    subprocess boundary, the first-reading source)."""
    now = now or datetime.now(timezone.utc)
    events = fold.read_events()
    by = fold.fold(events)[3]
    lines = []
    if not dry_run:
        lines += [f"verify-owed: timed: {x}" for x in owed_timed.run_pass(by, now, reader, events=events)]
        events = fold.read_events()
        by = fold.fold(events)[3]
    cands = candidates(by, now, set(only) if only else None)
    if observe is None:
        cands, script_lines = provision.script_rows(cands, events, ledger_path(),
                                                   lane="verifier", dry_run=dry_run)
        lines.extend(script_lines)
    if not cands:
        lines.append("verify-owed: nothing to observe")
    elif dry_run:
        lines += [f"verify-owed: DRY RUN would observe {r['spec']} {r['criterion']}" for r in cands]
    elif observe is not None:
        for r in cands:
            try:
                obs = observe(r)
            except Exception as x:  # noqa: BLE001
                obs = {"verdict": "blocked", "capability": "other", "why": f"the observer raised {type(x).__name__}"}
            lines.append(f"verify-owed: {r['spec']} {r['criterion']} -> {record(r, obs)}")
    elif _open_batch(events, now):
        lines.append("verify-owed: an owed-verifier batch is already open; nothing dispatched this run")
    else:
        import sweep_owed
        lines += dispatch_batch(cands, runner or sweep_owed.Runner(), now)
    events = fold.read_events()
    lines += file_rework_briefs(fold.fold(events)[3], now, events, dry_run=dry_run)
    for line in lines:
        print(line)
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(prog="doit verify-owed", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print what would happen; write nothing")
    ap.add_argument("--preflight", action="store_true", help="one line per stack component; exit 1 if any is absent")
    ap.add_argument("--check", action="append", metavar="SPEC/CRITERION", help="observe exactly this check (repeatable)")
    a = ap.parse_args(argv)
    if a.preflight:
        lines, ok = preflight()
        print("\n".join(lines))
        return 0 if ok else 1
    run(dry_run=a.dry_run, only=a.check)
    return 0


if __name__ == "__main__":
    sys.exit(main())
