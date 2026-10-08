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
# L-spec-0440/R9c: killed, void, or "shipped or later" — a spec in one of these
# states has genuinely finished its own lifecycle, independent of whether its
# charter has closed. `closed-unbuilt`/`dropped` are excluded on purpose:
# nothing was ever built for either, so no worktree exists to reap.
TERMINAL_SPEC_STATES = {"killed", "void", "accepted", "shipped-owed-due",
                         "shipped-owed-evidence", "shipped-owed-expired", "closed-shipped"}


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


def _shipped_event(evs):
    return next((e for e in reversed(evs) if e.get("type") == "shipped"), None)


def reap_worktrees(events, *, dry_run=False):
    """R15a: a spec's worktree goes the instant ITS OWN reason to exist ends —
    it shipped and origin/<main> already holds the merge sha, or it was
    killed and its branch is confirmed pushed to origin — never waiting on
    `reap_merged_specs`'s whole-charter-state judge()/patch-id path, and never
    consulting local main's own ancestry (that stays `judge()`'s job). Scope:
    every spec carrying its own `shipped` event (`shipped` itself, and every
    TERMINAL_SPEC_STATES state reached through one — accepted, the three
    shipped-owed-* states, closed-shipped, per Assumption 3), OR in state
    `killed` (which carries no `shipped` event) — `void` carries neither and
    is deliberately untouched here (Assumption 3: "nothing shipped").
    Returns (reaped, retained): `reaped` rows `{spec, project, path, proof}`,
    `retained` rows `{spec, project, path, why}` — never destroyed silently,
    never retained silently."""
    specs, charters, _, _ = fold.fold(events)
    groups = {}
    for sid, s in specs.items():
        if not (_shipped_event(s["evs"]) or s["state"] == "killed"):
            continue
        c = charters.get(s["charter"]) or {"evs": []}
        project = next((e.get("project") for e in reversed(c["evs"]) if e.get("project")), None)
        groups.setdefault(project, []).append(sid)

    reaped, retained = [], []
    for project in sorted(groups, key=str):
        repo = fold.ROOT / "repos" / str(project)
        try:
            main_branch = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
            wts = worktrees(repo)
        except Undetermined as e:
            for sid in sorted(groups[project]):
                retained.append({"spec": sid, "project": project, "path": None, "why": str(e)})
            continue
        for sid in sorted(groups[project]):
            s = specs[sid]
            branch = (build(s["evs"]) or {}).get("branch") or sid.lower()
            wt = wts.get(branch)
            if not wt:
                continue  # nothing to reap for this spec right now
            row = {"spec": sid, "project": project, "path": wt}
            # 0 — SD-R15-1a: standing rework reuses this worktree/branch; never removed.
            if fold.standing_rejects(s["evs"]):
                retained.append({**row, "why": "standing rejection"})
                continue
            # 1 — uncommitted changes, tracked or not: never removed, never pushed.
            try:
                dirty = git(wt, "status", "--porcelain").stdout.strip()
            except Undetermined as e:
                retained.append({**row, "why": str(e)})
                continue
            if dirty:
                retained.append({**row, "why": "uncommitted changes"})
                continue
            shipped = _shipped_event(s["evs"])
            if shipped is not None:
                sha = shipped.get("sha")
                if not sha:
                    retained.append({**row, "why": "shipped event carries no sha"})
                    continue
                try:
                    on_origin = git(repo, "merge-base", "--is-ancestor", sha,
                                    f"refs/remotes/origin/{main_branch}", ok=True).returncode == 0
                except Undetermined as e:
                    retained.append({**row, "why": str(e)})
                    continue
                if not on_origin:
                    retained.append({**row, "why": f"origin/{main_branch} does not contain {sha[:12]}"})
                    continue
                # Thinker 2026-10-08: a rework of an already-shipped spec (e.g. an owed check
                # failed -> rebuild) commits ON TOP of the shipped sha. The shipped sha being on
                # origin says nothing about that newer work; reaping here deleted L-spec-0399's
                # rework worktree every 5 min and killed four graders. Keep any worktree whose
                # branch tip is not yet on origin/<main>.
                try:
                    tip_on_origin = git(repo, "merge-base", "--is-ancestor", f"refs/heads/{branch}",
                                        f"refs/remotes/origin/{main_branch}", ok=True).returncode == 0
                except Undetermined as e:
                    retained.append({**row, "why": str(e)})
                    continue
                if not tip_on_origin:
                    retained.append({**row, "why": f"{branch} tip holds work not on origin/{main_branch} (rework in flight)"})
                    continue
                proof = "origin-merged"
            else:
                # killed: push the branch (never --force, never any other ref), then
                # remove only once origin itself confirms it — never in dry_run.
                try:
                    if not dry_run:
                        p = git(repo, "push", "origin", f"refs/heads/{branch}", ok=True)
                        if p.returncode != 0:
                            retained.append({**row, "why": p.stderr.strip() or "git push origin failed"})
                            continue
                    confirmed = git(repo, "ls-remote", "--exit-code", "origin",
                                    f"refs/heads/{branch}", ok=True).returncode == 0
                except Undetermined as e:
                    retained.append({**row, "why": str(e)})
                    continue
                if not confirmed:
                    retained.append({**row, "why": f"{branch} is not (yet) confirmed on origin"})
                    continue
                proof = "origin-branch"
            if dry_run:
                reaped.append({**row, "proof": proof})
                continue
            try:
                git(repo, "worktree", "remove", wt)
            except Undetermined as e:
                retained.append({**row, "why": str(e)})
                continue
            reaped.append({**row, "proof": proof})
    return reaped, retained


class GroupFailure(Exception):
    """Raised by `reap_merged_specs` only when >=1 repo group's own
    `worktrees()`/`patch_ids()` could not be established (missing repo,
    invalid `.git`, corrupt object store) — and only AFTER every group,
    healthy or not, has already been judged/reaped/ledgered: a per-group
    failure never stops the loop from reaching the next group (the one
    behavioral difference from `main()`, which `sys.exit()`s on its own
    single repo). `.reaped` carries every branch genuinely reaped across
    every OTHER, healthy group, so a caller (`reap_tmp.run`, R9b) that
    catches this still has the real partial result to report alongside the
    isolated `errors["worktrees"]` text."""
    def __init__(self, message, reaped):
        super().__init__(message)
        self.reaped = reaped


def _terminal_groups(events):
    """project -> [spec ids], for every spec whose `fold.spec_state` is one
    of R9c's terminal states. `project` is resolved the same way `main()`
    already resolves it for a whole charter (its own events' `project`
    field), just per spec via that spec's own charter."""
    specs, charters, _, _ = fold.fold(events)
    groups = {}
    for sid, s in specs.items():
        if s["state"] not in TERMINAL_SPEC_STATES:
            continue
        c = charters.get(s["charter"]) or {"evs": []}
        project = next((e.get("project") for e in reversed(c["evs"]) if e.get("project")), None)
        groups.setdefault(project, []).append(sid)
    return specs, groups


def reap_merged_specs(events, *, dry_run=False):
    """The second, additive entry point (R9c): every spec whose state is
    terminal is reaped as soon as ITS OWN repo group proves it dead — never
    gated on the whole charter closing the way `main()` is. One
    `tree-reaped` event per repo group (not per spec), same shape and same
    actor technique `main()` already uses, so §4.11's "nothing destroyed
    silently" holds here too."""
    wt_reaped, _wt_retained = reap_worktrees(events, dry_run=dry_run)
    wt_paths_by_project = {}
    for row in wt_reaped:
        wt_paths_by_project.setdefault(str(row["project"]), []).append(row["path"])

    specs, groups = _terminal_groups(events)
    all_reaped, failures = [], []
    for project in sorted(groups, key=str):
        spec_ids = sorted(groups[project])
        repo = fold.ROOT / "repos" / str(project)
        reaped, retained, reasons = [], [], []
        try:
            main_branch = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
            wts, main_ids = worktrees(repo), patch_ids(repo, "HEAD", DEPTH)
        except Undetermined as e:
            reason = str(e)
            for spec in spec_ids:
                branch = (build(specs[spec]["evs"]) or {}).get("branch") or spec.lower()
                retained.append(branch)
                reasons.append(f"{branch}: {reason}")
            failures.append(f"{project}: {reason}")
        else:
            for spec in spec_ids:
                branch, wt, dead, why = judge(repo, spec, specs[spec]["evs"], main_branch, wts, main_ids)
                if dead and not dry_run:
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
        all_reaped += reaped
        if not dry_run and (reaped or retained):
            os.environ["DOIT_LEDGER_FILE"] = os.environ.get(
                "DOIT_REAP_LEDGER_FILE", os.environ.get("DOIT_LEDGER_FILE", "L-executor-0001.jsonl"))
            # R15a: the paths `reap_worktrees` removed for THIS project — scoped to
            # project, not to this group's own spec set (the requirement's own
            # shape); empty when it removed nothing here.
            fold.append(["tree-reaped", str(project), f"reaped:={json.dumps(reaped)}",
                         f"retained:={json.dumps(retained)}",
                         f"retained_reason:={json.dumps(reasons)}", f"repo={repo}",
                         f"worktrees:={json.dumps(wt_paths_by_project.get(str(project), []))}"])
    if failures:
        raise GroupFailure("; ".join(failures), all_reaped)
    return all_reaped


def terminal_worktrees(events):
    """R9d condition (c): every terminal-state spec's own worktree path,
    keyed by that (normalized) path — read BEFORE any reap runs, so a
    worktree this same pass is about to remove is still listed (the caller,
    `reap_tmp.run`, calls this ahead of `reap_merged_specs`' own mutation).
    Covers BOTH of R9d's (c) clauses in one lookup: a worktree that gets
    reaped this pass is, by definition, a terminal spec's worktree too, so a
    single "under some terminal spec's worktree" test subsumes the
    just-reaped case. A repo this call cannot establish contributes nothing
    for its specs — fail-safe, never a false protection."""
    specs, groups = _terminal_groups(events)
    out = {}
    for project, spec_ids in groups.items():
        repo = fold.ROOT / "repos" / str(project)
        try:
            wts = worktrees(repo)
        except Undetermined:
            continue
        for sid in spec_ids:
            branch = (build(specs[sid]["evs"]) or {}).get("branch") or sid.lower()
            wt = wts.get(branch)
            if wt:
                out[os.path.normpath(wt)] = sid
    return out


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
