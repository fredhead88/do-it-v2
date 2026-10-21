"""realm_omens: the realm's omen model, computed from the ledger and handed to the page as data.

Read-only. Nothing here appends to, edits or locks the ledger. The page (realm.html) keeps the omens that are
pure live state (executor silent, packets unclaimed, quota, disk ...) and takes the ledger-history ones from here:

  recurring problems  only if the problem occurred in the last 24 h; variants merged under one canonical name;
                      lifetime counts are a trend line, not the headline; quiet for 24 h = resolved.
  blocked builders    specs whose LATEST build event is BLOCKED (no later build-started / shipped).
  churn               unshipped, unkilled specs graded 3+ times in the last 48 h and still unconfirmed.
  vanished            spawns with no spawn-done / spawn-failed / spawn-stale whose spec has not shipped.
  merge info          whether a gate-clean spec was actually merged (a window or a commit on master).

Every omen carries owner, next action, since (first occurrence of the CURRENT active spell) and last seen.
Only owner `operator` goes in NEEDS YOU; SYSTEM HEALTH never exceeds severity 1 (warning).
"""
import datetime as dt
import importlib.util
import json
import pathlib
import re
import subprocess

RECENT_H = 24            # a recurring problem is current only if it occurred this recently
CHURN_H = 48
DIGEST_MAX_AGE = dt.timedelta(hours=2)
DIGEST_FUTURE_SKEW = dt.timedelta(minutes=5)
SPELL_GAP_H = 24         # occurrences closer than this belong to one active spell
MIN_RECURRENCE = 3       # lifetime occurrences before a problem counts as "recurring"

# ---- aliases: variants of one problem -> one canonical name -------------------------------------------------
ALIASES = [
    ("relay-serves-claimed-seat",
     r"^serving-(pane|pass|planner|relay)-|^duplicate-(seat-server|serving)|^double-serve-race|^planner-serving-races|^seat-claim-race"),
    ("grader-view-incomplete",
     r"^grad(er|e|ing)-(view|env|packet|seat|dsn|live-db)|^grade-view|^grading-(view|env)|^verify-cmd-needs-env"),
    ("sweep-capability-mismatch",
     r"^sweep-|^sweeper-|^owed-(check|sweep|criterion|wake)-?.*(unobservable|capab|schema|referent|guess|unanchored)|^owed-sweep"),
    ("seat-model-source-missing", r"^seat-model|^serving-model-source|^models-toml"),
    ("reviewer-depth-drift", r"^reviewer-depth"),
    ("seat-transcript-unresolved", r"(stamp|seat)-transcript-unresolved"),
]
_ALIASES = [(c, re.compile(p)) for c, p in ALIASES]


def canonical(name):
    n = str(name or "")
    for canon, rx in _ALIASES:
        if rx.search(n):
            return canon
    return n


# ---- ownership --------------------------------------------------------------------------------------------
OWNERS = ("operator", "thinker", "executor", "relay", "planner", "deploy")
OWNER_NORM = {"you": "operator", "yitzy": "operator", "deployer": "deploy", "launcher": "executor", "orc": "executor",
              "integrator": "executor", "look": "thinker", "nobody": "thinker", "": "thinker"}
CLASS_OWNER = {"relay-serves-claimed-seat": "relay", "look-packet-unserved": "relay", "look-spec-idle": "executor",
               "look-origin-behind": "executor", "look-merge-undeployed": "deploy", "deploy-manual-or-drift": "deploy",
               "spec-shape": "planner", "grader-view-incomplete": "planner"}
OWNER_NEXT = {
    "operator": "decide it (or say it is accepted); the Thinker has nothing further to do here",
    "thinker": "file the corrective unit for this problem, or record in writing that its cost is accepted",
    "executor": "dispatch or close the stuck work this problem points at",
    "relay": "start or restart the relay so claimed seats are served exactly once",
    "planner": "re-plan or rewrite the units this problem names",
    "deploy": "deploy the merged work (or fix the deploy path)",
}


def norm_owner(o, default="thinker"):
    o = str(o or "").strip().lower()
    o = OWNER_NORM.get(o, o)
    return o if o in OWNERS else default


# ---- reading the ledger, cheaply -----------------------------------------------------------------------------
WANT = {"lesson", "brief", "build-started", "build-done", "build-blocked", "verdict", "merge-gate-clean", "shipped",
        "spawn-started", "spawn-done", "spawn-failed", "spawn-stale", "window-verified", "window-merged",
        "shipped-in-window", "spec-killed", "project-merged", "decision", "spec-amended", "spec-written", "unblocked",
        "brief-answered", "note-answered", "message-sent"}
_MARKERS = tuple(('"type": "%s"' % t).encode() for t in WANT) + tuple(('"type":"%s"' % t).encode() for t in WANT)


def _t(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def reduce_event(e):
    """The few fields the omen model needs; None for any other row."""
    ty = e.get("type")
    if ty not in WANT:
        return None
    r = {"t": e.get("ts", ""), "ty": ty, "s": e.get("subject"), "pr": e.get("project")}
    if ty in ("lesson", "brief"):
        r.update(pb=e.get("problem"), own=e.get("owner"), fix=e.get("fix"), cond=e.get("condition"), key=e.get("key"),
                 why=_t(e.get("why") or e.get("text") or e.get("statement"), 200), actor=e.get("actor"))
    elif ty in ("spawn-started", "build-started", "spawn-done", "spawn-failed", "spawn-stale"):
        r.update(sp=e.get("spawn"), role=e.get("role"))
    elif ty == "build-done":
        r.update(st=e.get("status"), sp=e.get("spawn"))
    elif ty == "verdict":
        r["ok"] = bool(e.get("confirmed"))
    elif ty == "window-verified":
        r["kept"] = [x for x in (e.get("kept") or []) if isinstance(x, str)]
    elif ty in ("window-merged", "shipped-in-window", "project-merged"):
        r["kept"] = [x for x in (e.get("specs") or e.get("kept") or []) if isinstance(x, str)]
    elif ty == "message-sent":
        r.update(to=e.get("to"), why=_t(e.get("text") or e.get("why"), 200))
    elif ty == "build-blocked":
        r["why"] = _t(e.get("reason"), 200)
    return r


def scan(events_dir, cache):
    """All reduced rows, oldest first. `cache` maps filename -> (size, rows): only changed files are re-parsed."""
    out = []
    seen = set()
    for f in sorted(pathlib.Path(events_dir).glob("*.jsonl")):
        try:
            size = f.stat().st_size
        except OSError:
            continue
        seen.add(f.name)
        hit = cache.get(f.name)
        if hit and hit[0] == size:
            out.extend(hit[1])
            continue
        rows = []
        try:
            data = f.read_bytes()
        except OSError:
            continue
        end = data.rfind(b"\n") + 1            # a torn tail is never consumed
        for line in data[:end].split(b"\n"):
            if not line or not any(m in line for m in _MARKERS):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("subject") == "operator-ruling" or e.get("problem") == "operator-ruling":
                continue
            r = reduce_event(e)
            if r:
                if not r.get("actor"):
                    r.pop("actor", None)
                rows.append(r)
        cache[f.name] = (size, rows)
        out.extend(rows)
    for k in [k for k in cache if k not in seen]:
        del cache[k]
    out.sort(key=lambda r: r["t"])
    return out


# ---- problem classes (the Thinker's fires.py classifier when present, else the problem field) ---------------
_FIRES = {"fn": None, "tried": False}


def _fires_classify(root):
    if not _FIRES["tried"]:
        _FIRES["tried"] = True
        try:
            p = pathlib.Path(root) / "fires.py"
            if p.is_file():
                spec = importlib.util.spec_from_file_location("realm_fires", p)
                m = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(m)
                _FIRES["fn"] = m.classify
        except Exception:  # noqa: BLE001 — a missing classifier degrades to the problem field
            _FIRES["fn"] = None
    return _FIRES["fn"]


def problem_class(r, root=None):
    fn = _fires_classify(root) if root else None
    if fn:
        try:
            return fn({"problem": r.get("pb"), "text": r.get("why")})
        except Exception:  # noqa: BLE001
            pass
    pb = str(r.get("pb") or "")
    return pb if pb and pb not in ("unassigned",) and not pb.startswith("L-") else "unclassified"


# ---- helpers -------------------------------------------------------------------------------------------------
def parse(t):
    try:
        d = dt.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def iso(d):
    return d.isoformat(timespec="seconds") if d else None


DEAD_RE = re.compile(r"killed|void|dropped|retracted")
DONE_RE = re.compile(r"^(shipped|accepted)")
LANDED_RE = re.compile(r"shipped|accepted|proving|complete")


def _gone(state):
    """The spec is finished one way or the other: nothing about it can still be wrong in flight."""
    return bool(DEAD_RE.search(state or "") or DONE_RE.search(state or ""))


def _spells(times, now):
    """(start, last) of the CURRENT active spell: walk back from the newest occurrence while gaps stay under SPELL_GAP_H."""
    ts = sorted(times)
    last = ts[-1]
    start = last
    for a in reversed(ts[:-1]):
        if (start - a).total_seconds() / 3600 > SPELL_GAP_H:
            break
        start = a
    return start, last


def _item(id_, kind, sev, title, detail, owner, nxt, since, last, subjects=(), trend="", variants=(), anchor="chronicle", fix=None):
    owner = norm_owner(owner)
    needs = owner == "operator"
    return {"id": id_, "kind": kind, "sev": (sev if needs else min(sev, 1)), "title": title, "detail": detail,
            "owner": owner, "next": nxt, "since": iso(since), "last": iso(last), "subjects": list(subjects),
            "trend": trend, "variants": sorted(variants), "needs_you": needs, "anchor": anchor, "fix": fix}


# ---- the four ledger-history omens ---------------------------------------------------------------------------
def recurring(rows, states, now, root=None):
    """(active, resolved) recurring-problem omens."""
    st = dict(states or [])
    groups = {}
    for r in rows:
        if r["ty"] not in ("lesson", "brief"):
            continue
        t = parse(r["t"])
        if not t:
            continue
        raw = problem_class(r, root)
        g = groups.setdefault(canonical(raw), {"times": [], "variants": set(), "fix": None, "own": None, "why": "", "n24": 0, "n7": 0})
        g["times"].append(t)
        g["variants"].add(raw)
        age_h = (now - t).total_seconds() / 3600
        g["n24"] += age_h <= RECENT_H
        g["n7"] += age_h <= 168
        if str(r.get("fix") or "").startswith("L-"):
            g["fix"] = str(r["fix"]).split(":")[0].split()[0]
        if r.get("own"):
            g["own"] = r["own"]
        if t >= max(g["times"]):
            g["why"] = r.get("why") or g["why"]
    active, resolved = [], []
    for name, g in groups.items():
        n = len(g["times"])
        if n < MIN_RECURRENCE or name == "unclassified":     # the catch-all bucket is not a problem
            continue
        fix = g["fix"]
        fix_state = st.get(fix, "") if fix else ""
        if fix and fix_state and not DEAD_RE.search(fix_state) and not LANDED_RE.search(fix_state) and fix_state != "unknown":
            continue                                     # a fix is being built: the fix owns it, not an omen
        start, last = _spells(g["times"], now)
        first = min(g["times"])
        owner = norm_owner(g["own"], CLASS_OWNER.get(name, "thinker"))
        if name in CLASS_OWNER and norm_owner(g["own"], "") in ("", "thinker"):
            owner = CLASS_OWNER[name]
        if owner == "operator":
            owner = "thinker"     # a look brief naming the operator is a claim, not a request: look-*/infra problems are the Thinker's
        variants = sorted(v for v in g["variants"] if v != name)
        trend = f"{g['n24']} in 24h · {g['n7']} in 7d · {n} since {first.strftime('%m-%d')}"
        landed = bool(fix and LANDED_RE.search(fix_state))
        title = f"{name}: {g['n24']} in the last 24h" + (f" (fix {fix} {fix_state}, still firing)" if landed else "")
        detail = (g["why"] or "") + (f" · variants merged: {', '.join(variants)}" if variants else "")
        nxt = OWNER_NEXT[owner] if not landed else f"check whether {fix} fixed it; it is {fix_state} and the problem is still firing"
        age = (now - last).total_seconds() / 3600
        if age <= RECENT_H:
            sev = 2 if (owner == "operator" and g["n24"] >= 10) else 1 if g["n24"] >= 5 or owner == "operator" else 0
            active.append(_item("rec:" + name, "recurring", sev, title, detail, owner, nxt, start, last, trend=trend, variants=variants,
                                anchor="chronicle", fix=fix))
        elif age <= 168:
            it = _item("rec:" + name, "recurring", 0, f"{name}: quiet since {last.strftime('%m-%d %H:%M')}", detail, owner, "none: it stopped",
                       start, last, trend=trend, variants=variants)
            it["resolved_at"] = iso(last + dt.timedelta(hours=RECENT_H))
            resolved.append(it)
    active.sort(key=lambda i: (-i["sev"], i["last"] or ""))
    resolved.sort(key=lambda i: i["last"], reverse=True)
    return active, resolved[:40]


def _by_spec(rows):
    by = {}
    for r in rows:
        s = r.get("s")
        if isinstance(s, str) and s.startswith("L-spec-"):
            by.setdefault(s, []).append(r)
    return by


MOVED_ON = ("decision", "spec-amended", "spec-written", "unblocked", "brief-answered", "note-answered")


def blocked_builders(by, states, now):
    """[(spec, blocking event, pending)]: pending is True when a decision / amendment / rewrite / unblock / answered
    escalation came after the block, i.e. the work is waiting to be re-dispatched, not stuck."""
    st = dict(states or [])
    hits = []
    for s, ev in by.items():
        if _gone(st.get(s)):
            continue
        last = None
        for e in ev:
            if e["ty"] in ("build-started", "shipped", "verdict", "merge-gate-clean"):
                last = None                               # work moved on after the block: no longer blocked
            elif e["ty"] == "build-blocked" or (e["ty"] == "build-done" and e.get("st") not in (None, "DONE")):
                last = e
        if last:
            pending = any(e["ty"] in MOVED_ON and e["t"] > last["t"] for e in ev)
            hits.append((s, last, pending))
    return hits


def _blk(e):
    return (e.get("st") or "BLOCKED") if e["ty"] == "build-done" else "BLOCKED"


def blocked_items(hits, now):
    """(blocked omen, re-dispatch-pending omen); either may be None."""
    out = []
    for pending in (False, True):
        h = [x for x in hits if x[2] == pending]
        if not h:
            out.append(None)
            continue
        ts = [parse(e["t"]) for _, e, _ in h]
        n = len(h)
        if pending:
            out.append(_item("redispatch", "redispatch", 0, f"{n} spec{'s' if n > 1 else ''} re-dispatch pending",
                             "A decision, amendment, rewrite or answer followed the block: " + ", ".join(s for s, _, _ in h),
                             "executor", "re-dispatch " + ", ".join(s for s, _, _ in h[:3]), min(ts), max(ts),
                             subjects=[s for s, _, _ in h], anchor="forge"))
        else:
            out.append(_item("blocked", "blocked", 1, f"{n} builder{'s' if n > 1 else ''} blocked, nothing since",
                             ", ".join(f"{s} {_blk(e)}" for s, e, _ in h), "executor",
                             "re-dispatch or kill " + ", ".join(s for s, _, _ in h[:3]) + " (no decision, amendment or unblock on record)",
                             min(ts), max(ts), subjects=[s for s, _, _ in h], anchor="forge"))
    return out


def owed_digest_items(root, now):
    """Read the digest on every call; its rows own classification and inclusion."""
    path = pathlib.Path(root) / "state" / "owed-digest.json" if root is not None else None

    def warning(reason, stale=False):
        return _item("owed-digest-stale" if stale else "owed-digest-missing", "owedhealth", 1,
                     "Digest rows are stale" if stale else "Digest unavailable",
                     f"{path if path is not None else 'state/owed-digest.json'}: {reason}", "executor",
                     "refresh the digest and check its scheduled reader", now, now, anchor="town")

    if path is None:
        return [warning("no root")]
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return [warning(f"cannot read digest ({type(exc).__name__}): {exc}")]
    if not isinstance(data, dict) or not all(isinstance(data.get(k), list) for k in ("needs_you", "health")):
        return [warning("expected an object with list-valued needs_you and health")]
    at = parse(data.get("at"))
    if at is None:
        return [warning("at is missing or unparseable")]
    if at - now > DIGEST_FUTURE_SKEW:
        return [warning("at is more than 5 minutes in the future")]
    if not all(isinstance(row, dict) for key in ("needs_you", "health") for row in data[key]):
        return [warning("digest rows must be objects")]
    items = [warning("at is older than 2 hours; displayed rows are stale", stale=True)] if now - at > DIGEST_MAX_AGE else []
    for index, row in enumerate(data["needs_you"]):
        spec = row.get("spec")
        item = _item(f"owedop:{index}", "owedop", 1,
                     f"{spec} {row.get('criterion') or ''}: operator action", row.get("ask", ""), "operator",
                     "carry out the digest's ask", parse(row.get("since")), at, subjects=[spec], anchor="town")
        item["since"] = row.get("since")
        items.append(item)
    groups = {}
    for row in data["health"]:
        groups.setdefault(str(row.get("kind") or "unknown"), []).append(row)
    for kind, rows in groups.items():
        detail = "\n".join(f"{row.get('spec') or '-'} {row.get('criterion') or '-'}: "
                           + str(row.get("text") or row.get("reason") or kind) for row in rows)
        times = [t for row in rows if (t := parse(row.get("since"))) is not None]
        items.append(_item(f"owedhealth:{kind}", "owedhealth", 1, f"{len(rows)} {kind} digest rows", detail,
                           "thinker", "resolve the checks listed in the digest", min(times) if times else at, at,
                           subjects=[row["spec"] for row in rows if row.get("spec")], anchor="town"))
    return items


def decision_requests(rows, now):
    """NEEDS YOU (c): message-sent addressed to the operator, with no brief-answered / note-answered on that subject since."""
    hits = []
    for i, r in enumerate(rows):
        if r["ty"] == "message-sent" and str(r.get("to") or "").lower() in ("operator", "you"):
            if not any(x["ty"] in ("brief-answered", "note-answered") and x.get("s") == r.get("s") and x["t"] > r["t"] for x in rows[i + 1:]):
                hits.append(r)
    if not hits:
        return None
    ts = [parse(r["t"]) for r in hits]
    return _item("decide", "decide", 1, f"{len(hits)} decision request{'s' if len(hits) > 1 else ''} sent to you, unanswered",
                 "; ".join(f"{r.get('s')}: {r.get('why') or ''}"[:140] for r in hits[:4]), "operator",
                 "answer " + ", ".join(sorted({str(r.get('s')) for r in hits})[:3]), min(ts), max(ts),
                 subjects=[], anchor="keep")


def churn_item(by, states, now):
    st = dict(states or [])
    cut = now - dt.timedelta(hours=CHURN_H)
    hits = []
    for s, ev in by.items():
        if _gone(st.get(s)):
            continue
        vs = [e for e in ev if e["ty"] == "verdict" and parse(e["t"]) and parse(e["t"]) >= cut]
        if len(vs) < 3 or vs[-1].get("ok"):
            continue
        if any(e["ty"] == "merge-gate-clean" and e["t"] > vs[-1]["t"] for e in ev):
            continue
        hits.append((s, len(vs), parse(vs[0]["t"]), parse(vs[-1]["t"])))
    if not hits:
        return None
    hits.sort(key=lambda h: -h[1])
    w = hits[0]
    return _item("churn", "churn", 1, f"{len(hits)} spec{'s' if len(hits) > 1 else ''} graded again and again (last 48h)",
                 f"{w[0]} graded {w[1]}x in 48h, never confirmed. " + ", ".join(f"{h[0]} x{h[1]}" for h in hits[1:6]),
                 "thinker", f"read the rejected criteria on {w[0]}; fix the review_path or evidence type, or kill the spec",
                 min(h[2] for h in hits), max(h[3] for h in hits), subjects=[h[0] for h in hits], anchor="court",
                 trend=f"{sum(h[1] for h in hits)} grades across {len(hits)} specs in 48h")


CAP_MIN = {"spec-writer": 30, "spec-auditor": 15, "builder": 90, "grader": 15, "reviewer": 30, "plan-auditor": 15, "research": 5,
           "reuse-scout": 15, "charter-reviewer": 30, "probe": 120, "owed-sweeper": 30}


def vanished_item(rows, states, now):
    st = dict(states or [])
    ended = {r.get("sp") for r in rows if r["ty"] in ("spawn-done", "spawn-failed", "spawn-stale") and r.get("sp")}
    shipped_at = {}
    for r in rows:
        if r["ty"] == "shipped" and r.get("s"):
            shipped_at[r["s"]] = max(shipped_at.get(r["s"], ""), r["t"])
    cut = now - dt.timedelta(hours=24)
    hits = []
    for r in rows:
        if r["ty"] not in ("spawn-started", "build-started") or not r.get("sp") or r["sp"] in ended:
            continue
        t = parse(r["t"])
        if not t or t < cut:
            continue
        role = r.get("role") or ("builder" if r["ty"] == "build-started" else "")
        age_min = (now - t).total_seconds() / 60
        if age_min <= 2 * CAP_MIN.get(role, 30):
            continue                                      # still inside its allowance
        s = r.get("s")
        if _gone(st.get(s)) or shipped_at.get(s, "") > r["t"]:
            continue
        hits.append((r["sp"], s, t))
    if not hits:
        return None
    return _item("vanished", "vanished", 1, f"{len(hits)} run{'s' if len(hits) > 1 else ''} vanished without a terminal event",
                 ", ".join(f"{sp} ({s})" for sp, s, _ in hits[:8]), "relay",
                 "check the pane that served " + hits[0][0] + "; re-dispatch " + str(hits[0][1]),
                 min(h[2] for h in hits), max(h[2] for h in hits), subjects=[h[1] for h in hits if h[1]], anchor="chronicle")


# ---- merged vs merely gate-clean ----------------------------------------------------------------------------
def master_merges(repos, cache=None, run=subprocess.run):
    """{spec: iso} for every `merge(L-spec-N)` style commit on the repos' current branch. Read-only `git log`."""
    out = {}
    for repo in repos:
        try:
            p = run(["git", "-C", str(repo), "log", "-n", "1500", "--format=%cI%x09%s"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if getattr(p, "returncode", 1) != 0:
            continue
        for line in p.stdout.splitlines():
            ts, _, subj = line.partition("\t")
            if not re.match(r"^(merge|window)\b", subj, re.I):
                continue
            for sid in re.findall(r"L-spec-\d+", subj):
                out[sid] = max(out.get(sid, ""), ts)
    return out


def merge_info(rows, by, states, master):
    st = dict(states or [])
    windowed = {}
    for r in rows:
        if r["ty"] in ("window-verified", "window-merged", "shipped-in-window", "project-merged"):
            for sid in r.get("kept") or []:
                if r["t"] >= windowed.get(sid, ("", ""))[0]:
                    windowed[sid] = (r["t"], r["s"] or r["ty"])
    out = {}
    for s, ev in by.items():
        if _gone(st.get(s)):
            continue
        mg = max((e["t"] for e in ev if e["ty"] == "merge-gate-clean"), default=None)
        if not mg:
            continue
        sh = max((e["t"] for e in ev if e["ty"] == "shipped"), default="")
        if sh > mg:
            continue
        via = None
        w = windowed.get(s)
        if w and w[0] > mg:
            via = "window " + str(w[1])
        elif master.get(s, "") > mg:
            via = "commit on master"
        out[s] = {"gate": mg, "merged": bool(via), "via": via}
    return out


# ---- the entry point -----------------------------------------------------------------------------------------
def compute(rows, states, now, root=None, master=None):
    """The whole model, as JSON-able data."""
    by = _by_spec(rows)
    active, resolved = recurring(rows, states, now, root)
    b1, b2 = blocked_items(blocked_builders(by, states, now), now)
    for it in (b1, b2, churn_item(by, states, now), vanished_item(rows, states, now), decision_requests(rows, now)):
        if it:
            active.append(it)
    active.extend(owed_digest_items(root, now))
    # self-check: drop an omen whose subject specs are all finished
    st = dict(states or [])
    active = [i for i in active if i["kind"] in ("owedop", "owedhealth") or not (i["subjects"] and all(_gone(st.get(s)) for s in i["subjects"]))]   # digest rows live on shipped specs
    active.sort(key=lambda i: (not i["needs_you"], -i["sev"], i["last"] or ""))
    return {"v": 2, "at": iso(now), "items": active, "resolved": resolved, "merge": merge_info(rows, by, states, master or {}),
            "needs_you": sum(1 for i in active if i["needs_you"]), "health": sum(1 for i in active if not i["needs_you"])}
