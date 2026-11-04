#!/usr/bin/env python3
"""Checks on pane_resume — classification survives trailer lines (AC1),
escalation scoping/release (AC2), the 2/5/15-minute backoff and the 3-per-6h
cap (AC3), the exact send target and the already-moved-on non-send (AC4), the
four excluded-pane shapes (AC5), a dangling `sessionId` (AC7), and the
composer's dim-vs-visible check (AC10). Run: python3 test_pane_resume.py

Nothing here reads the operator's real `~/.claude/sessions` or
`~/.claude/projects`, and nothing sends a real keystroke: every sessions/
projects dir is a tempdir, `send`/`capture` are fakes recording their own
calls, and the one live pid most fixtures use is this test process itself —
`panes._is_live` falls back to pid-liveness alone when a fixture's meta
carries no `procStart` to disagree with it.
"""
import json, os, pathlib, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

# R8.2: HOME/DOIT_ROOT isolation, set before any project module is imported —
# a direct `python3 test_pane_resume.py` must never resolve fold.ROOT to ~/.do-it.
_TMP8027 = pathlib.Path(tempfile.mkdtemp(prefix="doit-test-iso-"))
os.environ["HOME"] = str(_TMP8027 / "home")
os.environ["DOIT_ROOT"] = str(_TMP8027 / "root")

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fold, pane_resume, panes  # noqa: E402

n = 0


def ok(cond, why):
    global n
    assert cond, why
    n += 1


PID = os.getpid()
NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
KIDS = []


def kid():
    """A second live pid that is not this process, reaped at the end."""
    p = subprocess.Popen(["/bin/sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    KIDS.append(p)
    return p.pid


def new_root():
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "sessions").mkdir(), (d / "projects").mkdir(), (d / "events").mkdir()
    fold.ROOT, fold.EVENTS = d, d / "events"
    return d


def session(d, name, status="idle", session_id="s1", tmux="flow:1.1", pid=None):
    meta = {"name": name, "cwd": "/x", "status": status, "sessionId": session_id, "tmux": tmux}
    (d / "sessions" / f"{pid or PID}.json").write_text(json.dumps(meta))


def transcript(d, session_id, *entries):
    proj = d / "projects" / "-x-y"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{session_id}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))


def assistant(uuid, ts, text="", **kv):
    e = {"type": "assistant", "uuid": uuid, "timestamp": ts,
         "message": {"content": [{"type": "text", "text": text}]}}
    e.update(kv)
    return e


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def run_it(d, events, send=None, capture=None):
    calls = {"send": [], "capture": []}
    def _send(target, text):
        calls["send"].append((target, text))
        if send:
            send(target, text)
    def _capture(target):
        calls["capture"].append(target)
        return capture(target) if capture else ""
    out = pane_resume.run(events, now=NOW, sessions_dir=d / "sessions",
                          projects_dir=d / "projects", send=_send, capture=_capture)
    return out, calls


TRAILERS = [{"type": "system", "subtype": "x"}, {"type": "last-prompt", "lastPrompt": "x"},
            {"type": "cost-state", "totalCostUSD": 0.1}, {"type": "file-history-snapshot"}]
T2 = iso(NOW - timedelta(minutes=2))
AUTH = assistant("u-auth", T2, error="authentication_failed", isApiErrorMessage=True,
                 text="Not logged in · Please run /login")
QUOTA = assistant("u-quota", T2, error="rate_limit", isApiErrorMessage=True, apiErrorStatus=429,
                  quotaLimits={"status": "rejected"}, text="You've hit your weekly limit")
TRANSIENT = assistant("u-trans", T2, error="overloaded_error", isApiErrorMessage=True,
                      apiErrorStatus=529, text="Overloaded")
NORMAL = assistant("u-norm", T2, text="done")

# ── AC1 — the LAST assistant entry, never a trailer, classifies correctly ────
for entry, want in ((AUTH, "auth"), (QUOTA, "quota"), (TRANSIENT, "transient"), (NORMAL, "none")):
    last = pane_resume._last_assistant([entry] + TRAILERS)
    ok(last is entry, f"AC1: a trailer line is never taken for the last assistant entry ({want})")
    ok(pane_resume._classify(last) == want, f"AC1: {want} classifies correctly, got "
                                             f"{pane_resume._classify(last)}")

# ── AC7 — a dangling/absent sessionId sends nothing and never raises ─────────
d7 = new_root()
session(d7, "L-executor-0001", session_id="", tmux="flow:1.1")
out, calls = run_it(d7, [])
ok(out == {"resumed": [], "escalated": [], "skipped": [], "restarted": []}, f"AC7 (absent sessionId): {out}")
session(d7, "L-executor-0002", session_id="no-such-session", tmux="flow:1.2")
out, calls = run_it(d7, [])
ok(out == {"resumed": [], "escalated": [], "skipped": [], "restarted": []} and not calls["send"] and not calls["capture"], \
   f"AC7 (dangling sessionId): {out} {calls}")

# ── AC5 — four excluded shapes, each with a transient-matching transcript,
#    each in its OWN root so a shared pid never collides across fixtures ─────
d5a = new_root()
session(d5a, "L-executor-0010", status="busy", session_id="s10", tmux="flow:10")
transcript(d5a, "s10", TRANSIENT)
d5b = new_root()
session(d5b, "L-executor-0011", status="shell", session_id="s11", tmux="flow:11")
transcript(d5b, "s11", TRANSIENT)
d5c = new_root()
session(d5c, "harness-derived-name", status="idle", session_id="s12", tmux="flow:12")
transcript(d5c, "s12", TRANSIENT)
d5d = new_root()
dup_pid = kid()
session(d5d, "L-executor-0013", status="idle", session_id="s13", tmux="flow:13")
session(d5d, "L-executor-0013", status="idle", session_id="s13b", tmux="flow:13b", pid=dup_pid)
transcript(d5d, "s13", TRANSIENT), transcript(d5d, "s13b", TRANSIENT)
for tag, d in (("busy", d5a), ("shell", d5b), ("unresolved-role", d5c), ("duplicate-name", d5d)):
    out, calls = run_it(d, [], capture=lambda t: "❯ ")
    ok(out == {"resumed": [], "escalated": [], "skipped": [], "restarted": []},
       f"AC5 ({tag}): never sent a resume or an escalation: {out}")

# ── AC4 — the exact tmux target, verbatim, and no send for an already-moved-on
d4 = new_root()
session(d4, "L-executor-0020", session_id="s20", tmux="flow:9.@8")
transcript(d4, "s20", TRANSIENT)
out, calls = run_it(d4, [], capture=lambda t: "❯ ")
ok(out["resumed"] == ["L-executor-0020"], f"AC4: the transient match resumes: {out}")
ok(calls["send"] == [("flow:9.@8", "continue")],
   f"AC4: send target is the session file's own tmux field, verbatim, text 'continue': {calls['send']}")
d4b = new_root()
session(d4b, "L-executor-0021", session_id="s21", tmux="flow:1")
transcript(d4b, "s21", TRANSIENT, NORMAL)          # an EARLIER error, but the LAST turn is normal
out, calls = run_it(d4b, [], capture=lambda t: "❯ ")
ok(out == {"resumed": [], "escalated": [], "skipped": [], "restarted": []} and not calls["send"],
   f"AC4: already moved on — no send at all: {out}")

# ── AC10 — the composer check: dim/empty send, visible non-dim skips ─────────
DIM = "❯ \x1b[2mcheck the escalations for anything now closeable\x1b[0m"
BUSY = "❯ do not send anything here"
d10a = new_root()
session(d10a, "L-executor-0030", session_id="s30", tmux="flow:30")
transcript(d10a, "s30", TRANSIENT)
out_busy, calls_busy = run_it(d10a, [], capture=lambda t: BUSY)
ok(out_busy == {"resumed": [], "escalated": [], "skipped": ["L-executor-0030"], "restarted": []},
   f"AC10: visible non-dim text skips with composer-busy: {out_busy}")
d10b = new_root()
session(d10b, "L-executor-0031", session_id="s31", tmux="flow:31")
transcript(d10b, "s31", TRANSIENT)
out_dim, calls_dim = run_it(d10b, [], capture=lambda t: DIM)
ok(out_dim == {"resumed": ["L-executor-0031"], "escalated": [], "skipped": [], "restarted": []},
   f"AC10: the real measured dim-suggestion shape sends normally: {out_dim}")
d10c = new_root()
session(d10c, "L-executor-0032", session_id="s32", tmux="flow:32")
transcript(d10c, "s32", TRANSIENT)
out_empty, calls_empty = run_it(d10c, [], capture=lambda t: "❯ ")
ok(out_empty == {"resumed": ["L-executor-0032"], "escalated": [], "skipped": [], "restarted": []},
   f"AC10: an empty composer sends normally: {out_empty}")

# ── AC3 — 0/1/2/3 prior pane-resumed rows -> 2/5/15-minute waits, then a cap ──
d3 = new_root()
session(d3, "L-executor-0040", session_id="s40", tmux="flow:40")


def prior_rows(pane, count):
    return [{"type": "pane-resumed", "pane": pane, "reason_class": "transient",
             "stop_uuid": f"old-{i}", "ts": iso(NOW - timedelta(minutes=1))}
            for i in range(count)]


def stop_entry(uuid, ts):
    return assistant(uuid, iso(ts), error="overloaded_error", isApiErrorMessage=True,
                     apiErrorStatus=529, text="Overloaded")


for count, wait in ((0, 2), (1, 5), (2, 15)):
    events = prior_rows("L-executor-0040", count)
    transcript(d3, "s40", stop_entry(f"u-{count}", NOW - timedelta(minutes=wait) + timedelta(seconds=1)))
    out, calls = run_it(d3, events, capture=lambda t: "❯ ")
    ok(out == {"resumed": [], "escalated": [], "skipped": [], "restarted": []},
       f"AC3: {count} prior, {wait}min stop is too fresh by 1s — sends nothing: {out}")
    transcript(d3, "s40", stop_entry(f"u-{count}b", NOW - timedelta(minutes=wait)))
    out, calls = run_it(d3, events, capture=lambda t: "❯ ")
    ok(out["resumed"] == ["L-executor-0040"],
       f"AC3: {count} prior, exactly {wait}min elapsed — resumes: {out}")

cap_events = prior_rows("L-executor-0040", 3)
transcript(d3, "s40", stop_entry("u-cap", NOW - timedelta(minutes=30)))
out, calls = run_it(d3, cap_events, capture=lambda t: "❯ ")
ok(out["escalated"] == ["L-executor-0040"] and not calls["send"],
   f"AC3: a 4th stop within 6h escalates (cap) instead of resuming: {out}")
esc_events = cap_events + [{"type": "escalation-blocking", "subject": "L-executor-0040",
                            "reason_class": "cap", "stop_uuid": "u-cap", "irreversible": "x",
                            "ts": iso(NOW - timedelta(minutes=1))}]
out2, calls2 = run_it(d3, esc_events, capture=lambda t: "❯ ")
ok(out2 == {"resumed": [], "escalated": [], "skipped": [], "restarted": []},
   f"AC3: an open cap escalation is not re-escalated on the identical stop: {out2}")
d3b = new_root()          # calling run() again before the backoff elapses sends nothing (0 prior)
session(d3b, "L-executor-0041", session_id="s41", tmux="flow:41")
transcript(d3b, "s41", stop_entry("u-early", NOW - timedelta(minutes=1)))
out3, _ = run_it(d3b, [], capture=lambda t: "❯ ")
ok(out3 == {"resumed": [], "escalated": [], "skipped": [], "restarted": []}, f"AC3: an early re-call sends nothing: {out3}")

# ── AC2 — escalation suppression/release, scoped to (pane, reason_class) ─────
d2 = new_root()
session(d2, "L-executor-0050", session_id="s50", tmux="flow:50")
transcript(d2, "s50", AUTH)
out, calls = run_it(d2, [])
ok(out["escalated"] == ["L-executor-0050"] and not out["resumed"],
   f"AC2: a fresh auth match escalates once: {out}")
esc_ts = iso(NOW - timedelta(minutes=1))
open_auth = [{"type": "escalation-blocking", "subject": "L-executor-0050", "reason_class": "auth",
             "stop_uuid": "u-auth", "irreversible": "x", "ts": esc_ts}]
# (a) still open: nothing further, for the SAME reason_class
out_a, _ = run_it(d2, open_auth)
ok(out_a == {"resumed": [], "escalated": [], "skipped": [], "restarted": []}, f"AC2(a): still open — nothing further: {out_a}")
# (b) a DIFFERENT reason_class on the same pane still escalates, open (a) notwithstanding
transcript(d2, "s50", QUOTA)
out_b, _ = run_it(d2, open_auth)
ok(out_b["escalated"] == ["L-executor-0050"], f"AC2(b): a different reason_class still escalates: {out_b}")
# (c) recovery — a later non-error assistant turn after the open escalation —
#     then a further stop under the SAME reason_class escalates again
recovered = [AUTH, assistant("u-recovered", iso(NOW - timedelta(seconds=30)), text="back to work")]
transcript(d2, "s50", *recovered)
still_open_auth = open_auth      # the SAME open auth escalation, ts 1 minute ago
out_recovered_check, _ = run_it(d2, still_open_auth)
ok(out_recovered_check == {"resumed": [], "escalated": [], "skipped": [], "restarted": []},
   f"AC2(c) precondition: the recovery turn alone (last turn is normal) sends/escalates nothing: "
   f"{out_recovered_check}")
transcript(d2, "s50", AUTH, assistant("u-recovered", iso(NOW - timedelta(seconds=30)), text="back to work"),
          {**AUTH, "uuid": "u-auth-2", "timestamp": iso(NOW - timedelta(seconds=10))})
out_c, _ = run_it(d2, still_open_auth)
ok(out_c["escalated"] == ["L-executor-0050"],
   f"AC2(c): a further stop after recovery escalates again, same reason_class: {out_c}")

# L-spec-2045 AC3: ctx/held end identity comes from the writer's ledger source.
from unittest.mock import patch
import hashlib
for reason in ('ctx', 'held'):
    d = new_root()
    end = dict(type='planner-ended', subject='L-charter-9001', reason=reason,
               ts=iso(NOW - timedelta(minutes=5)), _src='L-planner-9001.jsonl:1')
    calls = []
    def restart(name):
        calls.append(name)
        return True
    out = pane_resume.run([end], now=NOW, sessions_dir=d / 'sessions', restart=restart)
    rows = fold.read_events(use_cache=False)
    resumed = [e for e in rows if e['type'] == 'pane-resumed']
    ok(out['restarted'] == ['L-planner-9001'] and calls == ['L-planner-9001'],
       '2045 AC3: restart exactly once on first cycle')
    ok(len(resumed) == 1 and resumed[0]['reason_class'] == 'ctx-ended'
       and resumed[0]['lag_s'] == 300, '2045 AC3: durable measured lag')
    pane_resume.run([end, *rows], now=NOW, sessions_dir=d / 'sessions', restart=restart)
    ok(len(calls) == 1, '2045 AC3: fresh ledger suppresses duplicate restart')
    body = json.dumps(resumed, sort_keys=True)
    print(json.dumps(dict(url='cli://L-spec-2045/AC3', status=0,
                         body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))
    start = dict(type='planner-started', subject=end['subject'], planner='L-planner-9002', ts=iso(NOW))
    ok(not pane_resume.ctx_ended_without_successor([end, start], NOW), '2045 AC3: later start suppresses')
    ok(not pane_resume.ctx_ended_without_successor([{**end, 'ts': 'bad'}], NOW),
       '2045 AC3: unknown lag cannot claim restart')
    live_root = new_root()
    with patch.object(panes, 'live_panes', return_value=[dict(name='L-planner-9002')]):
        out = pane_resume.run([end], now=NOW, sessions_dir=live_root / 'sessions', restart=restart)
        ok(not out['restarted'] and len(calls) == 1, '2045 AC3: live successor prevents restart')
    with patch.object(panes, 'live_panes', side_effect=OSError('undetermined')):
        out = pane_resume.run([end], now=NOW, sessions_dir=live_root / 'sessions', restart=restart)
        ok(not out['restarted'] and out['skipped'], '2045 AC3: failed live read never claims restart')

# Production uses the targeted launcher; unreadable metadata cannot authorize a restart.
d = new_root()
end = dict(type='planner-ended', subject='L-charter-9002', planner='L-planner-9002',
           reason='ctx', ts=iso(NOW - timedelta(minutes=1)))
with patch('up.restart_pane', return_value=False) as restart:
    out = pane_resume.run([end], now=NOW, sessions_dir=d / 'sessions')
    restart.assert_called_once_with('L-planner-9002')
ok(not out['restarted'] and out['skipped'] == ['L-planner-9002'],
   '2045 AC3: unavailable restart is an explicit skip')
ok(any(e.get('reason') == 'no-restart-entry' for e in fold.read_events(use_cache=False)),
   '2045 AC3: false targeted restart is recorded')
with patch('up.restart_pane', return_value=True) as restart:
    out = pane_resume.run([end], now=NOW, sessions_dir=d / 'sessions')
    restart.assert_called_once_with('L-planner-9002')
ok(out['restarted'] == ['L-planner-9002'], '2045 AC3: production default restarts targeted pane')
(d / 'sessions' / '123.json').write_text('{')
with patch.object(pane_resume, '_restart_ctx') as actuator:
    out = pane_resume.run([end], now=NOW, sessions_dir=d / 'sessions', restart=lambda name: True)
    ok(not out['restarted'] and out['skipped'], '2045 AC3: corrupt session metadata is undetermined')
    ok(not actuator.called, '2045 AC3: unknown successor cannot reach actuator')

for p in KIDS:
    p.kill()
    p.wait()

print(f"pane_resume: {n} checks pass")
