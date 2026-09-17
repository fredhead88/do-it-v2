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
TRAILER = ("\n---\nCarry this spec over into v2 form — Writes: line, AC ids, Verification "
           "block, under 400 lines. Change nothing substantive. charter: null (free-standing, "
           "D15). Source: v4 spec {source_id}.\n")

# Checked in this order, first hit wins; case-insensitive substring per path.
# `/versions/` and `migration` are separate rules on purpose: an Alembic path is
# `…/versions/0099_x.py`, which contains neither `/migrations/` nor `migration`.
PREDICATES = (("money", ("billing", "pricing", "cost", "spend", "settlement", "payout")),
              ("production-data", ("pipelines/", "schema", ".sql", "/migrations/")),
              ("production-config", ("deploy.sh", "cron", ".env", "secrets", ".service", "systemd")),
              ("migration", ("/versions/", "migration")))


class Refusal(Exception):
    """A by-name refusal. Always raised before `alloc`; `main` prints it and exits non-zero."""


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
    """(slot_path, spec_id, source_id, charter=None) for a v4 record id.

    The packet is the staged bytes plus the trailer — not a verbatim copy of the
    staged file, which would hand the writer no directive at all."""
    source = source.strip()
    if is_url(source):
        raise Refusal(f"carry: {source} is a url, not a v4 record id — the PR path builds its "
                      f"packet from --title/--body (pr_slot), and never fetches")
    path, record, source_id = resolve_record(source)
    if not (record.get("intent") or "").strip():
        raise Refusal(f"carry: refuses {source}: v4 record {path.name} has an empty or missing "
                      f"`intent` — there is nothing to carry")
    staged = staged_material(source, source_id, record)
    return _write_slot(staged + TRAILER.format(source_id=source_id).encode(), source_id)


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
    is a refusal, never a silently empty packet. AC9's live-fetch half is owed."""
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
            f"# {title}\n") + TRAILER.format(source_id=url)
    return _write_slot(text.encode(), url.strip())


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
    to a naive guard and the double-spend the guard exists to prevent happens anyway."""
    out = []
    for f in sorted((root() / "events").glob("*.jsonl")):
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip().startswith("{"):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
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
def resolve_target(a):
    """(project, repo). Never inherited: `dispatch` defaults `cwd` to `os.getcwd()`
    and `project` to that directory's basename, which would carry into this repo."""
    project = (a.project or os.environ.get("DOIT_PROJECT") or "").strip()
    if not project:
        raise Refusal("carry: --project is unset and DOIT_PROJECT is empty — name the project the "
                      "spec is written against")
    repo = pathlib.Path(a.repo) if a.repo else (root() / "repos" / project)
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
    p.add_argument("--force", action="store_true", help="carry a source that was already carried")
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    say = lambda m: print(m, file=sys.stderr)
    try:
        project, repo = resolve_target(a)
        source = a.source.strip()
        sid = source if is_url(source) else (numeric_prefix(source) or source)
        prior = None if a.force else already_carried(sid)
        if prior:
            raise Refusal(f"carry: {source} was already carried as {prior.get('subject')}; "
                          f"re-run with --force to carry again")
        if is_url(source):
            slot, spec_id, source_id, _ = pr_slot(source, a.title, a.body, repo)
            audited_at = now()
        else:
            # `handed_over_at` rides through VERBATIM: never re-parsed as a datetime
            # and re-serialized, which would rewrite the v4 record's own stamp.
            audited_at = (resolve_record(source)[1].get("handed_over_at") or now())
            slot, spec_id, source_id, _ = carried_slot(source)
    except Refusal as e:
        say(str(e))
        return 2
    path = str(root() / "content" / f"{spec_id}.md")
    cmd = [str(doit_bin()), "dispatch", "spec-writer", spec_id, "--packet", str(slot),
           "--path", path, "--cwd", str(repo), "--project", project]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        why = next((l for l in (r.stderr or "").splitlines() if l.startswith("FAILED ")),
                   (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["no reason given"])
        say(f"carry: {spec_id} was not written — {why if isinstance(why, str) else why[0]}")
        return 1
    ev = spec_written(spec_id)
    if ev is None:
        say(f"carry: spawn returned status {spawn_status(spec_id)} — no spec written")
        return 1
    footprint = ev.get("footprint") or []
    if not footprint:
        say(f"carry: {spec_id} was written with an empty footprint — no tier stamped")
        return 1
    tier, rule = review_tier(footprint)
    ap = subprocess.run([str(doit_bin()), "append", "spec-carried", spec_id,
                         f"source={source_id}", f"tier={tier}", f"audited_at={audited_at}"],
                        capture_output=True, text=True)
    if ap.returncode != 0:
        say(f"carry: {spec_id} was written but spec-carried did not append — "
            f"{(ap.stderr or '').strip()[-160:]}")
        return 1
    print(json.dumps({"spec": spec_id, "source": source_id, "tier": tier, "rule": rule,
                      "packet": str(slot), "path": path, "project": project}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
