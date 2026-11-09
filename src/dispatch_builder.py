"""Builder checkout and base admission checks, using dispatch's runtime seams."""
import pathlib
import re
import subprocess
from datetime import datetime


def worktree_preflight(runtime, a, cwd, ledger, base, fail):
    """Recreate conventional missing checkouts, then prove root and branch."""
    # Standalone five-argument callers have no dispatch base context. Real
    # dispatches use this existing admission seam for all builder git checks.
    dispatched = getattr(a, '_dispatch_builder', False)
    if dispatched:
        runtime.builder_effective_base(a, fail)
    worktree = pathlib.Path(cwd).resolve()
    conventional = (runtime.ROOT / "worktrees" / (a.project or "") / a.subject.lower()).resolve()
    if not worktree.is_dir():
        if a.project and worktree == conventional:
            import autodispatch
            repo = runtime.ROOT / "repos" / a.project
            try:
                main = autodispatch._real_runner(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"])
                if main.get("code", 1) == 0:
                    branch = main.get("stdout", "").strip()
                    result = autodispatch.add_builder_worktree(repo, worktree, a.subject.lower(), f"origin/{branch}")
                    if result.get("code", 1) == 0:
                        runtime.emit(ledger, base, "worktree-recreated", worktree=str(worktree), branch=a.subject.lower())
            except (OSError, subprocess.SubprocessError):
                fail("builder worktree recreation failed", reason="preflight:worktree-missing")
        if not worktree.is_dir():
            fail("builder worktree missing or recreation failed", reason="preflight:worktree-missing")
    top = subprocess.run(["git", "-C", str(worktree), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    if top.returncode or pathlib.Path(top.stdout.strip()).resolve() != worktree:
        fail("builder cwd is not a git worktree root", reason="preflight:not-worktree")
    branch = subprocess.run(["git", "-C", str(worktree), "symbolic-ref", "--short", "HEAD"],
                            capture_output=True, text=True)
    if branch.returncode or branch.stdout.strip() != a.subject.lower():
        fail("builder worktree is not on its expected branch", reason="preflight:wrong-branch")
    if dispatched:
        runtime.builder_branch_preflight(a, cwd, __import__("fold").read_events(), fail)
        a.start_head = __import__("wip_resume").head_of(worktree)   # this attempt's start, for WIP scoping
    __import__("builder_deps").prepare(worktree, runtime.ROOT, runtime.ROOT / "repos" / (a.project or ""), subject=a.subject)


def effective_base(runtime, a, fail):
    """Compare packet authority with the resolver, never a packet's boolean."""
    import packet
    try:
        effective = packet._effective_base(runtime.ROOT / 'repos' / (a.project or ''),
                                           a.project, runtime.spec_path(a.subject), a.subject)
    except (ValueError, OSError, TypeError, subprocess.SubprocessError):
        __import__('branch_refresh').before_refusal(runtime, a, 'stale-base')
        fail('stale-base: effective project/base cannot be resolved; regenerate the packet',
             reason='stale-base')
    a.effective_base_sha = effective
    if (not re.fullmatch(r'[0-9a-f]{40}', a.pinned_base_sha)
            or a.pinned_base_sha != effective):
        __import__('branch_refresh').before_refusal(runtime, a, 'stale-base')
        fail('stale-base: packet pin differs from the current effective base; regenerate the packet',
             reason='stale-base')
    return effective


def pending_conflict_rework(events, subject):
    """Only the subject's still-pending authorized first recovery is exempt."""
    import conflict
    own = [e for e in events if e.get('subject') == subject]
    pending = None
    for e in own:
        if e.get('type') == 'build-done' and e.get('status') == 'DONE':
            pending = None
        elif e.get('type') in {'merge-conflict', 'conflict-rework'} and e.get('actor') == 'executor':
            pending = e
    if not pending or pending.get('branch', subject.lower()) != subject.lower():
        return False
    # Cross-file events can share a second; an ambiguous completed recovery
    # must never revive an exception through content-based ledger ordering.
    try:
        if any(e.get('type') == 'build-done' and e.get('status') == 'DONE'
               and datetime.fromisoformat(e['ts']) >= datetime.fromisoformat(pending['ts']) for e in own):
            return False
    except (KeyError, ValueError, TypeError):
        return False
    count = conflict.conflict_attempts(own, subject).attempt - 1
    if pending['type'] == 'conflict-rework':
        return (count == 1 and pending.get('attempt') == 1
                and pending.get('verdict') == 're-dispatch')
    return count == 0


def branch_preflight(runtime, a, cwd, events, fail):
    """Obtain ancestry with git's abortable rebase; never recreate a branch."""
    if __import__('branch_refresh').dispatch_preflight(runtime, a, cwd, events, fail):
        return
    if runtime.pending_conflict_rework(events, a.subject):
        return

    def git(*args):
        return subprocess.run(['git', '-C', str(cwd), *args], capture_output=True,
                              text=True, timeout=30)

    reason = 'branch-not-descendant'
    saved = __import__('wip_resume').applicable_timeout(events, a.subject)
    try:
        if saved and (saved.get('branch') != a.subject.lower()
                      or git('cat-file', '-e', str(saved.get('last_commit')) + '^{commit}').returncode):
            fail(reason + ': timeout-resume checkpoint is missing or not on this spec branch', reason=reason)
        ancestor = git('merge-base', '--is-ancestor', a.effective_base_sha, 'HEAD')
        if ancestor.returncode == 0:
            return
        if ancestor.returncode != 1:
            fail(reason + ': ancestry could not be proved; regenerate and redispatch after git is repaired', reason=reason)
        status = git('status', '--porcelain=v1', '--untracked-files=all')
        if status.returncode or status.stdout:
            fail(reason + ': builder/operator must preserve and clean dirty work before packet regeneration and redispatch', reason=reason)
        for name in ('rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD'):
            state = git('rev-parse', '--git-path', name)
            path = pathlib.Path(state.stdout.strip())
            if not path.is_absolute():
                path = pathlib.Path(cwd) / path
            if state.returncode or path.exists():
                fail(reason + ': builder/operator must finish the existing git operation', reason=reason)
        common = git('merge-base', 'HEAD', a.effective_base_sha)
        if common.returncode or not re.fullmatch(r'[0-9a-f]{40}', common.stdout.strip()):
            fail(reason + ': common ancestry cannot be resolved', reason=reason)
        # A clean index and no pre-existing operation are established before
        # rebase owns any state. Git retains ORIG_HEAD and abort restores it.
        try:
            rebased = git('rebase', '--no-autostash', '--no-update-refs', '--rebase-merges', a.effective_base_sha)
            if rebased.returncode:
                git('rebase', '--abort')
                fail(reason + ': ' + (__import__('wip_resume').TIMEOUT_CONFLICT if saved else
                     'rebase conflict/command failure; Executor uses the existing subject-authorized '
                     'conflict-rework packet to dispatch the builder on its own branch'), reason=reason)
        except (OSError, subprocess.SubprocessError):
            git('rebase', '--abort')
            raise
        proved = git('merge-base', '--is-ancestor', a.effective_base_sha, 'HEAD')
        if proved.returncode:
            fail(reason + ': refreshed ancestry could not be proved', reason=reason)
    except (OSError, subprocess.SubprocessError):
        fail(reason + ': ancestry/rebase command failed or timed out; repair git before packet regeneration and redispatch', reason=reason)


