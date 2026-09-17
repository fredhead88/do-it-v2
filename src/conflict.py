#!/usr/bin/env python3
"""conflict — a merge conflict is a re-dispatch, not a blanket refusal.

Two pure functions, no ledger writes of their own:

  conflicted(branch, main, repo)   -> ConflictResult(conflicts, paths)
  conflict_attempts(events, spec)  -> Attempts(attempt, verdict)

★ WHY THIS EXISTS. `merge_gate.gate()` folds a real content conflict into the
same undifferentiated `Undetermined` it raises for a shallow clone, an ambiguous
ref and a malformed writes: grant, so `main_()` recorded all of them as one
`merge-gate-rework` — and the Executor's only rule for that is escalation. Two
specs building the same files is the normal case when overlap builds
concurrently, so the normal case cost a human decision every time. The
collision itself is not the interesting fact; WHICH PATHS collided and WHETHER
THIS SPEC HAS COLLIDED BEFORE are, and neither survived the fold.

★ THE MERGE IS PERFORMED, NEVER MODELLED (D113). `merge-tree --write-tree`
does the real three-way merge in memory and names the colliding paths itself.
Nothing here diffs a range, guesses at a merge-base, or greps a message for a
filename.

★ AND IT REUSES THE HARDENING RATHER THAN REIMPLEMENTING IT (D114, D115).
Every git call goes through `merge_gate.run()` behind `begin_run()`, so the
GIT_* strip, the pinned config, the shallow / replace-ref / submodule / ambiguous
-ref hard stops and the per-run deadline all apply identically. A second
hand-rolled git wrapper is how one of those comes to be missing from the other.

★ `-z`, AND THE PARSE STOPS AT THE EMPTY FIELD. Measured against git 2.53:

    <tree-oid>\\0                                   <- clean merge ends here, rc 0
    <mode> <oid> <stage>\\t<path>\\0  ...            <- one per conflicted stage
    \\0                                             <- the empty field: END of it
    1\\0<path>\\0CONFLICT (contents)\\0<message>\\0   <- informational, NOT a verdict

Stopping at the empty field is load-bearing, not tidiness: the message section
carries an `Auto-merging <path>` record for files that merged CLEANLY, so a
parse that runs to the end reports paths that never collided. And the delimiter
is a bare NUL with no trailing newline — `str.strip()` does not remove `\\x00`
and `str.splitlines()` never splits on it, so a path containing a literal tab or
a newline survives here only because the split is on `\\0` and the path is
everything after the FIRST tab of its field.
"""
import collections

import merge_gate as mg

ConflictResult = collections.namedtuple("ConflictResult", "conflicts paths")
Attempts = collections.namedtuple("Attempts", "attempt verdict")


def conflicted(branch, main, repo):
    """Would merging `branch` into `main` conflict, and on which paths?

    `repo` is the directory the git toplevel is resolved from — what the gate
    derives from the process cwd today. THE PROCESS CWD IS NOT CHANGED: this is a
    read-only question asked from inside the Executor's own process, and moving
    that process's cwd is a side effect no caller of a read-only check can be
    asked to expect.

    `branch`/`main` are resolved fresh rather than handed in, so the answer is
    independently testable, and the deadline clock restarts here — the budget is
    per run, not per process. Any precondition `preflight()`/`resolve()` already
    enforces re-raises `Undetermined` unchanged: a repository whose history
    cannot be trusted cannot answer "would this conflict?" either.
    """
    mg.begin_run(repo)
    main_sha, branch_sha = mg.resolve(main), mg.resolve(branch)
    if main_sha == branch_sha:
        # Nothing to merge is not a conflict. `gate()` calls this could-not-
        # determine because an empty diff is not a clean MERGE; here the question
        # is narrower and has an answer.
        return ConflictResult(False, [])
    p = mg.run(("merge-tree", "--write-tree", "-z", main_sha, branch_sha))
    if p.returncode == 0:
        return ConflictResult(False, [])
    if p.returncode != 1:
        # 0 is clean and 1 is conflicts; anything else is git failing, and a
        # failed merge-tree is could-not-determine, never "no conflict".
        raise mg.Undetermined("merge-tree --write-tree -z: " +
                              (p.stderr.decode(errors="replace").strip()
                               or f"exited {p.returncode}"))
    fields = p.stdout.decode("utf-8", "surrogateescape").split("\0")
    paths, seen = [], set()
    for f in fields[1:]:            # [0] is the tree oid
        if not f:
            break                   # the empty field ends the conflicted-file block
        path = f.partition("\t")[2]         # FIRST tab only — the path may hold more
        if path and path not in seen:
            seen.add(path)
            paths.append(path)              # git's own order, deduplicated across stages
    return ConflictResult(True, paths)


def conflict_attempts(events, spec):
    """How many times this spec has collided, and what that means.

    The first conflict is a re-dispatch and the second is an escalation — R8's
    rule, which had no mechanical basis at all before this: nothing counted, so
    every conflict was `could not determine`, and every one of those was an
    escalation on attempt one.

    Pure. It reads the events list the caller holds — never the ledger itself,
    never a file — and writes nothing. `subject` is compared by exact string
    equality: no prefix, no glob, so a sibling spec colliding on the identical
    paths cannot inflate this spec's count.
    """
    prior = sum(1 for e in events
                if e.get("type") == "conflict-rework" and e.get("subject") == spec)
    attempt = prior + 1
    return Attempts(attempt, "re-dispatch" if attempt == 1 else "escalate")
