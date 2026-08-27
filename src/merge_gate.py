#!/usr/bin/env python3
"""merge-gate — perform the merge in memory and diff the result. Never model it.

  merge_gate.py <branch> [main] [--spec ID] [--writes GLOB ...]

THE SCAR (§4.11, D108): a path main gained *after* the branch's `base_sha`, and
that the branch then deletes, is absent at both ends of `base_sha..branch` and
produces no numstat row at all — not a violation, *nothing*. Measured: 7 of the
last 200 merges on main removed a path and landed.

★ HOW (D113, superseding D110). `git merge-tree --write-tree` performs the real
three-way merge and hands back the tree it would produce; the gate diffs current
main against THAT. Two earlier mechanisms were tried and both were wrong:

  - `base_sha..branch` (v1) — blind to the scar entirely, which is D108.
  - two-dot diff + live merge-base confirmation (D110) — flags nothing spurious
    in simple history, but a long-lived branch has SEVERAL merge-bases and git
    picks one arbitrarily. Verified: 2 bases, git chose the one lacking the
    file, the gate said clean, and the real merge deleted it.

Both were reasoning ABOUT a merge. This asks git to do it. The precision that
D110's longest paragraph defends falls out for free, because the real merge —
not a model of it — decides what the branch actually removes.

Three caller states, never conflated (§5.3): nothing-to-report -> proceed (0) ·
removals/reverts outside the grant -> rework (1) · could-not-determine ->
rework (1). AN UNRESOLVABLE REF, A CONFLICT, A TIMEOUT, AN EXHAUSTED HISTORY
WINDOW AND A MISSING `writes:` GRANT ALL READ AS REWORK, NEVER AS CLEAN.

Runs at the Executor's merge step (§3.9, D17), BEFORE `--no-ff`.
"""
import json, os, re, subprocess, sys, time

import fold

# A removal under a `migrations/` path SEGMENT is always named, grant or not — the
# excision case fools every consistency check by removing the evidence along with
# the artifact (D108). Segment, not prefix: real migrations live at
# `supabase/migrations/`, and a prefix test silently exempts them.
MIGRATIONS = re.compile(os.environ.get("DOIT_MIGRATIONS", r"(^|/)migrations/"), re.I)
GIT_TIMEOUT = float(os.environ.get("DOIT_GIT_TIMEOUT", "20"))     # seconds, per call
DEADLINE = float(os.environ.get("DOIT_GATE_DEADLINE", "60"))      # seconds, whole run
REVERT_DEPTH = int(os.environ.get("DOIT_REVERT_DEPTH", "200"))    # revisions per path
_START = time.monotonic()


class Undetermined(Exception):
    """Anything the gate could not establish. Never reads as clean."""


def git(*args):
    if time.monotonic() - _START > DEADLINE:
        raise Undetermined(f"deadline of {DEADLINE:.0f}s exceeded — a partial scan is not a clean one")
    try:
        p = subprocess.run(("git",) + args, capture_output=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:      # git missing, killed, hung
        raise Undetermined(f"git {' '.join(args)}: {e}")
    if p.returncode:
        raise Undetermined(" ".join(args) + ": " +
                           (p.stderr.decode(errors="replace").strip() or "failed"))
    return p.stdout


def blob(ref, path):
    """The blob id, None if the path is genuinely ABSENT there. A git failure is
    NOT absence — it raises, because could-not-determine may never read as clean.
    (The old code returned None for both, and a quoted path then vanished.)"""
    try:
        p = subprocess.run(("git", "rev-parse", f"{ref}:{path}"),
                           capture_output=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise Undetermined(f"rev-parse {ref}:{path}: {e}")
    err = p.stderr.decode(errors="replace")
    if p.returncode == 0:
        return p.stdout.decode().strip()
    if "does not exist" in err or "exists on disk, but not in" in err:
        return None
    raise Undetermined(f"rev-parse {ref}:{path}: {err.strip() or 'failed'}")


def glob_rx(g):
    """`*` never crosses a path separator; `**` does. fnmatch's `*` crosses, which
    is how a grant of `*.py` came to cover `secrets/prod_keys.py`."""
    out, i = "", 0
    while i < len(g):
        if g.startswith("**", i):
            out, i = out + ".*", i + 2
        elif g[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif g[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(g[i]), i + 1
    return re.compile(out)


def granted(path, grant):
    """A grant covers a path, a directory's contents, or a glob — never a SIBLING.
    `src` must not grant `src_backup/keys.py`, so the boundary is a separator."""
    for g in (x for x in grant if x):
        if g.endswith("/"):
            if path.startswith(g):
                return True
        elif glob_rx(g).fullmatch(path):
            return True
        elif not any(c in g for c in "*?") and path.startswith(g.rstrip("/") + "/"):
            return True
    return False


def diff_names(a, b):
    """`git diff -z --name-status`, parsed. -z gives RAW paths — no C-quoting, so
    a Hebrew or space-bearing filename survives instead of failing every lookup."""
    toks = git("diff", "-z", "--name-status", a, b).decode("utf-8", "surrogateescape").split("\0")
    out, i = [], 0
    while i < len(toks) and toks[i]:
        st = toks[i]
        if st[0] in "RC":                       # status, source, destination
            out.append((st, toks[i + 1], toks[i + 2])); i += 3
        else:
            out.append((st, toks[i + 1], None)); i += 2
    return out


def reverted(path, tree, main):
    """The merged content is an EARLIER revision of this path on main."""
    here, now = blob(tree, path), blob(main, path)
    if here is None or here == now:
        return False
    revs = git("rev-list", f"-{REVERT_DEPTH + 1}", main, "--", path).split()
    if len(revs) > REVERT_DEPTH:
        raise Undetermined(f"{path}: more than {REVERT_DEPTH} revisions — window exhausted, "
                           f"which is could-not-determine, not clean")
    return any(blob(c.decode(), path) == here for c in revs)


def gate(branch, main, grant):
    """What the merge would actually do to main, filtered to paths outside the grant."""
    git("rev-parse", "--verify", f"{main}^{{commit}}")
    git("rev-parse", "--verify", f"{branch}^{{commit}}")
    # ★ The real three-way merge, in memory. Not a model of one.
    tree = git("merge-tree", "--write-tree", main, branch).decode().splitlines()[0].strip()
    removed, reverts = [], []
    for st, path, dest in diff_names(main, tree):
        # D removes it · R moves it elsewhere · T replaces the file with something
        # that is not it (a symlink over a migration is the excision case exactly).
        if st[0] in "DRT" and (MIGRATIONS.search(path) or not granted(path, grant)):
            if st[0] == "R" and dest and granted(dest, grant):
                continue                                  # moved INTO the branch's own area
            where = f" -> {dest}" if dest else ""
            removed.append(f"{path}@{(blob(main, path) or '?')[:7]}{where}")
        elif st[0] == "M" and not granted(path, grant) and reverted(path, tree, main):
            reverts.append(path)
    return removed, reverts


def writes_grant(spec):
    """The spec's `writes:` grant. A named spec with no grant is could-not-determine
    — silently returning [] made every legitimate removal look like a violation,
    which is the misapplication that gets a guard switched off."""
    p = fold.ROOT / "content" / f"{spec}.md"
    if not p.exists():
        raise Undetermined(f"no content file for {spec} — cannot read its writes: grant")
    for line in p.read_text().splitlines():
        m = re.match(r"\s*[|*_\-\s]*writes\b[*_:| ]+(.+)", line, re.I)
        if m:
            body = m.group(1).strip().strip("|").replace("`", "")
            return [g for g in re.split(r"[,\s|]+", body) if g]
    raise Undetermined(f"{spec} states no writes: grant — nothing to filter against")


def main_(argv):
    branch, rest = argv[0], list(argv[1:])
    main = rest[0] if rest and not rest[0].startswith("-") else os.environ.get("DOIT_MAIN", "main")
    spec, grant, flag = None, [], None
    for a in rest:
        if a.startswith("--"):
            flag = a[2:]
            if flag not in ("spec", "writes"):
                sys.exit(f"merge-gate: unknown flag --{flag}")     # a typo is not a verdict
        elif flag == "spec":
            spec, flag = a, None
        elif flag == "writes":
            grant.append(a)
    os.environ.setdefault("DOIT_LEDGER_FILE", "L-executor-0001.jsonl")
    subject = spec or branch
    try:
        if spec:
            grant += writes_grant(spec)
        removed, reverts = gate(branch, main, grant)
        status = "rework" if (removed or reverts) else "clean"
        fold.append([f"merge-gate-{status}", subject, f"branch={branch}"]
                    + ([f"removed={json.dumps(removed)}", f"reverted={json.dumps(reverts)}"]
                       if status == "rework" else []))
        print(f"merge-gate: {status}"
              + (f"\n  removed:  {', '.join(removed)}" if removed else "")
              + (f"\n  reverted: {', '.join(reverts)}" if reverts else ""))
        return 0 if status == "clean" else 1
    except Undetermined as e:
        fold.append(["merge-gate-rework", subject, f"branch={branch}", f"undetermined={e}"])
        print(f"merge-gate: rework — could not determine: {e}")
        return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(main_(sys.argv[1:]))
