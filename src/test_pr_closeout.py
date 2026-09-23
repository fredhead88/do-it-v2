#!/usr/bin/env python3
"""One runnable check on `pr_closeout`. Run: python3 test_pr_closeout.py

Everything is a fixture — `DOIT_ROOT` points under a fresh temp dir, and
`intake.comment`/`intake.close`/`intake.has_marker`/`intake.run` are all
replaced with recording fakes BEFORE any fixture calls `pr_closeout.run` or
`tick._record()` (AC10), so no `gh` process and no network connection is ever
reached from this file. `carry.scan_events`/`already_carried`/`spec_written`
are exercised for REAL, against events this file writes straight to the
fixture ledger (`carry.py`'s own precedent, mirroring `test_carry.py`'s own
`write_event`), because the idempotency reads in `pr_closeout.py` ARE the
thing under test.

Own temp `DOIT_ROOT` for the whole file (own events dir, never the operator's
real `~/.do-it`); AC7's own end-to-end fixture shares this same root, with
`carry.uncarried`/`carry.sync_v4`/`intake.run` faked for that section only.
"""
import json, os, pathlib, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp(prefix="pr-closeout-test-"))
os.environ["DOIT_ROOT"] = str(TMP)
os.environ.pop("DOIT_PROJECT", None)
os.environ["DOIT_NO_POKE"] = "1"
os.environ["V4_LEDGER_DIR"] = str(TMP / "v4-ledger")
os.environ["V4_INBOX_DIR"] = str(TMP / "v4-inbox")
os.environ["V4_STAGING_DIR"] = str(TMP / "v4-staging")
for d in ("events", "content"):
    (TMP / d).mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import carry, dispatch, fold, intake, pr_closeout, tick  # noqa: E402

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


# ── network guard (AC10) ────────────────────────────────────────────────────
import urllib.request  # noqa: E402


def no_network(*a, **k):
    raise AssertionError("test_pr_closeout.py must never open a network connection")


urllib.request.urlopen = no_network


def no_subprocess(argv, **kw):
    raise AssertionError(f"test_pr_closeout.py must never shell out for real: {argv}")


intake.subprocess = no_subprocess

# ── the four fakes (AC10): assigned before any fixture calls run() ─────────
COMMENT_CALLS, CLOSE_CALLS, HAS_MARKER_CALLS = [], [], []


def fake_comment(url, body, marker):
    COMMENT_CALLS.append((url, body, marker))
    return True


def fake_close(url):
    CLOSE_CALLS.append(url)
    return True


def fake_has_marker(url, marker):
    HAS_MARKER_CALLS.append((url, marker))
    return False


intake.comment, intake.close, intake.has_marker = fake_comment, fake_close, fake_has_marker


def reset_calls():
    COMMENT_CALLS.clear()
    CLOSE_CALLS.clear()
    HAS_MARKER_CALLS.clear()


TICK = [0]


def stamp():
    TICK[0] += 1
    return f"2026-09-20T00:{TICK[0] // 60:02d}:{TICK[0] % 60:02d}+00:00"


def write_event(fname, e):
    """Appends one raw event line straight to `$DOIT_ROOT/events/<fname>` —
    bypassing `dispatch.emit`'s required-fields door entirely (that door is
    `spec-carried`'s own `source`/`tier`/`audited_at`, irrelevant to a fixture
    that never calls `carry.append_spec_carried` for real)."""
    e = {"v": 1, "ts": e.pop("ts", stamp()), **e}
    p = TMP / "events" / fname
    with open(p, "a") as fh:
        fh.write(json.dumps(e) + "\n")


def closeout_events():
    return carry.scan_events(lambda e: e.get("type") in ("inbound-closed", "inbound-pr-commented"))


# ══ AC1 + AC2 · the success path and its idempotency ═══════════════════════
URL1 = "https://github.com/o/r/pull/1"
SPEC1 = "L-spec-9001"
write_event("L-carry-local.jsonl", {"type": "spec-carried", "subject": SPEC1, "source": URL1})
WRITTEN_TS = stamp()
write_event("L-spec-writer-0001.jsonl", {"type": "spec-written", "subject": SPEC1, "ts": WRITTEN_TS})

r1 = pr_closeout.run([], [{"url": URL1}])
check(r1 == {"closed": [URL1], "commented": []}, f"AC1: return dict: {r1}")
check(len(COMMENT_CALLS) == 1 and COMMENT_CALLS[0][0] == URL1
      and COMMENT_CALLS[0][2] == f"closed:{SPEC1}", f"AC1: comment call: {COMMENT_CALLS}")
check(SPEC1 in COMMENT_CALLS[0][1] and WRITTEN_TS in COMMENT_CALLS[0][1],
      f"★ AC1: success body carries spec_id and written ts verbatim: {COMMENT_CALLS[0][1]!r}")
check(CLOSE_CALLS == [URL1], f"AC1: close call: {CLOSE_CALLS}")
closed_ev = [e for e in closeout_events() if e["type"] == "inbound-closed"]
check(len(closed_ev) == 1 and closed_ev[0]["subject"] == URL1 and closed_ev[0]["source"] == URL1
      and closed_ev[0]["project"] == "albert-scott" and closed_ev[0]["spec_id"] == SPEC1,
      f"AC1: appended inbound-closed: {closed_ev}")

reset_calls()
r1b = pr_closeout.run([], [{"url": URL1}])
check(r1b == {"closed": [], "commented": []}, f"AC2: rerun return dict empty: {r1b}")
check(not COMMENT_CALLS and not CLOSE_CALLS, "AC2: rerun makes zero further intake.* calls")
check(len([e for e in closeout_events() if e["type"] == "inbound-closed"]) == 1,
      "AC2: rerun appends nothing further")

# ══ AC3 (variants A, B) + AC4 (idempotency, then success supersedes) ═══════
URL2 = "https://github.com/o/r/pull/2"
SPEC2 = "L-spec-9002"
# Two shapes deliberately (A3): one `source=`, one `subject=` — §5.2's OR-match
# must find both regardless of which field a given `carry-failed` used.
write_event("L-executor-local.jsonl", {"type": "carry-failed", "source": URL2, "error": "first"})
write_event("L-executor-local.jsonl", {"type": "carry-failed", "subject": URL2, "error": "second"})

reset_calls()
r2a = pr_closeout.run([], [{"url": URL2}])
check(r2a == {"closed": [], "commented": [URL2]}, f"AC3 variant A: return dict: {r2a}")
check(len(COMMENT_CALLS) == 1 and COMMENT_CALLS[0][2] == "carry-failed",
      f"AC3 variant A: comment call: {COMMENT_CALLS}")
check("first" in COMMENT_CALLS[0][1] and "second" not in COMMENT_CALLS[0][1],
      f"★ AC3 variant A: body carries the OLDEST error only: {COMMENT_CALLS[0][1]!r}")
check(not CLOSE_CALLS, "AC3 variant A: intake.close is never called on this path")
commented_ev = [e for e in closeout_events() if e["type"] == "inbound-pr-commented"]
check(len(commented_ev) == 1 and commented_ev[0]["subject"] == URL2 and commented_ev[0]["source"] == URL2
      and commented_ev[0]["project"] == "albert-scott" and commented_ev[0]["marker"] == "carry-failed",
      f"AC3 variant A: appended inbound-pr-commented: {commented_ev}")

# AC4 (second call): the ledger read alone must skip it — zero further calls.
reset_calls()
r2b = pr_closeout.run([], [{"url": URL2}])
check(r2b == {"closed": [], "commented": []}, f"AC4 second call: return dict: {r2b}")
check(not COMMENT_CALLS and not CLOSE_CALLS, "AC4 second call: zero further intake.* calls")
check(len(commented_ev) == 1, "AC4 second call: appends nothing further")

# AC3 variant B: carried-after-failure, still awaiting a spec — NOT a failure.
write_event("L-carry-local.jsonl", {"type": "spec-carried", "subject": SPEC2, "source": URL2})
reset_calls()
r2c = pr_closeout.run([], [{"url": URL2}])
check(r2c == {"closed": [], "commented": []}, f"AC3 variant B: return dict: {r2c}")
check(not COMMENT_CALLS and not CLOSE_CALLS, "AC3 variant B: zero calls while spec_written unresolved")

# AC4 (third call): the retry succeeded and its spec is now written — full
# success path fires regardless of the standing carry-failed history.
WRITTEN_TS2 = stamp()
write_event("L-spec-writer-0002.jsonl", {"type": "spec-written", "subject": SPEC2, "ts": WRITTEN_TS2})
reset_calls()
r2d = pr_closeout.run([], [{"url": URL2}])
check(r2d == {"closed": [URL2], "commented": []}, f"AC4 third call: return dict: {r2d}")
check(len(COMMENT_CALLS) == 1 and COMMENT_CALLS[0][2] == f"closed:{SPEC2}",
      f"AC4 third call: success comment fires: {COMMENT_CALLS}")
check(CLOSE_CALLS == [URL2], f"AC4 third call: close fires: {CLOSE_CALLS}")
check(len([e for e in closeout_events() if e["type"] == "inbound-closed" and e["source"] == URL2]) == 1,
      "AC4 third call: one inbound-closed lands for URL2")

# ══ AC5 · never carried, never failed — no action at all ═══════════════════
URL5 = "https://github.com/o/r/pull/5"
reset_calls()
r5 = pr_closeout.run([], [{"url": URL5}])
check(r5 == {"closed": [], "commented": []}, f"AC5: return dict: {r5}")
check(not COMMENT_CALLS and not CLOSE_CALLS, "AC5: zero intake.* calls for a never-touched url")

# ══ AC11 · a fully-settled url ABSENT from open_spec_prs gets no action ════
reset_calls()
r11 = pr_closeout.run([], [])   # URL1 is fully closed-out in scan_events, but not passed in
check(r11 == {"closed": [], "commented": []}, f"AC11: return dict: {r11}")
check(not COMMENT_CALLS and not CLOSE_CALLS, "AC11: an absent url is never acted on")

# ══ AC8 · no literal `ingest_inbound_spec` in the source text ══════════════
SRC = (pathlib.Path(__file__).parent / "pr_closeout.py").read_text()
check("ingest_inbound_spec" not in SRC, "AC8: pr_closeout.py names no ingest_inbound_spec")

# ══ AC9 · file size cap ═════════════════════════════════════════════════════
LINES = SRC.splitlines()
check(len(LINES) <= 400, f"AC9: pr_closeout.py is {len(LINES)} physical lines (cap 400)")

# ══ AC6 (SD10) · both event types survive fold.read_events() project-filtered,
#    a mismatched/missing-project sibling does not ═════════════════════════
fold.PROJECT = "albert-scott"
try:
    write_event("L-negctl-local.jsonl", {"type": "inbound-closed", "subject": "decoy", "source": "decoy"})
    filtered = fold.read_events()
    pos = [e for e in filtered if e["type"] in ("inbound-closed", "inbound-pr-commented")
           and e.get("subject") in (URL1, URL2)]
    check(len(pos) == 3, f"★ AC6: both positive event types, all occurrences, survive the filter: {len(pos)}")
    check(all(e.get("project") == "albert-scott" for e in pos), "AC6: every positive carries project")
    neg = [e for e in filtered if e.get("subject") == "decoy"]
    check(not neg, "★ AC6: the no-project negative control is filtered OUT, proving the filter fires")
finally:
    fold.PROJECT = None

# ══ AC7 (SD11) · tick._record() wiring, success and failure ════════════════
AC7_URL, AC7_SPEC = "https://github.com/o/r/pull/7", "L-spec-9007"
write_event("L-carry-local.jsonl", {"type": "spec-carried", "subject": AC7_SPEC, "source": AC7_URL})
AC7_WRITTEN = stamp()
write_event("L-spec-writer-0007.jsonl", {"type": "spec-written", "subject": AC7_SPEC, "ts": AC7_WRITTEN})


def _intake_run_ok(events):
    return {"spec_prs": [{"url": AC7_URL}], "note_prs": []}


uncarried_calls, sync_calls = [], []


def _uncarried_stub(events):
    uncarried_calls.append(events)
    return []


def _sync_stub(events):
    sync_calls.append(events)


intake.run, carry.uncarried, carry.sync_v4 = _intake_run_ok, _uncarried_stub, _sync_stub
reset_calls()
result_ok = tick._record()
check(result_ok is not None, "AC7 success: tick._record() completes, no concurrent lock held")
check(len(COMMENT_CALLS) == 1 and COMMENT_CALLS[0][2] == f"closed:{AC7_SPEC}",
      f"★ AC7 success: tick._record() reaches pr_closeout.run for real: {COMMENT_CALLS}")
check(uncarried_calls and any(e.get("type") == "inbound-closed" and e.get("source") == AC7_URL
                              for e in uncarried_calls[-1]),
      "★ AC7 success: the re-read carry.uncarried receives includes pr_closeout's own append")
last_tick = [json.loads(l) for l in tick.tick_path().read_text().splitlines() if l.strip()
             and json.loads(l)["type"] == "tick"][-1]
check("intake_error" not in last_tick, f"AC7 success: no intake_error on a clean pass: {last_tick}")

# Failure variant: pr_closeout.run itself raises — caught, prefixed, never
# stopping the re-read/carry.uncarried/carry.sync_v4/lane.
orig_run = pr_closeout.run
pr_closeout.run = lambda events, prs: (_ for _ in ()).throw(RuntimeError("boom"))
uncarried_calls.clear()
sync_calls.clear()
result_fail = tick._record()
check(result_fail is not None, "AC7 failure: tick._record() still completes")
last_tick2 = [json.loads(l) for l in tick.tick_path().read_text().splitlines() if l.strip()
              and json.loads(l)["type"] == "tick"][-1]
check("pr_closeout: boom" in last_tick2.get("intake_error", ""),
      f"★ AC7 failure: intake_error carries the prefixed pr_closeout error: {last_tick2}")
check("carry_error" not in last_tick2 or "boom" not in last_tick2.get("carry_error", ""),
      "★ AC7 failure: never joined into the errors list feeding carry_error")
check(bool(uncarried_calls) and bool(sync_calls), "AC7 failure: carry.uncarried/sync_v4 still ran")
pr_closeout.run = orig_run

print(f"pr_closeout: {N} checks OK")
