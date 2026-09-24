#!/usr/bin/env python3
"""pane_resume — a stalled pane says why, and the tick restarts what can be
restarted (L-charter-0033 R3).

  pane_resume.run(events, *, now=None, sessions_dir=None, projects_dir=None,
                  send=None, capture=None) -> dict

Called from `tick._record()` as `pane_resume.run(ev)`, on the SAME re-read
`ev` `carry.sync_v4` already consumed. Every live Claude pane with
`status=="idle"` has its OWN top-level session transcript's LAST real
`assistant` line (never a `system`/`last-prompt`/`cost-state`/
`file-history-snapshot` trailer — those are different `type`s entirely, so
filtering on `type == "assistant"` and keeping the last match already skips
them) classified, read-only, into one of four buckets:

  auth       `isApiErrorMessage` true, `error == "authentication_failed"`.
  quota      `isApiErrorMessage` true, `quotaLimits.status == "rejected"`.
  transient  `isApiErrorMessage` true, not quota-rejected, and either
             `apiErrorStatus` in {429, 529, 503} or the entry's own text/error
             contains "overloaded"/"capacity"/"rate limit" (case-insensitive)
             — the spec's own Unknowns resolution; no genuine instance existed
             on this box at spec time to confirm the exact shape against.
  none       anything else — including a normal last turn, even when an
             earlier turn in the same file was an error: it already moved on.

A transient match is resumed with the fixed literal `continue`, but only once
the pane's own composer is confirmed empty or showing only a dim suggestion
(never a stuck operator line — `_composer_busy`), after a 2/5/15-minute
backoff keyed off the matched entry's OWN timestamp (never wall time between
calls — this function never sleeps or schedules), capped at 3 resumes per
pane per trailing 6 hours; a 4th stop escalates instead
(`reason_class: "cap"`). An auth or a quota match escalates immediately.
Every escalation reuses the existing `escalation-blocking` type, scoped to
`(pane, reason_class)`: an open one under one reason_class never suppresses a
different reason_class on the same pane, and is released — for THIS
function's own purposes, never a board-wide rule (Boundaries) — by a
`decision`/`unblocked` naming the pane, or by a later real (non-error)
assistant turn in the transcript timed after the escalation's own `ts`.

A Codex pane is never a candidate: `panes.live_panes()` is Claude-only by its
own docstring, and nothing here widens that (owed, AC9 — no Codex liveness
source exists in this footprint).

Every tmux call goes through the two injectable `send`/`capture` callables
(SD10) so a test — and the production tick's own test suite — never opens a
real pane or sends a real keystroke by accident.
"""
import collections, json, os, pathlib, subprocess, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dispatch, fold, panes  # noqa: E402

# The tick's own ledger file (tick.TICK_NAME, duplicated rather than imported:
# `tick.py` imports THIS module, and importing back would be a cycle — the same
# reasoning `pane_end._bind_root`'s docstring gives for its own duplication).
LEDGER_NAME = "L-tick-local.jsonl"

PROJECTS = pathlib.Path.home() / ".claude" / "projects"

BACKOFF = {0: 2, 1: 5, 2: 15}          # prior pane-resumed count -> minutes to wait
CAP = 3                                 # resumes per pane per trailing 6h; the 4th escalates
WINDOW_HOURS = 6

TRANSIENT_STATUSES = (429, 529, 503)
TRANSIENT_WORDS = ("overloaded", "capacity", "rate limit")

DIM_START = "\x1b[2m"
DIM_RESETS = ("\x1b[0m", "\x1b[22m", "\x1b[39m")
MARKER = "❯ "                      # "❯ " — the composer prompt

_LABELS = {
    "auth": ("an auth failure", "/login as the operator"),
    "quota": ("a quota exhaustion", "the operator's own account limit — wait for reset or intervene"),
    "cap": ("its 3-resume cap for the trailing 6 hours", "the operator's own call on whether to continue it"),
}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _default_send(target, text):
    subprocess.run(["tmux", "send-keys", "-t", target, text, "Enter"])


def _default_capture(target):
    r = subprocess.run(["tmux", "capture-pane", "-e", "-p", "-t", target],
                        capture_output=True, text=True)
    return r.stdout


def _composer_busy(captured):
    """The last line containing the marker; everything after it must be empty,
    or fully wrapped in an SGR dim code, to be safe. No marker line at all is
    read as busy — refusing to guess a composer state it never saw, never the
    reverse."""
    lines = [ln for ln in (captured or "").splitlines() if MARKER in ln]
    if not lines:
        return True
    after = lines[-1][lines[-1].rindex(MARKER) + len(MARKER):].rstrip()
    if not after:
        return False
    return not (after.startswith(DIM_START) and after.endswith(DIM_RESETS))


def _entry_text(entry):
    msg = entry.get("message") or {}
    parts = [c.get("text") or "" for c in (msg.get("content") or [])
             if isinstance(c, dict) and c.get("type") == "text"]
    return " ".join(parts)


def _classify(entry):
    """auth | quota | transient | none — read-only, off ONE entry."""
    if not entry.get("isApiErrorMessage"):
        return "none"
    if entry.get("error") == "authentication_failed":
        return "auth"
    if (entry.get("quotaLimits") or {}).get("status") == "rejected":
        return "quota"
    text = f"{_entry_text(entry)} {entry.get('error') or ''}".lower()
    if entry.get("apiErrorStatus") in TRANSIENT_STATUSES or any(w in text for w in TRANSIENT_WORDS):
        return "transient"
    return "none"


def _read_jsonl(path):
    try:
        text = pathlib.Path(path).read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _last_assistant(entries):
    """The LAST `type == "assistant"` entry — never the file's literal last
    line, which may be a trailer none of the four classes ever match (AC1)."""
    last = None
    for e in entries:
        if e.get("type") == "assistant":
            last = e
    return last


def _find_transcript(session_id, projects_dir):
    root = pathlib.Path(os.path.expanduser(str(projects_dir))) if projects_dir else PROJECTS
    matches = sorted(root.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _released(events, entries, pane, reason_class):
    """Whether the newest `(pane, reason_class)` escalation-blocking, if any,
    has been released — this unit's own predicate (Boundaries), never
    `fold.open_escalations`, which knows nothing of reason_class or a
    transcript."""
    opens = [e for e in events if e.get("type") == "escalation-blocking"
             and e.get("subject") == pane and e.get("reason_class") == reason_class]
    if not opens:
        return True
    esc_ts = max((panes._ts(e.get("ts")) or EPOCH for e in opens), default=EPOCH)
    for e in events:
        if e.get("type") in ("decision", "unblocked") and e.get("subject") == pane:
            t = panes._ts(e.get("ts"))
            if t and t > esc_ts:
                return True
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        t = panes._ts(entry.get("timestamp"))
        if t and t > esc_ts and _classify(entry) == "none":
            return True
    return False


def _escalation_kv(pane, reason_class, stop_uuid):
    label, action = _LABELS[reason_class]
    return dict(subject=pane, reason_class=reason_class, stop_uuid=stop_uuid,
                why=f"{pane} stopped on {label} — {action}; not resumed automatically",
                irreversible=f"{pane}'s turn already ended ({reason_class}); it will not be retried "
                             f"automatically — {action}")


def _live_join(sessions_dir, idle_names):
    """name -> the ONE live SESSIONS/<pid>.json meta matching it, or absent
    when zero or more than one file claims that name (Boundaries)."""
    d = pathlib.Path(os.path.expanduser(str(sessions_dir))) if sessions_dir else panes.SESSIONS
    by_name = collections.defaultdict(list)
    try:
        files = sorted(d.glob("*.json"))
    except OSError:
        files = []
    for f in files:
        if not f.stem.isdigit():
            continue
        try:
            meta = json.loads(f.read_text())
        except Exception:
            continue
        if not isinstance(meta, dict) or meta.get("name") not in idle_names:
            continue
        try:
            if not panes._is_live(int(f.stem), meta):
                continue
        except Exception:
            continue
        by_name[meta.get("name")].append(meta)
    return {name: metas[0] for name, metas in by_name.items() if len(metas) == 1}


def run(events, *, now=None, sessions_dir=None, projects_dir=None, send=None, capture=None):
    now = now or fold.NOW
    send, capture = send or _default_send, capture or _default_capture
    events = list(events or ())
    resumed, escalated, skipped = [], [], []

    live = panes.live_panes(sessions_dir=sessions_dir, events=events, now=now)
    idle_names = {p.get("name") for p in live
                  if p.get("status") == "idle" and panes.ledger_role(p.get("name"))}
    joined = _live_join(sessions_dir, idle_names)
    fold.EVENTS.mkdir(parents=True, exist_ok=True)
    dst = fold.EVENTS / LEDGER_NAME

    for name in sorted(joined):
        meta = joined[name]
        session_id, target = meta.get("sessionId"), meta.get("tmux")
        if not session_id or not target:
            continue
        transcript = _find_transcript(session_id, projects_dir)
        if transcript is None:
            continue
        entries = _read_jsonl(transcript)
        last = _last_assistant(entries)
        if last is None:
            continue
        reason_class = _classify(last)
        if reason_class == "none":
            continue
        stop_uuid = last.get("uuid")

        if reason_class in ("auth", "quota"):
            if _released(events, entries, name, reason_class):
                dispatch.emit(dst, {}, "escalation-blocking", **_escalation_kv(name, reason_class, stop_uuid))
                escalated.append(name)
            continue

        # transient — at most one resume per distinct stop, ever (idempotent
        # on stop_uuid), regardless of the 6h window below.
        if any(e.get("type") == "pane-resumed" and e.get("pane") == name
               and e.get("stop_uuid") == stop_uuid for e in events):
            continue
        window_start = now - timedelta(hours=WINDOW_HOURS)
        prior = [e for e in events if e.get("type") == "pane-resumed" and e.get("pane") == name
                 and (panes._ts(e.get("ts")) or window_start) >= window_start]
        attempt = len(prior)
        if attempt >= CAP:
            if _released(events, entries, name, "cap"):
                dispatch.emit(dst, {}, "escalation-blocking", **_escalation_kv(name, "cap", stop_uuid))
                escalated.append(name)
            continue
        stop_ts = panes._ts(last.get("timestamp"))
        if stop_ts is None or (now - stop_ts).total_seconds() / 60 < BACKOFF[attempt]:
            continue                                  # not yet due — send nothing (AC3)
        if _composer_busy(capture(target)):
            dispatch.emit(dst, {}, "pane-resume-skipped", pane=name, reason="composer-busy", stop_uuid=stop_uuid)
            skipped.append(name)
            continue
        send(target, "continue")
        dispatch.emit(dst, {}, "pane-resumed", pane=name, reason_class="transient",
                      attempt=attempt + 1, command="continue", stop_uuid=stop_uuid)
        resumed.append(name)

    return {"resumed": resumed, "escalated": escalated, "skipped": skipped}
