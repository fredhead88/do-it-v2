#!/usr/bin/env python3
"""Fable test-review dossier (L-spec-2407, charter L-charter-0094 R6).

`build(project)` lines up every test the Fable review must look at, slowest
and never-failed first, with its evidence beside it, under
`$DOIT_ROOT/test-evidence/<project>/dossier/`. `check(proposal, project)` says
whether a written proposal covers every queued test.

Makes no model call and no network call; the only subprocess is
`git ls-files` (fixed argv, no shell) in the project checkout. Undetermined
is never covered: every unreadable input is a ValueError (build) or an
`undetermined:` entry (check).

    python3 src/fable_dossier.py build <project>
    python3 src/fable_dossier.py check <proposal> <project>
"""
import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import base_red

TRACKED_PREFIXES = ("tests/", "api/tests/", "as_toolkit/tests/")
TRACKED_BASENAME = re.compile(r"^test_.*\.(py|sh)$")
SPEC_NAMED = re.compile(r"^test_[0-9]{3,4}_")
DECISIONS = ("keep-at-merge", "nightly-only", "retire")
TAG_ORDER = ("slow", "spec-named", "duplicate")
RANK_LINE = re.compile(r"^([0-9]+)\. (.+?) \| tier=(T[1-4]) \| seconds=")
ENTRY_SPLIT = re.compile(r"\s::(?:\s|$)")

TEMPLATE = """# Fable test-review proposal template

This document changes nothing until the operator approves it. Fable proposes;
the operator decides. Nothing is deleted, moved or retired by writing it.

Write one entry line per queued test (every test in queue.md, exactly once):

    - <path> :: <decision> :: <reason> :: pin=<yes|no>

- `<path>` is the repo-relative test path exactly as it appears in queue.md.
- `<decision>` is one of:
  - `keep-at-merge`
  - `nightly-only`
  - `retire`
  - `merge-into:<path>` (a different, tracked test file)
- `<reason>` is a non-empty single line.
- `pin=yes` flags a wording-only or file-layout pin; otherwise `pin=no`.

`retire` means move the file to `archive/`, never delete it. A test is never
deleted without operator approval (charter constraint).

An entry that is missing, repeated, has a decision outside the four, an empty
reason, a pin value other than yes or no, or a `merge-into` target that is the
test itself or not a tracked test file, is reported by
`python3 src/fable_dossier.py check <proposal> <project>`.
"""


# ---- shared helpers -------------------------------------------------------

def _evidence_dir(project):
    base_red._repo(project)  # ValueError for an unknown project
    return base_red.fold.ROOT / "test-evidence" / project


def _safe(path, where):
    if (not isinstance(path, str) or not path or ".." in path
            or path.startswith("/") or "\\" in path):
        raise ValueError(f"{where}: unsafe test path {path!r}")
    return path


def _tracked_tests(project):
    repo = base_red._repo(project)
    try:
        proc = subprocess.run(["git", "ls-files"], cwd=str(repo), capture_output=True,
                              text=True, check=False)
    except OSError as e:
        raise ValueError(f"checkout: git ls-files failed: {e}")
    if proc.returncode != 0:
        raise ValueError(f"checkout: git ls-files exited {proc.returncode}")
    tracked = set()
    for line in proc.stdout.splitlines():
        if (line.startswith(TRACKED_PREFIXES) and TRACKED_BASENAME.match(line.rsplit("/", 1)[-1])
                and ".." not in line):
            tracked.add(line)
    if not tracked:
        raise ValueError("checkout: no tracked test files")
    return tracked


def _load(path, name):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise ValueError(f"{name}: absent or unparsable ({type(e).__name__}: {e})")


def _num(v, where):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v != v:
        raise ValueError(f"{where}: not a non-negative number: {v!r}")
    return v


def _fmt(v):
    return "none" if v is None else json.dumps(v)


# ---- build ----------------------------------------------------------------

def _inputs(project):
    evidence = _evidence_dir(project)
    tracked = _tracked_tests(project)
    history = _load(evidence / "history.json", "history.json")
    dups = _load(evidence / "duplicates.json", "duplicates.json")
    slowest = _load(evidence / "slowest.json", "slowest.json")
    durfile = base_red._repo(project) / "scripts" / "ci" / "shard_durations.json"
    durations = _load(durfile, "shard_durations.json")

    if (not isinstance(history, dict) or not isinstance(history.get("tests"), dict)
            or not isinstance(history.get("sources"), list)):
        raise ValueError("history.json: expected an object with 'sources' list and 'tests' object")
    for k, row in history["tests"].items():
        _safe(k, "history.json")
        if not isinstance(row, dict):
            raise ValueError(f"history.json: row for {k!r} is not an object")
        if "failures" in row:
            _num(row["failures"], f"history.json {k}.failures")
    if not isinstance(dups, list) or not all(isinstance(d, dict) for d in dups):
        raise ValueError("duplicates.json: expected a list of objects")
    for d in dups:
        _safe(d.get("test"), "duplicates.json")
    if not isinstance(slowest, list) or not all(isinstance(d, dict) for d in slowest):
        raise ValueError("slowest.json: expected a list of objects")
    for d in slowest:
        _safe(d.get("test"), "slowest.json")
    if not isinstance(durations, dict) or not isinstance(durations.get("files"), dict):
        raise ValueError("shard_durations.json: expected an object with a 'files' object")
    for k, v in durations["files"].items():
        _safe(k, "shard_durations.json")
        _num(v, f"shard_durations.json {k}")
    return tracked, history, dups, slowest, durations["files"]


def _plan(tracked, history, dups, slowest, files):
    total = sum(files.values())
    prefix, cum = [], 0
    for path, secs in sorted(files.items(), key=lambda kv: (-kv[1], kv[0])):
        if total <= 0 or cum * 5 >= total * 4:
            break
        prefix.append(path)
        cum += secs
    slow = {p for p in prefix if p in tracked}
    stale_durations = len(prefix) - len(slow)
    spec_named = {p for p in tracked if SPEC_NAMED.match(p.rsplit("/", 1)[-1])}
    dup_entries = {}
    stale_dups = 0
    for d in dups:
        if d["test"] in tracked:
            dup_entries.setdefault(d["test"], []).append(d)
        else:
            stale_dups += 1
    rank = {}
    for i, d in enumerate(slowest, 1):
        rank.setdefault(d["test"], i)

    def failures(p):
        row = history["tests"].get(p)
        return None if row is None or "failures" not in row else row["failures"]

    rows = []
    for p in slow | spec_named | set(dup_entries):
        tags = [t for t, s in zip(TAG_ORDER, (slow, spec_named, dup_entries)) if p in s]
        f = failures(p)
        if "slow" in tags and not f:
            tier = "T1"
        elif "spec-named" in tags:
            tier = "T2"
        elif "duplicate" in tags:
            tier = "T3"
        else:
            tier = "T4"
        rows.append({"path": p, "tier": tier, "tags": tags, "seconds": files.get(p),
                     "failures": f})
    rows.sort(key=lambda r: (r["tier"], r["seconds"] is None,
                             -(r["seconds"] or 0), r["path"]))
    return rows, dup_entries, rank, stale_durations, stale_dups


def _block(r, history, dup_entries, rank):
    row = history["tests"].get(r["path"])
    lines = [f"# {r['path']}", "",
             f"- path: {r['path']}",
             f"- tier: {r['tier']}",
             f"- tags: {','.join(r['tags'])}",
             f"- seconds: {_fmt(r['seconds'])}",
             "- history row: " + ("none (unknown history, not 'never failed')" if row is None
                                  else json.dumps(row)),
             ]
    entries = dup_entries.get(r["path"], [])
    if entries:
        lines.append("- duplicates:")
        for d in entries:
            lines.append(f"  - subset_of: {d.get('subset_of')} | covered: {d.get('covered')}")
    else:
        lines.append("- duplicates: none")
    lines.append("- slowest.json rank: " + (str(rank[r["path"]]) if r["path"] in rank
                                           else "not listed"))
    return "\n".join(lines) + "\n"


def _queue_md(project, now, rows, history, stale_durations, stale_dups):
    counts = {t: sum(1 for r in rows if r["tier"] == t) for t in ("T1", "T2", "T3", "T4")}
    out = ["# Fable review queue", "",
           f"project: {project}",
           f"generated_at: {now}",
           "tier-counts: " + " ".join(f"{t}={n}" for t, n in counts.items()),
           f"stale-durations={stale_durations}",
           f"stale-duplicates={stale_dups}",
           "history-sources:"]
    out += [f"- {json.dumps(s)}" for s in history["sources"]] or ["- none"]
    out += ["", "## Queue", ""]
    for i, r in enumerate(rows, 1):
        out.append(f"{i}. {r['path']} | tier={r['tier']} | seconds={_fmt(r['seconds'])} | "
                   f"failures={_fmt(r['failures'])} | tags={','.join(r['tags'])} | "
                   f"block=blocks/{r['path']}.md")
    return "\n".join(out) + "\n"


def build(project, *, now=None):
    tracked, history, dups, slowest, files = _inputs(project)
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    elif isinstance(now, datetime.datetime):
        now = now.isoformat()
    now = str(now)
    rows, dup_entries, rank, stale_d, stale_u = _plan(tracked, history, dups, slowest, files)

    evidence = _evidence_dir(project)
    final = evidence / "dossier"
    tmp = evidence / f"dossier.tmp-{os.getpid()}"
    old = evidence / f"dossier.old-{os.getpid()}"
    for stale in (tmp, old):
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)
    try:
        for r in rows:
            target = tmp / "blocks" / (r["path"] + ".md")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_block(r, history, dup_entries, rank), encoding="utf-8")
        (tmp / "queue.md").write_text(_queue_md(project, now, rows, history, stale_d, stale_u),
                                      encoding="utf-8")
        (tmp / "proposal-template.md").write_text(TEMPLATE, encoding="utf-8")
        had_old = final.exists()
        if had_old:
            os.rename(final, old)
        try:
            os.rename(tmp, final)
        except BaseException:
            if had_old:
                os.rename(old, final)
            raise
        shutil.rmtree(old, ignore_errors=True)
    finally:
        for sibling in (tmp, old):
            if sibling.exists():
                shutil.rmtree(sibling, ignore_errors=True)
    return final


# ---- check ----------------------------------------------------------------

def _undetermined(cause):
    return {"covered": False, "missing": [f"undetermined: {cause}"]}


def check(proposal_path, project):
    try:
        evidence = _evidence_dir(project)
        tracked = _tracked_tests(project)
    except ValueError as e:
        return _undetermined(str(e))
    try:
        queue_text = (evidence / "dossier" / "queue.md").read_text(encoding="utf-8")
    except (OSError, ValueError) as e:
        return _undetermined(f"queue.md unreadable ({type(e).__name__})")
    queue = [m.group(2) for m in map(RANK_LINE.match, queue_text.splitlines()) if m]
    if not queue:
        return _undetermined("queue.md holds no queued tests")
    try:
        text = pathlib.Path(proposal_path).read_text(encoding="utf-8")
    except (OSError, ValueError) as e:
        return _undetermined(f"proposal unreadable ({type(e).__name__})")
    if not text.strip():
        return _undetermined("proposal is empty")

    queued = set(queue)
    entries = {}
    for line in text.splitlines():
        if not line.startswith("- "):
            continue
        parts = [p.strip() for p in ENTRY_SPLIT.split(line[2:].rstrip())]
        if parts[0] in queued:
            entries.setdefault(parts[0], []).append(parts)

    def valid(parts):
        if len(parts) != 4:
            return False
        path, decision, reason, pin = parts
        if not reason or pin not in ("pin=yes", "pin=no"):
            return False
        if decision.startswith("merge-into:"):
            target = decision[len("merge-into:"):]
            return target != path and target in tracked
        return decision in DECISIONS

    missing = [p for p in queue if len(entries.get(p, [])) != 1 or not valid(entries[p][0])]
    return {"covered": not missing, "missing": missing}


# ---- CLI ------------------------------------------------------------------

def main(argv):
    usage = "usage: fable_dossier.py build <project> | check <proposal> <project>"
    try:
        if len(argv) == 3 and argv[1] == "build":
            print(build(argv[2]))
            return 0
        if len(argv) == 4 and argv[1] == "check":
            result = check(argv[2], argv[3])
            print(json.dumps(result))
            return 0 if result["covered"] else 1
    except ValueError as e:
        print(f"fable_dossier: {e}", file=sys.stderr)
        return 1
    print(usage, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
