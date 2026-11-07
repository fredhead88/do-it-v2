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
- Operator work and items over four hours old share one [YOU] batch per pass.
  Deadman owns its alert; lane-stale work stays quiet while the delivery pane is up.
- Three consecutive missing-pane passes trigger one [DOWN] alert per episode.
  While the pane is absent, item heads-ups stay queued until recovery.
Answer every item with `/home/albert/do-it-v2/doit thinker-answer <src> "<why>"`. That writes a `decision` with the
item's own subject and `ref=<src>`, so fold, the board and the realm all see it answered.

Read-only on the ledger; writes only its state file and log. Run by cron every 5 min.
`--list` prints the open items and changes nothing.
"""
import datetime as dt, json, os, pathlib, re, subprocess, sys, tempfile, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold  # noqa: E402
import notification_policy
import tg_say
from zoneinfo import ZoneInfo

ROOT = str(fold.ROOT)
STATE = f"{ROOT}/state/thinker-inbox.json"
LOG = f"{ROOT}/logs/thinker-inbox.log"
PANE_FILE = f"{ROOT}/state/thinker-pane"          # e.g. %152; written by the Thinker session
SELF_FILE = f"{ROOT}/state/thinker-self-pane"
ESCALATE_FILE = "/var/tmp/push/foreman/ESCALATE.md"
MAX_AGE_D = 21
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
        os.chmod(LOG, 0o600)
        f.write(f"{now().astimezone(ZoneInfo('Asia/Jerusalem')).isoformat(timespec='seconds')} {msg}\n")


def ask_of(e):
    if e.get("kind") == "owed-expired":
        return f"owed check(s) {e.get('criteria')} expired unobserved; default: {e.get('default')}"
    return str(e.get("asks") or e.get("why") or e.get("text") or e.get("kind") or "escalation")


def _original_age(item, at):
    """Unknown or future event times cannot establish how long work has waited."""
    try:
        stamp = dt.datetime.fromisoformat(item.get("ts").replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return None
        age = at - stamp
        return age if age >= dt.timedelta(0) else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def should_page(item, now):
    """Deadman and pane-up lane nudges stay quiet; then apply ownership or age."""
    if item.get("kind") == "merge-deadman":
        return False
    if (item.get("kind") == "lane-stale" or str(item.get("subject", "")).startswith("lane-stale:")) and item.get("pane_up", True):
        return False
    if item.get("owner") == "operator":
        return True
    age = _original_age(item, now)
    return age is not None and age > dt.timedelta(minutes=240)


def queue_line(items, now):
    """Render overlapping queue counts without disclosing any item content."""
    total = operator = old = deadman = unknown = 0
    for item in items:
        total += 1
        operator += item.get("owner") == "operator"
        deadman += item.get("kind") == "merge-deadman"
        age = _original_age(item, now)
        unknown += age is None
        old += age is not None and age > dt.timedelta(minutes=240)
    return (f"Thinker queue: {total} open; {operator} operator-owned; {old} over 4 hours; "
            f"{deadman} merge-deadman; {unknown} age unknown.")


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
        if e.get("kind") == "merge-deadman":
            owner = "thinker"   # deadman owns its alert, regardless of classifier words or irreversible
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


def direct(i):
    return (str(i["src"]).startswith("L-planner-") or i["owner"] == "operator"
            or str(i["subject"]).startswith("L-charter-"))


def self_pane():
    try:
        p = pathlib.Path(SELF_FILE).read_text().strip()
    except OSError:
        return None
    return p if p and _alive(p) else None


def item_fields(i):
    # Only public ledger identifiers enter the sample; never relay arbitrary
    # subjects, asks, source refs or diagnostics to the operator's phone.
    subject = str(i.get("subject") or "")
    subject = subject if re.fullmatch(r"L-(?:spec|charter)-[0-9]{4,6}", subject) else "Queued work"
    summary = subject + (": operator decision" if i.get("owner") == "operator" else ": waiting over four hours")
    return dict(what="Work needs your attention.", real=summary,
                you="Ask the Thinker for an explanation and recommendation.")


def _refusal(st, refs, stamp, reason):
    for ref in refs:
        st["tg_refused"][ref] = stamp
    save_state(st)
    for ref in refs:
        log(f"refused ref={json.dumps(ref)} reason={reason}")


def _page(st, candidates, stamp):
    selected, summaries = [], []
    for row in sorted(candidates, key=lambda i: str(i["src"])):
        try:
            fields = item_fields(row)
            notification_policy.format_message("[YOU]", **fields)
        except ValueError:
            _refusal(st, [row["src"]], stamp, "format-invalid")
            continue
        selected.append(row["src"])
        summaries.append(fields["real"])
    if not selected:
        return
    omitted = max(0, len(selected) - 3)
    try:
        text = notification_policy.format_message(
            "[YOU]", f"{len(selected)} eligible items need your attention.",
            "; ".join(summaries[:3]) + f". {omitted} omitted from this sample.",
            "Ask the Thinker for an explanation and recommendation.")
    except ValueError:
        _refusal(st, selected, stamp, "format-invalid")
        return
    result = tg_say.deliver("thinker-inbox", text, label="[YOU]", root=ROOT)
    if result.state == "refused":
        _refusal(st, selected, stamp, "delivery-refused")
    elif result.state in ("sent", "held"):
        target = "tg" if result.state == "sent" else "tg_pending"
        st[target].update(dict.fromkeys(selected, stamp))
        save_state(st)


def _outage(st):
    try:
        text = notification_policy.format_message(
            "[DOWN]", "The Thinker's delivery session is unavailable.",
            "Pending work stays queued.", "Reopen flow and resume the Thinker.")
    except ValueError:
        st["pane_refused"] = True
        save_state(st)
        log("outage-refused reason=format-invalid")
        return
    result = tg_say.deliver("thinker-inbox", text, label="[DOWN]", root=ROOT)
    if result.state in ("sent", "held", "refused"):
        st[{"sent": "pane_alerted", "held": "pane_pending", "refused": "pane_refused"}[result.state]] = True
        save_state(st)
        if result.state == "refused":
            log("outage-refused reason=delivery-refused")


def short(i):
    if i.get("kind") == "master-full-red":
        # Preserve all failed ids and the tested merge range for --list/pane.
        # Commit subjects and ids are data, including when written by a reader
        # other than the producer's own sanitized question path.
        text = f"{i['subject']} [master-full-red] {i['ask']} (src {i['src']})"
        return " ".join("".join(c if c.isprintable() else " " for c in text).split())
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
    for key in ("delivered", "tg", "tg_refused", "tg_pending"):
        st[key] = {k: v for k, v in st.get(key, {}).items() if k in live}
    stamp = now().isoformat(timespec="seconds")
    p = pane()
    st["pane_misses"] = 0 if p else st.get("pane_misses", 0) + 1
    if p:
        for key in ("pane_alerted", "pane_pending", "pane_refused"):
            st[key] = False
    save_state(st)

    sp = self_pane()
    if sp:
        dnew = [i for i in items if direct(i) and i["src"] not in st["delivered"]]
        if dnew:
            msg = ("[thinker-inbox DIRECT] " + " ; ".join(
                ("FOR THE OPERATOR: " if i["owner"] == "operator" else "") + short(i) for i in dnew))[:3500]
            if type_into(sp, msg):
                st["delivered"].update(dict.fromkeys((i["src"] for i in dnew), stamp))
                save_state(st)
                log(f"direct {len(dnew)}")
        try:
            lines = pathlib.Path(ESCALATE_FILE).read_text().splitlines()
        except OSError:
            lines = None
        if lines is not None:
            seen = st.get("esc_lines")
            if seen is None or seen > len(lines):
                st["esc_lines"] = len(lines)
                save_state(st)
            else:
                fresh = [line for line in lines[seen:] if line.strip()]
                if not fresh or type_into(sp, ("[foreman-escalation] " + " || ".join(line[:400] for line in fresh))[:3500]):
                    st["esc_lines"] = len(lines)
                    save_state(st)

    new = [i for i in items if i["src"] not in st["delivered"]]
    if new and p and pane_idle(p):
        groups = {}
        for i in new:
            groups.setdefault(i["kind"] or i["type"], []).append(i)
        parts = []
        for kind, group in groups.items():
            if len(group) > 3 and kind != "master-full-red":
                parts.append(f"{len(group)}x {kind} ({', '.join(str(x['subject']) for x in group[:12])}{'…' if len(group) > 12 else ''})")
            else:
                parts += [("FOR THE OPERATOR, send them a plain-English explanation and your recommendation: "
                           if x["owner"] == "operator" else "") + short(x) for x in group]
        msg = (f"[thinker-inbox] {len(new)} new item(s) waiting on the Thinker: " + " ; ".join(parts) +
               f". Full list: python3 {ROOT}/thinker-inbox.py --list. Answer each with /home/albert/do-it-v2/doit thinker-answer <src> \"<why>\".")
        # Master-full incident details include every failed id and tested merge.
        # Do not mark those incidents delivered after truncating their payload.
        message = msg if any(i["kind"] == "master-full-red" for i in new) else msg[:3500]
        if type_into(p, message):
            st["delivered"].update(dict.fromkeys((i["src"] for i in new), stamp))
            save_state(st)
            log(f"delivered {len(new)}")

    if p:
        acknowledged = set(st["tg"]) | set(st["tg_pending"]) | set(st["tg_refused"])
        candidates = [i for i in items if i["src"] not in acknowledged
                      and should_page(dict(i, pane_up=True), now())]
        _page(st, candidates, stamp)
    elif st["pane_misses"] >= 3 and not any(st.get(key) for key in ("pane_alerted", "pane_pending", "pane_refused")):
        _outage(st)


if __name__ == "__main__":
    main()
