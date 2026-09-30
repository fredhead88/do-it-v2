#!/usr/bin/env python3
"""push_origin — the tick's push pass (L-charter-0042 R13(c), L-spec-0484).

A merge is not complete until origin carries it: local-ahead-of-origin for
10+ minutes is an alarm (`look._check_origin_behind`, elsewhere). This module
is the OTHER half — the thing that keeps origin from falling behind in the
first place. Once per tick, for each `[push]`-configured project whose newest
`shipped` sha is newer than its newest `origin-pushed`, fast-forward-push
that sha onto `refs/heads/<main>` on `origin`.

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


def _newest(events, type_, key, value):
    """Newest (by `fold.ts`) event of `type_` with `event[key] == value` and
    a truthy `sha` — `None` when there is none."""
    rows = [e for e in events if e.get("type") == type_ and e.get(key) == value and e.get("sha")]
    return max(rows, key=lambda e: fold.ts(e.get("ts"))) if rows else None


def _push_one(project, main, events, runner):
    """One project's own five-step state machine (Produces). Returns whether
    `origin-pushed` landed."""
    shipped = _newest(events, "shipped", "project", project)
    if shipped is None:
        return False
    sha = str(shipped["sha"])
    if not _HEX40.match(sha):
        return False   # not a real sha (a test fixture's short sha, say) — never used
    pushed = _newest(events, "origin-pushed", "subject", project)
    if pushed is not None and fold.ts(pushed.get("ts")) >= fold.ts(shipped.get("ts")):
        return False   # nothing newer than what is already recorded (idempotence)
    repo = fold.ROOT / "repos" / project
    if not repo.is_dir():
        return False   # no repo at all: skip, no event, no raise
    rc, head = _call(runner, ["symbolic-ref", "--short", "HEAD"], repo, 10)
    if rc != 0 or str(head or "").strip() != main:
        return False   # detached, or checked out on anything but <main>: a human's state
    rc, _out = _call(runner, ["fetch", "origin"], repo, 30)
    if rc != 0:
        return False   # fetch failed: skip, retried next tick
    rc, _out = _call(runner, ["merge-base", "--is-ancestor", sha, f"origin/{main}"], repo, 10)
    if rc == 0:
        # Already on origin (a hand push, say) — record it so the trigger
        # clears; push nothing, since there is nothing left to push.
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
