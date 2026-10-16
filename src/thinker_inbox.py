#!/usr/bin/env python3
"""thinker-inbox — every escalation and question gets delivered (Thinker L-thinker-0004, 2026-10-11).

Problem it closes (operator, 2026-10-11): the realm showed ravens "waiting on the Thinker"
that the Thinker never saw. Escalations and questions were ledger appends with no delivery
channel; the Executor only mentioned them in its own pane. Questions were invisible on the
board until their deadline. Answers on a different subject never closed the raven.

Rule: one notion of "open", fold's own (`fold.open_escalations` + `fold.answered`), plus every
unanswered `question` from the moment it is asked, not only once overdue.
- Every item is typed into the Thinker pane, batched, when the pane is idle. Operator-owned items (the
  realm's OPERATOR_RE, or `irreversible`) are flagged so the Thinker sends the operator a plain-English
  explanation and recommendation. Raw builder questions never go to the operator (operator: "jargon").
- An item still open 60 min after delivery, or after three consecutive missing-pane passes,
  gets one deduplicated operator request with a recommendation.
Answer every item with `~/.do-it/thinker-answer <src> "<why>"`. That writes a `decision` with the
item's own subject and `ref=<src>`, so fold, the board and the realm all see it answered.

Read-only on the ledger; writes only its state file and log. Run by cron every 5 min.
`--list` prints the open items and changes nothing.
"""
import datetime as dt, json, os, pathlib, re, subprocess, sys, tempfile, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold  # noqa: E402

ROOT = str(fold.ROOT)
STATE = f"{ROOT}/state/thinker-inbox.json"
LOG = f"{ROOT}/logs/thinker-inbox.log"
PANE_FILE = f"{ROOT}/state/thinker-pane"          # e.g. %152; written by the Thinker session
TG = f"{ROOT}/telegram/tg-send.sh"
PROJECT = "albert-scott"
MAX_AGE_D = 21
ESCALATE_MIN = 60
OPERATOR_RE = re.compile(  # verbatim from the realm (realm.html OPERATOR_RE) so both agree on ownership
    r"\b(money|refund|payment|payout|charge|invoice|bill|credential|secret|token|password|api[- ]?key|"
    r"irreversible|drop (table|column)|delete (data|rows|records)|destroy|truncate|production write|"
    r"write(s)? to prod|prod(uction)? (data|db|database) (write|change)|downgrade|product (call|decision|choice)|"
    r"pricing|client[- ]facing|customer[- ]facing|spend|budget|quota|account limit|weekly limit)\b", re.I)


def now():
    return dt.datetime.now(dt.timezone.utc)


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"{now().isoformat(timespec='seconds')} {msg}\n")


def ask_of(e):
    if e.get("kind") == "owed-expired":
        return f"owed check(s) {e.get('criteria')} expired unobserved; default: {e.get('default')}"
    return str(e.get("asks") or e.get("why") or e.get("text") or e.get("kind") or "escalation")


def open_items():
    ev = fold.read_events()
    ans = fold.answered(ev)
    cutoff = now() - dt.timedelta(days=MAX_AGE_D)
    items = {}
    for e in fold.open_escalations(ev):
        if e["_src"] not in ans:
            items[e["_src"]] = e
    for e in ev:  # every unanswered question, from the moment it is asked
        if e.get("type") == "question" and e["_src"] not in ans:
            items[e["_src"]] = e
    out = []
    for src, e in items.items():
        # No project filter: an item tagged with the wrong project is still waiting on someone
        # (2026-10-11: L-spec-0818's escalation inherited a mistaken do-it-v2 tag and was missed).
        t = fold.ts(e.get("ts"))
        if t and t < cutoff:
            continue
        ask = ask_of(e)
        # phrases that only look like credentials ("one-token change", "detect-secrets") are not operator asks
        scrub = re.sub(r"detect-secrets|allowlist secret|secret[- ]scan(\.sh|ner)?|\.?secrets\.baseline|tokens? (budget|count|change)|"
                       r"(one|single|\d+)[- ]tokens?", "", ask + " " + str(e.get("default") or ""), flags=re.I)
        owner = "operator" if (e.get("irreversible") or OPERATOR_RE.search(scrub)) else "thinker"
        if e.get("kind") == "owed-expired":
            owner = "thinker"   # the default text names no operator act; the Thinker rules on re-date/waive/accept
        out.append({"src": src, "subject": e.get("subject"), "type": e["type"], "kind": e.get("kind"),
                    "ask": ask, "deadline": e.get("deadline"), "ts": e.get("ts"), "owner": owner})
    out.sort(key=lambda i: str(i["ts"]))
    return out


def load_state():
    try:
        with open(STATE) as source:
            return json.load(source)
    except (OSError, ValueError):
        return {"delivered": {}, "tg": {}}


def write_atomic(path, text):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=parent, delete=False) as out:
            temporary = out.name
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def save_state(s):
    write_atomic(STATE, json.dumps(s, indent=1) + "\n")


def _alive(p):
    try:
        r = subprocess.run(["tmux", "display", "-p", "-t", p, "#{pane_dead}"], capture_output=True, text=True)
        return r.returncode == 0 and r.stdout.strip() == "0"
    except OSError:
        return False


def pane():
    # Thinker 2026-10-16: after a tmux rebuild the saved pane id goes stale (%14 -> %2), and every item
    # went to Telegram as "Thinker not running". Fall back to the v2 layout's Thinker pane and re-save it.
    try:
        p = pathlib.Path(PANE_FILE).read_text().strip()
    except OSError:
        p = ""
    if p and _alive(p):
        return p
    try:
        fb = subprocess.run(["tmux", "display", "-p", "-t", f"{os.environ.get('FLOW_SESSION', 'flow')}:2.0", "#{pane_id}"], capture_output=True, text=True)
    except OSError:
        return None
    q = fb.stdout.strip() if fb.returncode == 0 else ""
    if q and _alive(q):
        try:
            write_atomic(PANE_FILE, q + "\n")
        except OSError:
            pass
        return q
    return None


def pane_idle(p):
    r = subprocess.run(["tmux", "capture-pane", "-p", "-t", p], capture_output=True, text=True)
    tail = "\n".join(r.stdout.splitlines()[-14:])
    return r.returncode == 0 and not re.search(r"esc to interrupt|Compacting|tokens · thinking", tail)


def type_into(p, msg):
    typed = subprocess.run(["tmux", "send-keys", "-t", p, "-l", msg])
    if typed.returncode != 0:
        return False
    time.sleep(1)
    return subprocess.run(["tmux", "send-keys", "-t", p, "Enter"]).returncode == 0


def telegram(msg):
    try:
        result = subprocess.run([TG, msg], capture_output=True, text=True, timeout=30,
                                env={**os.environ, "TG_SENDER": "thinker-inbox"})
        return result.returncode == 0 and result.stdout.splitlines()[-1:] == ["200"]
    except (OSError, subprocess.TimeoutExpired):
        return False


def short(i):
    return f"{i['subject']} [{i['kind'] or i['type']}] {i['ask'][:140]} (src {i['src']})"


def main():
    items = open_items()
    if "--list" in sys.argv:
        for i in items:
            print(i["owner"], "|", short(i))
        print(len(items), "open")
        return
    st = load_state()
    live = {i["src"] for i in items}
    st["delivered"] = {k: v for k, v in st["delivered"].items() if k in live}
    st["tg"] = {k: v for k, v in st["tg"].items() if k in live}
    t_now = now().isoformat(timespec="seconds")

    # operator-owned items come to the Thinker first, who sends the operator a plain-English
    # explanation and recommendation. Raw builder questions are jargon (operator, 2026-10-11).
    th = items
    new = [i for i in th if i["src"] not in st["delivered"]]
    p = pane()
    st["pane_misses"] = 0 if p else st.get("pane_misses", 0) + 1
    if new and p and pane_idle(p):
        groups = {}
        for i in new:
            groups.setdefault(i["kind"] or i["type"], []).append(i)
        parts = []
        for k, l in groups.items():
            if len(l) > 3:
                parts.append(f"{len(l)}x {k} ({', '.join(x['subject'] for x in l[:12])}{'…' if len(l) > 12 else ''})")
            else:
                parts += [("FOR THE OPERATOR, send them a plain-English explanation and your recommendation: " if x["owner"] == "operator" else "") + short(x) for x in l]
        msg = (f"[thinker-inbox] {len(new)} new item(s) waiting on the Thinker: " + " ; ".join(parts) +
               f". Full list: python3 {ROOT}/thinker-inbox.py --list. Answer each with {ROOT}/thinker-answer <src> \"<why>\".")
        if type_into(p, msg[:3500]):
            for i in new:
                st["delivered"][i["src"]] = t_now
            log(f"delivered {len(new)} to {p}")

    # unanswered too long, or no Thinker pane: Telegram once per item
    stale = []
    for i in th:
        d = st["delivered"].get(i["src"])
        late = d and (now() - fold.ts(d)).total_seconds() > ESCALATE_MIN * 60
        missing = not p and st["pane_misses"] >= 3
        if ((late and p) or missing) and i["src"] not in st["tg"]:
            stale.append(i)
    if stale:
        why = "the Thinker's session is not running" if not p else "the Thinker has not answered within an hour"
        subs = sorted({str(i["subject"]) for i in stale})
        recommendation = ("reopen flow and resume the Thinker" if not p else
                          "review the pending questions with the Thinker")
        sent = telegram(f"NEEDS YOU: {len(stale)} pipeline item(s) are waiting and {why}. "
                        f"Recommendation: {recommendation}. "
                        f"Specs: {', '.join(subs[:8])}{'…' if len(subs) > 8 else ''}.")
        if sent:
            for i in stale:
                st["tg"][i["src"]] = t_now
            log(f"tg-stale {len(stale)} ({why})")
        else:
            log(f"tg-failed {len(stale)}")
    save_state(st)


if __name__ == "__main__":
    main()
