#!/usr/bin/env python3
"""pr_closeout — closes out a carried, spec-written `inbound-spec` PR with one
success comment; comments once, verbatim, on a PR whose most recent carry
attempt failed. Never merges, never reads a PR's title/body — the only
untrusted text it ever surfaces is a `carry-failed` event's own `error`
field, passed straight through as a plain string (L-charter-0031 §3 reserves
semantic reading to a human/Thinker).

  pr_closeout.run(events, open_spec_prs)   one tick's worth of the R3 sweep,
                                            wired into `tick._record()`
                                            between `intake.run`'s own
                                            try/except and the ledger
                                            re-read (SD11).

Every live GitHub effect is `intake`'s own (`comment`/`close`); this module
issues no subprocess call, and no `gh`-shaped call, of its own. `events` is
accepted for wave-2 seam-signature parity with `intake.run`/`notes.run` only
(A9) — the authoritative per-url state read is always `carry.scan_events()`,
never this argument.
"""
import pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import carry, dispatch, intake  # noqa: E402

SUCCESS_TEMPLATE = (
    "Carried into DO-IT as {spec_id} (spec written {written_at}). "
    "Follow it with `doit events {spec_id}` on the builder box. "
    "Closing: the spec is now the ledger's, not this PR's."
)
FAILURE_TEMPLATE = (
    "Carry failed: {error} This PR stays open — a future `doit tick` "
    "retries automatically once the issue is resolved."
)


def _matches(url):
    return lambda e: str(e.get("subject")) == url or str(e.get("source")) == url


def _already_closed_out(url):
    """§5.1 — an `inbound-closed` for `url` already lands; every further
    check for it this call is skipped entirely."""
    return bool(carry.scan_events(
        lambda e: e.get("type") == "inbound-closed" and _matches(url)(e)))


def _already_commented_failed(url):
    """§5.2's own idempotency gate — a ledger read, never a live
    `intake.has_marker` check."""
    return bool(carry.scan_events(
        lambda e: e.get("type") == "inbound-pr-commented"
        and e.get("marker") == "carry-failed" and _matches(url)(e)))


def _carry_failed_events(url):
    """Every `carry-failed` event naming `url`, oldest first (`ts`)."""
    hits = carry.scan_events(
        lambda e: e.get("type") == "carry-failed" and _matches(url)(e))
    return sorted(hits, key=lambda e: e.get("ts") or "")


def _most_recent_attempt_is_failure(url):
    """A9(b): the newer of `carry.already_carried(url)`'s hit (if any) and
    the latest qualifying `carry-failed` hit decides — no `already_carried`
    hit at all, or its `ts` is STRICTLY OLDER than the latest `carry-failed`
    hit's `ts`, means failed; a same-or-newer `spec-carried` means carried
    and still awaiting a spec, never a failure."""
    failed = _carry_failed_events(url)
    if not failed:
        return False
    latest_failed_ts = failed[-1].get("ts") or ""
    carried = carry.already_carried(url)
    if carried is None:
        return True
    return (carried.get("ts") or "") < latest_failed_ts


def run(events, open_spec_prs):
    """One R3 sweep over `open_spec_prs`. `events` is unused (A9). Returns
    `{"closed": [...], "commented": [...]}` — urls acted on THIS call only."""
    del events
    closed, commented = [], []
    for pr in open_spec_prs or []:
        url = pr.get("url") if isinstance(pr, dict) else None
        if not url:
            continue
        if _already_closed_out(url):
            continue
        carried = carry.already_carried(url)
        spec_id = carried.get("subject") if carried else None
        written = carry.spec_written(spec_id) if spec_id else None
        if written:
            body = SUCCESS_TEMPLATE.format(spec_id=spec_id, written_at=written.get("ts"))
            intake.comment(url, body, f"closed:{spec_id}")
            intake.close(url)
            dispatch.emit(intake.ledger_path(), {}, "inbound-closed",
                          subject=url, source=url, project="albert-scott", spec_id=spec_id)
            closed.append(url)
            continue
        if _already_commented_failed(url):
            continue
        if not _most_recent_attempt_is_failure(url):
            continue
        oldest = _carry_failed_events(url)[0]
        body = FAILURE_TEMPLATE.format(error=oldest.get("error"))
        intake.comment(url, body, "carry-failed")
        dispatch.emit(intake.ledger_path(), {}, "inbound-pr-commented",
                      subject=url, source=url, project="albert-scott", marker="carry-failed")
        commented.append(url)
    return {"closed": closed, "commented": commented}
