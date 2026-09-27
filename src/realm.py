"""doit realm — the ledger as a lit overworld, served live on localhost.

Read-only on the DO-IT root. Tails events/*.jsonl, watches seat/ for claims,
re-derives states through fold.py, and pushes everything to the page over
Server-Sent Events. Never appends, never edits, never touches a repository.

    doit realm                 serve on http://127.0.0.1:8642 (loopback, no key)
    doit realm --public        bind every interface; the page and API then require
                               the key in ~/.do-it/realm.key (created on first run)
    doit realm --port 9000     another port
    doit realm --hours 48      replay window handed to the page on open
    doit realm --snapshot F    write the bootstrap JSON to F and exit
                               (what the hosted artifact embeds)
    doit realm --audit         print the wall-clock audit and the omen metrics as JSON and
                               exit: what look / omens.py can read without HTTP (read-only)
    doit realm --push URL      also push the last 24 h to the hosted realm at URL
                               every 30 s (and the whole ledger every 10 min), with
                               the token in ~/.do-it/realm.push. Outbound only.
"""
import argparse
import bisect
import collections
import datetime as dt
import http.server
import json
import os
import pathlib
import queue
import re
import secrets
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(os.environ.get("DOIT_ROOT", pathlib.Path.home() / ".do-it"))
EVENTS, SEAT = ROOT / "events", ROOT / "seat"
HERE = pathlib.Path(__file__).resolve().parent
PAGE = HERE / "realm.html"
# The checkout the panes execute from. The realm may run from its own worktree; states and the
# "merged, not running" signal must still be judged against the live one.
LIVE_REPO = pathlib.Path(os.environ.get("DOIT_LIVE_REPO", pathlib.Path.home() / "do-it-v2"))
FOLD = (LIVE_REPO / "src" / "fold.py") if (LIVE_REPO / "src" / "fold.py").is_file() else (HERE / "fold.py")

KEEP = {
    "spawn-started", "spawn-done", "spawn-failed", "build-started", "build-done", "build-blocked",
    "verdict", "review", "merge-gate-clean", "merge-gate-rework", "shipped", "escalation-blocking",
    "question", "decision", "spec-written", "charter-filed", "plan-written", "tick", "spec-killed",
    "deploy-landed", "deploy-failed", "deploy-started", "deploy-refused", "charter-retracted", "charter-complete",
    "brief", "brief-answered", "lesson", "spec-carried", "inbound-registered", "inbound-closed", "inbound-covered",
    "spec-shape-failed", "owed-ac", "owed-met", "unblocked",
}
CHARTER_ONLY = {"audit-finding", "charter-gap", "l1-complete", "sweep-fixpoint", "cut-written", "planner-started",
                "planner-ended", "probe-run", "seam-undefined"}   # kept only on charter subjects
ROLE_CAPS_MIN = {"spec-writer": 30, "spec-auditor": 15, "builder": 90, "grader": 15, "reviewer": 30,
                 "plan-auditor": 15, "research": 5, "reuse-scout": 15, "charter-reviewer": 30, "probe": 120}


# ---------------------------------------------------------------- shaping
def role_of(spawn):
    if not spawn:
        return None
    parts = spawn.split("-")
    return "-".join(parts[1:-1]) if len(parts) >= 3 else None


def actor_of(path):
    parts = pathlib.Path(path).stem.split("-")
    return "-".join(parts[1:-1]) if len(parts) >= 3 else pathlib.Path(path).stem


def trunc(s, n=240):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def compact(e, actor):
    """One ledger row → the small dict the page animates from. None if not kept."""
    ty = e.get("type")
    subj = e.get("subject")
    if ty not in KEEP and not (ty in CHARTER_ONLY and str(subj or "").startswith("L-charter-")):
        return None
    if ty == "lesson" and not (actor == "thinker" and e.get("problem")):
        return None                      # only the Thinker's problem-naming lessons are a handled signal
    r = {"t": e.get("ts", ""), "ty": ty, "s": subj, "a": actor, "pr": e.get("project")}
    if ty in CHARTER_ONLY:
        r["k"] = e.get("kind") or e.get("stage") or e.get("status")
        r["why"] = trunc(e.get("finding") or e.get("why") or e.get("reason") or e.get("gap") or e.get("text") or e.get("seam") or "", 220)
        if ty == "spawn-done":
            pass
        return {k: v for k, v in r.items() if v is not None}
    sp = e.get("spawn")
    if sp:
        r["sp"] = sp
        r["r"] = e.get("role") or role_of(sp)
    if ty == "spawn-done":
        r.update(dur=e.get("duration_ms"), tok=e.get("subagent_tokens"), st=e.get("status"), m=e.get("model_used"))
    elif ty == "spawn-failed":
        r["why"] = trunc(e.get("why"), 160)
    elif ty == "spawn-started":
        pass
    elif ty == "build-done":
        r.update(st=e.get("status"), vx=e.get("verify_exit"))
    elif ty == "build-blocked":
        r["why"] = trunc(e.get("reason"), 200)
    elif ty == "verdict":
        r.update(ok=e.get("confirmed"), n=e.get("n"), ca=len(e.get("cannot_assess") or []))
    elif ty == "review":
        r.update(d=e.get("depth"), nb=e.get("n_blocking"))
    elif ty == "merge-gate-rework":
        r["why"] = trunc(e.get("undetermined") or e.get("why"), 160)
    elif ty in ("escalation-blocking", "question"):
        r.update(why=trunc(e.get("why") or e.get("asks"), 260), df=trunc(e.get("default"), 200), dl=e.get("deadline") or "",
                 irr=trunc(e.get("irreversible"), 160) if e.get("irreversible") and not re.match(r"^\s*(none|no|false)\b", str(e.get("irreversible")), re.I) else None,
                 bl=", ".join(str(x) for x in (e.get("blocks") or [])[:4]) or None)
    elif ty == "decision":
        r["why"] = trunc(e.get("why"), 260)
        if e.get("ref"):
            r["ref"] = e.get("ref")
            r["rs"] = _ref_subject(e.get("ref"))
    elif ty == "unblocked":
        r["why"] = trunc(e.get("why") or e.get("reason"), 200)
    elif ty == "spec-written":
        r.update(ac=e.get("ac_count"), fp=len(e.get("footprint") or []), ch=e.get("charter"), re=bool(e.get("reasserted")) or None)
    elif ty == "charter-filed":
        r["ti"] = trunc(e.get("title"), 200)
    elif ty == "tick":
        r["ln"] = e.get("lane")
    elif ty in ("spec-killed", "charter-retracted"):
        r["why"] = trunc(e.get("why"), 200)
    elif ty == "shipped":
        r["sha"] = e.get("sha")
    elif ty == "brief":
        r.update(pb=e.get("problem"), cond=e.get("condition"), key=e.get("key"), own=e.get("owner"), fix=e.get("fix") if re.match(r"^L-(charter|spec)-\d+$", str(e.get("fix") or "")) else None,
                 why=trunc(e.get("why") or e.get("statement") or e.get("text"), 220), req=e.get("requirement"))
    elif ty == "brief-answered":
        r.update(ref=e.get("ref"), why=trunc(e.get("why"), 160), rs=_ref_subject(e.get("ref")))
    elif ty == "lesson":
        r.update(pb=e.get("problem"), fix=e.get("fix"), why=trunc(e.get("text") or e.get("why"), 240))
    elif ty in ("spec-carried", "inbound-closed", "inbound-covered"):
        r.update(src=e.get("source") or e.get("pr"), why=trunc(e.get("why") or e.get("reason"), 160))
    elif ty == "inbound-registered":
        r.update(au=e.get("author_login"), ti=trunc(e.get("title"), 120), src=e.get("source"))
    elif ty == "spec-shape-failed":
        r["why"] = trunc(e.get("why") or e.get("reason") or ", ".join(str(x) for x in (e.get("findings") or [])[:3]), 160)
    elif ty in ("owed-ac", "owed-met"):
        r.update(c=e.get("criterion"), dl=e.get("wake_at") or e.get("deadline"))
    return {k: v for k, v in r.items() if v is not None}


_REF_CACHE = {}


def _ref_subject(ref):
    """`brief-answered.ref` is `<file>:<line>`; resolve it to that line's subject, read-only."""
    if not ref or ":" not in str(ref):
        return None
    if ref in _REF_CACHE:
        return _REF_CACHE[ref]
    name, _, n = str(ref).rpartition(":")
    out = None
    try:
        f = EVENTS / name
        if f.is_file() and n.isdigit():
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if i == int(n):
                    out = json.loads(line).get("subject")
                    break
    except (OSError, json.JSONDecodeError, ValueError):
        out = None
    _REF_CACHE[ref] = out
    return out


def read_all():
    """Every kept row in every file, oldest first, plus per-file byte offsets."""
    rows, offsets, raw = [], {}, []
    for f in sorted(EVENTS.glob("*.jsonl")):
        actor = actor_of(f)
        data = f.read_bytes()
        offsets[f.name] = len(data) - (0 if data.endswith(b"\n") or not data else len(data.split(b"\n")[-1]))
        for line in data.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw.append(e)
            c = compact(e, actor)
            if c:
                rows.append(c)
    rows.sort(key=lambda r: r["t"])
    raw.sort(key=lambda e: str(e.get("ts", "")))
    return rows, raw, offsets


def role_stats(raw):
    roles = collections.defaultdict(lambda: {"done": 0, "failed": 0, "ms": [], "tok": [], "fails": collections.Counter()})
    for e in raw:
        if e.get("type") not in ("spawn-done", "spawn-failed"):
            continue
        r = role_of(e.get("spawn"))
        if not r:
            continue
        d = roles[r]
        if e["type"] == "spawn-done":
            d["done"] += 1
            if e.get("duration_ms"):
                d["ms"].append(e["duration_ms"])
            if e.get("subagent_tokens"):
                d["tok"].append(e["subagent_tokens"])
        else:
            d["failed"] += 1
            w = (e.get("why") or "").lower()
            k = ("unserved" if "unserved" in w else "timeout" if "timeout" in w or "timed out" in w
                 else "contamination" if "contamin" in w else "refused" if "refus" in w
                 else "killed" if "sigterm" in w or "killed" in w else "other")
            d["fails"][k] += 1
    out = {}
    for r, d in roles.items():
        ms, tok = sorted(d["ms"]), d["tok"]
        out[r] = {"done": d["done"], "failed": d["failed"],
                  "med_min": round(statistics.median(ms) / 60000, 1) if ms else None,
                  "p90_min": round(ms[int(len(ms) * 0.9)] / 60000, 1) if ms else None,
                  "med_tok": int(statistics.median(tok)) if tok else None,
                  "fails": dict(d["fails"])}
    return out


CHARTER_RE = re.compile(r"L-charter-\d+")


def charter_of(v):
    m = CHARTER_RE.search(str(v or ""))
    return m.group(0) if m else None


SECTION_RE = re.compile(r"^##\s*\d*\.?\s*(.+?)\s*$", re.M)


def charter_text(cid):
    """Intent, done-condition and requirement count, read from content/<charter>.md."""
    f = ROOT / "content" / f"{cid}.md"
    if not f.is_file():
        return {}
    try:
        body = f.read_text(errors="replace")
    except OSError:
        return {}
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    parts = SECTION_RE.split(body)
    secs = {parts[i].strip().lower(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}
    out = {}
    for key in ("corrects", "target"):
        m = re.search(rf"^{key}\s*:\s*(.+)$", body[:4000], re.M)
        if m:
            out[key] = m.group(1).strip().strip('"')
    for name, text in secs.items():
        paras = [p.strip().replace("\n", " ") for p in re.split(r"\n\s*\n", text) if p.strip()]
        if name.startswith("intent") and paras:
            out["intent"] = trunc(paras[0], 900)
        elif name.startswith("done") and paras:
            out["done"] = trunc(paras[0], 500)
        elif name.startswith("requirement"):
            out["n_req"] = sum(1 for l in text.splitlines() if re.match(r"^\s*(?:[-*]|R\d+|\d+[.)])\s", l))
    return out


_SPEC_CACHE = {}


def spec_text(sid):
    """A spec's title and one plain paragraph of what it is for, from content/<spec>.md."""
    f = ROOT / "content" / f"{sid}.md"
    if not f.is_file():
        return None
    try:
        mtime = f.stat().st_mtime
        c = _SPEC_CACHE.get(sid)
        if c and c[0] == mtime:
            return c[1]
        body = f.read_text(errors="replace")
    except OSError:
        return None
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    fm = {}
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end > 0:
            for m in re.finditer(r"^(title|spec_id|charter|author|status)\s*:\s*(.+)$", body[3:end], re.M):
                fm[m.group(1)] = m.group(2).strip().strip('"')
            body = body[end + 4:]
    generic = re.compile(r"^(goal|intent|summary|purpose|what|why|context|background|problem|requirements?|acceptance|verification|writes|boundaries|scope|assumptions|evidence|notes?|done)\b", re.I)
    h1s = [m.group(1).strip() for m in re.finditer(r"^#\s+(.+)$", body, re.M)]
    # v2 specs carry `unit:` (and sometimes `title:`) in a plain header block before the first heading
    head = body[:2500]
    for key in ("title", "unit"):
        m = re.search(rf"^{key}\s*:\s*(.+)$", head, re.M)
        if m and key not in fm:
            fm[key] = m.group(1).strip().strip('"')
    title = fm.get("title") or fm.get("unit") or fm.get("spec_id") or next((t for t in h1s if not generic.match(t)), None) or (h1s[0] if h1s else sid)
    title = re.sub(r"^L-spec-\d+\s*[·:-]\s*", "", title)
    title = re.sub(r"^\d+-", "", title).replace("-", " ") if re.match(r"^[a-z0-9-]+$", title) else title
    # the first prose paragraph under a heading that names intent, else the first prose paragraph at all
    parts = re.split(r"^#{1,3}\s+(.+?)\s*$", body, flags=re.M)
    intent = None
    for i in range(1, len(parts) - 1, 2):
        name = parts[i].lower()
        if re.search(r"intent|goal|purpose|what\b|why\b|summary|problem|outcome", name):
            paras = [x.strip() for x in re.split(r"\n\s*\n", parts[i + 1]) if x.strip() and not x.strip().startswith(("|", "```", "-", "*", "<"))]
            if paras:
                intent = paras[0]
                break
    if not intent:
        paras = [x.strip() for x in re.split(r"\n\s*\n", body) if x.strip() and not x.strip().startswith(("#", "|", "```", "-", "*", "<", "---"))]
        intent = paras[0] if paras else ""
    intent = re.sub(r"\s+", " ", intent)
    out = {"title": trunc(title, 140), "intent": trunc(intent, 600), "charter": charter_of(fm.get("charter"))}
    for key in ("corrects", "target"):
        m = re.search(rf"^{key}\s*:\s*(.+)$", head, re.M)
        if m:
            out[key] = m.group(1).strip().strip('"')
    _SPEC_CACHE[sid] = (mtime, out)
    return out


def charter_index(raw):
    """charter → {title, project, filed, planned, retracted, intent, done, n_req}; spec → charter."""
    ch, spec_ch = {}, {}
    for e in raw:
        ty, subj = e.get("type"), e.get("subject")
        if ty == "charter-filed":
            ch.setdefault(subj, {}).update(title=trunc(e.get("title"), 220), project=e.get("project"), filed=e.get("ts"))
        elif ty == "spec-written":
            c = charter_of(e.get("charter"))
            if c and subj:
                spec_ch[subj] = c
                ch.setdefault(c, {}).setdefault("project", e.get("project"))
        elif ty in ("plan-written", "charter-retracted", "charter-complete"):
            ch.setdefault(subj, {})[ty.split("-")[1]] = e.get("ts")
    # A spec file may name a charter in its frontmatter that its spec-written event did not carry.
    content = ROOT / "content"
    if content.is_dir():
        for f in content.glob("L-spec-*.md"):
            if f.stem in spec_ch:
                continue
            try:
                m = re.search(r"^charter:\s*(L-charter-\d+)", f.read_text(errors="replace")[:3000], re.M)
            except OSError:
                continue
            if m:
                spec_ch[f.stem] = m.group(1)
                ch.setdefault(m.group(1), {})
    for cid, meta in ch.items():
        meta.update(charter_text(cid))
    return ch, spec_ch



PR_RE = re.compile(r"/pull/(\d+)")


def maker_index(raw, states):
    """Trusted inbound authors and what became of each thing they sent in.

    An inbound-registered event is the thing arriving (a PR). A spec file under
    content/ whose author or carry source names that PR is what it became."""
    authors = {}
    tf = ROOT / "trusted_inbound_authors.toml"
    if tf.is_file():
        for m in re.finditer(r'login\s*=\s*"([^"]+)"[\s\S]*?email\s*=\s*"([^"]+)"', tf.read_text(errors="replace")):
            authors[m.group(1)] = {"email": m.group(2)}
    if not authors:
        authors["yitzchak-eg"] = {"email": "yitzchak@ephraimgreenblatt.com"}
    st = dict(states or [])
    pr_to_spec = {}
    content = ROOT / "content"
    if content.is_dir():
        for f in content.glob("carry-L-spec-*.packet.md"):
            m = PR_RE.search(f.read_text(errors="replace")[:6000])
            if m:
                pr_to_spec.setdefault(m.group(1), f.name[len("carry-"):-len(".packet.md")])
        for f in content.glob("L-spec-*.md"):
            head = f.read_text(errors="replace")[:4000]
            if "yitzchak" not in head.lower():
                continue
            m = PR_RE.search(head)
            if m:
                pr_to_spec.setdefault(m.group(1), f.stem)
    out = {}
    for login, meta in authors.items():
        items = {}
        for e in raw:
            if e.get("type") != "inbound-registered" or e.get("author_login") != login:
                continue
            src = e.get("source") or e.get("subject") or ""
            m = PR_RE.search(src)
            pr = m.group(1) if m else src
            it = items.setdefault(pr, {"pr": pr, "url": src if src.startswith("http") else None, "ts": e.get("ts"),
                                       "title": re.sub(r"^(spec-pending-review|inbound-spec):\s*", "", e.get("title") or "").strip(),
                                       "kind": (e.get("title") or "").split(":")[0], "project": e.get("project"),
                                       "what": trunc(re.sub(r"^## What / why\s*", "", (e.get("body") or "").strip()).split("\n\n")[0].replace("\n", " "), 320)})
            it["ts"] = min(it["ts"], e.get("ts") or it["ts"])
        for pr, it in items.items():
            spec = pr_to_spec.get(pr)
            it["spec"] = spec
            it["state"] = st.get(spec) if spec else "pending"
        specs = {it["spec"] for it in items.values() if it["spec"]}
        for e in raw:
            if e.get("type") == "shipped" and e.get("subject") in specs:
                for it in items.values():
                    if it["spec"] == e["subject"]:
                        it["shipped"] = e.get("ts")
                        it["sha"] = e.get("sha")
        out[login] = {**meta, "name": "Yitzy" if login.startswith("yitz") else login,
                      "items": sorted(items.values(), key=lambda x: x["ts"] or "", reverse=True)}
    return out


_CI = {"at": 0.0, "v": None}
CI_REPO = pathlib.Path(os.environ.get("DOIT_CI_REPO", "/opt/albert-scott"))
CI_BRANCH = os.environ.get("DOIT_CI_BRANCH", "master")


def ci_status():
    """Master's latest workflow conclusions, via `gh run list` (read-only), cached five minutes."""
    if time.monotonic() - _CI["at"] < 300 and _CI["v"] is not None:
        return _CI["v"]
    v = None
    try:
        p = subprocess.run(["gh", "run", "list", "--branch", CI_BRANCH, "--limit", "25", "--json", "name,conclusion,status,updatedAt,headSha"],
                           capture_output=True, text=True, timeout=40, cwd=str(CI_REPO))
        runs = json.loads(p.stdout or "[]")
        if runs:
            # per workflow, the latest COMPLETED conclusion on the branch: a workflow that last ran red is red
            # until it runs again, whichever commit it ran on
            latest = {}
            for r in sorted(runs, key=lambda r: r.get("updatedAt") or ""):
                if r.get("status") == "completed":
                    latest[r["name"]] = r
            fails = [r for r in latest.values() if r.get("conclusion") == "failure"]
            v = {"sha": runs[0]["headSha"][:7], "branch": CI_BRANCH, "red": len(fails), "green": bool(latest) and not fails,
                 "runs": [{"name": r["name"], "conclusion": r.get("conclusion"), "at": r.get("updatedAt"), "sha": r["headSha"][:7]} for r in latest.values()],
                 "red_since": min((r.get("updatedAt") or "" for r in fails), default=None)}
    except (OSError, ValueError, subprocess.SubprocessError) as ex:
        sys.stderr.write(f"realm: gh failed: {ex}\n")
    _CI.update(at=time.monotonic(), v=v)
    return v


_FIRES = {"at": 0.0, "v": None}


def fires_register():
    """The Thinker's fires register (~/.do-it/fires.py --json, read-only): recurring problem classes and their fixes. Cached ten minutes."""
    script = ROOT / "fires.py"
    if not script.is_file():
        return None
    if time.monotonic() - _FIRES["at"] < 600 and _FIRES["v"] is not None:
        return _FIRES["v"]
    v = None
    try:
        p = subprocess.run([sys.executable, str(script), "--json"], capture_output=True, text=True, timeout=180, env={**os.environ, "DOIT_ROOT": str(ROOT)})
        data = json.loads(p.stdout or "[]")
        if isinstance(data, list):
            v = [{"cls": d.get("class"), "n": d.get("n"), "first": d.get("first"), "last": d.get("last"), "fix": d.get("fix"),
                  "fix_state": d.get("fix_state"), "flag": d.get("flag") or ""} for d in data]
    except (OSError, ValueError, subprocess.SubprocessError) as ex:
        sys.stderr.write(f"realm: fires.py failed: {ex}\n")
    _FIRES.update(at=time.monotonic(), v=v)
    return v


QUOTA_RE = re.compile(r"5h\s+(\d+)%\s*\S?\s*([0-9:]+Z?)?.*?7d\s+(\d+)%\s*\S?\s*([A-Za-z]{3}\s+[0-9:]+Z?)?")
TMUX_SESSION = os.environ.get("DOIT_TMUX_SESSION", "flow")


def quota_status():
    """The weekly and five-hour usage figures each pane prints on its status line (read-only capture)."""
    try:
        p = subprocess.run(["tmux", "list-panes", "-s", "-t", TMUX_SESSION, "-F", "#{pane_id}\t#{window_name}\t#{pane_title}"], capture_output=True, text=True, timeout=10)
        if p.returncode != 0:
            return None
        panes = []
        for line in p.stdout.splitlines():
            pid, win, title = (line.split("\t") + ["", ""])[:3]
            cap = subprocess.run(["tmux", "capture-pane", "-p", "-t", pid, "-S", "-30"], capture_output=True, text=True, timeout=10).stdout
            m = None
            for l in cap.splitlines():
                mm = QUOTA_RE.search(l)
                if mm:
                    m = mm
            if not m:
                continue
            name = re.sub(r"^[^A-Za-z]*", "", title or win).strip() or win
            panes.append({"name": name[:24], "h5": int(m.group(1)), "h5_reset": m.group(2), "d7": int(m.group(3)), "d7_reset": m.group(4)})
        if not panes:
            return None
        worst = max(panes, key=lambda x: max(x["h5"], x["d7"]))
        mx = max(worst["h5"], worst["d7"])
        return {"panes": panes, "max_pct": mx, "reset": (worst["h5_reset"] if worst["h5"] >= worst["d7"] else worst["d7_reset"]),
                "kind": "5h" if worst["h5"] >= worst["d7"] else "7d"}
    except (OSError, subprocess.SubprocessError):
        return None


def sys_signals():
    """Signals no ledger row carries: the root disk, the live checkout vs origin/main, master's CI, the panes' quota."""
    import shutil
    out = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    ci = ci_status()
    if ci:
        out["ci"] = ci
    q = quota_status()
    if q:
        out["quota"] = q
    f = fires_register()
    if f is not None:
        out["fires"] = f
    try:
        u = shutil.disk_usage("/")
        out["disk"] = {"root_pct": round(100 * u.used / u.total, 1), "free_gb": round(u.free / 1e9, 1)}
        t = shutil.disk_usage("/tmp")
        out["disk"]["tmp_pct"] = round(100 * t.used / t.total, 1)
    except OSError:
        pass
    try:
        repo = LIVE_REPO
        behind = subprocess.run(["git", "-C", str(repo), "rev-list", "--count", "HEAD..origin/main"], capture_output=True, text=True, timeout=10)
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10)
        fetched = (repo / ".git" / "FETCH_HEAD")
        out["live"] = {"behind": int(behind.stdout.strip() or 0) if behind.returncode == 0 else None, "head": head.stdout.strip(),
                       "fetched": dt.datetime.fromtimestamp(fetched.stat().st_mtime, dt.timezone.utc).isoformat(timespec="seconds") if fetched.is_file() else None}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return out


_OMENS_SCRIPT = {"at": None, "lines": []}


def tick_omens():
    """Run the Thinker's own ~/.do-it/omens.py (read-only, prints lines) so the page can show what the tick sees."""
    script = ROOT / "omens.py"
    if not script.is_file():
        return None
    try:
        p = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=180, env={**os.environ, "DOIT_ROOT": str(ROOT)})
        lines = []
        for l in p.stdout.splitlines():
            parts = [x.strip() for x in l.split("|", 2)]
            if len(parts) == 3:
                lines.append({"sev": parts[0], "name": parts[1], "detail": trunc(parts[2], 200)})
        _OMENS_SCRIPT.update(at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), lines=lines)
        return _OMENS_SCRIPT
    except (OSError, subprocess.SubprocessError) as ex:
        sys.stderr.write(f"realm: omens.py failed: {ex}\n")
        return None


# ---------------------------------------------------------------- metric history (for trends)
HISTORY_FILE = ROOT / "realm-history.json"      # the realm's own state, never the ledger
HISTORY = {}                                     # metric -> [[iso, value], ...], oldest first
HISTORY_KEEP_H = 72
_H_LOCK = threading.Lock()


def _parse(t):
    try:
        d = dt.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)   # a bare deadline is UTC


def metrics_at(rows, states, claims, at, sys_sig=None, makers=None, spec_ch=None):
    """The number behind each omen, as of `at` (a datetime). Rows are compact, oldest first.
    Everything here is reconstructable from the ledger at any past time except states-based
    counts (owed, closable, churn-by-state) which use the states given."""
    T = at.isoformat(timespec="seconds")
    H1 = (at - dt.timedelta(hours=1)).isoformat(timespec="seconds")
    H12 = (at - dt.timedelta(hours=12)).isoformat(timespec="seconds")
    H24 = (at - dt.timedelta(hours=24)).isoformat(timespec="seconds")
    rs = [r for r in rows if r["t"] <= T]
    m = {}
    # lane and executor gap
    ticks = [r for r in rs if r["ty"] == "tick"]
    if ticks:
        m["lane"] = ticks[-1].get("ln")
        lt = _parse(ticks[-1]["t"])
        m["exec_gap_min"] = round((at - lt).total_seconds() / 60, 1) if lt else None
    # ravens: open escalations and overdue ones
    esc, dec = {}, {}
    for r in rs:
        if r["ty"] in ("escalation-blocking", "question"):
            esc.setdefault(r["s"], []).append(r)
        elif r["ty"] == "decision" and r["s"] in esc:
            esc.pop(r["s"], None)
    m["ravens"] = len(esc)
    over = 0
    for subj, l in esc.items():
        a0 = _parse(l[0]["t"])
        dl = _parse(l[-1].get("dl") or "")
        if (a0 and (at - a0).total_seconds() > 4 * 3600) or (dl and dl < at):
            over += 1
    m["ravens_overdue"] = over
    # shelf: written, not built or killed
    shelf = {}
    for r in rs:
        if r["ty"] == "spec-written":
            shelf[r["s"]] = r["t"]
        elif r["ty"] in ("build-started", "spec-killed"):
            shelf.pop(r["s"], None)
    m["shelf"] = len(shelf)
    oldest = min((_parse(t) for t in shelf.values() if _parse(t)), default=None)
    m["shelf_oldest_h"] = round((at - oldest).total_seconds() / 3600, 1) if oldest else 0
    # inbound: registered, not carried or closed
    inb = {}
    for r in rs:
        if r["ty"] == "inbound-registered" and r.get("src"):
            inb[r["src"]] = r["t"]
        elif r["ty"] in ("spec-carried", "inbound-closed", "inbound-covered") and r.get("src"):
            for k in list(inb):
                if str(r["src"]) in k or k in str(r["src"]):
                    inb.pop(k, None)
    m["inbound"] = sum(1 for t in inb.values() if _parse(t) and (at - _parse(t)).total_seconds() > 2 * 3600)
    # runs: unclaimed, vanished, failure rate, deadlocks, blocked, gate
    started, ended = {}, set()
    for r in rs:
        if r["ty"] in ("spawn-started", "build-started") and r.get("sp"):
            started[r["sp"]] = r
        elif r["ty"] in ("spawn-done", "spawn-failed") and r.get("sp"):
            ended.add(r["sp"])
    unclaimed = vanished = 0
    for sp, r in started.items():
        if sp in ended:
            continue
        t0 = _parse(r["t"])
        age = (at - t0).total_seconds() / 60 if t0 else 0
        role = r.get("r") or role_of(sp)
        cap = ROLE_CAPS_MIN.get(role, 30)
        cl = _parse(claims.get(sp) or "")
        if 1 < age <= 3 * cap and not (cl and cl <= at):
            unclaimed += 1
        if 3 * cap < age < 48 * 60:
            vanished += 1
    m["unclaimed"], m["vanished"] = unclaimed, vanished
    rec = [r for r in rs if r["ty"] in ("spawn-done", "spawn-failed") and r["t"] > H1]
    m["spike"] = round(sum(1 for r in rec if r["ty"] == "spawn-failed") / len(rec), 2) if len(rec) >= 5 else 0
    dead = churn = blocked = gate = 0
    per = {}
    for r in rs:
        if not str(r.get("s") or "").startswith("L-spec-"):
            continue
        d = per.setdefault(r["s"], {"cont": 0, "lastCont": "", "lastDone": "", "lastWritten": "", "verd": 0, "lastOk": None, "build": None, "buildT": "", "gate": None, "gateT": "", "last": ""})
        d["last"] = r["t"]
        ty = r["ty"]
        if ty == "spawn-failed" and re.search(r"contamin", str(r.get("why") or ""), re.I) and not re.search(r"identical packet|D120", str(r.get("why") or ""), re.I):
            d["cont"] += 1
            d["lastCont"] = r["t"]
        elif ty == "spec-written":
            d["lastWritten"] = r["t"]
        elif ty == "spawn-done":
            d["lastDone"] = r["t"]
        elif ty == "verdict":
            d["verd"] += 1
            d["lastOk"] = bool(r.get("ok"))
        elif ty in ("build-done", "build-blocked"):
            d["build"] = "BLOCKED" if ty == "build-blocked" else r.get("st")
            d["buildT"] = r["t"]
        elif ty in ("merge-gate-clean", "merge-gate-rework"):
            d["gate"] = ty
            d["gateT"] = r["t"]
    stx = dict(states or [])
    for sid, d in per.items():
        if d.get("last", "") < H24 or re.search(r"killed|void|dropped", stx.get(sid, "")):
            continue
        if d["cont"] >= 2 and d["lastDone"] < d["lastCont"] and d.get("lastWritten", "") < d["lastCont"]:
            dead += 1
        if d["verd"] >= 3 and d["lastOk"] is False:
            churn += 1
        if d["build"] and d["build"] != "DONE" and d["buildT"] > H12:
            blocked += 1
        if d["gate"] == "merge-gate-rework" and d["gateT"] > H12:
            gate += 1
    m.update(deadlock=dead, churn=churn, blocked=blocked, gate=gate)
    # shape failures: specs whose latest shape event is a failure with no later spec-written; and their hours lost
    shape_open = 0; shape_h = 0.0
    last_shape, last_written = {}, {}
    for r in rs:
        if r["ty"] == "spec-shape-failed":
            last_shape[r["s"]] = r["t"]
        elif r["ty"] == "spec-written":
            last_written[r["s"]] = r["t"]
    for sid, tt in last_shape.items():
        if last_written.get(sid, "") <= tt:
            shape_open += 1
            t0 = _parse(tt)
            shape_h += (at - t0).total_seconds() / 3600 if t0 else 0
    m["shape"] = shape_open; m["shape_hours"] = round(shape_h, 1)
    # merge conflicts: branches with 2+ reworks mentioning conflict in the last day
    conf = {}
    for r in rs:
        if r["ty"] == "merge-gate-rework" and r["t"] > H24 and "conflict" in str(r.get("why") or "").lower():
            conf[r["s"]] = conf.get(r["s"], 0) + 1
    m["conflict"] = sum(1 for v in conf.values() if v >= 2)
    # owed checks per criterion: open (owed-ac without a later owed-met for the same criterion), overdue, close rate
    ac = {}
    for r in rs:
        if r["ty"] == "owed-ac" and r.get("c"):
            ac[(r["s"], r["c"])] = {"t": r["t"], "dl": r.get("dl"), "met": False}
        elif r["ty"] == "owed-met" and r.get("c") and (r["s"], r["c"]) in ac:
            ac[(r["s"], r["c"])]["met"] = True
    if ac and states:
        stx = dict(states)
        ac = {k: v for k, v in ac.items() if re.match(r"^(shipped|accepted)", stx.get(k[0], ""))}
    if ac:
        open_ = [v for v in ac.values() if not v["met"]]
        over = sum(1 for v in open_ if _parse(v.get("dl") or "") and _parse(v["dl"]) < at)
        m["owed_checks"] = len(open_); m["owed_overdue"] = over; m["owed_close_rate"] = round(100 * (len(ac) - len(open_)) / len(ac), 1)
    # states-based (now only): owed, closable
    if states:
        st = dict(states)
        m["owed"] = sum(1 for v in st.values() if "owed" in v)
        if spec_ch:
            by = {}
            for spec, c in spec_ch.items():
                by.setdefault(c, []).append(st.get(spec))
            closable = 0
            for c, vals in by.items():
                cs = st.get(c, "")
                vals = [v for v in vals if v]
                if not vals or re.search(r"complete|retracted", cs, re.I):
                    continue
                live = [v for v in vals if not re.search(r"killed|void|dropped|unknown", v)]
                if live and all(re.match(r"^(accepted|shipped)$", v) for v in live) and not any(subj in esc for subj, _ in [(sp, 0) for sp, cc in spec_ch.items() if cc == c]):
                    closable += 1
            m["closable"] = closable
    # merged, not shipped, for more than 30 minutes
    mg_open = 0
    lastmg, lastsh = {}, {}
    for r in rs:
        if r["ty"] == "merge-gate-clean":
            lastmg[r["s"]] = r["t"]
        elif r["ty"] == "shipped":
            lastsh[r["s"]] = r["t"]
    for sid, tt in lastmg.items():
        if lastsh.get(sid, "") < tt and _parse(tt) and (at - _parse(tt)).total_seconds() > 1800:
            mg_open += 1
    m["deploy_wait"] = mg_open
    if sys_sig:
        if sys_sig.get("ci"):
            m["ci_red"] = sys_sig["ci"]["red"]
        if sys_sig.get("quota"):
            m["quota_pct"] = sys_sig["quota"]["max_pct"]
        if sys_sig.get("disk"):
            m["disk"] = sys_sig["disk"]["root_pct"]
        if sys_sig.get("live") and sys_sig["live"].get("behind") is not None:
            m["behind"] = sys_sig["live"]["behind"]
    return {k: v for k, v in m.items() if v is not None}


def wallclock(rows, states, now):
    """Where wall-clock goes: the meter running right now (per wait bucket), and the window's burn."""
    st = dict(states or [])
    by = {}
    for r in rows:
        if str(r.get("s") or "").startswith("L-spec-"):
            by.setdefault(r["s"], []).append(r)
    H = lambda a: max(0.0, (now - a).total_seconds() / 3600) if a else 0.0
    live = {k: {"n": 0, "hours": 0.0, "oldest": 0.0, "specs": []} for k in
            ("shelf", "await_grade", "await_review", "await_merge", "await_deploy", "owed")}
    def put(k, sid, since):
        h = H(since)
        b = live[k]; b["n"] += 1; b["hours"] += h; b["oldest"] = max(b["oldest"], h); b["specs"].append([sid, round(h, 1)])
    rework_build = rework_grade = 0.0; rebuilt = regraded = blocked = 0
    burn = {}; started = {}
    for sid, ev in by.items():
        state = st.get(sid, "")
        last = {}
        for e in ev:
            last[e["ty"]] = e
            if e["ty"] in ("spawn-started", "build-started") and e.get("sp"):
                started[e["sp"]] = e
        bl = [e for e in ev if e["ty"] == "spawn-done" and (e.get("r") or role_of(e["sp"])) == "builder" and e.get("dur")]
        gl = [e for e in ev if e["ty"] == "spawn-done" and (e.get("r") or role_of(e["sp"])) == "grader" and e.get("dur")]
        if len(bl) > 1:
            rebuilt += 1; rework_build += sum(x["dur"] for x in bl[1:]) / 3600000
        if len(gl) > 1:
            regraded += 1; rework_grade += sum(x["dur"] for x in gl[1:]) / 3600000
        blocked += max(sum(1 for e in ev if e["ty"] == "build-done" and e.get("st") != "DONE"), sum(1 for e in ev if e["ty"] == "build-blocked"))
        if re.search(r"killed|void|dropped", state):
            continue
        # the meter right now: which wait is this spec sitting in?
        w = last.get("spec-written"); b0 = last.get("build-started"); bd = last.get("build-done"); vd = last.get("verdict"); rv = last.get("review"); mg = last.get("merge-gate-clean"); sh = last.get("shipped")
        t = lambda e: _parse(e["t"]) if e else None
        if state == "written" and w and not b0:
            put("shelf", sid, t(w))
        elif state in ("built", "graded", "building") and bd and (not vd or t(vd) < t(bd)) and bd.get("st") == "DONE":
            put("await_grade", sid, t(bd))
        elif state == "graded" and vd and (not rv or t(rv) < t(vd)):
            put("await_review", sid, t(vd))
        elif state == "reviewing" and rv and (not mg or t(mg) < t(rv)):
            put("await_merge", sid, t(rv))
        elif "owed" in state and sh:
            put("owed", sid, t(sh))
        elif state == "shipped" and mg and (not sh or t(sh) < t(mg)):
            put("await_deploy", sid, t(mg))
    for r in rows:
        if r["ty"] == "spawn-failed":
            w = (r.get("why") or "").lower()
            k = "unserved" if "unserved" in w else "timeout" if "time" in w else "contamination" if "contamin" in w else "refused" if "refus" in w else "other"
            s0 = started.get(r.get("sp"))
            hrs = (_parse(r["t"]) - _parse(s0["t"])).total_seconds() / 3600 if s0 and _parse(s0["t"]) else 0
            b = burn.setdefault(k, {"n": 0, "hours": 0.0}); b["n"] += 1; b["hours"] += max(0, hrs)
    # the executor: when did it last ACT (not tick), and the longest dispatch gap in the window
    acts = [r for r in rows if r.get("a") == "executor" and r["ty"] != "tick"]
    disp = [_parse(r["t"]) for r in rows if r["ty"] in ("spawn-started", "build-started")]
    gap = (0, None, None)
    for a, b in zip(disp, disp[1:]):
        if a and b and (b - a).total_seconds() / 3600 > gap[0]:
            gap = ((b - a).total_seconds() / 3600, a.isoformat(timespec="seconds"), b.isoformat(timespec="seconds"))
    for k in live:
        live[k]["hours"] = round(live[k]["hours"], 1); live[k]["oldest"] = round(live[k]["oldest"], 1)
        live[k]["specs"].sort(key=lambda x: -x[1]); live[k]["specs"] = live[k]["specs"][:12]
    for k in burn:
        burn[k]["hours"] = round(burn[k]["hours"], 1)
    deploys = collections.Counter(r["ty"] for r in rows if r["ty"].startswith("deploy"))
    # rule 3 (agreed 2026-09-27): a critical condition older than 30 minutes with no owner action escalates to the Thinker.
    # Server-side, from the 5-minute series, for the classes a metric carries; the page does the same for its own.
    CRIT = {"unclaimed": ("unclaimed", 1), "deadlock": ("deadlock", 1), "disk": ("disk", 92), "behind": ("behind", 3), "lane": ("lane", 80), "shape_hours": ("shape", 48), "ci_red": ("ci", 1)}
    escalate = []
    with _H_LOCK:
        for metric, (cls, thr) in CRIT.items():
            ser = HISTORY.get(metric) or []
            since = None
            for t, v in reversed(ser):
                if v is None or v < thr:
                    break
                since = t
            if since and (now - _parse(since)).total_seconds() >= 1800:
                escalate.append({"class": cls, "since": since, "value": ser[-1][1]})
    if acts and (now - _parse(acts[-1]["t"])).total_seconds() > 1800:
        escalate.append({"class": "executor", "since": acts[-1]["t"], "value": round((now - _parse(acts[-1]["t"])).total_seconds() / 60)})
    return {"at": now.isoformat(timespec="seconds"), "live": live, "escalate": escalate,
            "rework": {"rebuilt": rebuilt, "regraded": regraded, "build_hours": round(rework_build, 1), "grade_hours": round(rework_grade, 1), "blocked": blocked},
            "burn": burn, "executor_last_act": acts[-1]["t"] if acts else None,
            "dispatch_gap": {"hours": round(gap[0], 1), "from": gap[1], "to": gap[2]},
            "deploys": dict(deploys)}


def history_load():
    global HISTORY
    try:
        HISTORY = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.is_file() else {}
    except (OSError, json.JSONDecodeError):
        HISTORY = {}


def history_add(sample, at):
    T = at.isoformat(timespec="seconds")
    cutoff = (at - dt.timedelta(hours=HISTORY_KEEP_H)).isoformat(timespec="seconds")
    with _H_LOCK:
        for k, v in sample.items():
            ser = HISTORY.setdefault(k, [])
            if ser and ser[-1][0] >= T:
                continue
            ser.append([T, v])
            while ser and ser[0][0] < cutoff:
                ser.pop(0)
        try:
            HISTORY_FILE.write_text(json.dumps(HISTORY, separators=(",", ":")))
        except OSError as ex:
            sys.stderr.write(f"realm: history not saved: {ex}\n")


def history_backfill(rows, claims, hours=24, step_min=15):
    """Reconstruct the ledger-derived series for the last `hours`, only where nothing is stored yet."""
    now = dt.datetime.now(dt.timezone.utc)
    earliest = min((ser[0][0] for ser in HISTORY.values() if ser), default=now.isoformat(timespec="seconds"))
    at = now - dt.timedelta(hours=hours)
    added = 0
    while at < now:
        T = at.isoformat(timespec="seconds")
        if T < earliest:
            m = metrics_at(rows, None, claims, at)
            with _H_LOCK:
                for k, v in m.items():
                    bisect.insort(HISTORY.setdefault(k, []), [T, v])
            added += 1
        at += dt.timedelta(minutes=step_min)
    with _H_LOCK:
        for ser in HISTORY.values():
            ser.sort()
    return added


def sample_forever():
    """Every five minutes: the metrics now, appended to the history and streamed."""
    history_load()
    try:
        rows, raw, _ = read_all()
        n = history_backfill(rows, seat_claims())
        if n:
            sys.stderr.write(f"realm: backfilled {n} history points\n")
    except Exception as ex:  # noqa: BLE001
        sys.stderr.write(f"realm: backfill failed: {ex}\n")
    while True:
        try:
            rows, raw, _ = read_all()
            states = HUB.states or derive_states() or []
            HUB.states = states
            _, spec_ch = charter_index(raw)
            now = dt.datetime.now(dt.timezone.utc)
            m = metrics_at(rows, states, seat_claims(), now, sys_signals(), None, spec_ch)
            history_add(m, now)
            HUB.send("history", {"history": HISTORY, "now": m, "wallclock": wallclock(rows, states, now)})
        except Exception as ex:  # noqa: BLE001
            sys.stderr.write(f"realm: sample failed: {ex}\n")
        time.sleep(300)


def derive_states():
    """`doit states` through fold.py, in a subprocess so a fold error never kills the server."""
    try:
        p = subprocess.run([sys.executable, str(FOLD), "states"], capture_output=True, text=True,
                           timeout=120, env={**os.environ, "DOIT_ROOT": str(ROOT)})
        return [l.split("\t") for l in p.stdout.splitlines() if "\t" in l]
    except Exception as ex:  # noqa: BLE001 — the page shows a stale census, never a dead server
        sys.stderr.write(f"realm: states failed: {ex}\n")
        return None


def seat_claims():
    """spawn → ISO claim time, from the .claimed file mtimes."""
    out = {}
    if not SEAT.is_dir():
        return out
    for f in SEAT.glob("*.claimed"):
        out[f.name[:-8]] = dt.datetime.fromtimestamp(f.stat().st_mtime, dt.timezone.utc).isoformat(timespec="seconds")
    return out


def seat_summary():
    if not SEAT.is_dir():
        return {"packets": 0, "unclaimed": 0, "served": 0}
    names = os.listdir(SEAT)
    packets = [n[:-10] for n in names if n.endswith(".packet.md")]
    have = set(names)
    return {"packets": len(packets),
            "unclaimed": sum(1 for p in packets if f"{p}.claimed" not in have),
            "served": sum(1 for p in packets if f"{p}.output.json" not in have and f"{p}.claimed" in have and f"{p}.result.json" in have or f"{p}.output.json" in have)}


def queue_stats(rows, claims):
    waits = []
    for r in rows:
        if r["ty"] in ("spawn-started", "build-started") and r.get("sp") in claims:
            try:
                a = dt.datetime.fromisoformat(r["t"])
                b = dt.datetime.fromisoformat(claims[r["sp"]])
                w = (b - a).total_seconds() / 60
                if 0 <= w < 24 * 60:
                    waits.append(w)
            except ValueError:
                pass
    waits.sort()
    return {"n": len(waits), "med_min": round(statistics.median(waits), 1) if waits else None,
            "p90_min": round(waits[int(len(waits) * 0.9)], 1) if waits else None,
            "max_min": round(waits[-1], 1) if waits else None}


def bootstrap(hours, states=None):
    rows, raw, _ = read_all()
    claims = seat_claims()
    for r in rows:
        if r.get("sp") in claims and r["ty"] in ("spawn-started", "build-started"):
            r["cl"] = claims[r["sp"]]
    now = dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(hours=hours)).isoformat(timespec="seconds") if hours else ""
    window_rows = [r for r in rows if r["t"] >= since] if since else rows
    # keep every open spawn's start even if it predates the window, so it can still finish on screen
    terminal = {r.get("sp") for r in window_rows if r["ty"] in ("spawn-done", "spawn-failed")}
    started_in = {r.get("sp") for r in window_rows if r["ty"] in ("spawn-started", "build-started")}
    for r in rows:
        if r["t"] < since and r["ty"] in ("spawn-started", "build-started") and r.get("sp") in terminal and r.get("sp") not in started_in:
            window_rows.append(r)
    window_rows.sort(key=lambda r: r["t"])
    titles = {e["subject"]: trunc(e.get("title"), 160) for e in raw if e.get("type") == "charter-filed"}
    charters, spec_charter = charter_index(raw)
    counts = collections.Counter(e.get("type") for e in raw)
    if states is None:
        states = derive_states() or []
    return {"live": True, "now": now.isoformat(timespec="seconds"), "root": str(ROOT),
            "window": [window_rows[0]["t"] if window_rows else now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")],
            "ledger_start": rows[0]["t"] if rows else None, "n_events": len(raw), "events": window_rows,
            "stats": role_stats(raw), "states": states,
            "seat": seat_summary(), "titles": titles, "counts": dict(counts.most_common(60)),
            "queue": queue_stats(rows, claims), "caps": ROLE_CAPS_MIN, "charters": charters, "spec_charter": spec_charter, "makers": maker_index(raw, states), "specs": {sid: t for sid in dict(states) if sid.startswith("L-spec-") for t in [spec_text(sid)] if t}, "sys": sys_signals(), "tick_omens": _OMENS_SCRIPT if _OMENS_SCRIPT["at"] else None, "history": HISTORY, "wallclock": wallclock(rows, states, now)}


# ---------------------------------------------------------------- streaming
class Hub:
    def __init__(self):
        self.clients, self.lock = set(), threading.Lock()
        self.states = None
        self.recent = collections.deque(maxlen=6000)   # (kind, data) in arrival order, for /api/since
        self.seq = int(time.time() * 1000)             # monotonic across restarts, so an old client's seq is never "ahead"
        self.started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    def add(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            self.clients.add(q)
        return q

    def drop(self, q):
        with self.lock:
            self.clients.discard(q)

    def send(self, kind, data):
        msg = (kind, json.dumps(data, separators=(",", ":")))
        with self.lock:
            if kind in ("ledger", "claim"):
                self.seq += 1
                self.recent.append((self.seq, kind, data))
            dead = []
            for q in self.clients:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self.clients.discard(q)


HUB = Hub()


def since(seq, t=None):
    """Everything the stream would have delivered after sequence `seq`, or after ledger time `t`.
    A client that names a time older than this server's buffer gets the rows from the ledger itself,
    so a server restart or a dropped connection never leaves a page silently behind."""
    with HUB.lock:
        rows = [(n, k, d) for (n, k, d) in HUB.recent if n > seq]
        top = HUB.seq
        started = HUB.started
    ledger = [d for (_, k, d) in rows if k == "ledger"]
    claims = [d for (_, k, d) in rows if k == "claim"]
    if t:
        if t < started:                      # the gap predates this server: read the ledger for it
            allrows, _, _ = read_all()
            ledger = [r for r in allrows if r["t"] > t]
            cl = seat_claims()
            claims = [{"sp": sp, "cl": ts} for sp, ts in cl.items() if ts > t]
        else:
            ledger = [d for d in ledger if d.get("t", "") > t]
    return {"seq": top, "ledger": ledger, "claims": claims, "started": started,
            "states": HUB.states, "seat": seat_summary(), "now": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}


def tail_forever(poll=1.0):
    """Follow every events/*.jsonl from its current end; new lines become `ledger` messages."""
    _, _, offsets = read_all()
    partial = {}
    known_claims = seat_claims()
    last_states, want_states = time.monotonic(), False
    while True:
        time.sleep(poll)
        try:
            for f in sorted(EVENTS.glob("*.jsonl")):
                size = f.stat().st_size
                off = offsets.get(f.name, 0)
                if size < off:            # truncated or rotated: start over on this file, silently
                    off, partial[f.name] = 0, b""
                if size == off:
                    continue
                with open(f, "rb") as fh:
                    fh.seek(off)
                    chunk = fh.read(size - off)
                buf = partial.get(f.name, b"") + chunk
                lines = buf.split(b"\n")
                partial[f.name] = lines[-1]
                offsets[f.name] = size - len(lines[-1])
                actor = actor_of(f)
                for line in lines[:-1]:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    c = compact(e, actor)
                    if c:
                        HUB.send("ledger", c)
                        if c["ty"] == "tick":
                            want_states = True
            claims = seat_claims()
            for sp, ts in claims.items():
                if sp not in known_claims:
                    HUB.send("claim", {"sp": sp, "cl": ts})
            known_claims = claims
            if want_states or time.monotonic() - last_states > 120:
                want_states, last_states = False, time.monotonic()
                st = derive_states()
                if st is not None:
                    HUB.states = st
                    HUB.send("states", {"states": st, "seat": seat_summary()})
        except Exception as ex:  # noqa: BLE001
            sys.stderr.write(f"realm: tail error: {ex}\n")


def push_forever(url, token, hours):
    """Outbound push to the hosted realm: the window every 30 s, the whole ledger every 10 min."""
    url = url.rstrip("/")
    last_history = 0.0
    while True:
        try:
            snap = bootstrap(hours, HUB.states)
            snap["live"], snap["remote"] = False, True
            _post(f"{url}/api/push?file=snapshot", token, snap)
            if time.monotonic() - last_history > 600:
                hist = bootstrap(0, HUB.states)
                hist["live"], hist["remote"] = False, True
                _post(f"{url}/api/push?file=history", token, hist)
                last_history = time.monotonic()
        except Exception as ex:  # noqa: BLE001
            sys.stderr.write(f"realm: push failed: {ex}\n")
        time.sleep(30)


def _post(url, token, obj):
    body = json.dumps(obj, separators=(",", ":")).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Realm-Push": token, "Content-Length": str(len(body))})
    with urllib.request.urlopen(req, timeout=60) as r:
        if r.status != 200:
            raise RuntimeError(f"{r.status} from {url}")


def heartbeat_forever():
    n = 0
    while True:
        time.sleep(15)
        n += 1
        HUB.send("heartbeat", {"now": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
        if n % 4 == 0:                       # every minute
            HUB.send("sys", sys_signals())
        if n % 20 == 1:                      # every five minutes, staggered
            o = tick_omens()
            if o:
                HUB.send("tick_omens", o)


# ---------------------------------------------------------------- http
class Handler(http.server.BaseHTTPRequestHandler):
    hours = 24
    protocol_version = "HTTP/1.1"   # every response below sets Content-Length or streams chunked
    key = None          # None: open (loopback). Otherwise every request must carry it.

    def _authorized(self, qs):
        if not self.key:
            return True
        given = qs.get("key", [None])[0] or self.headers.get("X-Realm-Key")
        return bool(given) and secrets.compare_digest(given, self.key)

    def log_message(self, fmt, *args):  # quiet; the page is the log
        if os.environ.get("DOIT_REALM_VERBOSE"):
            super().log_message(fmt, *args)

    def _json(self, obj, status=200):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            # The page holds no ledger data; it is served open so a browser that
            # remembered the key can present it on the API calls that follow.
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._authorized(qs):
            self._json({"error": "key required"}, 401)
            return
        if u.path == "/api/bootstrap":
            hours = int(qs.get("hours", [self.hours])[0])
            self._json(bootstrap(hours, HUB.states))
        elif u.path == "/api/states":
            st = derive_states()
            if st is not None:
                HUB.states = st
            self._json({"states": HUB.states or [], "seat": seat_summary()})
        elif u.path == "/api/charter":
            cid = qs.get("id", [""])[0]
            rows, raw, _ = read_all()
            charters, spec_ch = charter_index(raw)
            mine = {s for s, c in spec_ch.items() if c == cid} | {cid}
            claims = seat_claims()
            out = []
            for r in rows:
                if r.get("s") in mine:
                    if r.get("sp") in claims and r["ty"] in ("spawn-started", "build-started"):
                        r = {**r, "cl": claims[r["sp"]]}
                    out.append(r)
            self._json({"id": cid, "meta": charters.get(cid, {}), "specs": sorted(mine - {cid}), "rows": out})
        elif u.path == "/api/spec":
            sid = qs.get("id", [""])[0]
            self._json(spec_text(sid) or {"error": "no file"}, 200)
        elif u.path == "/api/since":
            try:
                seq = int(qs.get("seq", ["0"])[0])
            except ValueError:
                seq = 0
            self._json(since(seq, qs.get("t", [None])[0]))
        elif u.path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            q = HUB.add()

            def chunk(b):
                self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n")
                self.wfile.flush()
            try:
                chunk(f"event: hello\ndata: {json.dumps({'seq': HUB.seq, 'started': HUB.started})}\n\n".encode())
                while True:
                    try:
                        kind, data = q.get(timeout=20)
                    except queue.Empty:
                        chunk(b": keepalive\n\n")
                        continue
                    chunk(f"event: {kind}\ndata: {data}\n\n".encode())
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                HUB.drop(q)
                self.close_connection = True
        else:
            self._json({"error": "not found"}, 404)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="doit realm", description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=None, help="interface to bind (default 127.0.0.1, or 0.0.0.0 with --public)")
    ap.add_argument("--public", action="store_true", help="bind every interface and require the key in ~/.do-it/realm.key")
    ap.add_argument("--port", type=int, default=8642)
    ap.add_argument("--hours", type=int, default=24, help="replay window handed to the page on open")
    ap.add_argument("--snapshot", help="write bootstrap JSON here and exit (--hours 0 = whole ledger)")
    ap.add_argument("--push", help="hosted realm URL to push snapshots to (token in ~/.do-it/realm.push)")
    ap.add_argument("--audit", action="store_true", help="print the wall-clock audit + omen metrics as JSON and exit")
    a = ap.parse_args(argv)
    if not EVENTS.is_dir():
        sys.exit(f"realm: no ledger at {EVENTS}")
    if a.audit:
        rows, raw, _ = read_all()
        states = derive_states() or []
        _, spec_ch = charter_index(raw)
        now = dt.datetime.now(dt.timezone.utc)
        history_load()
        out = {"at": now.isoformat(timespec="seconds"),
               "metrics": metrics_at(rows, states, seat_claims(), now, sys_signals(), None, spec_ch),
               "wallclock": wallclock(rows, states, now),
               "history_tail": {k: v[-13:] for k, v in HISTORY.items()}}   # last hour of 5-min samples
        print(json.dumps(out, separators=(",", ":")))
        return
    if a.snapshot:
        history_load()
        snap = bootstrap(a.hours)
        snap["live"] = False
        pathlib.Path(a.snapshot).write_text(json.dumps(snap, separators=(",", ":")))
        print(f"{a.snapshot}: {len(snap['events'])} rows, {len(snap['states'])} states")
        return
    if not PAGE.is_file():
        sys.exit(f"realm: page missing at {PAGE}")
    Handler.hours = a.hours
    host = a.host or ("0.0.0.0" if a.public else "127.0.0.1")
    key = None
    if a.public or host not in ("127.0.0.1", "localhost", "::1"):
        kf = ROOT / "realm.key"
        if not kf.is_file():
            kf.write_text(secrets.token_urlsafe(24))
            kf.chmod(0o600)
        key = kf.read_text().strip()
        Handler.key = key
    threading.Thread(target=tail_forever, daemon=True).start()
    threading.Thread(target=heartbeat_forever, daemon=True).start()
    threading.Thread(target=tick_omens, daemon=True).start()
    threading.Thread(target=fires_register, daemon=True).start()
    threading.Thread(target=sample_forever, daemon=True).start()
    if a.push:
        tf = ROOT / "realm.push"
        if not tf.is_file():
            sys.exit(f"realm: --push needs the token in {tf} (deploy.sh writes it)")
        threading.Thread(target=push_forever, args=(a.push, tf.read_text().strip(), a.hours), daemon=True).start()
        print(f"realm: pushing to {a.push} every 30 s")
    srv = http.server.ThreadingHTTPServer((host, a.port), Handler)
    srv.daemon_threads = True
    if key:
        ips = []
        try:
            ips = [l for l in subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split() if ":" not in l]
        except Exception:  # noqa: BLE001
            pass
        for ip in ips or [host]:
            print(f"realm: http://{ip}:{a.port}/?key={key}")
        print(f"realm: bound to {host}:{a.port}; every request needs the key in {ROOT / 'realm.key'} (ledger {ROOT}, read-only; ctrl-c to stop)")
    else:
        print(f"realm: http://{host}:{a.port}/  (ledger {ROOT}, read-only; ctrl-c to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nrealm: down")


if __name__ == "__main__":
    main()
