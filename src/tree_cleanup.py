#!/usr/bin/env python3
"""tree-cleanup — reap only what is provably dead, retain the rest, say why (§4.11).

  tree_cleanup.py <charter> [--repo DIR] [--dry-run]

*(D33 + D76, narrowed by D88.)* Cleanup runs per charter at close, never per
merge: on an L2-complete verdict or on `charter-retracted`. Both are terminal
states in which §2.5 leaves no non-terminal spec, so the judgment D33 made this a
spawn for — "this branch is still needed by wave 3" — cannot arise, and a script
takes its place.

★ THE REAPING RULE. Provably dead = the spec's `ready_sha` is genuinely covered
by main **∧** the branch tip is covered too **∧** the worktree is clean. **Any
test that fails, or that cannot be run, defaults to `retained`** — §5.3's three
states applied to destruction, where could-not-run behaves like a violation.

★ ANCESTRY IS PATCH-ID TESTED, NEVER `git branch --merged`. Under squash-merge
that flag returns a confident wrong answer, which is §8.11's instrument running
and confidently wrong. `--is-ancestor` answers the merge-commit case; when it
says no, every commit the branch holds over main must match a patch-id already on
main, or the branch is retained.

★ IT REFUSES AN OPEN CHARTER. The state test is here rather than in the caller's
prose, because a script the Executor can be reasoned out of calling with the
wrong charter destroys live worktrees (§7.3: a rule that is not a check did not
ship).

Nothing is destroyed silently and nothing accumulates silently: every branch left
standing is on the event with its reason, and the board's oldest-unreaped-worktree
line (D28, D55) is the backstop against an over-shy reaper.
"""
import argparse, json, os, pathlib, subprocess, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold, merge_gate  # noqa: E402

DEPTH = int(os.environ.get("DOIT_REAP_DEPTH", "200"))    # how far back on main patch-ids are read
CLOSED = ("L2-complete", "retracted")


class Undetermined(Exception):
    """Anything the reaper could not establish. Never reads as dead."""


def git(repo, *args, stdin=None, ok=False):
    """Pinned exactly as the merge gate pins it (D114/D115): every `GIT_*` variable
    dropped, `LC_ALL=C`, no quotepath. A destructive decision read from the
    caller's config or locale is a decision the branch author can influence."""
    try:
        p = subprocess.run(("git", "-C", str(repo)) + merge_gate.PINNED + tuple(args),
                           capture_output=True, text=True, timeout=30,
                           env=merge_gate.ENV, input=stdin)
    except (OSError, subprocess.SubprocessError) as e:
        raise Undetermined(f"git {' '.join(args)}: {e}")
    if p.returncode and not ok:
        raise Undetermined(" ".join(args) + ": " + (p.stderr.strip() or "failed"))
    return p


def patch_ids(repo, rev, limit):
    ids = set()
    for sha in git(repo, "rev-list", f"-{limit}", rev).stdout.split():
        out = git(repo, "diff-tree", "-p", "--no-commit-id", sha).stdout
        pid = git(repo, "patch-id", "--stable", stdin=out).stdout.split()
        if pid:
            ids.add(pid[0])
    return ids


def covered(repo, sha, main, main_ids=None):
    """Is this commit's content already on main? An ancestor is; so is a commit
    whose patch-id matches one on main, which is the squash-merge case."""
    if git(repo, "merge-base", "--is-ancestor", sha, main, ok=True).returncode == 0:
        return True
    extra = git(repo, "rev-list", f"{main}..{sha}").stdout.split()
    if not extra:
        raise Undetermined(f"{sha[:12]} is not an ancestor of {main} and holds nothing over it")
    ids = main_ids if main_ids is not None else patch_ids(repo, main, DEPTH)
    for c in extra:
        out = git(repo, "diff-tree", "-p", "--no-commit-id", c).stdout
        pid = git(repo, "patch-id", "--stable", stdin=out).stdout.split()
        if not pid:
            # A merge commit produces no patch-id. Unanswerable, so retained.
            raise Undetermined(f"{c[:12]} has no patch-id (a merge commit?) — ancestry unanswerable")
        if pid[0] not in ids:
            return False
    return True


def worktrees(repo):
    """branch -> path, from git's own list. The filesystem is not asked."""
    out, wt, found = git(repo, "worktree", "list", "--porcelain").stdout, None, {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            wt = line.split(" ", 1)[1]
        elif line.startswith("branch ") and wt:
            found[line.split(" ", 1)[1].removeprefix("refs/heads/")] = wt
    return found


def build(evs):
    """The newest build-done for a spec — the branch and ready_sha the fold holds."""
    return next((e for e in reversed(evs) if e.get("type") == "build-done"), None)


def judge(repo, spec, evs, main, wts, main_ids):
    """One spec: (branch, worktree, dead?, why-if-not). Every raised Undetermined
    lands as a retention reason — that is the whole point of the three states."""
    bd = build(evs)
    branch = (bd or {}).get("branch") or spec.lower()
    wt = wts.get(branch)
    if git(repo, "rev-parse", "--verify", f"refs/heads/{branch}", ok=True).returncode != 0:
        return branch, wt, False, None if not wt else f"branch {branch} is gone but its worktree is not"
    if not bd or not bd.get("ready_sha"):
        return branch, wt, False, "no build-done carries a ready_sha — ancestry is unanswerable"
    try:
        for label, sha in (("ready_sha", bd["ready_sha"]), ("branch tip", branch)):
            if not covered(repo, sha, main, main_ids):
                return branch, wt, False, f"{label} holds work not on {main}"
        if wt and git(wt, "status", "--porcelain").stdout.strip():
            return branch, wt, False, "worktree is not clean"
    except Undetermined as e:
        return branch, wt, False, str(e)
    return branch, wt, True, None


def reap(repo, branch, wt, patch_id_proof):
    if wt:
        git(repo, "worktree", "remove", wt)
    p = git(repo, "branch", "-d", branch, ok=True)
    if p.returncode:
        # `-d` uses git's own ancestry, which is the flag D33 forbids relying on:
        # it refuses a squash-merged branch this script has already proved dead.
        # It is kept as a free second opinion and overridden only after the proof.
        if not patch_id_proof:
            raise Undetermined(f"git branch -d {branch}: {p.stderr.strip()}")
        git(repo, "branch", "-D", branch)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="doit reap", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("charter", help="the charter being closed — L2-complete or retracted")
    ap.add_argument("--repo", default=None, help="default: $DOIT_ROOT/repos/<the charter's project>")
    ap.add_argument("--dry-run", action="store_true", help="judge and print; destroy nothing, write nothing")
    a = ap.parse_args(argv)

    ev = fold.read_events()
    specs, charters, _, _ = fold.fold(ev)
    c = charters.get(a.charter)
    if not c:
        sys.exit(f"reap: no charter {a.charter} in the ledger")
    if c["state"] not in CLOSED:
        sys.exit(f"reap: {a.charter} is {c['state']}, not one of {'/'.join(CLOSED)} — "
                 f"cleanup runs per charter AT CLOSE (§4.11, D33). Nothing was touched.")

    project = next((e.get("project") for e in reversed(c["evs"]) if e.get("project")), None)
    repo = pathlib.Path(a.repo or fold.ROOT / "repos" / str(project))
    if not (repo / ".git").exists() and not repo.is_dir():
        sys.exit(f"reap: no repository at {repo} — ln -s <path> {repo} is the operator's line")
    try:
        main_branch = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
        wts, main_ids = worktrees(repo), patch_ids(repo, "HEAD", DEPTH)
    except Undetermined as e:
        sys.exit(f"reap: {e} — nothing was touched")

    reaped, retained, reasons = [], [], []
    for spec in sorted(s["id"] for s in specs.values() if s["charter"] == a.charter):
        branch, wt, dead, why = judge(repo, spec, specs[spec]["evs"], main_branch, wts, main_ids)
        if dead and not a.dry_run:
            proof = git(repo, "merge-base", "--is-ancestor", branch, main_branch, ok=True).returncode != 0
            try:
                reap(repo, branch, wt, proof)
            except Undetermined as e:
                dead, why = False, str(e)
        if dead:
            reaped.append(branch)
        elif why:
            retained.append(branch)
            reasons.append(f"{branch}: {why}")

    out = {"reaped": reaped, "retained": retained, "retained_reason": reasons}
    if not a.dry_run:
        os.environ["DOIT_LEDGER_FILE"] = os.environ.get(
            "DOIT_REAP_LEDGER_FILE", os.environ.get("DOIT_LEDGER_FILE", "L-executor-0001.jsonl"))
        fold.append(["tree-reaped", a.charter, f"reaped:={json.dumps(reaped)}",
                     f"retained:={json.dumps(retained)}",
                     f"retained_reason:={json.dumps(reasons)}", f"repo={repo}"])
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
