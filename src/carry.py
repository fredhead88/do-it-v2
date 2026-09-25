#!/usr/bin/env python3
"""carry — one inbound v4 record (or PR) becomes a `charter: null` spec-writer spawn.

  carry.py <source> [--repo DIR] [--project NAME] [--force] [--title T] [--body B]

`<source>` is a v4 ledger record id (`1454`, or the full filename stem) or an
http(s) PR url. The v4 path resolves the record and its staged material, refuses
BY NAME when either cannot be resolved, allocates `L-spec-NNNN`, writes the packet
(the staged bytes plus the precedent's carry trailer — `L-spec-writer-0063`'s
packet is exactly that, verified byte-for-byte), and hands it to `doit dispatch
spec-writer` with an absolute `--path`, an explicit `--cwd`/`--project`, and no
`--charter`. This replaces the hand path used for v4 records 1448–1460.

Two rules earn their own words, because getting either wrong costs $5 a time:

* **The inbox is read first, `spec_file` only when readable.** Every registered
  record 1448–1462 carries a `spec_file` under `~/.claude/spec-staging/` and none
  of those files exists; the copy the hand path read is `~/.claude/spec-inbox/`.
  A "spec_file first, unreadable ⇒ refuse" rule refuses every real source.
* **The review tier is read off the `spec-written` event's `footprint`**, never
  off markdown this tool parsed. The v2 corpus has five spellings of the
  `Writes:` block, so a parser miss yields an empty footprint and a silent
  `gates-only` — the unsafe direction. An empty footprint refuses to stamp.

Every refusal fires before `alloc`, so a refused carry allocates nothing and
spends nothing. A killed, split or failed spawn stamps nothing, which is what
makes it a retry rather than a permanently burned source.
"""
import argparse, glob, json, os, pathlib, re, subprocess, sys
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
FIELDS = ("spec_id", "intent", "status", "spec_file", "handed_over_at")

# The trailer the hand path typed, verbatim (em dash U+2014). `carry.py` appends no
# `spawn_id` line of its own — `dispatch` owns that (`packet += f"\n\nspawn_id: …"`).
# `{charter_clause}` is the one variable sentence (L-spec-0198, Produces): the
# free-standing literal, byte-identical to the precedent, when `charter` is not
# honored for any reason, or the honored charter's own id (D3) when it is.
TRAILER = ("\n---\nCarry this spec over into v2 form — Writes: line, AC ids, Verification "
           "block, under 400 lines. Change nothing substantive. {charter_clause} "
           "Source: v4 spec {source_id}.\n")


def _charter_clause(charter):
    """The TRAILER's one variable sentence. `charter` is `None` for every
    not-honored outcome (untrusted/absent/unknown/closed) — the free-standing
    literal, verbatim (D15) — and the charter id itself, `(declared, D3)`, only
    when honored."""
    return (f"charter: {charter} (declared, D3)." if charter is not None
            else "charter: null (free-standing, D15).")

# Checked in this order, first hit wins; case-insensitive substring per path.
# `/versions/` and `migration` are separate rules on purpose: an Alembic path is
# `…/versions/0099_x.py`, which contains neither `/migrations/` nor `migration`.
PREDICATES = (("money", ("billing", "pricing", "cost", "spend", "settlement", "payout")),
              ("production-data", ("pipelines/", "schema", ".sql", "/migrations/")),
              ("production-config", ("deploy.sh", "cron", ".env", "secrets", ".service", "systemd")),
              ("migration", ("/versions/", "migration")))


class Refusal(Exception):
    """A by-name refusal. Always raised before `alloc`; `main` prints it and exits non-zero."""


class CarryFailed(Exception):
    """Every failure AFTER `alloc` fires: a killed/failed spawn, an empty footprint, a
    `doit append spec-carried` that returned non-zero, or a `fold.check_append` refusal.
    Nothing is stamped on any of these paths, so an unforced retry of the same source
    succeeds afterward — identical semantics to today's dispatch-failure and
    killed-spawn paths. `main` prints it and exits non-zero, same as `Refusal`."""


def _dir(var, default):
    return pathlib.Path(os.environ.get(var) or (pathlib.Path.home() / default))


# Read at call time, never at import: the suite points these at fixture directories
# and must never touch the operator's real ledger, inbox or staging tree.
root = lambda: pathlib.Path(os.environ.get("DOIT_ROOT") or (pathlib.Path.home() / ".do-it"))
ledger_dir = lambda: _dir("V4_LEDGER_DIR", ".claude/ledger")
inbox_dir = lambda: _dir("V4_INBOX_DIR", ".claude/spec-inbox")
staging_dir = lambda: _dir("V4_STAGING_DIR", ".claude/spec-staging")
doit_bin = lambda: HERE.parent / "doit"
now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
is_url = lambda s: bool(re.match(r"^https?://", s.strip()))
numeric_prefix = lambda s: (re.match(r"^(\d+)", s.strip()) or [None, None])[1]

# Captured before `uncarried()`/`sync_v4()` shadow the name with a same-named
# parameter (the Plan's own declared seam name) — both call this alias for "the
# operator's real ledger dir" when their own `ledger_dir=` argument is None.
_default_ledger_dir = ledger_dir


def ledger_actor():
    """The actor a `DOIT_LEDGER_FILE` name resolves to — the identical derivation
    `fold.read_events()` applies to a ledger filename (D90, §2.5), so an append this
    module builds names the actor the fold would independently derive for it."""
    stem = pathlib.Path(os.environ.get("DOIT_LEDGER_FILE", "L-operator-local.jsonl")).stem
    parts = stem.split("-")
    return "-".join(parts[1:-1]) if len(parts) >= 3 else stem


# ── trusted-author inbound charter (L-spec-0198) ────────────────────────────────
def trusted_authors(path=None):
    """One `{"login", "email"}` dict per well-formed `[[author]]` table in the
    TOML doc at `path` (default `$DOIT_ROOT/trusted_inbound_authors.toml`, read
    at call time via `root()` — never at import). An incomplete table (either
    key absent, blank, or non-string) is dropped, not an error. Absent file, any
    parse exception (`tomllib.TOMLDecodeError` included), or a non-list `author`
    key all degrade to `[]` — never an exception (AC1)."""
    p = pathlib.Path(path) if path is not None else (root() / "trusted_inbound_authors.toml")
    try:
        import tomllib
        with open(p, "rb") as f:
            doc = tomllib.load(f)
    except (OSError, ValueError, ImportError):
        return []
    rows = doc.get("author")
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        login, email = row.get("login"), row.get("email")
        if isinstance(login, str) and login.strip() and isinstance(email, str) and email.strip():
            out.append({"login": login, "email": email})
    return out


def _frontmatter_value(spec_text, key):
    """The trimmed value of `key:` inside the leading `---`-delimited frontmatter
    block — the same block `pr_slot` writes and the live `~/.claude/spec-inbox/
    *.md` shape (`intent:`/`author:`/`base_sha:`/`charter:`). `None` for no
    leading block, no such key inside it, or a `null`/`~`/blank value."""
    lines = spec_text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    block, closed = [], False
    for line in lines[1:]:
        if line.strip() == "---":
            closed = True
            break
        block.append(line)
    if not closed:
        return None
    for line in block:
        m = re.match(rf"^{re.escape(key)}:[ \t]*(.*)$", line)
        if m:
            v = m.group(1).strip()
            if v[:1] in "'\"" and len(v) > 1 and v.endswith(v[0]):
                v = v[1:-1]
            return v if v not in ("", "null", "~") else None
    return None


def frontmatter_charter(spec_text):
    """The trimmed `charter:` value inside the leading frontmatter block, or
    `None` for no leading block, no `charter:` key, or a `null`/`~`/blank
    value (AC2)."""
    return _frontmatter_value(spec_text, "charter")


def _frontmatter_author_email(spec_text):
    """The email to match against `trusted_authors()`: the `<…>` substring of
    the frontmatter `author:` value, or — absent angle brackets — the whole
    trimmed value when it contains `@`. `None` otherwise (Boundaries)."""
    v = _frontmatter_value(spec_text, "author")
    if v is None:
        return None
    m = re.search(r"<([^<>]*)>", v)
    if m:
        return m.group(1).strip() or None
    return v.strip() if "@" in v else None


def trusted_author_for(spec_text):
    """The matched `trusted_authors()` row's `login` (or `email` when blank) —
    case-insensitive, trimmed, against `spec_text`'s own frontmatter `author:`
    email. `None` on no match, no rows, or unusable author frontmatter (every
    PR-sourced packet resolves here, A4)."""
    email = _frontmatter_author_email(spec_text)
    if not email:
        return None
    needle = email.strip().lower()
    for row in trusted_authors():
        if (row.get("email") or "").strip().lower() == needle:
            return row.get("login") or row.get("email")
    return None


def _charter_validity(charter_id):
    """(state, ever_filed) for a charter id, read via `scan_events(lambda e:
    True)` folded through `fold.fold()` — project-unfiltered (finding 2), NEVER
    `fold.read_events()`, whose import-time `DOIT_PROJECT` filter would drop a
    `charter-filed` event stamped under another project label. `state` is
    `None` when the ledger has never named this charter id at all."""
    import fold                          # here, not at the top: mirrors this
                                          # file's own lazy `import dispatch`/`import fold`
    events = scan_events(lambda e: True)
    _, charters, _, _ = fold.fold(events)
    c = charters.get(charter_id)
    if c is None:
        return None, False
    return c.get("state"), any(e.get("type") == "charter-filed" for e in c["evs"])


def _resolve_charter(spec_text, trusted_author):
    """(charter, charter_reason) — A9's four-way split, trust gated FIRST
    (Planner ruling, finding 1): an untrusted author's declared charter is
    recorded on `charter_reason` but never honored, even naming a real, open
    charter. Evaluated in order: trust, then presence, then validity."""
    named = frontmatter_charter(spec_text)
    if trusted_author is None:
        return None, ("untrusted" if named else "absent")
    if not named:
        return None, "absent"
    import tree_cleanup                  # here, not at the top: same lazy idiom
    state, ever_filed = _charter_validity(named)
    if not ever_filed:
        return None, "unknown"
    if state in tree_cleanup.CLOSED:
        return None, "closed"
    return named, None


# ── the v4 record ─────────────────────────────────────────────────────────────
def _regex_record(path, text):
    """A9's fallback, used only when PyYAML is not importable. Five named top-level
    keys, and a REFUSAL — never a mis-parse — for any of them that is folded or a
    block scalar. Never a general YAML parser."""
    rec = {}
    for k in FIELDS:
        m = re.search(rf"(?m)^{k}:[ \t]*(.*)$", text)
        if not m:
            rec[k] = None
            continue
        v = m.group(1).strip()
        folded = v in ("", "|", ">", "|-", ">-", "|+", ">+") or (
            v[:1] in "'\"" and not (len(v) > 1 and v.endswith(v[0])))
        if folded:
            raise Refusal(f"carry: refuses {path.name}: `{k}` is a folded or block scalar the "
                          f"fallback parser cannot resolve, and PyYAML is not importable (A9)")
        if v[:1] in "'\"":
            v = v[1:-1].replace("''", "'")
        rec[k] = None if v in ("null", "~") else v
    return rec


def load_record(path):
    """The five fields this tool reads, via `yaml.safe_load` when it is importable."""
    text = path.read_text()
    try:
        import yaml
    except ImportError:
        return _regex_record(path, text)
    d = yaml.safe_load(text) or {}
    rec = {k: d.get(k) for k in FIELDS}
    # `handed_over_at` rides through VERBATIM, off the raw text: `safe_load` turns an
    # UNQUOTED ISO stamp into a datetime, and str() on that re-serializes it
    # (`2026-09-15T12:52:02Z` becomes `2026-09-15 12:52:02+00:00`) — a silent rewrite
    # of the v4 record's own stamp, which `spec-carried` is supposed to carry across.
    m = re.search(r"(?m)^handed_over_at:[ \t]*(.*)$", text)
    if m:
        rec["handed_over_at"] = m.group(1).strip().strip("'\"") or None
    return rec


def resolve_record(source):
    """(record path, record, source id) for a v4 id. Zero matches and more than one
    are each their own refusal — the first of several matches is never guessed."""
    source, d = source.strip(), ledger_dir()
    prefix = numeric_prefix(source)
    hits = sorted(glob.glob(str(d / f"{prefix}-*.yml"))) if prefix else []
    if not hits:
        raise Refusal(f"carry: no v4 record matching {source} under {d}")
    if len(hits) > 1:
        raise Refusal(f"carry: {source} is ambiguous: {len(hits)} matches")
    p = pathlib.Path(hits[0])
    return p, load_record(p), prefix


def staged_material(source, prefix, record):
    """The inbox first; the record's `spec_file` only when it is a readable file."""
    pattern = str(inbox_dir() / f"{prefix}-*-spec.md")
    hits = sorted(glob.glob(pattern))
    if len(hits) > 1:
        raise Refusal(f"carry: {source} is ambiguous: {len(hits)} matches")
    if hits:
        return pathlib.Path(hits[0]).read_bytes()
    sf = record.get("spec_file")
    for cand in ([pathlib.Path(sf), staging_dir() / pathlib.Path(sf).name] if sf else []):
        if cand.is_file():
            return cand.read_bytes()
    raise Refusal(f"carry: refuses {source}: no staged spec — inbox glob {pattern} matched nothing "
                  f"and spec_file {sf} is not readable")


def _has_staged_material(prefix, record, inbox_dir_path):
    """Whether `staged_material`'s own two-step rule would resolve for this record —
    without reading or returning the bytes, and never raising: `uncarried()` reports
    a record's staging state either way, unlike `carry()`'s by-name refusal. `prefix`
    is always drawn from `ledger_dir`'s own directory listing (never event/attacker
    data), so this helper has no path-traversal surface of its own (security_path)."""
    try:
        hits = glob.glob(str(pathlib.Path(inbox_dir_path) / f"{prefix}-*-spec.md"))
        if len(hits) == 1:
            return True
        if len(hits) > 1:
            return False  # ambiguous — `carry()` would refuse, not succeed
        sf = record.get("spec_file")
        for cand in ([pathlib.Path(sf), staging_dir() / pathlib.Path(sf).name] if sf else []):
            if cand.is_file():
                return True
        return False
    except (OSError, ValueError):
        return False


# ── the slot ──────────────────────────────────────────────────────────────────
def _write_slot(body, source_id):
    """Allocate `content/L-spec-NNNN.md` with `dispatch.alloc` (O_EXCL, §2.8) and write
    the packet beside it. The packet path is always this tool's own — never a path
    built from the source string."""
    import dispatch                      # here, not at the top: it shells out at import
    content = root() / "content"
    content.mkdir(parents=True, exist_ok=True)
    spec_id = dispatch.alloc(content, "L-spec-", ".md").stem
    slot = content / f"carry-{spec_id}.packet.md"
    slot.write_bytes(body)
    return slot, spec_id, source_id, None


def carried_slot(source):
    """(slot_path, spec_id, source_id, charter, charter_reason) for a v4 record id.

    The packet is the staged bytes plus the trailer — not a verbatim copy of the
    staged file, which would hand the writer no directive at all. `charter` is
    honored only when the staged material's own `author:` frontmatter matches
    `trusted_authors()` FIRST (finding 1); `charter_reason` names why not
    otherwise (`untrusted`/`absent`/`unknown`/`closed`)."""
    source = source.strip()
    if is_url(source):
        raise Refusal(f"carry: {source} is a url, not a v4 record id — the PR path builds its "
                      f"packet from --title/--body (pr_slot), and never fetches")
    path, record, source_id = resolve_record(source)
    if not (record.get("intent") or "").strip():
        raise Refusal(f"carry: refuses {source}: v4 record {path.name} has an empty or missing "
                      f"`intent` — there is nothing to carry")
    staged = staged_material(source, source_id, record)
    text = staged.decode("utf-8", "replace")
    trusted_author = trusted_author_for(text)
    charter, charter_reason = _resolve_charter(text, trusted_author)
    trailer = TRAILER.format(charter_clause=_charter_clause(charter), source_id=source_id)
    slot, spec_id, source_id, _ = _write_slot(staged + trailer.encode(), source_id)
    return slot, spec_id, source_id, charter, charter_reason


def head_sha(repo):
    """The repo's HEAD, read off the ref store — never a subprocess, so the PR path
    makes no call at all beyond the two `doit` ones."""
    try:
        g = pathlib.Path(repo) / ".git"
        if g.is_file():
            g = pathlib.Path(g.read_text().split("gitdir:", 1)[1].strip())
        head = (g / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head or None
        ref = head.split(":", 1)[1].strip()
        common = g
        if (g / "commondir").is_file():
            common = (g / (g / "commondir").read_text().strip()).resolve()
        for c in (g / ref, common / ref, common / "packed-refs"):
            if not c.is_file():
                continue
            if c.name == "packed-refs":
                for line in c.read_text().splitlines():
                    if line.endswith(" " + ref):
                        return line.split()[0]
            else:
                return c.read_text().strip() or None
    except (OSError, IndexError, ValueError):
        pass
    return None


def pr_slot(url, title, body, repo):
    """The narrow PR path: construction only. Both flags are required — a missing one
    is a refusal, never a silently empty packet. AC9's live-fetch half is owed.
    `author:` is always the invoking operator's local username, never a real
    GitHub login (A4), so `trusted_author` — and therefore `charter` — always
    resolves `None`; `charter_reason` is always `"absent"` (no `charter:` key is
    ever written here) until a live PR-author fetch exists (out of scope)."""
    if not (title and title.strip()) or not (body and body.strip()):
        raise Refusal(f"carry: refuses {url}: the PR path needs both --title and --body — this "
                      f"command constructs the packet from flags and never fetches the PR")
    import getpass
    author = os.environ.get("USER") or (getpass.getuser() if hasattr(getpass, "getuser") else "")
    text = ("---\n"
            f"intent: {json.dumps(body)}\n"
            "source_brief: null\n"
            f"author: {json.dumps(author or 'unknown')}\n"
            f"base_sha: {json.dumps(head_sha(repo))}\n"
            "---\n\n"
            f"# {title}\n")
    trusted_author = trusted_author_for(text)
    charter, charter_reason = _resolve_charter(text, trusted_author)
    trailer = TRAILER.format(charter_clause=_charter_clause(charter), source_id=url)
    slot, spec_id, source_id, _ = _write_slot((text + trailer).encode(), url.strip())
    return slot, spec_id, source_id, charter, charter_reason


# ── the tier ──────────────────────────────────────────────────────────────────
def review_tier(footprint):
    """(tier, rule). `full` on the first predicate any path in the footprint matches;
    otherwise `("gates-only", "none")`. An EMPTY footprint is not answered here — the
    caller refuses to stamp rather than read it as `gates-only`."""
    for rule, needles in PREDICATES:
        for p in footprint or []:
            low = str(p).lower()
            if any(n in low for n in needles):
                return "full", rule
    return "gates-only", "none"


# ── the ledger, with the project filter neutralised ───────────────────────────
def scan_events(pred):
    """Every matching event under `$DOIT_ROOT/events`, oldest file first, with NO
    project filter. `fold.read_events()` drops every event whose `project` differs
    from `DOIT_PROJECT`, so a spec carried under another project label is invisible
    to a naive guard and the double-spend the guard exists to prevent happens anyway.

    Every returned event is stamped with `actor`, derived from its source filename
    exactly as `fold.read_events()` derives it (L-spec-0198, finding 2) — additive;
    existing callers that never look at `actor` are unaffected."""
    out = []
    for f in sorted((root() / "events").glob("*.jsonl")):
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        parts = f.stem.split("-")
        actor = "-".join(parts[1:-1]) if len(parts) >= 3 else f.stem
        for line in lines:
            if not line.strip().startswith("{"):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            e = {**e, "actor": actor}
            if pred(e):
                out.append(e)
    return out


def already_carried(source_id):
    """The `spec-carried` event for this source, if any — the idempotency guard's read."""
    hits = scan_events(lambda e: e.get("type") == "spec-carried"
                       and str(e.get("source")) == str(source_id))
    return hits[-1] if hits else None


def spec_written(spec_id):
    hits = scan_events(lambda e: e.get("type") == "spec-written" and e.get("subject") == spec_id)
    return hits[-1] if hits else None


def spawn_status(spec_id):
    """What the spawn reported when it did not write: `spec-killed`, or the status
    scalar `dispatch` copied onto `spawn-done`."""
    for e in reversed(scan_events(lambda e: e.get("subject") == spec_id)):
        if e.get("type") == "spec-killed":
            return "killed"
        if e.get("type") == "spawn-done" and e.get("status"):
            return e["status"]
    return "unknown"


# ── the command ───────────────────────────────────────────────────────────────
def resolve_target(project, repo):
    """(project, repo). Never inherited: `dispatch` defaults `cwd` to `os.getcwd()`
    and `project` to that directory's basename, which would carry into this repo."""
    project = (project or os.environ.get("DOIT_PROJECT") or "").strip()
    if not project:
        raise Refusal("carry: --project is unset and DOIT_PROJECT is empty — name the project the "
                      "spec is written against")
    repo = pathlib.Path(repo) if repo else (root() / "repos" / project)
    if not repo.is_dir():
        raise Refusal(f"carry: --repo {repo} is not a directory — name the repo the spec is "
                      f"written against, rather than carry into the wrong one")
    return project, repo


def parser():
    p = argparse.ArgumentParser(prog="doit carry", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source", help="a v4 ledger record id (1454, or the full stem), or a PR url")
    p.add_argument("--repo", help="the spawn's cwd; default $DOIT_ROOT/repos/<project>")
    p.add_argument("--project", help="the project label; default $DOIT_PROJECT")
    p.add_argument("--title", help="PR path only: the spec title")
    p.add_argument("--body", help="PR path only: the intent")
    p.add_argument("--from-ledger", action="store_true", dest="from_ledger",
                   help="PR path only (SD13): resolve title/body from the newest "
                        "inbound-registered event naming <source>, in-process")
    p.add_argument("--force", action="store_true", help="carry a source that was already carried")
    return p


def _title_body_from_ledger(url):
    """(title, body) off the newest `inbound-registered` event naming `url` in
    `scan_events()` — in-process, never a shell (SD13)."""
    hits = scan_events(lambda e: e.get("type") == "inbound-registered" and str(e.get("source")) == url)
    if not hits:
        raise Refusal(f"carry: --from-ledger found no inbound-registered event naming {url}")
    ev = sorted(hits, key=lambda e: e.get("ts") or "")[-1]
    return ev.get("title") or "", ev.get("body") or ""


def _append_lint_warnings(spec_id, path, validate):
    """L-spec-0276/R5 (SD25): a packet-lint `warn` finding becomes one visible,
    deduplicated `spec-lint-warning` event per finding, appended via `doit
    append` (never in-process — this module shells out to `doit`, mirroring
    the `spec-carried` append it sits beside). Best-effort: a failed append
    (`doit append`'s own non-zero exit) is never raised as `CarryFailed` and
    never blocks the caller. `getattr`, not a bare call: `packet-lint` (wave 1)
    had not merged at this spec's own base_sha, so real `validate.py` carries
    no `spec_shape_warnings` yet (Wave 2 runs after Wave 1 merges —
    Constraints); `validate` is passed in, not imported here, so a test can
    monkeypatch the module object and this reads the patched version through
    the SAME reference `_do_carry` already holds.

    The dedup read is `scan_events()`, NOT `fold.read_events()` — the same
    reason `already_carried`/`spec_written` already use it: a project-filtered
    read is blind to a prior `spec-lint-warning` recorded under a different
    project label, and a blind dedup check re-appends exactly the duplicate it
    exists to prevent."""
    warn_fn = getattr(validate, "spec_shape_warnings", None)
    warn_findings = warn_fn(pathlib.Path(path).read_text()) if warn_fn else []
    if not warn_findings:
        return
    seen = {(e.get("subject"), e.get("finding"))
            for e in scan_events(lambda e: e.get("type") == "spec-lint-warning")}
    for f in warn_findings:
        if (spec_id, f) not in seen:
            subprocess.run([str(doit_bin()), "append", "spec-lint-warning", spec_id, f"finding={f}"],
                           capture_output=True, text=True)


def _do_carry(source, *, repo=None, project=None, force=False, title=None, body=None,
              from_ledger=False):
    """Today's `main()` logic, refactored into a private, importable callable — the
    seven-field dict `main()` used to print directly. Raises `Refusal` for every
    failure BEFORE `alloc` fires, `CarryFailed` for every failure after. Not a seam:
    no sibling's Consumes names `_do_carry`; call `carry()` instead."""
    project, repo = resolve_target(project, repo)
    source = source.strip()
    if from_ledger and (title or body):
        raise Refusal("carry: --from-ledger and --title/--body are mutually exclusive")
    sid = source if is_url(source) else (numeric_prefix(source) or source)
    prior = None if force else already_carried(sid)
    if prior:
        raise Refusal(f"carry: {source} was already carried as {prior.get('subject')}; "
                      f"re-run with --force to carry again")
    if is_url(source):
        if from_ledger:
            title, body = _title_body_from_ledger(source)
        slot, spec_id, source_id, charter, charter_reason = pr_slot(source, title, body, repo)
        audited_at = now()
    else:
        # `handed_over_at` rides through VERBATIM: never re-parsed as a datetime
        # and re-serialized, which would rewrite the v4 record's own stamp.
        audited_at = (resolve_record(source)[1].get("handed_over_at") or now())
        slot, spec_id, source_id, charter, charter_reason = carried_slot(source)
    # Re-derived from the packet's own persisted frontmatter (never re-computed
    # from the pre-trailer text) so `carried_slot`/`pr_slot` keep the 5-tuple
    # shape their Produces signature pins, and `_do_carry` still learns who the
    # trust match named for the `spec-carried` stamp (R10).
    trusted_author = trusted_author_for(pathlib.Path(slot).read_text())
    path = str(root() / "content" / f"{spec_id}.md")
    cmd = [str(doit_bin()), "dispatch", "spec-writer", spec_id]
    if charter is not None:
        cmd += ["--charter", charter]
    cmd += ["--packet", str(slot), "--path", path, "--cwd", str(repo), "--project", project]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        why = next((l for l in (r.stderr or "").splitlines() if l.startswith("FAILED ")),
                   (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["no reason given"])
        raise CarryFailed(f"carry: {spec_id} was not written — "
                          f"{why if isinstance(why, str) else why[0]}")
    ev = spec_written(spec_id)
    if ev is None:
        raise CarryFailed(f"carry: spawn returned status {spawn_status(spec_id)} — no spec written")
    # L-spec-0195/AC5: a spec the tools cannot read never reaches `spec-carried` —
    # named here, in `carry`'s own path, rather than deferred to whatever the
    # eventual `spec-writer` wrapper does with it, because carry-both-ledgers built
    # in wave 1, before `validate.spec_shape` existed for it to call.
    import validate                  # here, not at the top: mirrors this file's own
                                      # lazy `import fold`/`import dispatch` below
    findings = validate.spec_shape(pathlib.Path(path).read_text())
    if findings:
        raise CarryFailed(f"carry: {spec_id} fails spec-shape validation — {'; '.join(findings)}")
    footprint = ev.get("footprint") or []
    if not footprint:
        raise CarryFailed(f"carry: {spec_id} was written with an empty footprint — no tier stamped")
    tier, rule = review_tier(footprint)
    # The `spec-carried` append is checked against the fold BEFORE it fires (Target 3):
    # a refusal here stamps nothing, so an unforced retry of the same source succeeds
    # afterward — identically to the dispatch-failure and killed-spawn paths above.
    event = {"v": 1, "ts": now(), "type": "spec-carried", "subject": spec_id, "source": source_id,
             "tier": tier, "audited_at": audited_at, "footprint": footprint}
    # Null encoding, pinned (finding 3): each of the three is OMITTED — from the
    # event dict `check_append` sees AND from the append argv — when `None`,
    # never the literal `"null"` and never a `k:=null` forced key.
    for k, v in (("charter", charter), ("charter_reason", charter_reason),
                ("trusted_author", trusted_author)):
        if v is not None:
            event[k] = v
    actor = ledger_actor()
    import fold                          # here, not at the top: mirrors this file's own lazy
                                          # `import dispatch` inside `_write_slot` — `check_append`
                                          # is a sibling's seam, not yet on `fold.py` at base_sha
    reason = fold.check_append(event, actor)
    if reason is not None:
        raise CarryFailed(f"carry: {spec_id} would not be honoured by the fold — {reason}. "
                          f"Nothing was appended.")
    # L-spec-0276/R5 (SD25): appended BEFORE the `spec-carried` append below,
    # and best-effort — see `_append_lint_warnings`'s own docstring.
    _append_lint_warnings(spec_id, path, validate)
    ap_argv = [str(doit_bin()), "append", "spec-carried", spec_id,
              f"source={source_id}", f"tier={tier}", f"audited_at={audited_at}",
              f"footprint={json.dumps(footprint)}"]
    for k, v in (("charter", charter), ("charter_reason", charter_reason),
                ("trusted_author", trusted_author)):
        if v is not None:
            ap_argv.append(f"{k}={v}")
    ap = subprocess.run(ap_argv, capture_output=True, text=True)
    if ap.returncode != 0:
        raise CarryFailed(f"carry: {spec_id} was written but spec-carried did not append — "
                          f"{(ap.stderr or '').strip()[-160:]}")
    return {"spec": spec_id, "source": source_id, "tier": tier, "rule": rule,
            "packet": str(slot), "path": path, "project": project}


def carry(source, *, repo=None, project=None, force=False, title=None, body=None):
    """The Plan's own declared seam: the L-spec id. Raises `Refusal` before any
    `alloc`, `CarryFailed` after. A one-line wrapper — `_do_carry` is the whole
    implementation; this function exists so a caller who only wants the id, not
    every field, does not have to know `_do_carry` exists."""
    return _do_carry(source, repo=repo, project=project, force=force, title=title, body=body)["spec"]


def main(argv=None):
    a = parser().parse_args(argv)
    say = lambda m: print(m, file=sys.stderr)
    try:
        res = _do_carry(a.source, repo=a.repo, project=a.project, force=a.force,
                        title=a.title, body=a.body, from_ledger=a.from_ledger)
    except Refusal as e:
        say(str(e))
        return 2
    except CarryFailed as e:
        say(str(e))
        return 1
    print(json.dumps(res))
    return 0


# ── the other half: what the fold has not yet carried, and syncing it back ─────
def uncarried(events, inbox=None, ledger_dir=None):
    """One entry per {source, kind, registered_at, attempts, last_error}, sorted by
    `source` (string comparison).

    `kind: "v4"`: every `registered` v4 record directly under `ledger_dir` whose
    numeric stem prefix is a bare digit string and whose staged material currently
    resolves (`_has_staged_material`), EXCLUDED when a `spec-carried` event in
    `events` already names that exact source.

    `kind: "pr"`: every distinct `source` an `inbound-registered` event in `events`
    names, with no matching `spec-carried` event.

    L-charter-0033/board-owners, Target 3: a source named by an authorized-actor
    `inbound-covered` event (a duplicate-kill, no real carry) is ALSO excluded
    from both loops — actor-gated from the start: an `inbound-covered` from any
    other actor is read and does NOT drop the source, the same way an
    unauthorized event is ignored everywhere else in the ledger (ADR-0028-3)."""
    ld = pathlib.Path(ledger_dir) if ledger_dir is not None else _default_ledger_dir()
    ib = pathlib.Path(inbox) if inbox is not None else inbox_dir()
    carried_sources = {str(e.get("source")) for e in events if e.get("type") == "spec-carried"}
    import fold                          # here, not at the top: mirrors this file's own lazy
                                          # `import fold` inside `_do_carry`/`_charter_validity` —
                                          # `EMITS` is a sibling's seam, not yet on `fold.py` at
                                          # first-writing's base_sha
    covered_sources = {str(e.get("source")) for e in events
                       if e.get("type") == "inbound-covered"
                       and e.get("actor") in fold.EMITS.get("inbound-covered", set())}

    def attempts_for(source):
        fails = [e for e in events if e.get("type") == "carry-failed"
                 and str(e.get("source")) == source]
        return len(fails), (fails[-1].get("error") if fails else None)

    out = []
    if ld.is_dir():
        for p in sorted(ld.glob("*.yml")):
            prefix = p.stem.split("-", 1)[0]
            if not str(prefix).strip().isdigit():
                continue
            try:
                rec = load_record(p)
            except (OSError, ValueError):
                continue
            if (rec.get("status") or "") != "registered":
                continue
            if prefix in carried_sources or prefix in covered_sources:
                continue
            if not _has_staged_material(prefix, rec, ib):
                continue
            n, last = attempts_for(prefix)
            out.append({"source": prefix, "kind": "v4", "registered_at": rec.get("handed_over_at"),
                       "attempts": n, "last_error": last})

    seen_pr = set()
    for e in events:
        if e.get("type") != "inbound-registered":
            continue
        src = str(e.get("source"))
        if src in seen_pr or src in carried_sources or src in covered_sources:
            continue
        seen_pr.add(src)
        n, last = attempts_for(src)
        out.append({"source": src, "kind": "pr", "registered_at": e.get("ts"),
                   "attempts": n, "last_error": last})

    out.sort(key=lambda d: d["source"])
    return out


def sync_v4(events, ledger_dir=None):
    """For every `spec-carried` event in `events` (processed in the given order),
    flip the matching v4 record's `registered` status to `superseded` plus
    `superseded_by: <spec id>` — a two-line, additive splice, every other byte of
    the file unchanged. Returns the count of v4 records updated.

    Skips (never raises) a source that is not a bare digit string, a source that
    resolves to zero or more than one file under `ledger_dir`, or a record whose
    current `status:` line does not read exactly `registered` — including one this
    same call already advanced, so this is a one-time transition, never re-applied."""
    ld = pathlib.Path(ledger_dir) if ledger_dir is not None else _default_ledger_dir()
    n = 0
    for e in events:
        if e.get("type") != "spec-carried":
            continue
        source = str(e.get("source") or "").strip()
        if not source.isdigit():
            continue
        hits = sorted(ld.glob(f"{source}-*.yml"))
        if len(hits) != 1:
            continue
        p = hits[0]
        text = p.read_text()
        if not re.search(r"(?m)^status:\s*registered\s*$", text):
            continue
        spec_id = e.get("subject")
        new_text = re.sub(r"(?m)^status:.*\n",
                          f"status: superseded\nsuperseded_by: {spec_id}\n", text, count=1)
        p.write_text(new_text)
        n += 1
    return n


if __name__ == "__main__":
    sys.exit(main())
