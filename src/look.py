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
import dispatch, fold, pane_resume, panes, relay  # noqa: E402
LEDGER_NAME = "L-look-local.jsonl"
DEFAULT_TOML_PATH = pathlib.Path(__file__).resolve().parent.parent / "look.toml"
DEFAULTS = {
    "thresholds": {"tmp_high": 85, "tmp_high_clear": 75, "disk_high": 90, "disk_high_clear": 85,
                   "tmp_climbing_slope": 10, "tmp_climbing_full_within_h": 2, "pass_budget_s": 90},
    "prod": [{"project": "albert-scott", "repo": "/opt/albert-scott", "ssh_target": "root@167.71.46.51",
              "base_url": "http://127.0.0.1:8000", "version_path": "/version", "health_path": "/health"}],
    "pane_at_menu": {"codex_patterns": [], "codex_targets": []},  # codex_targets: this builder's own addition
}
_PASS_LOCKED = False   # True only while `run()` holds `look.lock` (skip re-locking, SD22)
_DRY_RUN = False
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
            "pane_at_menu": {**DEFAULTS["pane_at_menu"], **(doc.get("pane_at_menu") or {})}}
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
def _write_brief(condition, key, owner, reading, events, root):
    openkey, subject = f"{condition}|{key}", f"look:{condition}:{key}"
    if _open_ref(root, openkey, subject, events) is not None or _recently_cleared(root, subject, events): return None
    kv = dict(condition=condition, owner=owner, key=key, problem=f"look-{condition}", reading=reading,
              measured_at=fold.NOW.isoformat(timespec="seconds"), subject=subject, why=_why(reading))
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
    for row in relay.pending_packets(events, root=root):
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
def _check_crons(runner, deadline, events, root, briefs):
    try:
        import crons
    except ImportError:
        _fire_undetermined("crons", events, root, briefs)
        return
    text, und = _call(runner, deadline, 5, "crontab_text")
    if und:
        _fire_undetermined("crons", events, root, briefs)
        return
    try:
        missing = crons.check(crontab_text=text) or []
    except Exception:
        _fire_undetermined("crons", events, root, briefs)
        return
    for row in missing:
        name = row.get("name") if isinstance(row, dict) else str(row)
        r = _write_brief("cron-missing", name, "thinker", row, events, root)
        r and briefs.append(r)
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
def run(events, *, now=None, runner=None, root=None, toml_path=None, dry_run=False):
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
    briefs, answered = [], []
    try:
        deadline = runner.clock() + th["pass_budget_s"]
        single = len(cfg["prod"]) == 1
        for row in cfg["prod"]:
            _check_prod(row, runner, now, deadline, single, events, root, briefs, answered)
        _check_packet_unserved(events, root, briefs)
        _check_pane_at_menu_claude(events, root, briefs)
        _check_pane_at_menu_codex(cfg, runner, deadline, events, root, briefs)
        _check_pane_dead(events, now, root, briefs)
        _check_disk_tmp(runner, root, th, now, deadline, events, briefs, answered)
        _check_crons(runner, deadline, events, root, briefs)
        _check_spec_misrouted(events, root, briefs)
        r = _clear("look-stale", "look-stale", "fresh pass", events, root)
        r and answered.append(r)
        if not dry_run:
            st = _read_state(root) or {}
            st["last_pass"] = now.isoformat(timespec="seconds")
            _write_state(root, st)
    finally:
        _PASS_LOCKED, _DRY_RUN = False, False
        try:
            fcntl.flock(lockf, fcntl.LOCK_UN)
        except OSError: pass
        lockf.close()
    return {"briefs": briefs, "answered": answered}
def main(argv=None):
    ap = argparse.ArgumentParser(prog="doit look")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    result = run(fold.read_events(), dry_run=a.dry_run)
    print(f"look: {len(result['briefs'])} brief(s), {len(result['answered'])} answered")
    return 0
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
