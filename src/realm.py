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
    doit realm --push URL      also push the last 24 h to the hosted realm at URL
                               every 30 s (and the whole ledger every 10 min), with
                               the token in ~/.do-it/realm.push. Outbound only.
"""
import argparse
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

KEEP = {
    "spawn-started", "spawn-done", "spawn-failed", "build-started", "build-done", "build-blocked",
    "verdict", "review", "merge-gate-clean", "merge-gate-rework", "shipped", "escalation-blocking",
    "question", "decision", "spec-written", "charter-filed", "plan-written", "tick", "spec-killed",
    "deploy-landed", "deploy-failed", "charter-retracted", "charter-complete",
    "brief", "brief-answered", "lesson", "spec-carried", "inbound-registered", "inbound-closed", "inbound-covered",
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
        r.update(why=trunc(e.get("why") or e.get("asks"), 260), df=trunc(e.get("default"), 200), dl=e.get("deadline") or "")
    elif ty == "decision":
        r["why"] = trunc(e.get("why"), 260)
    elif ty == "spec-written":
        r.update(ac=e.get("ac_count"), fp=len(e.get("footprint") or []), ch=e.get("charter"))
    elif ty == "charter-filed":
        r["ti"] = trunc(e.get("title"), 200)
    elif ty == "tick":
        r["ln"] = e.get("lane")
    elif ty in ("spec-killed", "charter-retracted"):
        r["why"] = trunc(e.get("why"), 200)
    elif ty == "shipped":
        r["sha"] = e.get("sha")
    elif ty == "brief":
        r.update(pb=e.get("problem"), cond=e.get("condition"), key=e.get("key"), own=e.get("owner"),
                 why=trunc(e.get("why") or e.get("text"), 220), req=e.get("requirement"))
    elif ty == "brief-answered":
        r.update(ref=e.get("ref"), why=trunc(e.get("why"), 160), rs=_ref_subject(e.get("ref")))
    elif ty == "lesson":
        r.update(pb=e.get("problem"), fix=e.get("fix"), why=trunc(e.get("text") or e.get("why"), 240))
    elif ty in ("spec-carried", "inbound-closed", "inbound-covered"):
        r.update(src=e.get("source") or e.get("pr"), why=trunc(e.get("why") or e.get("reason"), 160))
    elif ty == "inbound-registered":
        r.update(au=e.get("author_login"), ti=trunc(e.get("title"), 120), src=e.get("source"))
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
    for name, text in secs.items():
        paras = [p.strip().replace("\n", " ") for p in re.split(r"\n\s*\n", text) if p.strip()]
        if name.startswith("intent") and paras:
            out["intent"] = trunc(paras[0], 900)
        elif name.startswith("done") and paras:
            out["done"] = trunc(paras[0], 500)
        elif name.startswith("requirement"):
            out["n_req"] = sum(1 for l in text.splitlines() if re.match(r"^\s*(?:[-*]|R\d+|\d+[.)])\s", l))
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


def sys_signals():
    """Signals no ledger row carries: the root disk, the live checkout vs origin/main, the tick's own omens."""
    import shutil
    out = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    try:
        u = shutil.disk_usage("/")
        out["disk"] = {"root_pct": round(100 * u.used / u.total, 1), "free_gb": round(u.free / 1e9, 1)}
        t = shutil.disk_usage("/tmp")
        out["disk"]["tmp_pct"] = round(100 * t.used / t.total, 1)
    except OSError:
        pass
    try:
        repo = HERE.parent
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


def derive_states():
    """`doit states` through fold.py, in a subprocess so a fold error never kills the server."""
    try:
        p = subprocess.run([sys.executable, str(HERE / "fold.py"), "states"], capture_output=True, text=True,
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
            "queue": queue_stats(rows, claims), "caps": ROLE_CAPS_MIN, "charters": charters, "spec_charter": spec_charter, "makers": maker_index(raw, states), "sys": sys_signals(), "tick_omens": _OMENS_SCRIPT if _OMENS_SCRIPT["at"] else None}


# ---------------------------------------------------------------- streaming
class Hub:
    def __init__(self):
        self.clients, self.lock = set(), threading.Lock()
        self.states = None
        self.recent = collections.deque(maxlen=6000)   # (kind, data) in arrival order, for /api/since
        self.seq = 0

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


def since(seq):
    """Everything the stream would have delivered after sequence number `seq`."""
    with HUB.lock:
        rows = [(n, k, d) for (n, k, d) in HUB.recent if n > seq]
        top = HUB.seq
    return {"seq": top, "ledger": [d for (_, k, d) in rows if k == "ledger"], "claims": [d for (_, k, d) in rows if k == "claim"],
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
        elif u.path == "/api/since":
            try:
                seq = int(qs.get("seq", ["0"])[0])
            except ValueError:
                seq = 0
            self._json(since(seq))
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
                chunk(f"event: hello\ndata: {json.dumps({'seq': HUB.seq})}\n\n".encode())
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
    a = ap.parse_args(argv)
    if not EVENTS.is_dir():
        sys.exit(f"realm: no ledger at {EVENTS}")
    if a.snapshot:
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
