#!/usr/bin/env python3
"""notes — the NOTES board: list every open `inbound-note` PR, age it, flag it
past 48h unanswered, and close one out.

  notes.run(events, open_note_prs)         one tick's list/re-list/gone pass
  notes.note_close(pr, response_file)      post FILE, close the PR, record it
  notes.board_rows(events)                 fold.render's NOTES rows

R4/R5, L-spec-0244 (wave 2, `intake-core`'s sibling). `body` is never read here
(charter §3: a human-or-Thinker answer is the point) — only `url`/
`author_login`/`title`/`created_at` cross this module's boundary.

§5-shaped precedence, mirroring `intake.py`'s own: the LATEST of
`inbound-note-listed`/`inbound-note-gone`/`note-answered` for a url decides
what happens next — never "ever emitted for this url" (SD18).
"""
import argparse, os, pathlib, re, sys
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import carry, dispatch, intake  # noqa: E402

root = lambda: pathlib.Path(os.environ.get("DOIT_ROOT") or (pathlib.Path.home() / ".do-it"))

# Constraints: `<pr>` must already be the canonical PR url — no normalization,
# no bare number.
PR_URL_RE = re.compile(r"^https://github\.com/fredhead88/albert-scott-platform/pull/\d+$")
NOTE_TYPES = ("inbound-note-listed", "inbound-note-gone", "note-answered")


def _collapse(s):
    """Board-forgery guard (fold.py's own `scope` precedent, AC12): an
    untrusted "any author" PR title/author_login is exactly the input that
    already defends `fold.render` against a forged `## ` heading — collapsed
    before it ever reaches a row."""
    return " ".join(str(s or "").split())


def _matches(url):
    return lambda e: str(e.get("subject")) == url or str(e.get("source")) == url


def _latest_of(events, url, types=NOTE_TYPES):
    """The LATEST event among `types` naming `url` in `events` — `None` if
    never sighted. Mirrors `intake.py`'s own `_latest_state`/
    `_latest_awaiting_event` idiom (sorted by `ts`, last wins)."""
    hits = [e for e in events if e.get("type") in types and _matches(url)(e)]
    return sorted(hits, key=lambda e: e.get("ts") or "")[-1] if hits else None


def _latest_state(events, url):
    e = _latest_of(events, url)
    return e["type"] if e else None


def _tracked_urls(events):
    return {str(e.get("subject") or e.get("source")) for e in events
            if e.get("type") in NOTE_TYPES} - {"", "None"}


def run(events, open_note_prs):
    """R4/R5 (AC1, AC2): `open_note_prs` is the one slice this call reasons
    over — never `intake.list_open` or any `gh`-shaped call of its own
    (SD14). Appends `inbound-note-listed` for a first sighting or a
    REOPENED note (latest state absent/`inbound-note-gone`/`note-answered`);
    nothing for a url already latest-`inbound-note-listed` (AC1). Appends
    `inbound-note-gone` for a tracked url absent from `open_note_prs`, ONLY
    when `open_note_prs` is non-empty THIS call — a `[]` listing failure
    (L-spec-0242 AC1/AC3) is never read as "every note vanished" (AC2/SD18).
    Ledger-write field naming: `created_at` -> `opened_at` (SD7)."""
    listed = gone = 0
    open_urls = set()
    for pr in open_note_prs:
        url = pr.get("url")
        if not url:
            continue
        open_urls.add(url)
        if _latest_state(events, url) == "inbound-note-listed":
            continue
        dispatch.emit(intake.ledger_path(), {}, "inbound-note-listed",
                      subject=url, source=url, project="albert-scott",
                      author_login=pr.get("author_login") or "",
                      title=pr.get("title") or "", opened_at=pr.get("created_at"))
        listed += 1
    if open_note_prs:
        for url in _tracked_urls(events):
            if url in open_urls:
                continue
            if _latest_state(events, url) == "inbound-note-listed":
                dispatch.emit(intake.ledger_path(), {}, "inbound-note-gone",
                              subject=url, source=url, project="albert-scott")
                gone += 1
    return {"listed": listed, "gone": gone}


def _age_hours(opened_at):
    try:
        opened = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - opened).total_seconds() / 3600.0


def board_rows(events):
    """One row per url whose LATEST tracked state is `inbound-note-listed`
    (AC3), age in hours off that event's own `opened_at`, `⚑ >48h` past 48h,
    none at or under (AC4). Title/author collapsed before interpolation
    (Board-forgery guard, AC12)."""
    rows = []
    for url in sorted(_tracked_urls(events)):
        e = _latest_of(events, url)
        if e is None or e["type"] != "inbound-note-listed":
            continue
        age = _age_hours(e.get("opened_at"))
        age_s = f"{age:.0f}h" if age is not None else "age unknown"
        flag = "  ⚑ >48h" if age is not None and age > 48 else ""
        rows.append(f"{url} · {_collapse(e.get('title'))} · {_collapse(e.get('author_login'))} "
                    f"· {age_s}{flag}")
    return rows


def _answered_ledger_path():
    """`note-answered` appends into whatever file `$DOIT_LEDGER_FILE` names
    (default `L-operator-local.jsonl`) — the `{thinker, operator}` actor
    grant, distinct from `intake.ledger_path()`'s `{intake}` file the listing
    events above use (fold.EMITS, AC9)."""
    return root() / "events" / os.environ.get("DOIT_LEDGER_FILE", "L-operator-local.jsonl")


def note_close(pr, response_file):
    """AC5–AC7: refuses (`1`, zero calls) on a malformed `pr` or a bad
    `response_file` (missing, not a regular file, or empty/whitespace-only),
    BEFORE any `intake.*` call. Never gates on `intake.has_marker`
    (Constraints) — comment then unconditional close every invocation; a
    ledger append happens only when both return truthy AND
    `carry.scan_events()` finds no existing `note-answered` naming `pr`
    (by `subject` or `source`)."""
    if not PR_URL_RE.match(str(pr) if pr is not None else ""):
        return 1
    if not response_file:
        return 1
    p = pathlib.Path(response_file)
    try:
        raw = p.read_bytes()
    except OSError:
        return 1
    text = raw.decode("utf-8", "replace")
    if not text.strip():
        return 1
    comment_ok = intake.comment(pr, text, "note-answered")
    close_ok = intake.close(pr)
    if not (comment_ok and close_ok):
        return 2
    prior = carry.scan_events(lambda e: e.get("type") == "note-answered"
                              and (str(e.get("subject")) == pr or str(e.get("source")) == pr))
    if not prior:
        dispatch.emit(_answered_ledger_path(), {}, "note-answered", subject=pr, source=pr,
                      project="albert-scott", response=str(response_file))
    return 0


def parser():
    p = argparse.ArgumentParser(prog="doit note-close",
                                description="post FILE as the closing comment on an "
                                            "inbound-note PR, close it, and record it")
    p.add_argument("pr", help="the canonical PR url — no bare number, no normalization")
    p.add_argument("--response", required=True, help="path to the response text file")
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    rc = note_close(a.pr, a.response)
    print({0: f"closed {a.pr}", 1: "refused: malformed pr or response file",
          2: "failed: intake.comment/intake.close reported failure — retryable"}[rc],
         file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
