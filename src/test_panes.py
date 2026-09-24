#!/usr/bin/env python3
"""Checks on pane identity and the live-pane register. Run: python3 test_panes.py

Every check here is a way the register could lie and look right: a pane name
that no ledger answers to, a dead pane still listed because the file is still on
disk, a recycled pid impersonating the pane that owned it, a status flattened
into busy/idle, a `ledger_file` invented out of a name that is not a ledger id,
and a decision that travelled by message and was counted as recorded because its
`src` was merely non-null.

Nothing here reads the operator's real ~/.claude/sessions or a real ledger:
every fixture directory is a tempdir, and every live pid is this process or a
child this file starts and reaps.
"""
import json, os, pathlib, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import panes  # noqa: E402

TMP = pathlib.Path(tempfile.mkdtemp())
NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)
n = 0
KIDS = []


def ok(cond, why):
    global n
    assert cond, why
    n += 1


def kid():
    """A live pid that is not this process, reaped at the end of the file."""
    p = subprocess.Popen(["/bin/sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    KIDS.append(p)
    return p.pid


def dead_pid():
    """A pid that is certainly not running: one past the kernel's maximum. The
    reaped-child alternative is a pid the kernel may hand straight back."""
    try:
        return int(pathlib.Path("/proc/sys/kernel/pid_max").read_text().strip()) + 1
    except Exception:
        p = subprocess.Popen(["/bin/true"])
        p.wait()
        return p.pid


def session(d, pid, **kv):
    d.mkdir(parents=True, exist_ok=True)
    body = {"pid": pid, "sessionId": "x", "procStart": panes.proc_start(pid),
            "version": "2.1.274", "kind": "interactive", "updatedAt": 1789639374938}
    body.update(kv)
    (d / f"{pid}.json").write_text(json.dumps(body))
    return d


def by_name(rows, name):
    return next((r for r in rows if r["name"] == name), None)


# ── AC1 · one name, whichever of the three forms it arrives in ───────────────
for form in ("L-thinker-0068.jsonl", "L-thinker-0068", "/tmp/x/events/L-thinker-0068.jsonl"):
    ok(panes.pane_name(form) == "L-thinker-0068",
       f"the pane's name is its ledger stem, given {form!r}: {panes.pane_name(form)!r}")
ok(panes.pane_name(pathlib.Path("/a/b/L-executor-0007.jsonl")) == "L-executor-0007",
   "a Path is the fourth spelling of the same three forms, not a fifth rule")

# ── AC5 · R13's single predicate: absent src AND unresolvable src both count ──
EVENTS = [
    {"type": "spec-written", "ts": "2026-09-15T12:00:00+00:00", "_src": "L-spec-0040.jsonl:3"},
    {"type": "message-sent", "carries": "decision", "_src": "L-executor-0007.jsonl:1"},
    {"type": "message-sent", "carries": "finding", "_src": "L-executor-0007.jsonl:2"},
    {"type": "message-sent", "carries": "decision", "src": "L-spec-0040.jsonl:3",
     "_src": "L-executor-0007.jsonl:3"},
    {"type": "message-sent", "carries": "ping", "_src": "L-executor-0007.jsonl:4"},
    {"type": "message-sent", "carries": "ping", "src": "x:1", "_src": "L-executor-0007.jsonl:5"},
    {"type": "message-sent", "carries": "decision", "src": "L-nope-0001.jsonl:9",
     "_src": "L-executor-0007.jsonl:6"},
]
ok(panes.unrecorded_messages(EVENTS) == 3,
   f"a decision/finding with no src, and one whose src names no event, are both unrecorded — "
   f"2 would be the null-only predicate: {panes.unrecorded_messages(EVENTS)}")
ok(panes.unrecorded_messages([e for e in EVENTS if e.get("carries") != "ping"]) == 3,
   "and the pings were never contributing: dropping them changes nothing")
ok(panes.unrecorded_messages([EVENTS[0], EVENTS[3]]) == 0,
   "a decision whose src resolves to an event in the ledger is recorded, not counted")

# ── AC6 · the empty and the message-free cases are 0, never a raise ──────────
ok(panes.unrecorded_messages([]) == 0, "no events, nothing unrecorded")
ok(panes.unrecorded_messages([{"type": "decision", "carries": "decision", "_src": "a:1"}]) == 0,
   "a `decision` EVENT is a decision on the record — only a `message-sent` can be unrecorded")

# ── AC7 · the measured file shape, joined to the ledger ──────────────────────
A = session(TMP / "a", os.getpid(), cwd="/tmp/proj-a", name="L-thinker-0001",
            nameSource="user", status="busy")
LEDGER = [{"type": "think-discarded", "ts": (NOW - timedelta(days=2)).isoformat(),
           "_src": "L-thinker-0001.jsonl:1"},
          {"type": "charter-filed", "ts": (NOW - timedelta(days=9)).isoformat(),
           "_src": "L-thinker-0001.jsonl:2"}]
rows = panes.live_panes(A, LEDGER, now=NOW)
ok(len(rows) == 1, f"one live file, one record — never a duplicate: {rows}")
r = rows[0]
ok(r["name"] == "L-thinker-0001" and r["name_source"] == "user" and r["contract"] == "thinker",
   f"name, its source, and the contract read off the ledger id: {r}")
ok(r["cwd"] == "/tmp/proj-a" and r["status"] == "busy" and r["ledger_file"] == "L-thinker-0001.jsonl",
   f"cwd and status verbatim, and the ledger the name resolves to: {r}")
ok(isinstance(r["last_event_age_days"], float) and abs(r["last_event_age_days"] - 2.0) < 1e-6,
   f"the age is the NEWEST event's, in days, from the injected now: {r['last_event_age_days']}")

# ── AC8 · a dead pid is absent: liveness is a predicate, not a file listing ──
session(A, dead_pid(), cwd="/tmp/proj-dead", name="L-builder-0049",
        nameSource="user", status="busy", procStart="1")
rows = panes.live_panes(A, LEDGER, now=NOW)
ok(len(rows) == 1 and rows[0]["name"] == "L-thinker-0001",
   f"the dead pane's file is on disk and out of the register, in any status: {rows}")

# ── the pid-reuse guard: the right pid with the wrong procStart is not it ────
B = session(TMP / "b", os.getpid(), cwd="/tmp/proj-a", name="L-thinker-0001",
            nameSource="user", status="busy", procStart="1")
ok(panes.live_panes(B, LEDGER, now=NOW) == [],
   "a live pid whose start time disagrees with the file is a RECYCLED pid, not the pane")

# ── AC9 · both real shapes resolve, and status is passed through ─────────────
C = TMP / "c"
exec_pid, planner_pid = kid(), kid()
session(C, exec_pid, cwd="/opt/albert-scott", name="L-executor-0007",
        nameSource="user", status="busy")
session(C, planner_pid, cwd="/opt/albert-scott", name="albert-scott-b2",
        nameSource="derived", status="shell", agent="planner")
rows = panes.live_panes(C, [], now=NOW)
ok(len(rows) == 2, f"both measured panes are live and both come back: {rows}")
a, b = by_name(rows, "L-executor-0007"), by_name(rows, "albert-scott-b2")
ok(a and a["contract"] == "executor" and a["ledger_file"] == "L-executor-0007.jsonl",
   f"no `agent` key: the contract and the ledger come from the name (measured shape a): {a}")
ok(a["last_event_age_days"] is None, f"a ledger with no events in hand is None, never 0: {a}")
ok(b and b["contract"] == "planner" and b["name_source"] == "derived",
   f"a harness-derived name takes its contract from `agent` (measured shape b): {b}")
ok(a["contract_reason"] is None and b["contract_reason"] is None,
   f"AC3: a resolved contract carries no reason — the field exists only to explain 'unknown': "
   f"{a['contract_reason']!r} {b['contract_reason']!r}")
ok(b["ledger_file"] is None and b["last_event_age_days"] is None,
   f"and no ledger file is fabricated from a name that is not a ledger id: {b}")
ok(b["status"] == "shell" and a["status"] == "busy",
   f"`shell` is real and survives: status is never coerced into a two-value enum: {b}")

# ── AC10 · junk is ignored, no file can raise, no name becomes a path ────────
(C / "1683033.dc7deeee.key").write_text("not json")
(C / "builder-14-360-design.md").write_text("# a stray note\n")
(C / f"{kid()}.json").write_text("{not json at all")
(C / f"{kid()}.json").write_text('["a", "list", "is", "not", "a", "session"]')
traversal = kid()
session(C, traversal, cwd="/tmp/proj-x", name="../../etc/passwd",
        nameSource="derived", status="idle")
rows = panes.live_panes(C, [], now=NOW)
ok(by_name(rows, "L-executor-0007") and by_name(rows, "albert-scott-b2"),
   f"AC9's two records are still exactly there after the junk lands beside them: {rows}")
ok(not [r for r in rows if r["name"] in (None, "not json", "a")],
   f"a .key, a .md, invalid JSON and a JSON list are not panes: {rows}")
ok(len(rows) == 3, f"the two real panes and the oddly-named live one, and nothing else: {rows}")
t = by_name(rows, "../../etc/passwd")
ok(t and t["ledger_file"] is None and t["contract"] == "unknown" and t["contract_reason"],
   f"AC3: a traversal-shaped name yields no ledger file, contract 'unknown', and a stated, "
   f"non-empty contract_reason — never the bare word None: {t}")

# ── the register never raises, and never invents a directory ────────────────
ok(panes.live_panes(TMP / "no-such-dir", [], now=NOW) == [],
   "a sessions dir that does not exist is an empty register, not a traceback")
ok(panes.live_panes(C, None, now=NOW) and panes.unrecorded_messages(None) == 0,
   "events=None is the same as no events, on both readers")

# ── the liveness primitives, on this process, so the predicate is not folklore ─
ok(panes.alive(os.getpid()) and not panes.alive(dead_pid()),
   "the probe answers for this process and for a pid past pid_max")
ok(panes.proc_start(os.getpid()) is not None and panes.proc_start(dead_pid()) is None,
   "and the OS start time reads for a live pid only")

# ── the role list is what keeps a path out ──────────────────────────────────
ok(panes.ledger_role("L-spec-writer-0007") == "spec-writer",
   "a role that carries its own hyphen is one role, not a split")
ok(panes.ledger_role("L-spec-0005") is None,
   "a CONTENT id is not a pane: `spec` is not a ledger role, so it names no ledger file")
ok(panes.ledger_role("../../etc/passwd") is None and panes.ledger_role(None) is None,
   "and neither is a path or nothing")

# ── AC1 (L-spec-0270) · relay joins the role list ────────────────────────────
ok("relay" in panes.ROLES, "AC1: the relay contract is a real ledger role, not a name it merely uses")
ok(panes.ledger_role("L-relay-0007") == "relay",
   "AC1: an L-relay-NNNN pane resolves to the relay role, so its ledger file address resolves")

for p in KIDS:
    p.kill()
    p.wait()

print(f"panes: {n} checks pass")
