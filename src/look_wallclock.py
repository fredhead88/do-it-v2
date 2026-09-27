#!/usr/bin/env python3
"""look_wallclock — the wall-clock blockers nobody could see, as four
independent checks (L-charter-0038 R6, L-spec-0389): quota pause, master CI
red, a queued message with no reply, and a Planner stuck re-escalating a
charter already decided.

  look_wallclock.run(events, runner, now, root, cfg, deadline, write_brief,
                      dry_run) -> {"events": [...], "briefs": [...]}

Called ONCE from `look.run()`, before the `look-stale` clear, wrapped there in
the same broad `except Exception` shape `_check_crons` uses around
`crons.check()`. `write_brief` IS `look._write_brief`, passed in rather than
imported: `look.py` already imports this module for the hook call above, so a
reverse import would cycle (`pane_resume.py`'s own reasoning for duplicating
`tick.LEDGER_NAME`). This module never imports `look`, duplicates its own
small jsonl/state/`_call` helpers for the same reason, and never reads
`_CLASSES` from within its own four checks — only `fix_for` does, called from
INSIDE look.py's own `_write_brief`. `_CLASSES` is a module-level dict on
THIS module, set and reset from `look.run()` by attribute assignment
(`look_wallclock._CLASSES = ...`), never by a reverse import. `resets_at`/
CI's "most recent" both carry whichever raw signal is present, never a
corrected one (Assumptions). Only `gh_run_list` is `Runner`-routed and
deadline-gated; every other read here is a plain filesystem/ledger read,
matching `_check_pane_at_menu_claude` precedent.
"""
import json, pathlib, re, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dispatch, fold, pane_resume, panes, relay  # noqa: E402

LEDGER_NAME = "L-look-local.jsonl"  # the SAME single ledger file look.py owns (SD12 Seams)

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
RED = {"failure", "timed_out", "startup_failure"}
DROP_CONCLUSIONS = {"cancelled", "skipped", "neutral"}

# Set (and reset to {}) from INSIDE look.run(); read only by fix_for(), below.
_CLASSES = {}


# ── small local duplicates (Assumptions: no reverse import of look.py) ──────
def _parse_jsonl(text):
    """[(1-indexed lineno, entry)] — malformed/blank lines skipped."""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip(): continue
        try: e = json.loads(line)
        except json.JSONDecodeError: continue
        if isinstance(e, dict): out.append((i, e))
    return out


def _read_jsonl_lines(path):
    """Unreadable -> `[]` (check 1: a missing/racing transcript is not a failure)."""
    try: text = pathlib.Path(path).read_text()
    except OSError: return []
    return _parse_jsonl(text)


def _read_jsonl_lines_strict(path):
    """Unreadable RAISES (check 3: undetermined, never a false "no match", AC5)."""
    return _parse_jsonl(pathlib.Path(path).read_text())


def _read_state(root):
    try: return json.loads((pathlib.Path(root) / "look" / "state.json").read_text())
    except (OSError, ValueError): return None


def _write_state(root, state):
    p = pathlib.Path(root) / "look" / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state))


def _call(runner, deadline, timeout, method, *args):
    """Local duplicate of `look._call`. Up to 2 attempts; (result, undetermined)."""
    fn = getattr(runner, method)
    for _ in range(2):
        if runner.clock() >= deadline: return None, True
        try: return fn(*args, timeout), False
        except Exception: continue
    return None, True


def _iso(s):
    """Aware-UTC parse; `None` on failure — never `fold.ts`'s silent `NOW`."""
    try: t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError): return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ── fix_for: condition -> in-flight corrective charter id ───────────────────
_CORRECTS_RE = re.compile(r"^corrects:\s*(.+)$", re.MULTILINE)


def fix_for(condition, root, events):
    """The first OPEN charter whose `corrects:` header names `condition`'s
    configured class, else `None`. Called from look.py's `_write_brief`."""
    cls = _CLASSES.get(condition)
    if not cls: return None
    root = pathlib.Path(root)
    for cid in relay.open_charters(events):
        path = root / "content" / f"{cid}.md"
        try: text = path.read_text()[:4000]
        except OSError: continue
        text = text.split("<!--", 1)[0]
        m = _CORRECTS_RE.search(text)
        if not m: continue
        values = {v.strip() for v in m.group(1).split(",") if v.strip()}
        if cls in values: return cid
    return None


# ── check 1: quota pause ─────────────────────────────────────────────────────
_RESETS_RE = re.compile(r"continuing automatically at ([^·]+)")


def _quota_hit_index(rows):
    """The LAST row whose entry is a "Usage limit reached" informational line."""
    idx = None
    for i, (_, e) in enumerate(rows):
        if (e.get("type") == "system" and e.get("subtype") == "informational"
                and str(e.get("content") or "").startswith("Usage limit reached")):
            idx = i
    return idx


def _parsed_resets_at(content):
    m = _RESETS_RE.search(content or "")
    return m.group(1).strip() if m else None


def _structured_resets_at(rows):
    for _, e in rows:
        v = (e.get("quotaLimits") or {}).get("resetsAt")
        if v: return v
    return None


def _sid_to_pane(events, now):
    """sessionId -> pane name, for every UNIQUELY-resolved live ledger pane."""
    live = panes.live_panes(sessions_dir=None, events=events, now=now)
    names = {p.get("name") for p in live if panes.ledger_role(p.get("name"))}
    joined = pane_resume._live_join(None, names)
    return {meta.get("sessionId"): name for name, meta in joined.items() if meta.get("sessionId")}


def _derive_quota_from_ledger(events):
    """The ledger's own permanent authority on "still open", for when
    state.json is lost (or dry-run, which never reads it for a decision)."""
    ended_refs = {e.get("ref") for e in events if e.get("type") == "pane-pause-ended"}
    out = {}
    for e in events:
        if e.get("type") != "pane-paused" or e.get("reason") != "quota": continue
        if e.get("_src") in ended_refs: continue
        sid = str(e.get("evidence") or "").split(".jsonl:")[0]
        if sid:
            out[sid] = {"pane": e.get("pane"), "src": e.get("_src"), "hit_ts": None}
    return out


def _check_quota(events, now, quota_state, state_missing, emit):
    proj_root = pane_resume.PROJECTS
    window_start = now - timedelta(hours=2)
    sid_to_pane = _sid_to_pane(events, now)

    try: files = sorted(proj_root.glob("*/*.jsonl"))
    except OSError: files = []
    for f in files:
        try: mtime = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
        except OSError: continue
        if mtime < window_start: continue
        rows = _read_jsonl_lines(f)
        hit_i = _quota_hit_index(rows)
        if hit_i is None: continue
        lineno, hit = rows[hit_i]
        sid = f.stem
        evidence = f"{sid}.jsonl:{lineno}"
        if any(e.get("type") == "pane-paused" and e.get("evidence") == evidence for e in events):
            continue
        pane = sid_to_pane.get(sid, sid)
        resets_at = _parsed_resets_at(str(hit.get("content") or ""))
        structured = _structured_resets_at(rows[hit_i:])
        if structured: resets_at = structured
        opens = [e for e in events if e.get("type") == "escalation-blocking"
                 and e.get("subject") == pane and e.get("reason_class") == "quota"]
        ref = None
        if opens:
            ref = max(opens, key=lambda e: fold.ts(e.get("ts"))).get("_src")
        kv = {"pane": pane, "reason": "quota", "evidence": evidence}
        if resets_at: kv["resets_at"] = resets_at
        if ref: kv["ref"] = ref
        row = emit("pane-paused", kv)
        if row is not None:
            quota_state[sid] = {"pane": pane, "src": row["_src"], "hit_ts": hit.get("timestamp")}

    open_entries = quota_state if not state_missing else _derive_quota_from_ledger(events)
    for sid, entry in list(open_entries.items()):
        pane, src, hit_ts_raw = entry.get("pane"), entry.get("src"), entry.get("hit_ts")
        tp = pane_resume._find_transcript(sid, None)
        if tp is None: continue
        rows = _read_jsonl_lines(tp)
        if hit_ts_raw is None:
            hi = _quota_hit_index(rows)
            hit_ts_raw = rows[hi][1].get("timestamp") if hi is not None else None
        hit_ts = panes._ts(hit_ts_raw) or EPOCH
        resolved = False
        for _, e in rows:
            if e.get("type") != "assistant": continue
            t = panes._ts(e.get("timestamp"))
            if t and t > hit_ts and pane_resume._classify(e) == "none":
                resolved = True
                break
        if not resolved: continue
        emit("pane-pause-ended", {"pane": pane, "reason": "quota", "ref": src})
        if not state_missing: quota_state.pop(sid, None)


# ── check 2: master CI ───────────────────────────────────────────────────────
def _check_ci(rows, runner, deadline, now, ci_state, emit, undetermined):
    for row in rows:
        project, repo, branch = row.get("project", "prod"), row["repo"], row.get("branch", "master")
        text, und = _call(runner, deadline, 10, "gh_run_list", repo, branch)
        if und:
            undetermined(f"ci:{project}")
            continue
        try:
            runs = json.loads(text)
            assert isinstance(runs, list)
        except Exception:
            undetermined(f"ci:{project}")
            continue
        cutoff = now - timedelta(days=7)
        completed = [r for r in runs if isinstance(r, dict) and r.get("status") == "completed"]
        recent = [r for r in completed if (_iso(r.get("updatedAt")) or cutoff) >= cutoff]
        kept = [r for r in recent if r.get("conclusion") not in DROP_CONCLUSIONS]
        workflows = row.get("workflows") or []
        names = set(workflows) if workflows else {r.get("name") for r in kept if r.get("name")}
        for wf in sorted(names):
            wf_runs = [r for r in kept if r.get("name") == wf]
            if not wf_runs: continue
            latest = max(wf_runs, key=lambda r: _iso(r.get("updatedAt")) or cutoff)
            concl = latest.get("conclusion")
            key = f"{project}|{wf}"
            prev = ci_state.get(key)
            prev_concl = (prev or {}).get("conclusion")
            if concl in RED:
                if prev_concl not in RED:
                    since = now.isoformat(timespec="seconds")
                    emit("ci-red", dict(repo=repo, workflow=wf, sha=latest.get("headSha"),
                                        run_url=latest.get("url"), since=since))
                    ci_state[key] = {"conclusion": concl, "since": since, "sha": latest.get("headSha")}
                # already red: no transition, `since` stays exactly as recorded.
            elif concl == "success":
                if prev is not None and prev_concl != "success":
                    emit("ci-green", dict(repo=repo, workflow=wf, sha=latest.get("headSha")))
                ci_state.pop(key, None)
            # a conclusion outside both sets: no transition, no state change.


# ── check 3: message answered ────────────────────────────────────────────────
def _check_message_answered(events, emit, undetermined):
    answered_refs = {e.get("ref") for e in events if e.get("type") == "message-answered"}
    for e in events:
        if e.get("type") != "message-sent": continue
        src = e.get("_src")
        if src in answered_refs: continue
        actor0, ts0 = e.get("actor"), fold.ts(e.get("ts"))
        replies = [c for c in events if c.get("ref") == src
                  and c.get("type") in ("message-sent", "decision", "brief-answered")
                  and c.get("actor") != actor0 and fold.ts(c.get("ts")) > ts0]
        if replies:
            reply = min(replies, key=lambda c: fold.ts(c.get("ts")))
            delay_min = (fold.ts(reply.get("ts")) - ts0).total_seconds() / 60
            emit("message-answered", dict(ref=src, by=reply.get("actor"), delay_min=delay_min))
            continue

        tokens = [t.strip() for t in str(e.get("to") or "").split(",") if t.strip()]
        pane_token = next((t for t in tokens if panes.ledger_role(t)), None)
        if pane_token is None: continue
        text = e.get("text")
        if not text: continue
        joined = pane_resume._live_join(None, {pane_token})
        meta = joined.get(pane_token)
        if meta is None:
            undetermined("message-answered")
            continue
        tp = pane_resume._find_transcript(meta.get("sessionId"), None)
        if tp is None:
            undetermined("message-answered")
            continue
        try:
            entries = [e2 for _, e2 in _read_jsonl_lines_strict(tp)]
        except OSError:
            undetermined("message-answered")
            continue
        needle = " ".join(str(text).split())[:120]
        match_idx = None
        for i, entry in enumerate(entries):
            if entry.get("type") != "user": continue
            if needle and needle in " ".join(pane_resume._entry_text(entry).split()):
                match_idx = i
                break
        if match_idx is None: continue
        reply_entry = next((en for en in entries[match_idx + 1:]
                            if en.get("type") == "assistant" and pane_resume._classify(en) == "none"), None)
        if reply_entry is None: continue
        t1 = panes._ts(reply_entry.get("timestamp"))
        delay_min = (t1 - ts0).total_seconds() / 60 if t1 else None
        emit("message-answered", dict(ref=src, by=pane_token, delay_min=delay_min))


# ── check 4: planner attempts stuck ──────────────────────────────────────────
def _check_planner_stuck(events, root, write_brief, out_briefs):
    for cid in relay.open_charters(events):
        pa = relay.planner_attempts(events, cid)
        if pa.get("next") != "escalate": continue
        decisions = [e for e in events if e.get("type") == "decision" and e.get("subject") == cid]
        ended = [e for e in events if e.get("type") == "planner-ended"
                and (e.get("subject") == cid or fold.charter_id(e.get("charter")) == cid)]
        if not decisions or not ended: continue
        newest_decision = max(fold.ts(e.get("ts")) for e in decisions)
        newest_ended = max(fold.ts(e.get("ts")) for e in ended)
        if newest_decision <= newest_ended: continue
        r = write_brief("planner-attempts-stuck", cid, "thinker", {"reason": pa.get("last_reason")},
                        events, root, problem="planner-attempts-stuck")
        if r is not None: out_briefs.append(r)


# ── assembly ─────────────────────────────────────────────────────────────────
def run(events, runner, now, root, cfg, deadline, write_brief, dry_run):
    root = pathlib.Path(root)
    events = list(events or ())
    out_events, out_briefs = [], []
    dst = root / "events" / LEDGER_NAME
    dst.parent.mkdir(parents=True, exist_ok=True)

    raw_state = None if dry_run else _read_state(root)
    state_missing = dry_run or raw_state is None
    state = raw_state or {}
    wc = dict(state.get("wallclock") or {})
    quota_state = dict(wc.get("quota") or {})
    ci_state = dict(wc.get("ci") or {})

    def undetermined(name):
        r = write_brief("reading-undetermined", f"undetermined:{name}", "operator",
                        {"reading": name}, events, root)
        if r is not None: out_briefs.append(r)

    def emit(type_, kv):
        if dry_run:
            row = {**kv, "type": type_, "_src": "dry-run"}
            out_events.append(row)
            return row
        reason = dispatch.emit(dst, {}, type_, **kv)
        if reason is not None: return None
        row = {**kv, "type": type_, "_src": f"{LEDGER_NAME}:{sum(1 for _ in dst.open())}"}
        out_events.append(row)
        return row

    try: _check_quota(events, now, quota_state, state_missing, emit)
    except Exception: undetermined("quota")
    try: _check_ci(cfg.get("ci") or [], runner, deadline, now, ci_state, emit, undetermined)
    except Exception: undetermined("ci")
    try: _check_message_answered(events, emit, undetermined)
    except Exception: undetermined("message-answered")
    try: _check_planner_stuck(events, root, write_brief, out_briefs)
    except Exception: undetermined("planner-attempts-stuck")

    if not dry_run:
        # Re-read rather than reuse `state`: `write_brief` calls above (undetermined,
        # planner-stuck) may have written their OWN openkey through look.py's `_mark`
        # mid-pass, and reusing the stale pre-pass `state` here would clobber it.
        latest = _read_state(root) or {}
        latest["wallclock"] = {"quota": quota_state, "ci": ci_state}
        _write_state(root, latest)

    return {"events": out_events, "briefs": out_briefs}
