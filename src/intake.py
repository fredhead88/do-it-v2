#!/usr/bin/env python3
"""intake — one rate-guarded `gh` read per tick lists albert-scott's open
`inbound-spec`/`inbound-note` PRs, auto-registers a trusted author's not-yet-
registered spec PR, and leaves an untrusted author's exactly as today for a
human (ADR-0028-5, L-charter-0031).

  intake.run(events)          one tick's worth of listing + register/await,
                               wired into `tick._record()`.

Primitives only: `comment`/`close`/`has_marker` are produced here for the
`spec-pr-closeout` sibling; nothing in this module's own `run()` calls any of
them. Reading or summarising a PR's semantic content is out of scope (charter
§3) — `run()` hands `inbound-note` PRs through UNREAD, as `note_prs`.

The dedup/precedence read is ALWAYS `carry.scan_events()` — unscoped by
`$DOIT_PROJECT` — never the `events` argument `run()`/`board_rows()` receive
(§5). `carry.trusted_authors()`/`carry.scan_events()` are consumed, never
re-implemented.
"""
import base64, json, os, pathlib, subprocess as _subprocess, sys
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import carry, dispatch  # noqa: E402 — dispatch.emit is this module's own append path

REPO = "fredhead88/albert-scott-platform"          # A4: the one hardcoded target
RATE_FLOOR = 200                                   # SD16's own default (Q5, not reopened)
INBOUND_PREFIX = "docs/do-it/inbound/"

# Read at call time, never at import (carry.py's own idiom) — a test replaces
# this wholesale before any call reaches `gh`.
subprocess = _subprocess

root = lambda: pathlib.Path(os.environ.get("DOIT_ROOT") or (pathlib.Path.home() / ".do-it"))
now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")


def ledger_path():
    """`$DOIT_ROOT/events/L-intake-local.jsonl` — the fold derives actor
    `intake` from this exact filename (D90), matching `fold.EMITS`."""
    return root() / "events" / "L-intake-local.jsonl"


def _run(argv):
    """One `subprocess.run` via the module-level `subprocess` — `(False, "")`
    on a non-zero exit or any OSError, never a raise."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True)
    except OSError:
        return False, ""
    return r.returncode == 0, (r.stdout or "")


def _rate_ok():
    """Exactly one `gh api rate_limit`-shaped call per `run()`. `True` unless
    the stub/gh reports `core.remaining < RATE_FLOOR` — a failed or
    unparsable read fails OPEN (never blocks a tick on a `gh` hiccup)."""
    ok, out = _run(["gh", "api", "rate_limit"])
    if not ok:
        return True
    try:
        remaining = json.loads(out)["resources"]["core"]["remaining"]
    except (ValueError, KeyError, TypeError):
        return True
    return remaining >= RATE_FLOOR


def _head_file(path, head_sha):
    """One file's content at the PR's head sha — `""` on any failure, never a
    raise. Fires only from `_register()`, i.e. only for a PR's first
    registration (SD5/SD16)."""
    ok, out = _run(["gh", "api", f"repos/{REPO}/contents/{path}",
                    "-f", f"ref={head_sha}", "-q", ".content"])
    if not ok or not out.strip():
        return ""
    try:
        return base64.b64decode("".join(out.split())).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def list_open(label):
    """One `gh pr list`-shaped call for `label` → a list of `{url, number,
    author_login, title, body, created_at, files, head_sha}` dicts, or `None`
    on a `gh` failure or unparsable JSON — never raises."""
    ok, out = _run(["gh", "pr", "list", "--repo", REPO, "--label", label, "--state", "open",
                    "--json", "url,number,author,title,body,createdAt,files,headRefOid"])
    if not ok:
        return None
    try:
        rows = json.loads(out)
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    out_rows = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        files = [f.get("path") for f in (r.get("files") or []) if isinstance(f, dict) and f.get("path")]
        out_rows.append({"url": r.get("url"), "number": r.get("number"),
                         "author_login": (r.get("author") or {}).get("login") or "",
                         "title": r.get("title") or "", "body": r.get("body") or "",
                         "created_at": r.get("createdAt"), "files": files,
                         "head_sha": r.get("headRefOid")})
    return out_rows


# ── §5 precedence, over carry.scan_events() alone ───────────────────────────
def _matches(url):
    return lambda e: str(e.get("subject")) == url or str(e.get("source")) == url


def _latest_state(url):
    """`"registered"` when `inbound-registered`/`spec-carried` was EVER seen
    for `url` (stops registration AND awaiting forever — the only thing that
    does); else the type of the LATEST `inbound-awaiting`/`inbound-awaiting-
    gone` event for `url`; else `None` (never sighted before)."""
    hits = carry.scan_events(_matches(url))
    if any(e.get("type") in ("inbound-registered", "spec-carried") for e in hits):
        return "registered"
    awaiting = [e for e in hits if e.get("type") in ("inbound-awaiting", "inbound-awaiting-gone")]
    if not awaiting:
        return None
    return sorted(awaiting, key=lambda e: e.get("ts") or "")[-1]["type"]


def _awaiting_urls():
    """Every url whose §5 latest state is `inbound-awaiting` right now — the
    read `board_rows()` and `run()`'s own "gone" pass both share."""
    hits = carry.scan_events(lambda e: e.get("type") in ("inbound-awaiting", "inbound-awaiting-gone"))
    urls = {str(e.get("subject") or e.get("source")) for e in hits} - {"", "None"}
    return {u for u in urls if _latest_state(u) == "inbound-awaiting"}


def _latest_awaiting_event(url):
    hits = [e for e in carry.scan_events(_matches(url)) if e.get("type") == "inbound-awaiting"]
    return sorted(hits, key=lambda e: e.get("ts") or "")[-1] if hits else {}


# ── the two appends R1/R2 make ───────────────────────────────────────────────
def _register(pr):
    body = pr.get("body") or ""
    for path in sorted(f for f in (pr.get("files") or []) if f.startswith(INBOUND_PREFIX)):
        body += f"\n\n## {path}\n{_head_file(path, pr.get('head_sha'))}"
    dispatch.emit(ledger_path(), {}, "inbound-registered", subject=pr["url"], source=pr["url"],
                 project="albert-scott", title=pr.get("title") or "",
                 author_login=pr.get("author_login") or "", auto=True, body=body)


def _awaiting(pr):
    dispatch.emit(ledger_path(), {}, "inbound-awaiting", subject=pr["url"], source=pr["url"],
                 project="albert-scott", author_login=pr.get("author_login") or "",
                 title=pr.get("title") or "", opened_at=pr.get("created_at"))


def _gone(url):
    dispatch.emit(ledger_path(), {}, "inbound-awaiting-gone", subject=url, source=url,
                 project="albert-scott")


def _process_spec_prs(prs):
    trusted = {(a.get("login") or "").strip().lower() for a in carry.trusted_authors()}
    open_urls = set()
    for pr in prs:
        url = pr.get("url")
        if not url:
            continue
        open_urls.add(url)
        state = _latest_state(url)
        if state == "registered":
            continue
        if (pr.get("author_login") or "").strip().lower() in trusted:
            _register(pr)
            continue
        if state != "inbound-awaiting":
            _awaiting(pr)
    for url in _awaiting_urls():
        if url not in open_urls:
            _gone(url)


def run(events):
    """One rate-limit read; when healthy, one `gh pr list`-shaped call per
    label, then the R1/R2 register/await/gone pass over `inbound-spec`.
    `inbound-note` PRs are listed only, handed through unread (SD14). `events`
    is NOT the dedup source (§5 uses `carry.scan_events()` instead) — kept for
    wave-2 seam compatibility only. Never raises."""
    del events
    ledger_path().parent.mkdir(parents=True, exist_ok=True)
    if not _rate_ok():
        return {"spec_prs": [], "note_prs": []}
    raw_spec_prs = list_open("inbound-spec")
    note_prs = list_open("inbound-note") or []
    spec_prs = raw_spec_prs or []
    # A `gh` FAILURE (None) never runs the register/await/gone pass — a
    # transient listing failure must not be read as "every PR vanished" and
    # sweep every standing `inbound-awaiting` into `inbound-awaiting-gone`. A
    # genuinely EMPTY, successful listing (`[]`) still runs it, so §5(b)'s
    # "gone" pass fires when every open PR really has left.
    if raw_spec_prs is not None:
        _process_spec_prs(spec_prs)
    return {"spec_prs": spec_prs, "note_prs": note_prs}


# ── the primitives the closeout sibling composes over (produced, not called) ─
def _pr_state(url):
    ok, out = _run(["gh", "pr", "view", url, "--json", "state"])
    if not ok:
        return None
    try:
        return (json.loads(out).get("state") or "").upper()
    except ValueError:
        return None


def has_marker(url, marker):
    """Whether `url`'s comments already carry `marker` — `False` on any
    failure, never raises."""
    ok, out = _run(["gh", "pr", "view", url, "--json", "comments"])
    if not ok:
        return False
    try:
        comments = json.loads(out).get("comments") or []
    except ValueError:
        return False
    return any(marker in (c.get("body") or "") for c in comments)


def comment(url, body, marker):
    """Posts `body` (assumed to carry `marker`) once — a no-op, still `True`,
    on a second call for the same `(url, marker)`."""
    if has_marker(url, marker):
        return True
    ok, _ = _run(["gh", "pr", "comment", url, "--body", body])
    return ok


def close(url):
    """Closes `url` — never `--delete-branch`, never `--merge`. A PR already
    `CLOSED`/`MERGED` is a no-op returning `True` with no `gh pr close` call
    at all — idempotent on a second call, mirroring `comment()`'s own
    marker-gated idempotency."""
    if _pr_state(url) in ("CLOSED", "MERGED"):
        return True
    ok, _ = _run(["gh", "pr", "close", url])
    return ok


# ── SD9: the INBOUND-awaiting render rows, read through fold.py's lazy _intake() ─
def board_rows(events):
    """One `"<url> · awaiting registration · <author_login> · opened
    <opened_at>"` row per url whose §5 latest state is `inbound-awaiting`,
    sorted by url. `events` is unused (§5 reads `carry.scan_events()` alone) —
    kept so the signature matches what `fold.py`'s call site passes."""
    del events
    rows = []
    for url in sorted(_awaiting_urls()):
        e = _latest_awaiting_event(url)
        rows.append(f"{url} · awaiting registration · {e.get('author_login', '?')} · "
                    f"opened {e.get('opened_at', '?')}")
    return rows
