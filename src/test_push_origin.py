#!/usr/bin/env python3
"""Checks on push_origin — the tick's push pass (L-charter-0042 R13(c),
L-spec-0484). Run: python3 test_push_origin.py

Real `git` throughout, against temp bare repos standing in for `origin` and
temp clones standing in for `$R/repos/<project>` — never the operator's real
checkout, never the network. `fold.ROOT`/`fold.EVENTS` are repointed at a
fresh tempdir per fixture and restored after; `look.load_toml()` itself is
NOT overridden (`push_origin.run` takes no `toml_path`) — every fixture below
therefore uses the project names the SHIPPED `look.toml` already lists
(`do-it-v2`/main, `albert-scott`/master), so the real `[push]` defaults are
exactly what is under test.
"""
import json, pathlib, re, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fold, push_origin  # noqa: E402

BASE_T = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


def ts(n=0):
    return (BASE_T + timedelta(minutes=n)).isoformat(timespec="seconds")


def sh(args, cwd):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"git {args} failed in {cwd}: {p.stderr}"
    return p.stdout.strip()


def commit(clone, msg="c"):
    (clone / "f.txt").write_text(msg)
    sh(["add", "-A"], clone)
    sh(["-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", msg], clone)
    return sh(["rev-parse", "HEAD"], clone)


def make_pair(root, project, main):
    """A bare `origin` plus a real clone at `$R/repos/<project>`, HEAD on
    `main`, one commit already pushed both ways (so `origin/<main>` and local
    `<main>` start identical, as production's own steady state is)."""
    origin = root / f"{project}.git"
    sh(["init", "--bare", str(origin)], root)
    sh(["symbolic-ref", "HEAD", f"refs/heads/{main}"], origin)
    clone = root / "repos" / project
    clone.mkdir(parents=True)
    sh(["init", "-b", main, str(clone)], root)
    sh(["remote", "add", "origin", str(origin)], clone)
    commit(clone, "init")
    sh(["push", "-u", "origin", main], clone)
    # A manual init+remote-add+push (unlike `git clone`) never sets
    # `refs/remotes/origin/HEAD` on its own — `push_origin.targets`'s own
    # origin/HEAD fallback (AC8e) needs it, so set it explicitly here, once,
    # for every fixture (harmless for the ones that never look at it).
    sh(["remote", "set-head", "origin", "-a"], clone)
    return origin, clone


def shipped(project, sha, subject, minute=1):
    return {"type": "shipped", "subject": subject, "project": project, "sha": sha,
            "branch": f"l-{subject.lower()}", "ts": ts(minute)}


class RootCtx:
    """Points `fold.ROOT`/`fold.EVENTS` at a fresh tempdir for the life of a
    `with` block, restoring both after — the one thing every fixture below
    needs, since `push_origin.run` reads both as module globals."""
    def __enter__(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        (self.root / "events").mkdir(parents=True, exist_ok=True)
        self.saved = (fold.ROOT, fold.EVENTS)
        fold.ROOT, fold.EVENTS = self.root, self.root / "events"
        return self.root

    def __exit__(self, *exc):
        fold.ROOT, fold.EVENTS = self.saved


ALL_ARGVS = []   # every argv any RecordingRunner below ever saw — AC6 reads this


class RecordingRunner(push_origin.Runner):
    """Wraps the real `push_origin.Runner` (real git) — records every argv
    (into both its own `.calls` and the shared `ALL_ARGVS`), and can force a
    named subcommand to fail (`fail=`) or raise `TimeoutExpired` (`raise_on=`)."""
    def __init__(self, fail=None, raise_on=None):
        self.calls = []
        self.fail = fail or set()
        self.raise_on = raise_on or set()

    def git(self, args, cwd, timeout):
        self.calls.append(list(args))
        ALL_ARGVS.append(list(args))
        sub = args[0]
        if sub in self.raise_on:
            raise subprocess.TimeoutExpired(cmd="git", timeout=timeout)
        if sub in self.fail:
            return 1, "", "induced failure"
        return super().git(args, cwd, timeout)


class SelectiveFailRunner(push_origin.Runner):
    """Fails `push` only in the one named project's own repo — AC9's "one
    failing does not stop the other" needs per-project control a shared
    `fail=` set cannot give."""
    def __init__(self, fail_project):
        self.calls = []
        self.fail_project = fail_project

    def git(self, args, cwd, timeout):
        self.calls.append(list(args))
        ALL_ARGVS.append(list(args))
        if args[0] == "push" and pathlib.Path(cwd).name == self.fail_project:
            return 1, "", "induced failure"
        return super().git(args, cwd, timeout)


class RacingRunner(push_origin.Runner):
    """Reproduces a real non-fast-forward race: right after THIS pass's own
    `fetch` (which captures origin as it stood), a second clone pushes a
    DIFFERENT commit to the real bare origin — so the push_origin's own
    ancestor checks (against the now-stale local `origin/<main>`) still pass,
    and the real `push` a moment later fails for real, exactly as a genuine
    race would."""
    def __init__(self, racer_clone):
        self.calls = []
        self.racer_clone = racer_clone

    def git(self, args, cwd, timeout):
        self.calls.append(list(args))
        ALL_ARGVS.append(list(args))
        rc, out, err = super().git(args, cwd, timeout)
        if args[0] == "fetch":
            commit(self.racer_clone, "race")
            subprocess.run(["git", "push", "origin", "HEAD"], cwd=str(self.racer_clone),
                           capture_output=True, text=True)
        return rc, out, err


# ── merge-origin-0484 AC1/AC2/AC3 ────────────────────────────────────────────
with RootCtx() as root1:
    origin1, clone1 = make_pair(root1, "do-it-v2", "main")
    sha1 = commit(clone1, "feature")   # local main now one commit ahead, unpushed
    events1 = [shipped("do-it-v2", sha1, "L-spec-90001")]
    runner1 = RecordingRunner()
    result1 = push_origin.run(events1, runner=runner1)
    assert sh(["rev-parse", "main"], origin1) == sha1, "AC1: origin's main must equal the shipped sha"
    lines1 = (root1 / "events" / push_origin.EVENT_NAME).read_text().splitlines()
    recorded1 = [json.loads(l) for l in lines1]
    assert len(recorded1) == 1 and recorded1[0]["type"] == "origin-pushed" and recorded1[0]["sha"] == sha1 \
        and recorded1[0]["subject"] == "do-it-v2" and recorded1[0]["branch"] == "main", recorded1
    assert result1 == ["do-it-v2"], result1
    print("merge-origin-0484 AC1 ok")

    # AC2 — the SAME event, read back fresh, actor tick, not ignored by fold.
    ev2 = fold.read_events()
    op2 = [e for e in ev2 if e.get("type") == "origin-pushed"]
    assert len(op2) == 1 and op2[0]["actor"] == "tick" and op2[0]["sha"] == sha1, op2
    _, _, ignored2, _ = fold.fold(ev2)
    assert not any(e.get("type") == "origin-pushed" for e in ignored2), \
        f"AC2: a tick-authored origin-pushed must not be ignored: {ignored2}"
    print("merge-origin-0484 AC2 ok")

    # AC3 — idempotence: the SAME run again, now seeing the origin-pushed
    # it just wrote, records zero push argv and returns [].
    runner3 = RecordingRunner()
    result3 = push_origin.run(ev2, runner=runner3)
    assert result3 == [], result3
    assert not any(c[0] == "push" for c in runner3.calls), runner3.calls
    print("merge-origin-0484 AC3 ok")


# ── merge-origin-0484 AC4 · hand-pushed: record, push nothing ───────────────
with RootCtx() as root4:
    origin4, clone4 = make_pair(root4, "do-it-v2", "main")
    sha4 = commit(clone4, "hand-pushed")
    sh(["push", "origin", "main"], clone4)   # a hand push — origin already has it
    events4 = [shipped("do-it-v2", sha4, "L-spec-90004")]
    runner4 = RecordingRunner()
    result4 = push_origin.run(events4, runner=runner4)
    assert result4 == ["do-it-v2"], result4
    assert not any(c[0] == "push" for c in runner4.calls), \
        f"AC4: a hand-pushed sha must never be re-pushed: {runner4.calls}"
    recorded4 = [json.loads(l) for l in (root4 / "events" / push_origin.EVENT_NAME).read_text().splitlines()]
    assert len(recorded4) == 1 and recorded4[0]["sha"] == sha4, recorded4
    print("merge-origin-0484 AC4 ok")


# ── merge-origin-0484 AC5 · diverged history never pushes ───────────────────
with RootCtx() as root5:
    origin5, clone5 = make_pair(root5, "do-it-v2", "main")
    sha_local5 = commit(clone5, "local-only")           # local ahead, unpushed
    racer5 = root5 / "racer5"
    sh(["clone", str(origin5), str(racer5)], root5)
    commit(racer5, "origin-only")
    sh(["push", "origin", "HEAD"], racer5)              # origin now ahead by a DIFFERENT commit
    events5 = [shipped("do-it-v2", sha_local5, "L-spec-90005")]
    runner5 = RecordingRunner()
    result5 = push_origin.run(events5, runner=runner5)
    assert result5 == [], result5
    assert not any(c[0] == "push" for c in runner5.calls), runner5.calls
    assert sh(["rev-parse", "main"], origin5) != sha_local5, "AC5: origin's main must be unchanged"
    print("merge-origin-0484 AC5 ok")


# ── merge-origin-0484 AC7 · non-main HEAD, detached HEAD, and "publishes the
# shipped sha, not local HEAD" ───────────────────────────────────────────────
with RootCtx() as root7a:
    origin7a, clone7a = make_pair(root7a, "do-it-v2", "main")
    origin_main_before7a = sh(["rev-parse", "main"], origin7a)
    sha_main7a = commit(clone7a, "main-extra")
    sh(["checkout", "-b", "feature"], clone7a)
    commit(clone7a, "feature-extra")   # HEAD now on `feature`, not `main`
    events7a = [shipped("do-it-v2", sha_main7a, "L-spec-90007a")]
    runner7a = RecordingRunner()
    result7a = push_origin.run(events7a, runner=runner7a)
    assert result7a == [], result7a
    assert not any(c[0] == "push" for c in runner7a.calls), runner7a.calls
    assert not (root7a / "events" / push_origin.EVENT_NAME).exists(), "AC7a: no event at all"
    assert sh(["rev-parse", "main"], origin7a) == origin_main_before7a, \
        "AC7a: origin's main must be completely untouched when HEAD is on another branch"

with RootCtx() as root7b:
    origin7b, clone7b = make_pair(root7b, "do-it-v2", "main")
    sha_main7b = commit(clone7b, "main-extra-b")
    sh(["checkout", "--detach", "HEAD"], clone7b)
    events7b = [shipped("do-it-v2", sha_main7b, "L-spec-90007b")]
    runner7b = RecordingRunner()
    result7b = push_origin.run(events7b, runner=runner7b)
    assert result7b == [], result7b
    assert not any(c[0] == "push" for c in runner7b.calls), runner7b.calls

with RootCtx() as root7c:
    origin7c, clone7c = make_pair(root7c, "do-it-v2", "main")
    sha_shipped7c = commit(clone7c, "shipped-commit")
    sha_extra7c = commit(clone7c, "unshipped-extra")   # local main now ahead of the shipped sha too
    events7c = [shipped("do-it-v2", sha_shipped7c, "L-spec-90007c")]
    runner7c = RecordingRunner()
    result7c = push_origin.run(events7c, runner=runner7c)
    assert result7c == ["do-it-v2"], result7c
    origin_main7c = sh(["rev-parse", "main"], origin7c)
    assert origin_main7c == sha_shipped7c and origin_main7c != sha_extra7c, \
        f"AC7c: origin gets exactly the shipped sha, never local HEAD: {origin_main7c}"
print("merge-origin-0484 AC7 ok")


# ── merge-origin-0484 AC8 · scope/failure of fetch, and target resolution ───
with RootCtx() as root8a:
    events8a = [shipped("not-a-configured-project", "d" * 40, "L-spec-90008a")]
    runner8a = RecordingRunner()
    result8a = push_origin.run(events8a, runner=runner8a)
    assert result8a == [] and runner8a.calls == [], \
        f"AC8a: a shipped on an unlisted project makes zero git calls: {runner8a.calls}"

with RootCtx() as root8b:
    events8b = [shipped("do-it-v2", "e" * 40, "L-spec-90008b")]
    runner8b = RecordingRunner()
    result8b = push_origin.run(events8b, runner=runner8b)
    assert result8b == [], result8b
    assert not (root8b / "events" / push_origin.EVENT_NAME).exists(), "AC8b: no repo -> no event, no raise"

with RootCtx() as root8c:
    origin8c, clone8c = make_pair(root8c, "do-it-v2", "main")
    sha8c = commit(clone8c, "c8c")
    events8c = [shipped("do-it-v2", sha8c, "L-spec-90008c")]
    runner8c = RecordingRunner(fail={"fetch"})
    result8c = push_origin.run(events8c, runner=runner8c)
    assert result8c == [], result8c
    assert not any(c[0] == "push" for c in runner8c.calls), runner8c.calls
    assert not (root8c / "events" / push_origin.EVENT_NAME).exists(), "AC8c: a failed fetch -> no event"

with RootCtx() as root8d:
    origin8d, clone8d = make_pair(root8d, "do-it-v2", "main")
    sha8d = commit(clone8d, "c8d")
    events8d = [shipped("do-it-v2", sha8d, "L-spec-90008d")]
    runner8d = RecordingRunner(raise_on={"fetch"})
    result8d = push_origin.run(events8d, runner=runner8d)
    assert result8d == [], result8d
    assert not (root8d / "events" / push_origin.EVENT_NAME).exists(), "AC8d: a raising fetch -> no event, no raise"

with RootCtx() as root8e:
    origin8e, clone8e = make_pair(root8e, "custom-proj", "trunk")
    targets8e = push_origin.targets({"push": {"projects": ["custom-proj"], "main": {}}})
    assert targets8e == [("custom-proj", "trunk")], \
        f"AC8e: a project with no main= resolves it from origin/HEAD: {targets8e}"

with RootCtx() as root8f:
    (root8f / "repos").mkdir(parents=True, exist_ok=True)
    targets8f = push_origin.targets({"push": {"projects": ["ghost-proj"], "main": {}}})
    assert targets8f == [], f"AC8e(also): an unresolvable project is dropped, never guessed: {targets8f}"
print("merge-origin-0484 AC8 ok")


# ── merge-origin-0484 AC9 · a failed push never records success ────────────
with RootCtx() as root9a:
    origin9a, clone9a = make_pair(root9a, "do-it-v2", "main")
    sha9a = commit(clone9a, "c9a")
    events9a = [shipped("do-it-v2", sha9a, "L-spec-90009a")]
    runner9a = RecordingRunner(fail={"push"})
    result9a = push_origin.run(events9a, runner=runner9a)
    assert result9a == [], result9a
    assert not (root9a / "events" / push_origin.EVENT_NAME).exists(), "AC9a: a failed push -> no event"
    assert sh(["rev-parse", "main"], origin9a) != sha9a, "AC9a: origin unchanged on a failed push"
    push_calls9a = [c for c in runner9a.calls if c[0] == "push"]
    assert len(push_calls9a) == 1, f"AC9: at most one push attempt per project per tick: {push_calls9a}"
    runner9a2 = RecordingRunner()
    result9a2 = push_origin.run(events9a, runner=runner9a2)
    assert result9a2 == ["do-it-v2"], f"AC9a: a following healthy run retries and appends one: {result9a2}"

with RootCtx() as root9b:
    origin9b, clone9b = make_pair(root9b, "do-it-v2", "main")
    sha9b = commit(clone9b, "c9b")
    events9b = [shipped("do-it-v2", sha9b, "L-spec-90009b")]
    runner9b = RecordingRunner(raise_on={"push"})
    result9b = push_origin.run(events9b, runner=runner9b)
    assert result9b == [], result9b
    assert not (root9b / "events" / push_origin.EVENT_NAME).exists(), "AC9b: a raising push -> no event, no raise"

with RootCtx() as root9c:
    origin9c, clone9c = make_pair(root9c, "do-it-v2", "main")
    sha9c = commit(clone9c, "c9c")
    racer9c = root9c / "racer9c"
    sh(["clone", str(origin9c), str(racer9c)], root9c)
    events9c = [shipped("do-it-v2", sha9c, "L-spec-90009c")]
    runner9c = RacingRunner(racer9c)
    result9c = push_origin.run(events9c, runner=runner9c)
    assert result9c == [], f"AC9c: a real non-ff race never records success: {result9c}"
    assert not (root9c / "events" / push_origin.EVENT_NAME).exists(), "AC9c: no event on a raced-out push"

with RootCtx() as root9d:
    origin9d1, clone9d1 = make_pair(root9d, "do-it-v2", "main")
    sha9d1 = commit(clone9d1, "c9d1")
    origin9d2, clone9d2 = make_pair(root9d, "albert-scott", "master")
    sha9d2 = commit(clone9d2, "c9d2")
    events9d = [shipped("do-it-v2", sha9d1, "L-spec-90009d1"), shipped("albert-scott", sha9d2, "L-spec-90009d2")]
    runner9d = SelectiveFailRunner("do-it-v2")
    result9d = push_origin.run(events9d, runner=runner9d)
    assert result9d == ["albert-scott"], f"AC9d: one failing project does not stop the other: {result9d}"
    push_calls9d = [c for c in runner9d.calls if c[0] == "push"]
    assert len(push_calls9d) == 2, f"AC9: one push attempt per listed project, this tick: {push_calls9d}"
print("merge-origin-0484 AC9 ok")


# ── merge-origin-0484 AC6 · never-force / shared-checkout safety, over every
# argv any fixture above (AC1-AC5, AC7-AC9) actually issued ─────────────────
_SRC_TEXT = (pathlib.Path(__file__).parent / "push_origin.py").read_text()
for _token in ("--force", "-f ", "--force-with-lease", "--mirror", "--delete", "--all", "--tags", '"+'):
    assert _token not in _SRC_TEXT, f"AC6: forbidden token present in push_origin.py: {_token!r}"
_subs = {c[0] for c in ALL_ARGVS}
assert _subs and _subs <= {"fetch", "symbolic-ref", "rev-parse", "merge-base", "push"}, \
    f"AC6: an unexpected git subcommand was issued: {_subs}"
_push_argvs = [c for c in ALL_ARGVS if c[0] == "push"]
assert _push_argvs, "AC6: at least one push argv was recorded across the ACs above"
for _c in _push_argvs:
    assert len(_c) == 3 and _c[1] == "origin", f"AC6: push argv shape: {_c}"
    _sha_part, _sep, _ref_part = _c[2].partition(":")
    assert _sep == ":" and re.fullmatch(r"[0-9a-f]{40}", _sha_part), \
        f"AC6: push source must be exactly a 40-hex sha: {_c}"
    assert _ref_part.startswith("refs/heads/"), f"AC6: push destination must be refs/heads/<main>: {_c}"
assert not any("HEAD" in _c for _c in _push_argvs), f"AC6: no push argv may name HEAD as a source: {_push_argvs}"
print(f"merge-origin-0484 AC6 ok ({sorted(_subs)})")

print("push_origin: all checks pass")
