#!/usr/bin/env python3
"""Regenerate the generated half of the DO-IT lessons log from the ledger.

Reads every event under $DOIT_ROOT/events (default ~/.do-it/events) and renders, between the
`<!-- digest:start -->` / `<!-- digest:end -->` markers of the log file, (1) every `lesson`
event any actor appended, (2) the signals that ARE lessons whether or not anyone wrote one:
escalations, corrections, failed/unserved spawns, conflict re-dispatches, build deviations,
briefs, gate refusals — grouped by day and actor, with counts — so a review "what do we
change?" starts from the whole record, not from whoever remembered to write. Idempotent;
never touches text outside the markers. Usage: lessons_digest.py [--log PATH] [--days N]
"""
import argparse, collections, datetime as dt, glob, json, os, pathlib, re

ROOT = pathlib.Path(os.environ.get("DOIT_ROOT", os.path.expanduser("~/.do-it")))
LOG = pathlib.Path("/opt/albert-scott/docs/sessions/do-it-v2-lessons-log.md")
SIGNALS = {
    "lesson": "lessons written by a role",
    "escalation-blocking": "escalations (each one is a place the pipeline could not decide alone)",
    "correction": "corrections (a fact on the ledger was wrong)",
    "spawn-failed": "failed or unserved spawns (tokens or time spent for nothing)",
    "conflict-rework": "merge-conflict re-dispatches (footprints that collided)",
    "build-deviation": "builders' declared deviations (what the spec did not foresee)",
    "brief": "briefs filed on charters (a defect handed to its owner)",
    "spec-killed": "specs killed after being written",
    "rejected-criterion": "criteria the grader/reviewer rejected",
}
START, END = "<!-- digest:start -->", "<!-- digest:end -->"


def actor(path):
    m = re.match(r"L-([a-z-]+?)-\d+\.jsonl$", pathlib.Path(path).name)
    return m.group(1) if m else pathlib.Path(path).stem


def events():
    out = []
    for f in sorted(glob.glob(str(ROOT / "events" / "*.jsonl"))):
        for n, line in enumerate(open(f, encoding="utf-8"), 1):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            e["_actor"], e["_ref"] = actor(f), f"{pathlib.Path(f).name}:{n}"
            out.append(e)
    out.sort(key=lambda e: e.get("ts", ""))
    return out


def gist(e, n=220):
    for k in ("text", "title", "why", "line", "reason", "note", "summary", "what"):
        v = e.get(k)
        if isinstance(v, str) and v.strip():
            return re.sub(r"\s+", " ", v.strip())[:n]
    longest = max((v for v in e.values() if isinstance(v, str)), key=len, default="")
    return re.sub(r"\s+", " ", longest)[:n]


def render(evs, days):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    evs = [e for e in evs if e.get("ts", "") >= since]
    gate_refusals = [e for e in evs if "gate" in e.get("type", "") and "clean" not in e.get("type", "")]
    by_day = collections.defaultdict(lambda: collections.Counter())
    by_actor = collections.Counter()
    for e in evs:
        t = e.get("type")
        if t in SIGNALS:
            by_day[e["ts"][:10]][t] += 1
            by_actor[(e["_actor"], t)] += 1
    L = [START, f"_Generated {dt.datetime.now(dt.timezone.utc).isoformat(timespec='minutes')} by "
         f"`do-it-v2/scripts/lessons_digest.py` over the last {days} days of the ledger; do not edit by hand._", ""]
    # counts table
    L += ["### Signal counts by day", "", "| day | " + " | ".join(SIGNALS) + " |",
          "|---|" + "---|" * len(SIGNALS)]
    for d in sorted(by_day):
        L.append(f"| {d} | " + " | ".join(str(by_day[d][t] or "") for t in SIGNALS) + " |")
    L += ["", "### Who is producing the signals", ""]
    for (a, t), c in sorted(by_actor.items(), key=lambda x: (-x[1], x[0])):
        L.append(f"- {a} · {t}: {c}")
    # lessons written by roles
    L += ["", "### Lessons written by roles (`lesson` events)", ""]
    ls = [e for e in evs if e.get("type") == "lesson"]
    if not ls:
        L.append("_none yet_")
    for e in ls:
        L.append(f"- **{e['ts'][:16]}Z · {e['_actor']} · {e.get('subject','')}** · axis {e.get('axis','?')} · "
                 f"{e.get('verdict','?')} · fix: {e.get('fix','open')} · ref {e.get('ref','—')}  \n  {gist(e, 600)}")
    # the signals themselves
    for t, label in SIGNALS.items():
        if t == "lesson":
            continue
        rows = [e for e in evs if e.get("type") == t]
        L += ["", f"### {label} ({len(rows)})", ""]
        for e in rows:
            extra = ""
            if t == "escalation-blocking":
                extra = f" · default: {re.sub(chr(10), ' ', str(e.get('default', ''))[:160])}"
            if t == "spawn-failed":
                extra = f" · {e.get('reason', '') or e.get('status', '')}"
            if t == "brief":
                extra = f" · {e.get('requirement', '')} · {e.get('path', '')}"
            L.append(f"- {e['ts'][11:16]}Z {e['ts'][:10]} · {e['_actor']} · {e.get('subject','')} · {gist(e)}{extra} · `{e['_ref']}`")
    L += ["", f"### Merge-gate refusals ({len(gate_refusals)})", ""]
    for e in gate_refusals:
        L.append(f"- {e['ts'][:16]}Z · {e.get('subject','')} · {e.get('type')} · {gist(e)} · `{e['_ref']}`")
    L.append(END)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(LOG)); ap.add_argument("--days", type=int, default=14)
    a = ap.parse_args()
    log = pathlib.Path(a.log); body = log.read_text() if log.exists() else ""
    new = render(events(), a.days)
    if START in body and END in body:
        body = body[:body.index(START)] + new + body[body.index(END) + len(END):]
    else:
        body = body.rstrip() + "\n\n---\n\n## Generated digest (the whole record, not just what someone remembered)\n\n" + new + "\n"
    log.write_text(body)
    print(f"digest written to {log} ({len(new.splitlines())} lines)")


if __name__ == "__main__":
    main()
