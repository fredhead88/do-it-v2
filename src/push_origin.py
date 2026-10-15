#!/usr/bin/env python3
"""push_origin — the tick's push pass (L-charter-0042 R13(c), L-spec-0484).

A merge is not complete until origin carries it: local-ahead-of-origin for
10+ minutes is an alarm (`look._check_origin_behind`, elsewhere). This module
is the OTHER half — the thing that keeps origin from falling behind in the
first place. Once per tick, for each `[push]`-configured project, find the
newest `shipped` sha by local main's ancestry and fast-forward-push it onto
`refs/heads/<main>` on `origin` if origin does not already contain it.
Event timestamps never determine which commit to publish or skip.

Ref-only, always: `$R/repos/<project>` can be the operator's own shared,
live, DIRTY working tree (`$R/repos/albert-scott` is a symlink straight to
`/opt/albert-scott`), so this module never reads or writes the working tree
or the index — only `fetch`, `symbolic-ref`, `merge-base --is-ancestor` and a
single explicit-refspec `push`. Never forced, never mirrored, never a bulk
or wildcard destination, never HEAD or a branch name as the push source — a
diverged main, or a checkout sitting on anything other than `<main>`, is a
human's call, never this pass's (charter constraint: "merging stays a judged
step behind the gate").

`push_origin.run(events, *, runner=None) -> list[str]` — one pass, at most
one push attempt per project, returns the projects for which `origin-pushed`
landed. Called once per tick from `tick._record()`, in its own try/except;
never raises out of its caller. A failed fetch or push is retried by the
next tick, silently — nothing here is urgent enough to wake anyone by
itself (that is `look`'s `origin-behind` alarm's job).
"""
import pathlib, re, subprocess, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dispatch, fold, look  # noqa: E402

EVENT_NAME = "L-tick-originpush.jsonl"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


class Runner:
    """Real `git` by default. `git(args, cwd, timeout) -> (rc, stdout,
    stderr)` — unlike `look.Runner.git` (stdout only, never raises), a
    `subprocess.TimeoutExpired`/`OSError` here propagates to `_call` below,
    which is the one place that turns it into a plain failed call."""
    def git(self, args, cwd, timeout):
        p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr


def _call(runner, args, cwd, timeout):
    """(rc, stdout) on any real reply; (None, None) on a timeout/OSError —
    never raises out of here. Every call site treats `rc is None` exactly
    like `rc != 0` (a failed call, no event, retried next tick)."""
    try:
        rc, out, _err = runner.git(args, cwd, timeout)
        return rc, out
    except (subprocess.TimeoutExpired, OSError):
        return None, None


def targets(cfg):
    """`cfg["push"]["projects"]` paired with `cfg["push"]["main"][name]`. A
    project with no `main` entry resolves it from
    `git symbolic-ref --short refs/remotes/origin/HEAD` (minus the `origin/`
    prefix) run against `fold.ROOT/"repos"/<name>`; if that cannot be
    resolved either, the project is dropped rather than guessed at."""
    push = cfg.get("push") or {}
    names = push.get("projects") or []
    mains = push.get("main") or {}
    runner = Runner()
    out = []
    for name in names:
        main = mains.get(name)
        if not main:
            repo = fold.ROOT / "repos" / name
            rc, text = _call(runner, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo, 10)
            if rc != 0 or not text:
                continue
            ref = text.strip()
            if not ref.startswith("origin/"):
                continue
            main = ref[len("origin/"):]
        out.append((name, main))
    return out


def _newest_shipped(events, project, main, repo, runner):
    """Newest shipped sha in a totally ordered chain on local main.

    Dropped-window commits are excluded. Every remaining pair must be
    comparable, including older candidates later joined by a merge.
    """
    shas = sorted({str(e["sha"]) for e in events
                   if e.get("type") == "shipped" and e.get("project") == project
                   and _HEX40.fullmatch(str(e.get("sha", "")))})
    candidates = []
    newest = None
    for sha in shas:
        rc, _ = _call(runner, ["merge-base", "--is-ancestor", sha, f"refs/heads/{main}"], repo, 10)
        if rc != 0:
            continue
        is_newest = True
        for previous in candidates:
            rc, _ = _call(runner, ["merge-base", "--is-ancestor", previous, sha], repo, 10)
            if rc == 0:
                continue
            if rc != 1:
                return None
            rc, _ = _call(runner, ["merge-base", "--is-ancestor", sha, previous], repo, 10)
            if rc != 0:
                return None   # incomparable candidates or a failed ancestry check
            is_newest = False
        if is_newest:
            newest = sha
        candidates.append(sha)
    return newest


def _push_one(project, main, events, runner):
    """One project's own five-step state machine (Produces). Returns whether
    `origin-pushed` landed."""
    if not any(e.get("type") == "shipped" and e.get("project") == project for e in events):
        return False
    repo = fold.ROOT / "repos" / project
    if not repo.is_dir():
        return False   # no repo at all: skip, no event, no raise
    rc, head = _call(runner, ["symbolic-ref", "--short", "HEAD"], repo, 10)
    if rc != 0 or str(head or "").strip() != main:
        return False   # detached, or checked out on anything but <main>: a human's state
    sha = _newest_shipped(events, project, main, repo, runner)
    if sha is None:
        return False
    rc, _out = _call(runner, ["fetch", "origin"], repo, 30)
    if rc != 0:
        return False   # fetch failed: skip, retried next tick
    rc, _out = _call(runner, ["merge-base", "--is-ancestor", sha, f"origin/{main}"], repo, 10)
    if rc == 0:
        # Already on origin (a hand push, say) — record it so the trigger
        # clears; push nothing, since there is nothing left to push.
        if any(e.get("type") == "origin-pushed" and e.get("subject") == project
               and e.get("sha") == sha for e in events):
            return False
        return dispatch.emit(fold.EVENTS / EVENT_NAME, {}, "origin-pushed",
                             subject=project, sha=sha, branch=main) is None
    rc_local, _ = _call(runner, ["merge-base", "--is-ancestor", sha, f"refs/heads/{main}"], repo, 10)
    rc_origin, _ = _call(runner, ["merge-base", "--is-ancestor", f"origin/{main}", sha], repo, 10)
    if rc_local != 0 or rc_origin != 0:
        return False   # S not on local main, or local/origin have diverged: never push
    rc, _out = _call(runner, ["push", "origin", f"{sha}:refs/heads/{main}"], repo, 30)
    if rc != 0:
        return False   # failed push (a race, a non-ff): no event, retried next tick
    return dispatch.emit(fold.EVENTS / EVENT_NAME, {}, "origin-pushed",
                         subject=project, sha=sha, branch=main) is None


def run(events, *, runner=None):
    """One pass over every `[push]` target. Never raises; a bad target is a
    skip, not an abort of the others."""
    runner = runner or Runner()
    events = list(events or ())
    cfg = look.load_toml()
    pushed = []
    for project, main in targets(cfg):
        try:
            if _push_one(project, main, events, runner):
                pushed.append(project)
        except Exception:
            continue
    return pushed
