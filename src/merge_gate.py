#!/usr/bin/env python3
"""merge-gate — diff the branch against CURRENT main, never against base_sha.

  merge_gate.py <branch> [main] [--spec L-spec-0001] [--writes GLOB ...]

THE SCAR (§4.11, D108): a path main gained *after* the branch's `base_sha`, and
that the branch then deletes, is absent at both ends of `base_sha..branch` and
produces no numstat row at all — not a violation, *nothing*. A two-dot diff
against current main is the only diff that sees it. Measured: 7 of the last 200
merges on main removed a path and landed.

Three caller states, never conflated (§5.3): nothing-to-report -> proceed (0) ·
removals/reverts outside the grant -> rework (1) · could-not-determine ->
rework (1). AN UNRESOLVABLE REF NEVER READS AS CLEAN.

★ PRECISION. The two-dot diff FINDS candidates; the LIVE merge-base CONFIRMS
them. Without that second read the gate names every path main gained while the
branch was out — paths a three-way merge would keep, not delete — and a guard
that misapplies is a guard that gets worked around (operator evidence: the last
one "was often misapplying, so we would just get stuck"). This is not a retreat
to `base_sha`: `base_sha` is the RECORDED base, frozen when the branch was cut.
The merge-base is computed now and MOVES when the branch merges main — which is
precisely what the scar branch did, so the scar still lands. The two differ
exactly on the case D108 is about.

Runs at the Executor's merge step (§3.9, D17), BEFORE `--no-ff`.
"""
import fnmatch, json, os, pathlib, subprocess, sys

import fold

# A removal here is always named, grant or not — the excision case fools every
# consistency check by removing the evidence along with the artifact (D108).
MIGRATIONS = os.environ.get("DOIT_MIGRATIONS", "migrations/")


class Undetermined(Exception):
    """Anything the gate could not establish. Never reads as clean."""


def git(*args):
    p = subprocess.run(("git",) + args, capture_output=True, text=True)
    if p.returncode:
        raise Undetermined(" ".join(args) + ": " + (p.stderr.strip() or "failed"))
    return p.stdout


def blob(ref, path):
    """None when the path is absent at that ref — determinate, not a failure."""
    try:
        return git("rev-parse", f"{ref}:{path}").strip()
    except Undetermined:
        return None


def granted(path, grant):
    return any(fnmatch.fnmatch(path, g) or path.startswith(g.rstrip("*"))
               for g in grant if g)


def reverted(path, mb, main, branch, depth=50):
    """Survives the merge, but its content is a pre-main revision of the path."""
    here, now = blob(branch, path), blob(main, path)
    if here is None or here == now or blob(mb, path) != now:
        return False        # base != main means the branch never saw main's version
    # ponytail: last `depth` revisions of this path on main. Deeper needs a
    # measured case; nothing in the corpus reverts further back than that.
    return any(blob(c, path) == here
               for c in git("rev-list", f"-{depth}", main, "--", path).split())


def gate(branch, main, grant):
    git("rev-parse", "--verify", f"{main}^{{commit}}")
    git("rev-parse", "--verify", f"{branch}^{{commit}}")
    mb = git("merge-base", main, branch).strip()
    removed, reverts = [], []
    for line in git("diff", "--name-status", main, branch).splitlines():
        f = line.split("\t")
        if len(f) < 2:
            continue
        st, path = f[0], f[1]          # for R, f[1] is the path main loses
        if st[0] in "DR" and (path.startswith(MIGRATIONS) or not granted(path, grant)):
            if blob(mb, path) is not None:      # the branch HAD it, so the merge drops it
                removed.append(f"{path}@{(blob(main, path) or '?')[:7]}")
        elif st[0] == "M" and not granted(path, grant) and reverted(path, mb, main, branch):
            reverts.append(path)
    return removed, reverts


def writes_grant(spec):
    """`writes:` line in the spec's content file, comma- or space-separated."""
    p = fold.ROOT / "content" / f"{spec}.md"
    for line in (p.read_text().splitlines() if p.exists() else []):
        if line.strip().lower().startswith("writes:"):
            return [g for g in line.split(":", 1)[1].replace(",", " ").split() if g]
    return []


def main_(argv):
    branch, rest = argv[0], argv[1:]
    main = rest[0] if rest and not rest[0].startswith("-") else os.environ.get("DOIT_MAIN", "main")
    spec, grant, flag = None, [], None
    for a in rest:
        if a.startswith("--"):
            flag = a[2:]
        elif flag == "spec":
            spec, flag = a, None
        elif flag == "writes":
            grant.append(a)
    grant += writes_grant(spec) if spec else []
    os.environ.setdefault("DOIT_LEDGER_FILE", "L-executor-0001.jsonl")
    subject = spec or branch
    try:
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
