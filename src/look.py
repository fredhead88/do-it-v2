#!/usr/bin/env python3
"""look — the babysitter's ten-minute check, as a script (L-charter-0036 R1).
`look.run(events, *, now=None, runner=None, root=None, toml_path=None)` ->
`{"briefs": [...], "answered": [...]}`; CLI: `doit look [--dry-run]`.
One `flock -n $DOIT_ROOT/look/look.lock`-guarded pass, no session, no model.
Every external read goes through the injectable `Runner`, gated by
`runner.clock() < deadline` (the 90s pass budget, AC18) — a call past the
deadline never fires, and it plus every not-yet-started reading becomes
`reading-undetermined` instead. `emit_once` is the single dedupe-and-append
path (SD3): a live `state.json["open"]` entry, or (state deleted/invalid) an
unanswered ledger `brief`, suppresses a repeat; a 30-minute cooldown after
`brief-answered` holds off re-arming (AC9) — always appends to
`L-look-local.jsonl`, the one dedupe path `tick._record()`'s checks share.
Never deploys, restarts, resumes, sends keys, installs, or deletes (SD1).
"""
import argparse, fcntl, hashlib, json, os, pathlib, re, shutil, subprocess, sys, time
from datetime import datetime, timedelta, timezone
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dispatch, fold, look_wallclock, pane_resume, panes, relay  # noqa: E402
LEDGER_NAME = "L-look-local.jsonl"
DEFAULT_TOML_PATH = pathlib.Path(__file__).resolve().parent.parent / "look.toml"
DEFAULTS = {
    "thresholds": {"tmp_high": 85, "tmp_high_clear": 75, "disk_high": 90, "disk_high_clear": 85,
                   "tmp_climbing_slope": 10, "tmp_climbing_full_within_h": 2, "pass_budget_s": 90,
                   "grades_per_shipped_high": 1.5},
    "prod": [{"project": "albert-scott", "repo": "/opt/albert-scott", "ssh_target": "root@167.71.46.51",
              "base_url": "http://127.0.0.1:8000", "version_path": "/version", "health_path": "/health"}],
    "pane_at_menu": {"codex_patterns": [], "codex_targets": []},  # codex_targets: this builder's own addition
    # L-charter-0038/L-spec-0389: master-CI polling row and the condition->class
    # map `fix_for` reads (look_wallclock.py). Both ship with the one default
    # below / empty (Assumptions) — no look condition names a class yet.
    "ci": [{"project": "albert-scott", "repo": "/opt/albert-scott", "branch": "master", "workflows": []}],
    "classes": {},
}
_PASS_LOCKED = False   # True only while `run()` holds `look.lock` (skip re-locking, SD22)
_DRY_RUN = False
STREAK = 3   # R14b/R14c: three identical consecutive failures/refusals; a module
             # constant, not a look.toml knob (Boundaries: avoids a shared-file merge)
def load_toml(toml_path=None):
    path = pathlib.Path(toml_path) if toml_path is not None else DEFAULT_TOML_PATH
    try:
        import tomllib
        with open(path, "rb") as f:
            doc = tomllib.load(f)
    except (OSError, ValueError, ImportError):
        doc = {}
    return {"thresholds": {**DEFAULTS["thresholds"], **(doc.get("thresholds") or {})},
            "prod": doc.get("prod") or DEFAULTS["prod"],
            "pane_at_menu": {**DEFAULTS["pane_at_menu"], **(doc.get("pane_at_menu") or {})},
            "ci": doc["ci"] if "ci" in doc else DEFAULTS["ci"],
            "classes": doc.get("classes") or DEFAULTS["classes"]}
class Runner:
    """Real subprocess/shutil default; a test swaps the whole boundary (AC1)."""
    def ssh(self, target, cmd, timeout):
        return subprocess.run(["ssh", target, cmd], capture_output=True, text=True, timeout=timeout).stdout
    def git(self, args, cwd, timeout):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout).stdout
    def tmux_capture(self, target, timeout):
        return subprocess.run(["tmux", "capture-pane", "-e", "-p", "-t", target], capture_output=True,
                              text=True, timeout=timeout).stdout
    def crontab_text(self, timeout):
        return subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=timeout).stdout
    def disk_usage(self, path, timeout):
        du = shutil.disk_usage(path)
        return {"total": du.total, "used": du.used, "free": du.free}
    def gh_run_list(self, repo, branch, timeout):
        return subprocess.run(["gh", "run", "list", "--branch", branch, "--event", "push", "--json",
                               "name,status,conclusion,headSha,url,updatedAt", "--limit", "100"],
                              cwd=repo, capture_output=True, text=True, timeout=timeout).stdout
    def clock(self):
        return time.monotonic()
    def sleep(self, seconds):
        time.sleep(seconds)
def _call(runner, deadline, timeout, method, *args):
    """Up to 2 attempts, each deadline-gated (AC8/AC18); (result, undetermined)."""
    fn = getattr(runner, method)
    for _ in range(2):
        if runner.clock() >= deadline: return None, True
        try: return fn(*args, timeout), False
        except Exception: continue
    return None, True
def _read_state(root):
    try: return json.loads((root / "look" / "state.json").read_text())
    except (OSError, ValueError): return None
def _write_state(root, state):
    p = root / "look" / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state))
def _with_state_lock(root, fn):
    """Skips the flock when `run()`'s own pass already holds it (SD22)."""
    if _PASS_LOCKED:
        fn()
        return
    p = root / "look" / "look.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    lf = open(p, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lf.close()
        return
    try:
        fn()
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()
def _open_ref(root, openkey, subject, events):
    st = _read_state(root)
    if st is not None: return (st.get("open") or {}).get(openkey)
    answered = {e.get("ref") for e in events if e.get("type") == "brief-answered"}
    return next((e["_src"] for e in events if e.get("type") == "brief" and e.get("subject") == subject
                and e.get("_src") not in answered), None)
def _recently_cleared(root, subject, events, cooldown_min=30):
    mine = {e["_src"] for e in events if e.get("type") == "brief" and e.get("subject") == subject}
    times = [fold.ts(e.get("ts")) for e in events if e.get("type") == "brief-answered" and e.get("ref") in mine]
    return bool(times) and (fold.NOW - max(times)).total_seconds() < cooldown_min * 60
def _why(reading):
    if isinstance(reading, dict): return "; ".join(f"{k}={v}" for k, v in list(reading.items())[:4])
    return str(reading)
def _write_brief(condition, key, owner, reading, events, root, problem=None):
    openkey, subject = f"{condition}|{key}", f"look:{condition}:{key}"
    if _open_ref(root, openkey, subject, events) is not None or _recently_cleared(root, subject, events): return None
    kv = dict(condition=condition, owner=owner, key=key, problem=problem or f"look-{condition}", reading=reading,
              measured_at=fold.NOW.isoformat(timespec="seconds"), subject=subject, why=_why(reading))
    fix = look_wallclock.fix_for(condition, root, events)
    if fix is not None: kv["fix"] = fix
    if _DRY_RUN: return {**kv, "type": "brief", "_src": "dry-run"}
    dst_dir = root / "events"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / LEDGER_NAME
    if dispatch.emit(dst, {}, "brief", **kv): return None
    src = f"{LEDGER_NAME}:{sum(1 for _ in dst.open())}"
    _with_state_lock(root, lambda: _mark(root, openkey, src))
    return {**kv, "type": "brief", "_src": src}
def _mark(root, openkey, src):
    st = _read_state(root) or {}
    st.setdefault("open", {})[openkey] = src
    _write_state(root, st)
def _clear(condition, key, why, events, root):
    openkey, subject = f"{condition}|{key}", f"look:{condition}:{key}"
    ref = _open_ref(root, openkey, subject, events)
    if ref is None: return None
    if _DRY_RUN: return {"ref": ref, "type": "brief-answered"}
    dst = root / "events" / LEDGER_NAME
    if dispatch.emit(dst, {}, "brief-answered", ref=ref, why=f"cleared: {why}"): return None
    _with_state_lock(root, lambda: _unmark(root, openkey))
    return {"ref": ref, "type": "brief-answered"}
def _unmark(root, openkey):
    st = _read_state(root)
    if st is None: return
    (st.get("open") or {}).pop(openkey, None)
    _write_state(root, st)
def emit_once(condition, key, owner, reading, events, *, root=None, toml_path=None):
    """The single dedupe-and-append path (SD3), shared by `run()` and `tick._record()`."""
    root = pathlib.Path(root) if root is not None else fold.ROOT
    return _write_brief(condition, key, owner, reading, list(events or ()), root)
def _settle(condition, key, owner, bad, reading, events, root, briefs, answered):
    if bad:
        r = _write_brief(condition, key, owner, reading, events, root)
        r and briefs.append(r)
    else:
        r = _clear(condition, key, _why(reading), events, root)
        r and answered.append(r)
def _fire_undetermined(name, events, root, briefs):
    r = _write_brief("reading-undetermined", f"undetermined:{name}", "operator", {"reading": name}, events, root)
    r and briefs.append(r)
def _check_prod(row, runner, now, deadline, single, events, root, briefs, answered):
    project = row.get("project", "prod")
    mkey, hkey = ("prod sha" if single else f"{project} prod sha"), f"health:{project}"
    tip, tip_und = _call(runner, deadline, 10, "git", ["rev-parse", "HEAD"], row["repo"])
    body, body_und = _call(runner, deadline, 10, "ssh", row["ssh_target"], f'curl -sf {row["base_url"]}{row["version_path"]}')
    if tip_und or body_und:
        _fire_undetermined("merge-undeployed", events, root, briefs)
    else:
        behind, log_und = _merge_status(row, runner, now, deadline, tip, body)
        if log_und:
            _fire_undetermined("merge-undeployed", events, root, briefs)
        else:
            _settle("merge-undeployed", mkey, "deployer", behind, {"tip": str(tip).strip()},
                    events, root, briefs, answered)
    status, reads = _health_status(row, runner, deadline)
    if status is None:
        _fire_undetermined("prod-unhealthy", events, root, briefs)
    else:
        _settle("prod-unhealthy", hkey, "deployer", not status, {"reads": reads}, events, root, briefs, answered)
def _merge_status(row, runner, now, deadline, tip, body):
    try:
        deployed, tipsha = str(json.loads(body)["sha"]), str(tip).strip()
    except Exception: return None, True
    if tipsha.startswith(deployed) or deployed.startswith(tipsha): return False, False
    out, und = _call(runner, deadline, 10, "git", ["log", "--format=%ct", f"{deployed}..{tipsha}"], row["repo"])
    if und: return None, True
    try:
        times = [int(x) for x in out.split() if x.strip()]
    except Exception: return None, True
    if not times: return False, False
    return (now.timestamp() - min(times)) / 60 >= 15, False
_CHECKOUT = pathlib.Path(__file__).resolve().parent.parent   # the checkout root `up.HERE.parent` resolves to
SUPERVISOR_WEDGE_HOURS = 3   # SD6: a literal constant — no look.toml knob names this
def _merge_after(runner, deadline, checkout, sha):
    """Sorted-ascending committer epoch-seconds for every first-parent commit
    strictly after `sha` up to HEAD. Mirrors `_merge_status`'s own contract:
    `(None, True)` on any failure or elapsed deadline. `--first-parent` is the
    load-bearing flag: a bare `git log -1 --before=@ts` reconstruction can
    return a `--no-ff` merge's branch-side commit — whose committer date can
    predate the merge landing it on HEAD — as "HEAD at ts" when it never was."""
    out, und = _call(runner, deadline, 10, "git",
                     ["log", "--first-parent", "--format=%ct", f"{sha}..HEAD"], checkout)
    if und: return None, True
    try:
        times = sorted(int(x) for x in out.split() if x.strip())
    except Exception: return None, True
    return times, False
def _check_supervisor_stale_code(events, now, runner, deadline, root, briefs, answered):
    """R2 point 7: for each of planner/relay/executor, the newest LIVE-pid
    `supervisor-code` row (`boot`) is checked two ways — WEDGED (the oldest
    first-parent commit `boot["sha"]` lacks is itself >= SUPERVISOR_WEDGE_HOURS
    old: an unchanged sha, however old `boot` is, never wedges) and CYCLED
    (the newest child-start event at or after `boot["ts"]` already carried a
    `code_sha` that was ALREADY stale the moment it was written). No live pid
    for a kind: skipped entirely — neither briefed nor cleared."""
    for kind in ("planner", "relay", "executor"):
        rows = [e for e in events if e.get("type") == "supervisor-code" and e.get("kind") == kind]
        live = [e for e in rows if panes.alive(e.get("pid"))]
        if not live: continue
        boot = max(live, key=lambda e: fold.ts(e.get("ts")))
        times, und = _merge_after(runner, deadline, _CHECKOUT, boot["sha"])
        if und:
            _fire_undetermined("supervisor-stale-code", events, root, briefs)
            continue
        wedged = bool(times) and (now.timestamp() - times[0]) / 3600 >= SUPERVISOR_WEDGE_HOURS
        start_type = "planner-started" if kind == "planner" else "spawn-started"
        boot_ts = fold.ts(boot.get("ts"))
        starts = [e for e in events if e.get("type") == start_type and "code_sha" in e
                 and fold.ts(e.get("ts")) >= boot_ts and (kind == "planner" or e.get("role") == kind)]
        cycled, skip = False, False
        if starts:
            start = max(starts, key=lambda e: fold.ts(e.get("ts")))
            if start["code_sha"] == boot["sha"]:
                times2, und2 = times, und
            else:
                times2, und2 = _merge_after(runner, deadline, _CHECKOUT, start["code_sha"])
            if und2:
                _fire_undetermined("supervisor-stale-code", events, root, briefs)
                skip = True
            else:
                cycled = bool(times2) and times2[0] <= fold.ts(start["ts"]).timestamp() + 1
        if skip: continue
        reading = {"wedged": wedged, "cycled": cycled, "sha": boot["sha"]}
        _settle("supervisor-stale-code", kind, "thinker", wedged or cycled, reading,
               events, root, briefs, answered)
def _health_status(row, runner, deadline):
    reads = []
    for i in range(3):
        body, und = _call(runner, deadline, 10, "ssh", row["ssh_target"],
                          f'curl -s -o /dev/null -w "%{{http_code}}" {row["base_url"]}{row["health_path"]}')
        if und: return None, reads
        code = str(body).strip()
        reads.append(code)
        if code == "200": return True, reads
        if i < 2:
            runner.sleep(10)
    return False, reads
def _check_packet_unserved(events, root, briefs):
    claim_sec = int(os.environ.get("DOIT_SEAT_CLAIM_SEC", 300))
    for row in relay.pending_packets(events, root=root, served_by=None):
        sid = row["spawn"]
        if (root / "seat" / f"{sid}.claimed").exists() or row["age_min"] * 60 <= claim_sec: continue
        r = _write_brief("packet-unserved", sid, "relay", row, events, root)
        r and briefs.append(r)
    starts = {e.get("spawn") for e in events if e.get("type") in ("spawn-started", "build-started")}
    seatdir = root / "seat"
    if not seatdir.is_dir(): return
    now_ts = time.time()
    for p in sorted(seatdir.glob("*.packet.md")):
        sid = p.name[: -len(".packet.md")]
        if sid in starts or (seatdir / f"{sid}.claimed").exists(): continue
        if (seatdir / f"{sid}.output.json").exists() or (seatdir / f"{sid}.result.json").exists(): continue
        try:
            age_s = now_ts - p.stat().st_mtime
        except OSError: continue
        if age_s > 24 * 3600 or age_s <= claim_sec: continue
        r = _write_brief("packet-unserved", sid, "executor", {"started": False}, events, root)
        r and briefs.append(r)
def _read_jsonl(path):
    try:
        text = pathlib.Path(path).read_text()
    except OSError: return []
    out = []
    for line in text.splitlines():
        if not line.strip(): continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError: continue
        if isinstance(e, dict):
            out.append(e)
    return out
def _ask_user_question_id(entry):
    return next((c.get("id") for c in (entry.get("message") or {}).get("content") or []
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "AskUserQuestion"), None)
def _has_tool_result(entries, after_idx, tool_use_id):
    return any(isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") == tool_use_id
              for en in entries[after_idx + 1:] for c in (en.get("message") or {}).get("content") or [])
def _check_pane_at_menu_claude(events, root, briefs, sessions_dir=None, projects_dir=None):
    live = panes.live_panes(sessions_dir=sessions_dir, events=events)
    idle = {p["name"] for p in live if p.get("status") == "idle" and panes.ledger_role(p.get("name"))}
    joined = pane_resume._live_join(sessions_dir, idle)
    for pane, meta in sorted(joined.items()):
        sid, target = meta.get("sessionId"), meta.get("tmux")
        if not sid or not target: continue
        tp = pane_resume._find_transcript(sid, projects_dir)
        if tp is None: continue
        entries = _read_jsonl(tp)
        idx = max((i for i, en in enumerate(entries) if en.get("type") == "assistant"), default=None)
        if idx is None: continue
        last = entries[idx]
        cls = pane_resume._classify(last)
        key = f"{pane}:{last.get('uuid')}"
        if cls in ("quota", "auth"):
            r = _write_brief("pane-at-menu", key, "operator", {"class": cls}, events, root)
            r and briefs.append(r)
            continue
        tool_use_id = _ask_user_question_id(last)
        if tool_use_id and not _has_tool_result(entries, idx, tool_use_id):
            r = _write_brief("pane-at-menu", key, "thinker", {"class": "menu"}, events, root)
            r and briefs.append(r)
def _check_pane_at_menu_codex(cfg, runner, deadline, events, root, briefs):
    patterns = cfg["pane_at_menu"].get("codex_patterns") or []
    targets = cfg["pane_at_menu"].get("codex_targets") or []
    for target in targets:
        text, und = _call(runner, deadline, 5, "tmux_capture", target)
        if und or not text: continue
        tail = text.splitlines()[-20:]
        for pat in patterns:
            hit = next((ln for ln in tail if re.search(pat, ln)), None)
            if hit is not None:
                key = f"{target}:{hashlib.sha256(hit.encode()).hexdigest()[:12]}"
                r = _write_brief("pane-at-menu", key, "thinker", {"line": hit}, events, root)
                r and briefs.append(r)
                break
def _check_pane_dead(events, now, root, briefs):
    ended = {e.get("pane") for e in events if e.get("type") == "role-ended"}
    ended |= {e.get("planner") for e in events if e.get("type") == "planner-ended"}
    ended |= {e.get("spawn") for e in events if e.get("type") in ("spawn-done", "spawn-failed", "spawn-stale")}
    for e in events:
        if e.get("type") != "role-launched" or e.get("pane") in ended: continue
        if (now - fold.ts(e.get("ts"))).total_seconds() <= 60: continue
        try:
            alive = panes._is_live(e.get("pid"), {"procStart": e.get("proc_start")})
        except Exception: continue
        if alive: continue
        r = _write_brief("pane-dead", e.get("pane"), "operator", {"pid": e.get("pid"), "host": e.get("host")}, events, root)
        r and briefs.append(r)
def _tmp_slope(readings):
    """(slope pts/hour or None, trailing non-positive streak); needs >=2 points >=8min apart (AC14)."""
    pts = [(fold.ts(r["ts"]), r["tmp_pct"]) for r in readings]
    slopes = []
    for i in range(1, len(pts)):
        gap_min = (pts[i][0] - pts[i - 1][0]).total_seconds() / 60
        if gap_min >= 8:
            slopes.append((pts[i][1] - pts[i - 1][1]) / (gap_min / 60))
    if not slopes: return None, 0
    streak = 0
    for s in reversed(slopes):
        if s > 0: break
        streak += 1
    return slopes[-1], streak
def _check_disk_tmp(runner, root, th, now, deadline, events, briefs, answered):
    st = _read_state(root) or {}
    readings = list(st.get("readings") or [])
    tmp, tmp_und = _call(runner, deadline, 2, "disk_usage", "/tmp")
    if tmp_und:
        _fire_undetermined("tmp-high", events, root, briefs)
        _fire_undetermined("tmp-climbing", events, root, briefs)
    else:
        tmp_pct = tmp["used"] / tmp["total"] * 100
        bad, clear = tmp_pct >= th["tmp_high"], tmp_pct < th["tmp_high_clear"]
        if bad or clear:
            _settle("tmp-high", "tmp", "operator", bad, {"pct": tmp_pct}, events, root, briefs, answered)
        readings = (readings + [{"ts": now.isoformat(timespec="seconds"), "tmp_pct": tmp_pct}])[-12:]
        if not _DRY_RUN:
            _with_state_lock(root, lambda: _save_readings(root, readings))
        slope, streak = _tmp_slope(readings)
        if slope is None:
            _fire_undetermined("tmp-climbing", events, root, briefs)
        else:
            climbing = slope > 0 and (100 - tmp_pct) / slope <= th["tmp_climbing_full_within_h"]
            if climbing or streak >= 2:
                _settle("tmp-climbing", "tmp", "operator", climbing, {"slope": slope},
                        events, root, briefs, answered)
    disk, disk_und = _call(runner, deadline, 2, "disk_usage", "/")
    if disk_und:
        _fire_undetermined("disk-high", events, root, briefs)
    else:
        disk_pct = disk["used"] / disk["total"] * 100
        bad, clear = disk_pct >= th["disk_high"], disk_pct < th["disk_high_clear"]
        if bad or clear:
            _settle("disk-high", "disk", "operator", bad, {"pct": disk_pct}, events, root, briefs, answered)
def _save_readings(root, readings):
    st = _read_state(root) or {}
    st["readings"] = readings
    _write_state(root, st)
def _check_crons(text, und, events, root, briefs, crons_manifest=None, crond_dir=None):
    """`text`/`und` are the ONE shared `runner.crontab_text()` read `run()`
    performs (via `_call`) for both this check and `_check_cron_unwrapped` —
    neither re-reads it, so the "2 attempts before giving up" retry contract
    stays a single pair of attempts per pass, not one pair per cron check."""
    try:
        import crons
    except ImportError:
        _fire_undetermined("crons", events, root, briefs)
        return
    if und:
        _fire_undetermined("crons", events, root, briefs)
        return
    try:
        missing = crons.check(manifest=crons_manifest, crontab_text=text, crond_dir=crond_dir) or []
    except Exception:
        _fire_undetermined("crons", events, root, briefs)
        return
    for row in missing:
        name = row.get("name") if isinstance(row, dict) else str(row)
        r = _write_brief("cron-missing", name, "thinker", row, events, root)
        r and briefs.append(r)


def _check_cron_unwrapped(text, und, events, root, briefs, answered, crons_manifest=None, crond_dir=None):
    """R14a/R14c: a row whose installed line is not wrapped through `cron-run`
    briefs `cron-unwrapped`; a row that clears (wrapped, or no longer matched —
    absence is `cron-missing`'s, not this brief's to hold open) is cleared.
    Mirrors `_check_crons`'s own degrade-to-undetermined shape: a raise here
    (import failure, a fake `crons` lacking the new API, a bad read) never
    reads as clean. Shares `_check_crons`'s own `text`/`und` read (see there)."""
    if und:
        _fire_undetermined("cron-unwrapped", events, root, briefs)
        return
    try:
        import crons
        names = set(crons.unwrapped(manifest=crons_manifest, crontab_text=text, crond_dir=crond_dir) or [])
        all_rows = crons.rows(crons_manifest)
    except Exception:
        _fire_undetermined("cron-unwrapped", events, root, briefs)
        return
    for row in all_rows:
        name = row["name"]
        if name in names:
            r = _write_brief("cron-unwrapped", name, "thinker", {"name": name}, events, root)
            r and briefs.append(r)
        else:
            r = _clear("cron-unwrapped", name, "wrapped", events, root)
            r and answered.append(r)


def _read_cron_store(path):
    """The store's rows, oldest first — `None` when the store file is absent
    (the job has never run, or never wrapped yet) so the caller can skip it
    entirely, distinct from `[]` (a readable-but-empty store)."""
    try:
        text = path.read_text()
    except OSError:
        return None
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _check_cron_runs(now, events, root, briefs, answered, crons_manifest=None):
    """R14b/R14c, per cron row (`crons.rows(crons_manifest)`; the repo's own
    `crons.toml` when `None`): `repeat-failure` on the newest `STREAK` `end`
    rows all sharing one non-empty `error_class`; `cron-overrun` on the newest
    row (an `end` above its limit, or a `start` — an open run — older than its
    limit) — limit is `max_runtime_s`, else `crons.interval_s(schedule)`;
    neither readable makes the row permanently `bad` (`reading: "undetermined"`,
    R14c: never clean). A store file whose name has no manifest row is never
    read (we only iterate manifest rows); a manifest row with no store file
    yet is skipped, not briefed."""
    try:
        import crons, cron_run
        the_rows = crons.rows(crons_manifest)
    except Exception:
        _fire_undetermined("cron-runs", events, root, briefs)
        return
    for row in the_rows:
        name = row["name"]
        rows_ = _read_cron_store(cron_run.store_path(name, root))
        if not rows_:
            continue
        ends = [r for r in rows_ if r.get("ev") == "end"]
        last3 = ends[-STREAK:]
        classes = {r.get("error_class") for r in last3}
        bad_fail = (len(last3) == STREAK and len(classes) == 1
                   and next(iter(classes)) not in (None, ""))
        reading_fail = {"name": name, "error_class": last3[-1].get("error_class") if last3 else None,
                        "consecutive": len(last3), "error_tail": last3[-1].get("error_tail", "") if last3 else ""}
        _settle("repeat-failure", name, "thinker", bool(bad_fail), reading_fail, events, root, briefs, answered)
        last = rows_[-1]
        limit = row.get("max_runtime_s")
        if limit is None:
            try:
                limit = crons.interval_s(row["schedule"])
            except Exception:
                limit = None
        if last.get("ev") == "end":
            duration, open_run = last.get("duration_s") or 0, False
        else:
            try:
                duration = now.timestamp() - fold.ts(last.get("ts")).timestamp()
            except Exception:
                duration = 0
            open_run = True
        if limit is None:
            bad_over, reading_over = True, {"name": name, "reading": "undetermined", "open": open_run}
        else:
            bad_over = duration > limit
            reading_over = {"name": name, "duration_s": duration, "limit": limit, "open": open_run}
        _settle("cron-overrun", name, "thinker", bad_over, reading_over, events, root, briefs, answered)


def _dispatch_role_of_start(e):
    """`build-started` never carries `role` (it IS the builder's own start
    event); every other start event names its role directly."""
    if e.get("type") == "build-started":
        return "builder"
    if e.get("type") == "spawn-started":
        return e.get("role")
    return None


def _dispatch_class(e):
    """`reason` when present; else the `why` text up to the first `:`, at most
    40 characters, with digits removed (SD-R14-1's dispatch analogue)."""
    reason = e.get("reason")
    if reason:
        return reason
    why = (e.get("why") or "").split(":", 1)[0]
    return re.sub(r"\d+", "", why)[:40]


def _check_dispatch_failures(events, root, briefs, answered):
    """R14b for dispatch roles: the newest `STREAK` terminal events
    (`spawn-done`/`spawn-failed`) of a role — found by joining each terminal's
    `spawn` to the start event of the SAME `spawn` (never a `role` field on the
    terminal itself) — alarm `repeat-failure` (keyed by role) when all
    `STREAK` are `spawn-failed` with the same class. A terminal with no start
    of its own `spawn` is a refusal: skipped, never counted, never
    streak-breaking."""
    starts = {}
    for e in events:
        role = _dispatch_role_of_start(e)
        if role:
            starts[e.get("spawn")] = role
    by_role = {}
    for e in events:
        if e.get("type") not in ("spawn-done", "spawn-failed"):
            continue
        role = starts.get(e.get("spawn"))
        if role is None:
            continue  # a refusal — no start of its own spawn
        by_role.setdefault(role, []).append(e)
    for role, evs in by_role.items():
        newest = sorted(evs, key=lambda e: fold.ts(e.get("ts")))[-STREAK:]
        bad = len(newest) == STREAK and all(e.get("type") == "spawn-failed" for e in newest)
        classes = {_dispatch_class(e) for e in newest} if bad else set()
        bad = bad and len(classes) == 1
        reading = {"role": role, "consecutive": len(newest),
                  "class": next(iter(classes), None), "why": newest[-1].get("why") if newest else None}
        _settle("repeat-failure", role, "thinker", bad, reading, events, root, briefs, answered)
def _is_spec_writer_spawn(sid):
    parts = sid.split("-")
    return len(parts) >= 3 and "-".join(parts[1:-1]) == "spec-writer"
def _mtime_within(p, window):
    try: return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) >= window
    except OSError: return False
def _resolve(path, root):
    p = pathlib.Path(path)
    return p if p.is_absolute() else root / p
def _check_spec_misrouted(events, root, briefs):
    spec_path_fn = getattr(dispatch, "spec_path", None)   # a REAL-clock window, like AC5b's mtime check
    window = datetime.now(timezone.utc) - timedelta(hours=24)
    seatdir = root / "seat"
    starts = {e.get("spawn"): e for e in events if e.get("type") == "spawn-started"}
    cmds = [p for p in (sorted(seatdir.glob("*.cmd.json")) if seatdir.is_dir() else [])
            if _is_spec_writer_spawn(p.name[: -len(".cmd.json")]) and _mtime_within(p, window)]
    writes = [e for e in events if e.get("type") == "spec-written" and fold.ts(e.get("ts")) >= window]
    if spec_path_fn is None:
        if cmds or writes:
            r = _write_brief("reading-undetermined", "undetermined:spec-misrouted", "operator", {"reason": "dispatch.spec_path missing"}, events, root)
            r and briefs.append(r)
        return
    def brief(key, reading):
        r = _write_brief("spec-misrouted", key, "executor", reading, events, root)
        r and briefs.append(r)
    for p in cmds:
        sid = p.name[: -len(".cmd.json")]
        try:
            cmd = json.loads(p.read_text())
        except (OSError, ValueError):
            cmd = {}
        spawn_ev = starts.get(sid)
        subject, path = (spawn_ev.get("subject") if spawn_ev else None), cmd.get("path")
        if subject is None:
            brief(sid, {"path": path, "subject": None})
            continue
        want, have = _resolve(str(spec_path_fn(subject)), root), (_resolve(path, root) if path else None)
        if have is None or have != want:
            brief(subject, {"path": path, "want": str(want)})
    for e in writes:
        spec, path = e.get("spec"), e.get("path")
        if not spec: continue
        want, have = _resolve(str(spec_path_fn(spec)), root), (_resolve(path, root) if path else None)
        if have is None or have != want:
            brief(spec, {"path": path, "want": str(want)})
def _check_dispatchable_stale(events, now, root, briefs):
    """L-charter-0042/L-spec-0427, SD4: `autodispatch.candidates` folds
    `events` itself (`look.run` takes none), and anything `dispatchable`, or
    `blocked` specifically on a standing `autodispatch-failed`, whose `since`
    is more than 30 minutes old briefs `spec-dispatchable-stale` (owner
    `executor`) — `seat-wait` never alarms (it is legitimately waiting on
    capacity, SD2). `reading` is `dispatch-failed:<reason>` for the standing-
    failure case, `tick-not-running` for a structurally-fine one simply never
    picked up. Wrapped in the SAME broad `try/except Exception` shape
    `_check_wallclock` uses: a raise degrades to one `reading-undetermined`
    (`dispatch-stale`), nothing else."""
    try:
        import autodispatch
        specs, _, _, _ = fold.fold(events)
        rows = autodispatch.candidates(events, specs, now)
    except Exception:
        _fire_undetermined("dispatch-stale", events, root, briefs)
        return
    seen = set()
    for row in rows:
        sid = row["spec"]
        seen.add(sid)
        alarmable = row["status"] == "dispatchable" or (row["status"] == "blocked" and row.get("_dispatch_failed"))
        since_dt = fold.ts(row["since"])
        age_min = (now - since_dt).total_seconds() / 60
        bad = alarmable and age_min > 30
        reading = (f"dispatch-failed:{row['reason']}" if row.get("_dispatch_failed") else "tick-not-running") \
            if alarmable else "not-stale"
        _settle("spec-dispatchable-stale", sid, "executor", bad, reading, events, root, briefs, [])
    # AC23: a spec whose fold state has moved off `written` no longer appears
    # among `rows` at all — clear any standing brief it still carries.
    for sid, s in specs.items():
        if sid in seen or s.get("state") == "written":
            continue
        _clear("spec-dispatchable-stale", sid, "left written", events, root)


def _check_grades_per_shipped(events, now, root, briefs, answered, th):
    """L-spec-0482/R12.d: `fold.grades_per_shipped`'s trailing-24h ratio above
    `[thresholds] grades_per_shipped_high` (default 1.5) briefs
    `grades-per-shipped-high`, owner `thinker` — the same fire/quiet/clear
    shape `_settle` already gives every other threshold check here. `None`
    (nothing shipped in the window) is never bad."""
    ratio, specs_n, runs_n = fold.grades_per_shipped(events, now)
    bad = ratio is not None and ratio > th["grades_per_shipped_high"]
    _settle("grades-per-shipped-high", "grades-per-shipped", "thinker", bad,
            {"ratio": ratio, "specs": specs_n, "runs": runs_n}, events, root, briefs, answered)
def _check_wallclock(events, runner, now, root, cfg, deadline, dry_run, briefs):
    """The four wall-clock checks (L-charter-0038 R6), wrapped in the SAME
    lazily-guarded broad `except Exception` shape `_check_crons` uses around
    `crons.check()` (Seams) — a raise here degrades to one `reading-undetermined`
    (key `undetermined:wallclock`) and this pass's own "wallclock" key is `[]`."""
    try:
        result = look_wallclock.run(events, runner, now, root, cfg, deadline, _write_brief, dry_run)
    except Exception:
        _fire_undetermined("wallclock", events, root, briefs)
        return []
    briefs.extend(result["briefs"])
    return result["events"]
def run(events, *, now=None, runner=None, root=None, toml_path=None, dry_run=False,
        crons_manifest=None, crond_dir=None):
    global _PASS_LOCKED, _DRY_RUN
    now = now or datetime.now(timezone.utc)
    root = pathlib.Path(root) if root is not None else fold.ROOT
    runner = runner or Runner()
    events = list(events or ())
    cfg = load_toml(toml_path)
    th = cfg["thresholds"]
    lockdir = root / "look"
    lockdir.mkdir(parents=True, exist_ok=True)
    lockf = open(lockdir / "look.lock", "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lockf.close()
        return {"briefs": [], "answered": []}
    _PASS_LOCKED, _DRY_RUN = True, dry_run
    look_wallclock._CLASSES = cfg.get("classes") or {}
    briefs, answered = [], []
    wallclock_events = []
    try:
        deadline = runner.clock() + th["pass_budget_s"]
        single = len(cfg["prod"]) == 1
        for row in cfg["prod"]:
            _check_prod(row, runner, now, deadline, single, events, root, briefs, answered)
        _check_packet_unserved(events, root, briefs)
        _check_pane_at_menu_claude(events, root, briefs)
        _check_pane_at_menu_codex(cfg, runner, deadline, events, root, briefs)
        _check_pane_dead(events, now, root, briefs)
        _check_supervisor_stale_code(events, now, runner, deadline, root, briefs, answered)
        _check_disk_tmp(runner, root, th, now, deadline, events, briefs, answered)
        crontab_text, crontab_und = _call(runner, deadline, 5, "crontab_text")
        _check_crons(crontab_text, crontab_und, events, root, briefs, crons_manifest, crond_dir)
        _check_cron_unwrapped(crontab_text, crontab_und, events, root, briefs, answered, crons_manifest, crond_dir)
        _check_cron_runs(now, events, root, briefs, answered, crons_manifest)
        _check_dispatch_failures(events, root, briefs, answered)
        _check_spec_misrouted(events, root, briefs)
        _check_dispatchable_stale(events, now, root, briefs)
        _check_grades_per_shipped(events, now, root, briefs, answered, th)
        wallclock_events = _check_wallclock(events, runner, now, root, cfg, deadline, dry_run, briefs)
        r = _clear("look-stale", "look-stale", "fresh pass", events, root)
        r and answered.append(r)
        if not dry_run:
            st = _read_state(root) or {}
            st["last_pass"] = now.isoformat(timespec="seconds")
            _write_state(root, st)
    finally:
        _PASS_LOCKED, _DRY_RUN = False, False
        look_wallclock._CLASSES = {}
        try:
            fcntl.flock(lockf, fcntl.LOCK_UN)
        except OSError: pass
        lockf.close()
    return {"briefs": briefs, "answered": answered, "wallclock": wallclock_events}
def main(argv=None):
    ap = argparse.ArgumentParser(prog="doit look")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    result = run(fold.read_events(), dry_run=a.dry_run)
    print(f"look: {len(result['briefs'])} brief(s), {len(result['answered'])} answered")
    return 0
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
