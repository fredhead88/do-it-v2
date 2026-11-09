#!/usr/bin/env python3
"""look — the babysitter's ten-minute check, as a script (L-charter-0036 R1).
`look.run(events, *, now=None, runner=None, root=None, toml_path=None)` ->
`{"briefs": [...], "answered": [...]}`; CLI: `doit look [--dry-run]`.
One `flock -n $DOIT_ROOT/look/look.lock`-guarded pass, no session, no model.
Every external read goes through the injectable Runner and 90s budget.
All look/tick alerts share daily condition digests, capped at 50 briefs/day.
Each brief links to look/digests/YYYY-MM-DD.json, which accumulates affected
keys and suppression counters across subsequent passes under look.lock.
Never deploys, restarts, resumes, sends keys, installs, or deletes (SD1).
"""
import argparse, fcntl, hashlib, json, os, pathlib, re, shutil, subprocess, sys, time
from datetime import datetime, timedelta, timezone
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dispatch, fold, look_omens, look_digest, look_wallclock, pane_resume, panes, relay  # noqa: E402
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
    "omens": look_omens.DEFAULTS,
    # L-charter-0042/L-spec-0484, R13(c): the same `[push]` table
    # `push_origin.targets()` reads — kept here too (not only in
    # `push_origin.py`) so `_check_origin_behind` shares the identical
    # defaults without importing `push_origin` at module scope (Constraints:
    # `push_origin` imports `look`, so the reverse import stays lazy).
    "push": {"projects": ["do-it-v2", "albert-scott"],
             "main": {"do-it-v2": "main", "albert-scott": "master"},
             "path": {}},
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
    push_doc = doc.get("push") or {}
    return {"thresholds": {**DEFAULTS["thresholds"], **(doc.get("thresholds") or {})},
            "prod": doc.get("prod") or DEFAULTS["prod"],
            "pane_at_menu": {**DEFAULTS["pane_at_menu"], **(doc.get("pane_at_menu") or {})},
            "ci": doc["ci"] if "ci" in doc else DEFAULTS["ci"],
            "classes": doc.get("classes") or DEFAULTS["classes"],
            "omens": {**look_omens.DEFAULTS, **(doc.get("omens") or {})},
            "push": {"projects": push_doc.get("projects") or DEFAULTS["push"]["projects"],
                     "main": {**DEFAULTS["push"]["main"], **(push_doc.get("main") or {})},
                     "path": push_doc.get("path") or {}}}


def push_checkout(cfg, name):
    """Resolve the checkout shared by the push pass and origin-behind alarm."""
    path = ((cfg.get("push") or {}).get("path") or {}).get(name)
    return pathlib.Path(path) if path else fold.ROOT / "repos" / name


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
        proc = subprocess.run(["crontab", "-l"], capture_output=True, timeout=timeout)
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, proc.args)
        return proc.stdout.decode("utf-8")
    def disk_usage(self, path, timeout):
        du = shutil.disk_usage(path)
        return {"total": du.total, "used": du.used, "free": du.free}
    def gh_run_list(self, repo, branch, timeout):
        return subprocess.run(["gh", "run", "list", "--branch", branch, "--event", "push", "--json",
                               "name,status,conclusion,headSha,url,updatedAt", "--limit", "100"],
                              cwd=repo, capture_output=True, text=True, timeout=timeout).stdout
    def dispatch_processes(self, role, subject, timeout):
        pattern = re.escape(f"dispatch.py {role} {subject}")
        proc = subprocess.run(["pgrep", "-af", "--", pattern], capture_output=True,
                              text=True, timeout=timeout)
        if proc.returncode not in (0, 1):
            raise OSError("process reading unavailable")
        return proc.stdout
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
    return next((e["_src"] for e in events if e.get("type") == "brief" and (e.get("subject") == subject or any(
                    f"{e.get('condition')}|{i.get('key')}" == openkey for i in e.get("items", [])))
                and e.get("_src") not in answered), None)
def _recently_cleared(root, subject, events, cooldown_min=30):
    mine = {e["_src"] for e in events if e.get("type") == "brief" and e.get("subject") == subject}
    times = [fold.ts(e.get("ts")) for e in events if e.get("type") == "brief-answered" and e.get("ref") in mine]
    return bool(times) and (fold.NOW - max(times)).total_seconds() < cooldown_min * 60
def _why(reading):
    if isinstance(reading, dict): return "; ".join(f"{k}={v}" for k, v in list(reading.items())[:4])
    return str(reading)
_DIGEST = None

def _finish_digest(digest):
    def _emit_digest(brief):
        kv = {k: v for k, v in brief.items() if k != "type"}
        condition = brief["condition"]
        if condition.startswith("omen-"):
            rule = condition.split(":", 1)[0].removeprefix("omen-")
            fix = look_omens.fix_for(rule, digest.root, digest.events, look_wallclock._CLASSES)
        else:
            fix = look_wallclock.fix_for(condition, digest.root, digest.events)
        if fix is not None:
            kv["fix"] = fix
            brief["fix"] = fix
        dst = digest.root / "events" / LEDGER_NAME
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dispatch.emit(dst, {}, "brief", **kv):
            return None
        with dst.open() as stream:
            return f"{LEDGER_NAME}:{sum(1 for _ in stream)}"
    open_refs = set(((_read_state(digest.root) or {}).get("open") or {}).values())
    digest.finish(_emit_digest, lambda key, ref: _mark(digest.root, key, ref))
    if not digest.dry_run:
        for condition, key in digest.seen:
            ref = digest.data['conditions'][condition].get('ref')
            if ref in open_refs:
                _mark(digest.root, f'{condition}|{key}', ref)


def _write_brief(condition, key, owner, reading, events, root, problem=None):
    if _DIGEST is not None:
        return _DIGEST.add(condition, key, owner, reading, problem)
    result = None
    def write():
        nonlocal result
        digest = look_digest.Digest(root, events, fold.NOW, dry_run=_DRY_RUN)
        result = digest.add(condition, key, owner, reading, problem)
        _finish_digest(digest)
    _with_state_lock(root, write)
    return result
def _mark(root, openkey, src):
    st = _read_state(root) or {}
    st.setdefault("open", {})[openkey] = src
    _write_state(root, st)
def _clear(condition, key, why, events, root):
    openkey, subject = f"{condition}|{key}", f"look:{condition}:{key}"
    ref = _open_ref(root, openkey, subject, events)
    if ref is None: return None
    st = _read_state(root) or {}
    others = [k for k, v in (st.get("open") or {}).items() if v == ref and k != openkey]
    if others:
        if not _DRY_RUN:
            _with_state_lock(root, lambda: _unmark(root, openkey))
        return None
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
DEPLOY_GRACE_MIN = 90
def _merge_status(row, runner, now, deadline, tip, body):
    try:
        deployed, tipsha = str(json.loads(body)["sha"]), str(tip).strip()
    except Exception: return None, True
    if tipsha.startswith(deployed) or deployed.startswith(tipsha): return False, False
    # Thinker 2026-10-07 (21 briefs, all normal operation): deploys run on an
    # hourly cadence behind a ~25-minute pre-deploy test gate, so a merge can
    # legitimately sit undeployed for well over the old 15 minutes. The grace
    # is now per-row (`deploy_grace_min`, default 90), and age is measured from
    # the MERGE landing on the tip (--first-parent), not from a builder's
    # branch commit, whose committer date can predate the merge by hours.
    out, und = _call(runner, deadline, 10, "git",
                     ["log", "--first-parent", "--format=%ct", f"{deployed}..{tipsha}"], row["repo"])
    if und: return None, True
    try:
        times = [int(x) for x in out.split() if x.strip()]
    except Exception: return None, True
    if not times: return False, False
    grace = float(row.get("deploy_grace_min", DEPLOY_GRACE_MIN))
    return (now.timestamp() - min(times)) / 60 >= grace, False
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
    path = pathlib.Path(path)
    if path.parent.name == 'events' and path.suffix == '.jsonl':
        if not path.parent.is_dir():
            return []  # Preserve the raw reader's missing-directory behavior.
        # Ledger alerts include this pass's appends and archived virtual files.
        # Transcripts and cron stores below retain their distinct raw formats.
        import ledger_files
        try:
            text = ledger_files.read_text(path.parent, path.name)
        except FileNotFoundError:
            return []
    else:
        try:
            text = path.read_text()
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
def _scan_menu(rows):
    last, question, answered = None, (None, None), (False, None)
    for _, entry in rows:
        if entry.get("type") == "assistant":
            last = entry
            question = look_wallclock._defer(_ask_user_question_id, entry)
            answered = (False, None)
        elif question[0] and answered == (False, None):
            answered = look_wallclock._defer(_has_tool_result, [None, entry], 0, question[0])
    return last, question, answered


def _legacy_menu(path):
    entries = _read_jsonl(path)
    idx = max((i for i, entry in enumerate(entries) if entry.get("type") == "assistant"), default=None)
    if idx is None:
        return None, (None, None), (False, None)
    last = entries[idx]
    question = look_wallclock._defer(_ask_user_question_id, last)
    answered = (False, None)
    if question[0]:
        answered = look_wallclock._defer(_has_tool_result, entries, idx, question[0])
    return last, question, answered


def _check_pane_at_menu_claude(events, root, briefs, sessions_dir=None, projects_dir=None, *, tally=None):
    live = panes.live_panes(sessions_dir=sessions_dir, events=events)
    idle = {p["name"] for p in live if p.get("status") == "idle" and panes.ledger_role(p.get("name"))}
    joined = pane_resume._live_join(sessions_dir, idle)
    for pane, meta in sorted(joined.items()):
        sid, target = meta.get("sessionId"), meta.get("tmux")
        if not sid or not target: continue
        tp = pane_resume._find_transcript(sid, projects_dir)
        if tp is None: continue
        last, question, answered = look_wallclock._transcript_read(
            tp, lambda: _legacy_menu(tp), lambda factory: _scan_menu(factory()), tally)
        if last is None: continue
        cls = pane_resume._classify(last)
        key = f"{pane}:{last.get('uuid')}"
        if cls in ("quota", "auth"):
            r = _write_brief("pane-at-menu", key, "operator", {"class": cls}, events, root)
            r and briefs.append(r)
            continue
        tool_use_id = look_wallclock._value(question)
        if tool_use_id and not look_wallclock._value(answered):
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
def _seen_dead_before(root, pane, now):
    f = pathlib.Path(root) / "look-pane-dead-seen.json"
    try: seen = json.loads(f.read_text())
    except Exception: seen = {}
    first = seen.get(pane)
    if first is None:
        seen[pane] = now.isoformat()
        try: f.write_text(json.dumps(seen))
        except Exception: pass
        return False
    return (now - fold.ts(first)).total_seconds() >= 60
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
        # Thinker 2026-10-07: a planner's process exits ~10 s before its wrapper
        # writes planner-ended, so a single dead reading raced 47 false briefs.
        # Brief only when the pane is still dead and un-ended on a later run.
        if not _seen_dead_before(root, e.get("pane"), now): continue
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


def _check_machine(text, und, events, root, briefs, answered, repo=_CHECKOUT):
    if und:
        _fire_undetermined("machine", events, root, briefs)
        return
    try:
        import machine
        drifts = machine.check(live_models=machine.read_text(pathlib.Path(root) / "models.toml"),
                               live_crontab=text, repo=str(repo))
    except Exception:
        _fire_undetermined("machine", events, root, briefs)
        return
    if any(d.kind == "undetermined" for d in drifts):
        _fire_undetermined("machine", events, root, briefs)
        return
    by_subject = {d.subject: d for d in drifts}
    for subject in machine.NAMES:
        drift = by_subject.get(subject)
        reading = {"subject": subject, "kind": drift.kind, "detail": drift.detail} if drift else {"subject": subject, "status": "equal"}
        _settle("machine-drift", subject, "thinker", drift is not None,
                reading, events, root, briefs, answered)


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
def _check_autodispatch_failures(events, root, briefs, answered):
    """Only filename-authorized, explicitly classified passes are trustworthy."""
    from autodispatch import _hold_order
    ticks = sorted((e for e in events if e.get('type') == 'tick'
                    and fold.actor_of(str(e.get('_src') or '').rsplit(':', 1)[0]) == 'tick'),
                   key=_hold_order)
    if not ticks:
        return
    failures = []
    for e in reversed(ticks):
        if e.get('autodispatch_pass_failed') is not True:
            break
        failures.append(e)
    reading = dict(consecutive=len(failures),
                   sources=[e.get('_src') for e in reversed(failures)])
    if len(failures) >= STREAK or ticks[-1].get('autodispatch_pass_failed') is False:
        _settle('autodispatch-repeat-failure', 'autodispatch', 'executor',
                len(failures) >= STREAK, reading, events, root, briefs, answered)


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
def _fold_input(events, folded):
    if isinstance(folded, Exception):
        raise folded
    return fold.fold(events) if folded is None else folded


def _check_dispatchable_stale(events, now, root, briefs, *, folded=None):
    """L-charter-0042/L-spec-0427, SD4: `autodispatch.candidates` folds
    `events` itself (`look.run` takes none), and anything `dispatchable`, or
    `blocked` specifically on a standing `autodispatch-failed`, whose `since`
    exceeds the table's build limit briefs `spec-dispatchable-stale` (owner
    `executor`) — `seat-wait` never alarms (it is legitimately waiting on
    capacity, SD2). `reading` is `dispatch-failed:<reason>` for the standing-
    failure case, `tick-not-running` for a structurally-fine one simply never
    picked up. Wrapped in the SAME broad `try/except Exception` shape
    `_check_wallclock` uses: a raise degrades to one `reading-undetermined`
    (`dispatch-stale`), nothing else."""
    try:
        import autodispatch, lane_rules
        limit = lane_rules.load()["build"]["limit_min"]
        specs, _, _, _ = _fold_input(events, folded)
        rows = autodispatch.candidates(events, specs, now)
        readings = []
        for row in rows:
            alarmable = row["status"] == "dispatchable" or (row["status"] == "blocked" and row.get("_dispatch_failed"))
            age_min = (now - fold.ts(row["since"])) / timedelta(minutes=1)
            reading = (f"dispatch-failed:{row['reason']}" if row.get("_dispatch_failed") else "tick-not-running") \
                if alarmable else "not-stale"
            readings.append((row["spec"], bool(alarmable and age_min > limit), reading))
    except Exception:
        _fire_undetermined("dispatch-stale", events, root, briefs)
        return
    seen = set()
    for sid, bad, reading in readings:
        seen.add(sid)
        _settle("spec-dispatchable-stale", sid, "executor", bad, reading, events, root, briefs, [])
    # AC23: a spec whose fold state has moved off `written` no longer appears
    # among `rows` at all — clear any standing brief it still carries.
    for sid, s in specs.items():
        if sid in seen or s.get("state") == "written":
            continue
        _clear("spec-dispatchable-stale", sid, "left written", events, root)


def _check_grades_per_shipped(events, now, root, briefs, answered, th, *, folded=None):
    """L-spec-0482/R12.d: `fold.grades_per_shipped`'s trailing-24h ratio above
    `[thresholds] grades_per_shipped_high` (default 1.5) briefs
    `grades-per-shipped-high`, owner `thinker` — the same fire/quiet/clear
    shape `_settle` already gives every other threshold check here. `None`
    (nothing shipped in the window) is never bad."""
    by_subject = _fold_input(events, folded)[3]
    ratio, specs_n, runs_n = fold.grades_per_shipped(events, now, by_subject=by_subject)
    bad = ratio is not None and ratio > th["grades_per_shipped_high"]
    _settle("grades-per-shipped-high", "grades-per-shipped", "thinker", bad,
            {"ratio": ratio, "specs": specs_n, "runs": runs_n}, events, root, briefs, answered)


def _origin_behind_reading(runner, now, deadline, repo, branch):
    """One project/branch pair. Returns `(bad, reading, undetermined)`:
    `bad` is True for "ahead 10+ minutes" or "diverged" (fires), False for
    "equal" or "strictly behind" (never fires, never alarms a pull the tick
    never does) — `None`/`None`/`True` on anything unparseable, including an
    absent `origin/<branch>` ref or an elapsed deadline. Read-only: never a
    fetch, never a push (Constraints)."""
    exists, und = _call(runner, deadline, 10, "git",
                        ["rev-parse", "--verify", "--quiet", f"origin/{branch}"], repo)
    if und or not str(exists or "").strip():
        return None, None, True
    counts, und2 = _call(runner, deadline, 10, "git",
                         ["rev-list", "--left-right", "--count", f"origin/{branch}...refs/heads/{branch}"], repo)
    if und2:
        return None, None, True
    parts = str(counts or "").split()
    if len(parts) != 2:
        return None, None, True
    try:
        behind_n, ahead_n = int(parts[0]), int(parts[1])
    except ValueError:
        return None, None, True
    if ahead_n == 0:
        return False, {"branch": branch, "unpushed": 0}, False   # equal, or strictly behind
    if behind_n > 0:
        return True, {"branch": branch, "diverged": True, "unpushed": ahead_n}, False   # fires at once
    log, und3 = _call(runner, deadline, 10, "git",
                      ["log", "--format=%ct", f"origin/{branch}..refs/heads/{branch}"], repo)
    if und3:
        return None, None, True
    try:
        times = [int(x) for x in str(log or "").split() if x.strip()]
    except ValueError:
        return None, None, True
    if not times:
        return None, None, True
    age_min = (now.timestamp() - min(times)) / 60
    return age_min >= 10, {"branch": branch, "unpushed": ahead_n, "age_min": age_min}, False


def _check_owed_overdue(events, now, root, briefs):
    """One daily condition digest, with overdue criteria grouped by spec."""
    try:
        import owed_metrics
        rows = owed_metrics.summary(events, now)["overdue_24h"]
    except Exception: return
    grouped = {}
    for row in rows:
        grouped.setdefault(row['spec'], []).append(row)
    for sid, checks in sorted(grouped.items()):
        reading = {"spec": sid, "criteria": sorted({r['criterion'] for r in checks}), "checks": checks}
        r = _write_brief("owed-overdue", sid, "executor", reading, events, root)
        r and briefs.append(r)
def _check_origin_behind(cfg, runner, now, deadline, events, root, briefs, answered):
    """L-charter-0042/L-spec-0484 R13(c): "local-ahead-of-origin for 10+
    minutes is an alarm." One reading per `[push]` project; a project with no
    configured `main` is silently skipped here (`push_origin.targets`'s own
    `origin/HEAD` fallback is a PUSH-time resolution, not this read-only
    alarm's job)."""
    push = cfg.get("push") or {}
    projects = push.get("projects") or []
    mains = push.get("main") or {}
    for project in projects:
        branch = mains.get(project)
        if not branch:
            continue
        repo = push_checkout(cfg, project)
        bad, details, und = _origin_behind_reading(runner, now, deadline, repo, branch)
        if und:
            _fire_undetermined("origin-behind", events, root, briefs)
            continue
        reading = {"project": project, **(details or {})}
        _settle("origin-behind", project, "executor", bad, reading, events, root, briefs, answered)


_SPEC_ID_RE = re.compile(r"^L-spec-\d{4}$")


def _spec_idle_busy_spawn(evs, events, now):
    """An unterminated `spawn-started` on this subject younger than twice its
    role's cap in minutes (`dispatch.ROLES[role][1]`, default 60 for an
    unknown/absent role) — Assumption 9's last exclusion."""
    ended = {e.get("spawn") for e in events if e.get("type") in ("spawn-done", "spawn-failed", "spawn-stale")}
    for e in evs:
        if e.get("type") != "spawn-started":
            continue
        spawn_id = e.get("spawn")
        if spawn_id and spawn_id in ended:
            continue
        cap = dispatch.ROLES.get(e.get("role"), (None, 60, 0))[1]
        age_min = (now - fold.ts(e.get("ts"))).total_seconds() / 60
        if age_min < 2 * cap:
            return True
    return False


def _spec_idle_bad(sid, s, evs, events, cand_rows, held, answered_ids, now, *, covered=None):
    """SD2 owns exclusions; the existing idle limit still gates eligible stalls."""
    import lane_rules
    if s['state'] == 'unknown' or s['state'] in dispatch.SETTLED_STATES:
        return False
    # An overdue escalation is still an open escalation; it already surfaces
    # the problem and must not create an additional spec-idle alarm.
    if covered is None:
        answered_refs = fold.answered(events)
        covered = {e.get('subject') for e in fold.open_escalations(events)
                   if e.get('_src') not in answered_refs}
    if sid in covered:
        return False
    # Keep the existing helper call shape. Production supplies its classified
    # snapshot here; older callers supplying candidates still get SD2 evidence.
    classified = cand_rows
    if 'bucket' not in classified.get(sid, {}):
        specs, _, _, by_subject = fold.fold(events)
        unfinished = {key: value for key, value in specs.items()
                      if value['state'] != 'unknown' and value['state'] not in dispatch.SETTLED_STATES}
        classified = {r['spec']: r for r in fold.stall_rows(events, unfinished, now=now,
                      dwell={value['state']: -1 for value in unfinished.values()}, by_subject=by_subject)}
    limit = lane_rules.load()['idle']['limit_min']
    newest = max((fold.ts(e.get('ts')) for e in evs), default=None)
    return bool(classified.get(sid, {}).get('bucket') == 'STUCK' and newest is not None
                and (now - newest) / timedelta(minutes=1) >= limit)


def _check_spec_idle(events, now, root, briefs, answered, *, folded=None):
    """Classify once on the caller's snapshot; unavailable never clears briefs."""
    try:
        import lane_rules
        lane_rules.load()
        specs, _charters, _, by_subject = _fold_input(events, folded)
        unfinished = {sid: s for sid, s in specs.items()
                      if s['state'] != 'unknown' and s['state'] not in dispatch.SETTLED_STATES}
        classified = {r['spec']: r for r in fold.stall_rows(events, unfinished, now=now,
                      dwell={s['state']: -1 for s in unfinished.values()}, by_subject=by_subject)}
        answered_refs = fold.answered(events)
        covered = {e.get('subject') for e in fold.open_escalations(events)
                   if e.get('_src') not in answered_refs}
        readings = []
        for sid, s in specs.items():
            if not _SPEC_ID_RE.match(sid) or s["state"] == "unknown":
                continue
            evs = s.get("evs") or [e for e in events if e.get("subject") == sid]
            if not any(e.get("type") == "spec-written" for e in evs):
                continue
            bad = _spec_idle_bad(sid, s, evs, events, classified, set(), set(), now, covered=covered)
            idle_min = (now - max(fold.ts(e.get("ts")) for e in evs)).total_seconds() / 60
            readings.append((sid, bad, idle_min))
    except Exception:
        _fire_undetermined("spec-idle", events, root, briefs)
        return
    for sid, bad, idle_min in readings:
        _settle("spec-idle", sid, "executor", bad, {"spec": sid, "idle_min": idle_min},
                events, root, briefs, answered)


def _send_operator(text, root):
    """One argument, no shell; callers record a missing sender as sent=no."""
    try:
        return subprocess.run([str(root / "telegram" / "tg-send.sh"), text],
                              capture_output=True, timeout=10).returncode
    except OSError:
        return 1


def _append_watchdog(root, dst, event_type, **fields):
    dst.parent.mkdir(parents=True, exist_ok=True)
    refusal = dispatch.emit(dst, {}, event_type, **fields)
    if refusal:
        raise ValueError(refusal)


def heartbeat_missed(name, now, events, *, root=None, sender=None, dst=None):
    """Serialize alerts; propagate undetermined reads to the caller's error record."""
    root = pathlib.Path(root) if root is not None else fold.ROOT
    dst = pathlib.Path(dst) if dst is not None else root / "events" / LEDGER_NAME
    # Tick and look can watch the same job; serialize the read/dedupe/write cycle.
    if _DRY_RUN:
        return _heartbeat_missed(name, now, events, root=root, sender=sender, dst=dst)
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "heartbeat-watchdog.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _heartbeat_missed(name, now, events, root=root, sender=sender, dst=dst)


def _heartbeat_time(value):
    try:
        instant = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return instant.astimezone(timezone.utc) if instant.tzinfo is not None and instant.utcoffset() is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _heartbeat_status(last_start, first_missing, now, window=100):
    if last_start is not None:
        return "missed" if (now - last_start).total_seconds() > window * 60 else "healthy"
    return "undetermined"


def heartbeat_line(events, now):
    """Render supplied heartbeat snapshots only; measurement time determines freshness."""
    from zoneinfo import ZoneInfo
    now = _heartbeat_time(now)
    if now is None:
        return "Look heartbeat undetermined: invalid observation clock."
    rows = [e for e in events if isinstance(e, dict) and e.get("type") == "operator-alert"
            and e.get("subject") == "look" and e.get("kind") == "heartbeat-observation"
            and (stamp := _heartbeat_time(e.get("measured_at"))) is not None and stamp <= now
            and e.get("status") in ("healthy", "missed", "undetermined")]
    if rows:
        measured = max(_heartbeat_time(e["measured_at"]) for e in rows)
        newest = [e for e in rows if _heartbeat_time(e["measured_at"]) == measured]
        signatures = {json.dumps([e.get('last_start'), e.get('first_missing_at'), e.get('status')], sort_keys=True) for e in newest}
        if len(signatures) != 1:
            return "Look heartbeat undetermined: conflicting observations."
        row = newest[0]
        last = _heartbeat_time(row.get("last_start"))
        missing = _heartbeat_time(row.get("first_missing_at"))
        if last is not None and last <= measured:
            status = _heartbeat_status(last, None, now)
            local = last.astimezone(ZoneInfo("Asia/Jerusalem")).strftime("%Y-%m-%d %H:%M")
            return f"Look heartbeat {status}: last start {local}, {int((now-last).total_seconds()//60)} minutes ago."
        if row.get("last_start") is None and missing is not None and missing <= measured:
            return f"Look heartbeat undetermined: evidence unavailable for {int((now-missing).total_seconds()//60)} minutes."
        return "Look heartbeat undetermined: no usable observation."
    legacy = [_heartbeat_time(e.get("last_start")) for e in events if isinstance(e, dict)
              and e.get("type") == "operator-alert" and e.get("kind") == "heartbeat-missed" and e.get("subject") == "look"]
    legacy = [t for t in legacy if t is not None and t <= now]
    if legacy:
        return f"Look heartbeat missed: last known start {int((now-max(legacy)).total_seconds()//60)} minutes ago; recovery unconfirmed."
    return "Look heartbeat undetermined: no usable observation."


def _heartbeat_missed(name, now, events, *, root=None, sender=None, dst=None):
    """Persist observations separately from once-per-episode delivery dispositions."""
    import cron_run
    import notification_policy
    import tg_say
    root = pathlib.Path(root) if root is not None else fold.ROOT
    dst = pathlib.Path(dst) if dst is not None else root / "events" / LEDGER_NAME
    window = 3 * int(os.environ.get("DOIT_TICK_MIN", "5")) if name == "tick" else 100
    candidates = []
    for path, kind in ((cron_run.store_path(name, root), "start"), (cron_run.skip_store_path(name, root), "skip")):
        try:
            rows = _read_cron_store(path) or []
            candidates.extend(t for r in rows if isinstance(r, dict) and r.get("ev") == kind
                              and (t := _heartbeat_time(r.get("ts"))) is not None and t <= now)
        except (ValueError, UnicodeError, TypeError):
            pass
    newest = max(candidates, default=None)
    local_rows = _read_jsonl(dst)
    history = [e for e in [*events, *local_rows] if isinstance(e, dict)
               and e.get("type") == "operator-alert" and e.get("subject") == name]
    observations = [e for e in history if e.get("kind") == "heartbeat-observation"
                    and _heartbeat_time(e.get("measured_at")) is not None]
    # The serialized writer's last committed observation records a clock reset.
    # A pre-reset future measurement must not reset absence on every later run.
    local_observations = [e for e in local_rows if e.get('type') == 'operator-alert'
                          and e.get('subject') == name and e.get('kind') == 'heartbeat-observation'
                          and _heartbeat_time(e.get('measured_at')) is not None]
    previous = local_observations[-1] if local_observations else max(
        observations, key=lambda e: _heartbeat_time(e['measured_at']), default={})
    first_missing = None
    if newest is None:
        first_missing = _heartbeat_time(previous.get("first_missing_at"))
        if (first_missing is None or first_missing > now or
                _heartbeat_time(previous.get("measured_at")) > now):
            first_missing = now
    status = _heartbeat_status(newest, first_missing, now, window)
    missed = status != "healthy"
    if _DRY_RUN:
        return missed
    snapshot = dict(subject=name, kind="heartbeat-observation", sent="no", status=status,
                    detail=f"{name} heartbeat {status}", last_start=newest.isoformat() if newest else None,
                    first_missing_at=first_missing.isoformat() if first_missing else None,
                    measured_at=now.isoformat())
    _append_watchdog(root, dst, "operator-alert", **snapshot)
    age = (now - (newest or first_missing)).total_seconds()
    if not missed or (name != "tick" and age <= 180 * 60):
        return missed
    alerts = [e for e in history if e.get("kind") == "heartbeat-missed"
              and _heartbeat_time(e.get('last_start') or e.get('first_missing_at')) == (newest or first_missing)]
    if any(e.get("sent") == "yes" or e.get("delivery") in ("held", "sent") for e in alerts):
        return True
    attempted = [_heartbeat_time(e.get("attempted_at") or e.get("measured_at")) for e in alerts
                 if e.get("delivery") != "none"]
    attempted = [t for t in attempted if t is not None]
    if attempted and 0 <= (now - max(attempted)).total_seconds() < 3600:
        return True
    real = (f"No new heartbeat for {int(age//60)} minutes." if newest else
            f"Heartbeat evidence has been unavailable for {int(age//60)} minutes.")
    text = notification_policy.format_message("[DOWN]", f"{name.capitalize()} heartbeat needs attention.", real,
                "Check the scheduled job and restore heartbeat reporting.", None,
                "Watching for a newer start or lock skip.")
    error = None
    try:
        result = sender(text) if sender is not None else tg_say.deliver("look", text, label="[DOWN]", root=root)
        if isinstance(result, int) and not isinstance(result, bool) and sender is not None:
            delivery = "sent" if result == 0 else "failed"
        else:
            delivery = result.state if result.state in ("sent", "held", "failed", "refused") else "failed"
            if delivery == "sent" and result.http != 200:
                delivery = "failed"
    except Exception as exc:
        delivery, error = "failed", exc
    _append_watchdog(root, dst, "operator-alert", subject=name, kind="heartbeat-missed",
                     sent="yes" if delivery == "sent" else "no", delivery=delivery, detail=text,
                     last_start=newest.isoformat() if newest else None,
                     first_missing_at=first_missing.isoformat() if first_missing else None,
                     measured_at=now.isoformat(), attempted_at=now.isoformat(),
                     **({"sender_error": str(error)} if error else {}))
    if error is not None:
        raise error
    return True


def _check_heartbeat_missed(events, now, root, briefs):
    try:
        heartbeat_missed("tick", now, events, root=root)
    except Exception:
        _fire_undetermined("heartbeat-missed", events, root, briefs)


def _check_lane_rules(events, now, root, briefs):
    """Track table outages in state, independently of the wall-clock ledger ts."""
    try:
        import lane_rules
        st = _read_state(root) or {}
        try:
            lane_rules.load()
        except ValueError as exc:
            if _DRY_RUN or st.get("lane_rules_outage") == "yes":
                return
            detail = f"lane rules unreadable: {exc}"
            try:
                sent = "yes" if _send_operator(detail, root) == 0 else "no"
            except Exception:
                sent = "no"
            if sent == "yes" or not st.get("lane_rules_outage"):
                _append_watchdog(root, root / "events" / LEDGER_NAME, "operator-alert",
                                 subject="lane-rules-unreadable", kind="lane-rules-unreadable",
                                 sent=sent, detail=detail, measured_at=now.isoformat(timespec="seconds"))
            st["lane_rules_outage"] = sent
        else:
            st.pop("lane_rules_outage", None)
        if not _DRY_RUN:
            _write_state(root, st)
    except Exception:
        _fire_undetermined("lane-rules-unreadable", events, root, briefs)


def _check_lane_stale(events, now, root, briefs, *, folded=None):
    try:
        import lane_rules
        specs, _, _, _ = _fold_input(events, folded)
        dst = root / "events" / LEDGER_NAME
        seen_events = [*events, *_read_jsonl(dst)]
        previous = {(e.get("spec"), e.get("rule"), e.get("since")): e for e in seen_events
                if e.get("type") == "escalation-blocking" and e.get("kind") == "lane-stale"}
        pending = []
        import tick
        limits = lane_rules.load()
        for step in tick._steps(events, specs, now):
            row = fold.step_row(step, limits, now)
            if row['rule'] == 'undetermined':
                _fire_undetermined("lane-stale", events, root, briefs)
                continue
            key = row["spec"], row["rule"], row["since"]
            earlier = previous.get(key)
            if not row["overdue"]:
                continue
            if earlier:
                if (earlier.get("owner") != "executor"
                        or now - lane_rules._time(earlier["measured_at"]) < timedelta(minutes=60)):
                    continue
                owner = "thinker"
            else:
                if lane_rules.ladder(seen_events, row, now) != "inbox":
                    continue
                owner = ("executor" if step.state == "action"
                         and step.owner in {"tick", "executor", "relay", "sweeper"} else "thinker")
            previous[key] = dict(owner=owner, measured_at=now.isoformat())
            pending.append(dict(row, owner=owner, nextstep=step.action or "hold",
                                waiting_on=step.trigger or step.clears or "undetermined"))
        if _DRY_RUN:
            return
        for row in pending:
            sid, rule = row["spec"], row["rule"]
            _append_watchdog(root, dst, "escalation-blocking", subject=f"lane-stale:{sid}",
                             kind="lane-stale", owner=row["owner"], spec=sid, rule=rule, since=row["since"],
                             nextstep=row["nextstep"], waiting_on=row["waiting_on"],
                             measured_at=now.isoformat(timespec="seconds"),
                             why=row["reason"],
                             default=f"{row['owner']} follows {row['nextstep']} for {sid}; waiting on {row['waiting_on']}",
                             deadline=(now + timedelta(hours=24)).isoformat(timespec="seconds"),
                             revert="no automatic action was taken - nothing to revert")
    except Exception:
        _fire_undetermined("lane-stale", events, root, briefs)


def _check_wallclock(events, runner, now, root, cfg, deadline, dry_run, briefs, *, tally=None):
    """The four wall-clock checks (L-charter-0038 R6), wrapped in the SAME
    lazily-guarded broad `except Exception` shape `_check_crons` uses around
    `crons.check()` (Seams) — a raise here degrades to one `reading-undetermined`
    (key `undetermined:wallclock`) and this pass's own "wallclock" key is `[]`."""
    try:
        result = look_wallclock.run(events, runner, now, root, cfg, deadline, _write_brief, dry_run, tally=tally)
    except Exception:
        _fire_undetermined("wallclock", events, root, briefs)
        return []
    briefs.extend(result["briefs"])
    return result["events"]
def _check_omens(events, specs, runner, now, root, cfg, deadline, briefs, answered):
    if runner.clock() >= deadline:
        _fire_undetermined("omens", events, root, briefs)
        return
    try:
        if isinstance(specs, Exception):
            raise specs
        rows = look_omens.run(events, specs, runner, now, {**cfg["omens"], "_deadline": deadline})
    except Exception:
        _fire_undetermined("omens", events, root, briefs)
        return  # unknown readings must never clear a standing omen
    active = set()
    for row in rows:
        condition = f"omen-{row['rule']}:{row['key']}"
        active.add((condition, row['key']))
        owner = look_omens.OWNERS[row['rule']]
        r = _write_brief(condition, row['key'], owner,
                         {"detail": row['detail'], "severity": row['severity']}, events, root)
        if r is not None:
            r['subject'] = f"look:{condition}"
            briefs.append(r)
    # Recover open keys from both state and ledger (state may have been lost).
    old = set()
    for key in ((_read_state(root) or {}).get("open") or {}):
        if key.startswith("omen-") and "|" in key:
            old.add(tuple(key.rsplit("|", 1)))
    for e in events:
        condition = str(e.get("condition", ""))
        if condition.startswith("omen-"):
            old.add((condition, str(e.get("key", ""))))
    shelf_unknown = any(r['rule'] == 'shelf' and r['key'] == 'unavailable' for r in rows)
    for condition, key in old - active:
        if shelf_unknown and condition.startswith(('omen-shelf:', 'omen-shelf-stuck:')):
            continue
        r = _clear(condition, key, "omen no longer active", events, root)
        if r is not None:
            answered.append(r)


def run(events, *, now=None, runner=None, root=None, toml_path=None, dry_run=False,
        crons_manifest=None, crond_dir=None, repo=None):
    global _PASS_LOCKED, _DRY_RUN, _DIGEST
    tally = look_wallclock._new_tally()
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
        return {"briefs": [], "answered": [], **tally}
    _PASS_LOCKED, _DRY_RUN = True, dry_run
    _DIGEST = look_digest.Digest(root, events, now, dry_run=dry_run)
    look_wallclock._CLASSES = cfg.get("classes") or {}
    briefs, answered = [], []
    wallclock_events = []
    folded = None
    def evaluation_fold():
        nonlocal folded
        if folded is None:
            previous_now = fold.NOW
            try:
                fold.NOW = now
                folded = fold.fold(events)
            except Exception as exc:
                folded = exc
            finally:
                fold.NOW = previous_now
        return folded
    try:
        # Independent of tick: a poisoned dispatch item must not silence this alarm.
        import merge_deadman
        merge_deadman.run(events, now, root, dry_run=dry_run, sender=_send_operator)
        deadline = runner.clock() + th["pass_budget_s"]
        single = len(cfg["prod"]) == 1
        for row in cfg["prod"]:
            _check_prod(row, runner, now, deadline, single, events, root, briefs, answered)
        _check_packet_unserved(events, root, briefs)
        _check_pane_at_menu_claude(events, root, briefs, tally=tally)
        _check_pane_at_menu_codex(cfg, runner, deadline, events, root, briefs)
        _check_pane_dead(events, now, root, briefs)
        _check_supervisor_stale_code(events, now, runner, deadline, root, briefs, answered)
        _check_disk_tmp(runner, root, th, now, deadline, events, briefs, answered)
        import look_scratch
        look_scratch.check(events, root, briefs, answered)
        crontab_text, crontab_und = _call(runner, deadline, 5, "crontab_text")
        _check_crons(crontab_text, crontab_und, events, root, briefs, crons_manifest, crond_dir)
        _check_cron_unwrapped(crontab_text, crontab_und, events, root, briefs, answered, crons_manifest, crond_dir)
        _check_machine(crontab_text, crontab_und, events, root, briefs, answered,
                       repo=_CHECKOUT if repo is None else repo)
        _check_cron_runs(now, events, root, briefs, answered, crons_manifest)
        _check_heartbeat_missed(events, now, root, briefs)
        _check_lane_rules(events, now, root, briefs)
        _check_dispatch_failures(events, root, briefs, answered)
        _check_autodispatch_failures(events, root, briefs, answered)
        _check_spec_misrouted(events, root, briefs)
        _check_dispatchable_stale(events, now, root, briefs, folded=evaluation_fold())
        _check_grades_per_shipped(events, now, root, briefs, answered, th, folded=evaluation_fold())
        _check_origin_behind(cfg, runner, now, deadline, events, root, briefs, answered)
        _check_spec_idle(events, now, root, briefs, answered, folded=evaluation_fold())
        _check_lane_stale(events, now, root, briefs, folded=evaluation_fold())
        try:
            import merge_queue_watchdog
            merge_queue_watchdog.check(events, evaluation_fold()[0], now, root, dry_run=dry_run)
        except Exception:
            _fire_undetermined("merge-queue-no-step", events, root, briefs)
        _check_owed_overdue(events, now, root, briefs)
        omen_fold = evaluation_fold()
        omen_specs = omen_fold if isinstance(omen_fold, Exception) else omen_fold[0]
        _check_omens(events, omen_specs, runner, now, root, cfg, deadline, briefs, answered)
        wallclock_events = _check_wallclock(events, runner, now, root, cfg, deadline, dry_run, briefs, tally=tally)
        r = _clear("look-stale", "look-stale", "fresh pass", events, root)
        r and answered.append(r)
        _finish_digest(_DIGEST)
        if not dry_run:
            st = _read_state(root) or {}
            st["last_pass"] = now.isoformat(timespec="seconds")
            _write_state(root, st)
    finally:
        _PASS_LOCKED, _DRY_RUN, _DIGEST = False, False, None
        look_wallclock._CLASSES = {}
        try:
            fcntl.flock(lockf, fcntl.LOCK_UN)
        except OSError: pass
        lockf.close()
    return {"briefs": briefs, "answered": answered, "wallclock": wallclock_events, **tally}
def main(argv=None):
    ap = argparse.ArgumentParser(prog="doit look")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    print(f"look-start: pid={os.getpid()} at={datetime.now(timezone.utc).isoformat(timespec='seconds')}", flush=True)
    result = run(fold.read_events(), dry_run=a.dry_run)
    import resource
    print(f"look: {len(result['briefs'])} brief(s), {len(result['answered'])} answered"
          f" pid={os.getpid()} at={datetime.now(timezone.utc).isoformat(timespec='seconds')}"
          f" maxrss_mib={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024}"
          f" transcripts_read={result['transcripts_read']}"
          f" transcript_fallbacks={result['transcript_fallbacks']['count']}"
          f" transcripts_skipped={result['transcripts_skipped']}")
    return 0
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
