#!/usr/bin/env python3
"""§4.11's reaper, against the two failures it is written for: destroying work that
is not dead, and trusting `git branch --merged` under squash-merge.

Every check builds a real repository with real worktrees and a real merge. The
squash case is the one that matters — it is the case where git's own answer is
confidently wrong (§8.11), and a mocked git would agree with the bug.
"""
import json, os, pathlib, subprocess, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold, tree_cleanup  # noqa: E402

N = 0
HERE = pathlib.Path(__file__).resolve().parent


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


def sh(cwd, *cmd):
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))
    assert p.returncode == 0, f"{cmd}: {p.stderr}"
    return p.stdout.strip()


def repo(d):
    r = pathlib.Path(d) / "repo"
    r.mkdir(parents=True)
    sh(r, "git", "init", "-q", "-b", "main")
    (r / "seed.txt").write_text("seed\n")
    sh(r, "git", "add", "-A"); sh(r, "git", "commit", "-qm", "seed")
    return r


def branchwork(r, root, spec, content, merge="no-ff"):
    """One spec's branch and worktree, landed on main the way `merge` says."""
    b = spec.lower()
    wt = pathlib.Path(root) / "worktrees" / b
    sh(r, "git", "worktree", "add", "-q", str(wt), "-b", b, "main")
    (wt / f"{b}.txt").write_text(content)
    sh(wt, "git", "add", "-A"); sh(wt, "git", "commit", "-qm", f"build {spec}")
    ready = sh(wt, "git", "rev-parse", "HEAD")
    if merge == "no-ff":
        sh(r, "git", "merge", "-q", "--no-ff", b, "-m", f"merge({spec})")
    elif merge == "squash":
        sh(r, "git", "merge", "-q", "--squash", b)
        sh(r, "git", "commit", "-qm", f"squash({spec})")
    return b, str(wt), ready


def write(root, actor, *events):
    p = pathlib.Path(root) / "events" / f"{actor}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as fh:
        for i, e in enumerate(events):
            fh.write(json.dumps({"v": 1, "ts": f"2026-09-08T10:{i:02d}:00+00:00", **e}) + "\n")


def accepted(root, charter, spec, branch, ready, project=None):
    """The event trail that makes one spec derive to `accepted` (§2.5).
    `project` (L-spec-0440) is omitted by default — every pre-existing call
    site stays byte-identical — and, when given, lands on the FIRST event so
    `fold.fold()` finds it on the charter's own stream (`reap_merged_specs`/
    `terminal_worktrees` resolve a spec's repo through its charter's
    `project`, never the spec's own)."""
    kv = {"type": "spec-written", "subject": spec, "charter": charter}
    if project is not None:
        kv["project"] = project
    write(root, "L-executor-0001", kv,
          {"type": "build-done", "subject": spec, "branch": branch, "ready_sha": ready},
          {"type": "shipped", "subject": spec, "charter": charter})
    write(root, "L-grader-0001", {"type": "verdict", "subject": spec, "confirmed": True})
    write(root, "L-reviewer-0001", {"type": "review", "subject": spec, "depth": "gates-only"})


def close(root, charter):
    write(root, "L-executor-0001", {"type": "sweep-fixpoint", "subject": charter})
    write(root, "L-thinker-0001", {"type": "charter-filed", "subject": charter})
    write(root, "L-charter-reviewer-0001", {"type": "charter-review-complete", "subject": charter})


def reap(root, *argv):
    return subprocess.run([sys.executable, str(HERE / "tree_cleanup.py"), *argv],
                          capture_output=True, text=True,
                          env={k: v for k, v in os.environ.items() if k != "DOIT_PROJECT"}
                          | {"DOIT_ROOT": str(root)})


def events(root, type_=None):
    out = []
    for f in sorted((pathlib.Path(root) / "events").glob("*.jsonl")):
        out += [{**json.loads(l), "actor": f.stem} for l in f.read_text().splitlines() if l.strip()]
    return [e for e in out if type_ is None or e["type"] == type_]


def branches(r):
    return sh(r, "git", "for-each-ref", "--format=%(refname:short)", "refs/heads").split()


# ── an open charter is refused, and nothing is touched ────────────────────────
with tempfile.TemporaryDirectory() as d:
    r = repo(d)
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    accepted(d, "L-charter-0001", "L-spec-0001", b, ready)
    write(d, "L-thinker-0001", {"type": "charter-filed", "subject": "L-charter-0001"})
    p = reap(d, "L-charter-0001", "--repo", str(r))
    check(p.returncode != 0, "★ an open charter is refused — cleanup runs AT CLOSE (D33)")
    check("is open" in p.stderr and "Nothing was touched" in p.stderr, f"…and says so: {p.stderr}")
    check(events(d, "tree-reaped") == [], "…writing no event")
    check(b in branches(r) and pathlib.Path(wt).exists(), "…and destroying nothing")

# ── the provably dead: a --no-ff merge ────────────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    r = repo(d)
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    accepted(d, "L-charter-0001", "L-spec-0001", b, ready)
    close(d, "L-charter-0001")
    p = reap(d, "L-charter-0001", "--repo", str(r))
    check(p.returncode == 0, f"a closed charter reaps: {p.stderr}")
    check(json.loads(p.stdout)["reaped"] == [b], f"…the merged branch: {p.stdout}")
    check(b not in branches(r) and not pathlib.Path(wt).exists(), "…and the branch AND worktree are gone")
    e = events(d, "tree-reaped")
    check(len(e) == 1 and e[0]["reaped"] == [b] and e[0]["retained"] == [],
          f"one tree-reaped event carrying the same object (§4.11): {e}")

# ── ★ the squash-merge case: git's own flag is confidently wrong ──────────────
with tempfile.TemporaryDirectory() as d:
    r = repo(d)
    b, wt, ready = branchwork(r, d, "L-spec-0002", "squashed\n", merge="squash")
    check(b not in sh(r, "git", "branch", "--merged", "main").split(),
          "the premise: `git branch --merged` does not see a squash-merged branch")
    check(subprocess.run(["git", "-C", str(r), "merge-base", "--is-ancestor", ready, "main"],
                         capture_output=True).returncode != 0,
          "…and neither does --is-ancestor: the sha is genuinely not on main")
    accepted(d, "L-charter-0002", "L-spec-0002", b, ready)
    close(d, "L-charter-0002")
    p = reap(d, "L-charter-0002", "--repo", str(r))
    check(json.loads(p.stdout)["reaped"] == [b],
          f"★ patch-id sees the content is on main and reaps it: {p.stdout} {p.stderr}")
    check(b not in branches(r), "…and `git branch -d` refusing it did not stop the proven reap")

# ── everything else is retained, with a reason ────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    r = repo(d)
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    (pathlib.Path(wt) / "extra.txt").write_text("not merged\n")
    sh(wt, "git", "add", "-A"); sh(wt, "git", "commit", "-qm", "after the merge")
    accepted(d, "L-charter-0001", "L-spec-0001", b, ready)
    close(d, "L-charter-0001")
    p = reap(d, "L-charter-0001", "--repo", str(r))
    out = json.loads(p.stdout)
    check(out["reaped"] == [] and out["retained"] == [b],
          f"★ a commit on the branch that is not on main retains it: {out}")
    check("branch tip holds work not on main" in out["retained_reason"][0],
          f"…and the reason names which test failed: {out['retained_reason']}")
    check(b in branches(r) and pathlib.Path(wt).exists(), "…nothing destroyed")

with tempfile.TemporaryDirectory() as d:
    r = repo(d)
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    (pathlib.Path(wt) / "dirty.txt").write_text("uncommitted\n")
    accepted(d, "L-charter-0001", "L-spec-0001", b, ready)
    close(d, "L-charter-0001")
    out = json.loads(reap(d, "L-charter-0001", "--repo", str(r)).stdout)
    check(out["retained"] == [b] and "worktree is not clean" in out["retained_reason"][0],
          f"★ a merged branch with an untracked file in its worktree is RETAINED: {out}")

with tempfile.TemporaryDirectory() as d:
    r = repo(d)
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    write(d, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-0001",
                                 "charter": "L-charter-0001"},
          {"type": "shipped", "subject": "L-spec-0001", "charter": "L-charter-0001"})
    write(d, "L-grader-0001", {"type": "verdict", "subject": "L-spec-0001", "confirmed": True})
    write(d, "L-reviewer-0001", {"type": "review", "subject": "L-spec-0001"})
    close(d, "L-charter-0001")
    out = json.loads(reap(d, "L-charter-0001", "--repo", str(r)).stdout)
    check(out["retained"] == [b] and "unanswerable" in out["retained_reason"][0],
          f"★ no ready_sha is could-not-run, which behaves like a violation: {out}")

# ── a retracted charter is the other close state (D76) ────────────────────────
with tempfile.TemporaryDirectory() as d:
    r = repo(d)
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    write(d, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-0001",
                                 "charter": "L-charter-0001"},
          {"type": "build-done", "subject": "L-spec-0001", "branch": b, "ready_sha": ready})
    write(d, "L-operator-local", {"type": "charter-retracted", "subject": "L-charter-0001"})
    p = reap(d, "L-charter-0001", "--repo", str(r))
    check(p.returncode == 0 and json.loads(p.stdout)["reaped"] == [b],
          f"a retracted charter reaps too — its specs derive to dropped: {p.stdout} {p.stderr}")

# ── --dry-run judges and writes nothing ───────────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    r = repo(d)
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    accepted(d, "L-charter-0001", "L-spec-0001", b, ready)
    close(d, "L-charter-0001")
    p = reap(d, "L-charter-0001", "--repo", str(r), "--dry-run")
    check(json.loads(p.stdout)["reaped"] == [b], "--dry-run gives the same verdict")
    check(events(d, "tree-reaped") == [], "…writes no event")
    check(b in branches(r) and pathlib.Path(wt).exists(), "…and destroys nothing")

# ── the fold's rule, not the script's ─────────────────────────────────────────
check(fold.EMITS["tree-reaped"] == {"executor", "operator"},
      "★ tree-reaped is the record of destruction — only the closing seat may write it")
with tempfile.TemporaryDirectory() as d:
    r = repo(d)
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    accepted(d, "L-charter-0001", "L-spec-0001", b, ready)
    close(d, "L-charter-0001")
    p = subprocess.run([sys.executable, str(HERE / "tree_cleanup.py"), "L-charter-0001", "--repo", str(r)],
                       capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if k != "DOIT_PROJECT"}
                       | {"DOIT_ROOT": d, "DOIT_REAP_LEDGER_FILE": "L-builder-0001.jsonl"})
    check(p.returncode == 0, "the script does not police who runs it")
    board = subprocess.run([sys.executable, str(HERE / "fold.py")], capture_output=True, text=True,
                           env=dict(os.environ, DOIT_ROOT=d)).stdout
    check("unauthorized events recorded and ignored: 1" in board,
          f"★ …the FOLD does: a builder's tree-reaped is recorded and ignored:\n{board[-300:]}")

# ── the ancestry helper itself ────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    r = repo(d)
    head = sh(r, "git", "rev-parse", "HEAD")
    check(tree_cleanup.covered(r, head, "main") is True, "HEAD is covered by main")
    try:
        tree_cleanup.covered(r, "deadbeef" * 5, "main")
        check(False, "an unresolvable sha must not read as covered")
    except tree_cleanup.Undetermined:
        check(True, "★ an unresolvable sha raises Undetermined — it never reads as dead")

# ══════════════════════════════════════════════════════════════════════════
# L-spec-0440 R9c — `reap_merged_specs`: a second, per-spec entry point that
# reaps a killed/void/shipped-or-later spec's worktree WITHOUT waiting for
# its whole charter to close. Each check runs `reap_merged_specs` in a fresh
# subprocess (its own `DOIT_ROOT`), never the file's own `fold`/`tree_cleanup`
# import — this file's own module-level `fold.ROOT` is never touched.
# ══════════════════════════════════════════════════════════════════════════

def repo_for(d, project):
    r = pathlib.Path(d) / "repos" / project
    r.mkdir(parents=True)
    sh(r, "git", "init", "-q", "-b", "main")
    (r / "seed.txt").write_text("seed\n")
    sh(r, "git", "add", "-A"); sh(r, "git", "commit", "-qm", "seed")
    return r


def charter_project(d, charter, project):
    """`reap_merged_specs`/`terminal_worktrees` resolve a spec's repo through
    its CHARTER's own `project` field (`main()`'s own technique) — never the
    spec's, so this needs its own event on the charter's subject."""
    write(d, "L-thinker-0001", {"type": "charter-filed", "subject": charter, "project": project})


def run_merged(root, dry_run=False):
    code = ("import json,sys;"
            f"sys.path.insert(0, {str(HERE)!r});"
            "import fold, tree_cleanup;"
            "evs = fold.read_events();"
            f"out = tree_cleanup.reap_merged_specs(evs, dry_run={dry_run!r});"
            "print(json.dumps(out))")
    env = {k: v for k, v in os.environ.items() if k != "DOIT_PROJECT"} | {"DOIT_ROOT": str(root)}
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert p.returncode == 0, f"reap_merged_specs subprocess: {p.stderr}"
    return json.loads(p.stdout.strip().splitlines()[-1])


def read_via_fold(d, type_=None):
    """`fold.read_events()` itself, in a fresh subprocess — the actor it
    derives is the short role name ("executor"), never the raw filename this
    file's own `events()` helper uses, and AC12 needs the real one."""
    code = ("import json,sys;"
            f"sys.path.insert(0, {str(HERE)!r});"
            "import fold;"
            "print(json.dumps(fold.read_events()))")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=dict(os.environ, DOIT_ROOT=str(d)))
    assert p.returncode == 0, p.stderr
    evs = json.loads(p.stdout)
    return [e for e in evs if type_ is None or e.get("type") == type_]


# ── L0440-AC9 · a spec inside an OPEN charter is reaped early; siblings never judged ──
with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0440-proj9")
    charter_project(d, "L-charter-0440a", "l0440-proj9")
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    accepted(d, "L-charter-0440a", "L-spec-0001", b, ready, project="l0440-proj9")
    write(d, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-0002", "charter": "L-charter-0440a"})
    write(d, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-0003", "charter": "L-charter-0440a"},
          {"type": "build-started", "subject": "L-spec-0003", "charter": "L-charter-0440a"})
    out = run_merged(d)
    check(out == [b], f"L0440-AC9: A's branch reaped inside an open charter: {out}")
    check(b not in branches(r) and not pathlib.Path(wt).exists(), "L0440-AC9: worktree+branch gone")

# ── L0440-AC10 · a dirty-worktree sibling is retained; a building sibling is never judged ──
with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0440-proj10")
    charter_project(d, "L-charter-0440b", "l0440-proj10")
    bA, wtA, readyA = branchwork(r, d, "L-spec-0001", "x")
    accepted(d, "L-charter-0440b", "L-spec-0001", bA, readyA, project="l0440-proj10")
    bB, wtB, readyB = branchwork(r, d, "L-spec-0002", "y")
    (pathlib.Path(wtB) / "dirty.txt").write_text("uncommitted\n")
    accepted(d, "L-charter-0440b", "L-spec-0002", bB, readyB, project="l0440-proj10")
    write(d, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-0003", "charter": "L-charter-0440b"},
          {"type": "build-started", "subject": "L-spec-0003", "charter": "L-charter-0440b"})
    out = run_merged(d)
    check(out == [bA], f"L0440-AC10: only A reaped: {out}")
    check(bB in branches(r) and pathlib.Path(wtB).exists(), "L0440-AC10: B retained, dirty worktree kept")

# ── L0440-AC11 · dry-run reports the same verdict but destroys and ledgers nothing ──
with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0440-proj11")
    charter_project(d, "L-charter-0440c", "l0440-proj11")
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    accepted(d, "L-charter-0440c", "L-spec-0001", b, ready, project="l0440-proj11")
    out = run_merged(d, dry_run=True)
    check(out == [b], f"L0440-AC11: dry-run reports A as reaped: {out}")
    check(b in branches(r) and pathlib.Path(wt).exists(), "L0440-AC11: worktree/branch still exist")
    check(events(d, "tree-reaped") == [], "L0440-AC11: no ledger event on dry-run")

# ── L0440-AC12 · exactly one tree-reaped event, actor executor, reaped+retained shape ──
with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0440-proj12")
    charter_project(d, "L-charter-0440d", "l0440-proj12")
    bA, wtA, readyA = branchwork(r, d, "L-spec-0001", "x")
    accepted(d, "L-charter-0440d", "L-spec-0001", bA, readyA, project="l0440-proj12")
    bB, wtB, readyB = branchwork(r, d, "L-spec-0002", "y")
    (pathlib.Path(wtB) / "dirty.txt").write_text("uncommitted\n")
    accepted(d, "L-charter-0440d", "L-spec-0002", bB, readyB, project="l0440-proj12")
    out = run_merged(d)
    check(out == [bA], f"L0440-AC12: reaped list: {out}")
    e = read_via_fold(d, "tree-reaped")
    check(len(e) == 1, f"L0440-AC12: exactly one tree-reaped event: {e}")
    check(e[0]["actor"] == "executor", f"L0440-AC12: authoring actor is executor: {e[0]}")
    check(e[0]["reaped"] == [bA] and e[0]["retained"] == [bB], f"L0440-AC12: shape: {e[0]}")
    check(any("worktree is not clean" in rr for rr in e[0]["retained_reason"]),
          f"L0440-AC12: reason names B's dirty worktree: {e[0]['retained_reason']}")

# ══════════════════════════════════════════════════════════════════════════
# L-spec-0486 R15a — `reap_worktrees`: a spec's own worktree goes the instant
# ITS OWN reason to exist ends (shipped-and-on-origin, or killed-and-pushed),
# never waiting on the whole-charter `judge()`/patch-id path `reap_merged_specs`
# already had. Every check below drives a REAL bare `origin` remote — the
# whole point of R15a is a fact `--is-ancestor`/patch-id against local main
# cannot see.
# ══════════════════════════════════════════════════════════════════════════

def bare_origin(r):
    """A real bare `origin` for `r`, with `r`'s current branch pushed to it
    RIGHT NOW — call this before or after further local commits/merges to
    control whether origin does or does not carry them."""
    origin = pathlib.Path(r).parent / (pathlib.Path(r).name + "-origin.git")
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    if "origin" not in sh(r, "git", "remote").split():
        sh(r, "git", "remote", "add", "origin", str(origin))
    branch = sh(r, "git", "symbolic-ref", "--short", "HEAD")
    sh(r, "git", "push", "-q", "origin", branch)
    sh(r, "git", "fetch", "-q", "origin")
    return origin


def branch_only(r, root, spec, content):
    """A spec's branch and worktree, landed nowhere — the killed-spec shape:
    real work exists, but it never merged onto main."""
    b = spec.lower()
    wt = pathlib.Path(root) / "worktrees" / b
    sh(r, "git", "worktree", "add", "-q", str(wt), "-b", b, "main")
    (wt / f"{b}.txt").write_text(content)
    sh(wt, "git", "add", "-A"); sh(wt, "git", "commit", "-qm", f"build {spec}")
    return b, str(wt), sh(wt, "git", "rev-parse", "HEAD")


def shipped_trail(root, charter, spec, branch, ready, merge_sha, project=None):
    """The minimal event trail `fold.spec_state` derives bare `shipped` off
    (no verdict/review — those would derive `accepted`/`shipped-owed-*`
    instead, all reached THROUGH this same `shipped` event per Assumption 3)."""
    kv = {"type": "spec-written", "subject": spec, "charter": charter}
    if project is not None:
        kv["project"] = project
    write(root, "L-executor-0001", kv,
          {"type": "build-done", "subject": spec, "branch": branch, "ready_sha": ready},
          {"type": "shipped", "subject": spec, "charter": charter, "sha": merge_sha})


def run_reap_worktrees(root, dry_run=False):
    code = ("import json,sys;"
            f"sys.path.insert(0, {str(HERE)!r});"
            "import fold, tree_cleanup;"
            "evs = fold.read_events();"
            f"reaped, retained = tree_cleanup.reap_worktrees(evs, dry_run={dry_run!r});"
            "print(json.dumps({'reaped': reaped, 'retained': retained}))")
    env = {k: v for k, v in os.environ.items() if k != "DOIT_PROJECT"} | {"DOIT_ROOT": str(root)}
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert p.returncode == 0, f"reap_worktrees subprocess: {p.stderr}"
    return json.loads(p.stdout.strip().splitlines()[-1])


# ── L0486-AC1 · shipped, sha on origin -> reaped, proof origin-merged ──────────
with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0486-proj1")
    charter_project(d, "L-charter-0486a", "l0486-proj1")
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    merge_sha = sh(r, "git", "rev-parse", "HEAD")
    bare_origin(r)  # pushed AFTER the merge — origin has the merge sha
    shipped_trail(d, "L-charter-0486a", "L-spec-0001", b, ready, merge_sha, project="l0486-proj1")
    out = run_reap_worktrees(d)
    row = next((x for x in out["reaped"] if x["spec"] == "L-spec-0001"), None)
    check(row is not None and row["proof"] == "origin-merged",
          f"L0486-AC1: shipped-on-origin spec is reaped with proof origin-merged: {out}")
    check(not pathlib.Path(wt).exists(), "L0486-AC1: the worktree directory no longer exists")
    check(wt not in sh(r, "git", "worktree", "list"), "L0486-AC1: git worktree list no longer names it")
    check(b in branches(r), "L0486-AC1: the local branch still exists — reap_worktrees never deletes it")
print("L0486-AC1 ok")

# ── L0486-AC2 · not on origin retains; standing rework retains; reviewing/open absent ──
with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0486-proj2")
    charter_project(d, "L-charter-0486b", "l0486-proj2")
    bare_origin(r)  # pushed BEFORE the merge — origin never sees it
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    merge_sha = sh(r, "git", "rev-parse", "HEAD")
    shipped_trail(d, "L-charter-0486b", "L-spec-0001", b, ready, merge_sha, project="l0486-proj2")
    out = run_reap_worktrees(d)
    row = next((x for x in out["retained"] if x["spec"] == "L-spec-0001"), None)
    check(row is not None and "origin/main does not contain" in row["why"],
          f"L0486-AC2: a sha only on local main is retained, not reaped: {out}")
    check(pathlib.Path(wt).exists(), "L0486-AC2: the worktree directory still exists")

with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0486-proj2b")
    charter_project(d, "L-charter-0486b2", "l0486-proj2b")
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    merge_sha = sh(r, "git", "rev-parse", "HEAD")
    bare_origin(r)
    shipped_trail(d, "L-charter-0486b2", "L-spec-0001", b, ready, merge_sha, project="l0486-proj2b")
    write(d, "L-grader-0001", {"type": "rejected-criterion", "subject": "L-spec-0001", "criterion": "AC1"})
    out = run_reap_worktrees(d)
    row = next((x for x in out["retained"] if x["spec"] == "L-spec-0001"), None)
    check(row is not None and row["why"] == "standing rejection",
          f"L0486-AC2: a standing rejected-criterion retains before origin is even checked: {out}")
    check(pathlib.Path(wt).exists(), "L0486-AC2: the Executor's rework reuses this worktree")
    write(d, "L-grader-0002", {"type": "criterion-cleared", "subject": "L-spec-0001", "criterion": "AC1"})
    out2 = run_reap_worktrees(d)
    row2 = next((x for x in out2["reaped"] if x["spec"] == "L-spec-0001"), None)
    check(row2 is not None and row2["proof"] == "origin-merged",
          f"L0486-AC2: once cleared, the SAME worktree becomes reapable on a later pass: {out2}")

with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0486-proj2c")
    charter_project(d, "L-charter-0486b3", "l0486-proj2c")
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    merge_sha = sh(r, "git", "rev-parse", "HEAD")
    bare_origin(r)
    shipped_trail(d, "L-charter-0486b3", "L-spec-0001", b, ready, merge_sha, project="l0486-proj2c")
    write(d, "L-reviewer-0001", {"type": "must-fix", "subject": "L-spec-0001", "criterion": "AC2"})
    out = run_reap_worktrees(d)
    row = next((x for x in out["retained"] if x["spec"] == "L-spec-0001"), None)
    check(row is not None and row["why"] == "standing rejection",
          f"L0486-AC2: a standing must-fix retains too — D101's own rule: {out}")

with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0486-proj2d")
    charter_project(d, "L-charter-0486b4", "l0486-proj2d")
    b1, wt1, ready1 = branchwork(r, d, "L-spec-0001", "x")
    write(d, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-0001",
                                 "charter": "L-charter-0486b4", "project": "l0486-proj2d"},
          {"type": "build-done", "subject": "L-spec-0001", "branch": b1, "ready_sha": ready1})
    write(d, "L-grader-0001", {"type": "verdict", "subject": "L-spec-0001", "confirmed": False})
    wt2 = pathlib.Path(d) / "worktrees" / "l-spec-0002"
    sh(r, "git", "worktree", "add", "-q", str(wt2), "-b", "l-spec-0002", "main")
    write(d, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-0002",
                                 "charter": "L-charter-0486b4", "project": "l0486-proj2d"})
    out = run_reap_worktrees(d)
    seen = {x["spec"] for x in out["reaped"]} | {x["spec"] for x in out["retained"]}
    check("L-spec-0001" not in seen and "L-spec-0002" not in seen,
          f"L0486-AC2: a reviewing spec (no shipped) and an open spec are absent from both lists: {out}")
print("L0486-AC2 ok")

# ── L0486-AC3 · killed, branch pushed+confirmed -> reaped, proof origin-branch ──
with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0486-proj3")
    charter_project(d, "L-charter-0486c", "l0486-proj3")
    bare_origin(r)
    b, wt, ready = branch_only(r, d, "L-spec-0001", "x")
    write(d, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-0001",
                                 "charter": "L-charter-0486c", "project": "l0486-proj3"},
          {"type": "build-done", "subject": "L-spec-0001", "branch": b, "ready_sha": ready})
    write(d, "L-thinker-0001", {"type": "spec-killed", "subject": "L-spec-0001", "charter": "L-charter-0486c"})
    out = run_reap_worktrees(d)
    row = next((x for x in out["reaped"] if x["spec"] == "L-spec-0001"), None)
    check(row is not None and row["proof"] == "origin-branch",
          f"L0486-AC3: a killed spec's branch, pushed and confirmed, is reaped: {out}")
    check(not pathlib.Path(wt).exists(), "L0486-AC3: the worktree is gone")
    p = subprocess.run(["git", "-C", str(r), "ls-remote", "--exit-code", "origin", f"refs/heads/{b}"],
                       capture_output=True, text=True)
    check(p.returncode == 0 and b in p.stdout,
          f"L0486-AC3: a SEPARATE ls-remote, run after the function returned, lists the branch: {p.stdout}")

with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0486-proj3b")
    charter_project(d, "L-charter-0486c2", "l0486-proj3b")
    b, wt, ready = branch_only(r, d, "L-spec-0001", "x")
    write(d, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-0001",
                                 "charter": "L-charter-0486c2", "project": "l0486-proj3b"},
          {"type": "build-done", "subject": "L-spec-0001", "branch": b, "ready_sha": ready})
    write(d, "L-thinker-0001", {"type": "spec-killed", "subject": "L-spec-0001", "charter": "L-charter-0486c2"})
    # no `origin` remote at all
    out = run_reap_worktrees(d)
    row = next((x for x in out["retained"] if x["spec"] == "L-spec-0001"), None)
    check(row is not None and row["why"], f"L0486-AC3: no origin remote -> retained with a non-empty why: {out}")
    check(pathlib.Path(wt).exists(), "L0486-AC3: the worktree still exists")
print("L0486-AC3 ok")

# ── L0486-AC4 · uncommitted changes retain; never removed, never pushed ────────
with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0486-proj4")
    charter_project(d, "L-charter-0486d", "l0486-proj4")
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    merge_sha = sh(r, "git", "rev-parse", "HEAD")
    bare_origin(r)
    shipped_trail(d, "L-charter-0486d", "L-spec-0001", b, ready, merge_sha, project="l0486-proj4")
    (pathlib.Path(wt) / "tracked.txt").write_text("original\n")
    sh(wt, "git", "add", "-A"); sh(wt, "git", "commit", "-qm", "tracked file")
    (pathlib.Path(wt) / "tracked.txt").write_text("modified\n")
    out = run_reap_worktrees(d)
    row = next((x for x in out["retained"] if x["spec"] == "L-spec-0001"), None)
    check(row is not None and row["why"] == "uncommitted changes",
          f"L0486-AC4: a modified tracked file retains a shipped-on-origin worktree: {out}")
    check((pathlib.Path(wt) / "tracked.txt").read_text() == "modified\n", "L0486-AC4: the edit is intact")

with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0486-proj4b")
    charter_project(d, "L-charter-0486d2", "l0486-proj4b")
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    merge_sha = sh(r, "git", "rev-parse", "HEAD")
    bare_origin(r)
    shipped_trail(d, "L-charter-0486d2", "L-spec-0001", b, ready, merge_sha, project="l0486-proj4b")
    (pathlib.Path(wt) / "untracked.txt").write_text("stray\n")
    out = run_reap_worktrees(d)
    row = next((x for x in out["retained"] if x["spec"] == "L-spec-0001"), None)
    check(row is not None and row["why"] == "uncommitted changes",
          f"L0486-AC4: an untracked file also retains: {out}")

with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0486-proj4c")
    charter_project(d, "L-charter-0486d3", "l0486-proj4c")
    bare_origin(r)
    b, wt, ready = branch_only(r, d, "L-spec-0001", "x")
    write(d, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-0001",
                                 "charter": "L-charter-0486d3", "project": "l0486-proj4c"},
          {"type": "build-done", "subject": "L-spec-0001", "branch": b, "ready_sha": ready})
    write(d, "L-thinker-0001", {"type": "spec-killed", "subject": "L-spec-0001", "charter": "L-charter-0486d3"})
    (pathlib.Path(wt) / "dirty.txt").write_text("uncommitted\n")
    out = run_reap_worktrees(d)
    row = next((x for x in out["retained"] if x["spec"] == "L-spec-0001"), None)
    check(row is not None and row["why"] == "uncommitted changes",
          f"L0486-AC4: a killed spec with an uncommitted change also retains: {out}")
    p = subprocess.run(["git", "-C", str(r), "ls-remote", "origin", f"refs/heads/{b}"],
                       capture_output=True, text=True)
    check(p.stdout.strip() == "", f"L0486-AC4: nothing was pushed for the dirty killed worktree: {p.stdout!r}")
print("L0486-AC4 ok")

# ── L0486-AC5 · dry-run judges only; reap_merged_specs composes with reap_worktrees ──
with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0486-proj5")
    charter_project(d, "L-charter-0486e", "l0486-proj5")
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    merge_sha = sh(r, "git", "rev-parse", "HEAD")
    bare_origin(r)
    shipped_trail(d, "L-charter-0486e", "L-spec-0001", b, ready, merge_sha, project="l0486-proj5")
    out = run_reap_worktrees(d, dry_run=True)
    row = next((x for x in out["reaped"] if x["spec"] == "L-spec-0001"), None)
    check(row is not None and row["proof"] == "origin-merged", f"L0486-AC5: dry-run reports the would-reap row: {out}")
    check(pathlib.Path(wt).exists(), "L0486-AC5: dry-run destroys nothing")
    p = subprocess.run(["git", "-C", str(r), "ls-remote", "origin"], capture_output=True, text=True)
    check(b not in p.stdout, "L0486-AC5: dry-run pushes nothing (killed path untested here, but never pushes either)")

    # a PLAIN shipped spec is not in TERMINAL_SPEC_STATES — `reap_merged_specs`'s
    # OWN judge()/branch list is untouched by it, but it still removes the
    # worktree (via `reap_worktrees`), and writes NO tree-reaped for it (no
    # terminal group exists in this project) and no worktree-reaped of its own.
    out2 = run_merged(d)
    check(out2 == [], f"L0486-AC5: reap_merged_specs's own branch-judge list is untouched: {out2}")
    check(not pathlib.Path(wt).exists(), "L0486-AC5: reap_merged_specs removed the worktree via reap_worktrees")
    check(events(d, "tree-reaped") == [], "L0486-AC5: no tree-reaped for a shipped-only spec (no terminal group)")
    check(events(d, "worktree-reaped") == [], "L0486-AC5: tree_cleanup itself never writes worktree-reaped")

with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0486-proj5b")
    charter_project(d, "L-charter-0486f", "l0486-proj5b")
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    merge_sha = sh(r, "git", "rev-parse", "HEAD")
    bare_origin(r)
    write(d, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-0001",
                                 "charter": "L-charter-0486f", "project": "l0486-proj5b"},
          {"type": "build-done", "subject": "L-spec-0001", "branch": b, "ready_sha": ready},
          {"type": "shipped", "subject": "L-spec-0001", "charter": "L-charter-0486f", "sha": merge_sha})
    write(d, "L-grader-0001", {"type": "verdict", "subject": "L-spec-0001", "confirmed": True})
    write(d, "L-reviewer-0001", {"type": "review", "subject": "L-spec-0001", "depth": "gates-only"})
    out = run_merged(d)
    check(out == [b], f"L0486-AC5: this spec IS in a terminal state (accepted) — its own judge()/patch-id "
                       f"path (already merged locally) reaps the branch too: {out}")
    e = events(d, "tree-reaped")
    check(len(e) == 1, f"L0486-AC5: one tree-reaped for the terminal group: {e}")
    check(e[0].get("worktrees") == [wt], f"L0486-AC5: its worktrees field names the path reap_worktrees removed: {e[0]}")
    check(events(d, "worktree-reaped") == [], "L0486-AC5: reap_merged_specs itself still appends no worktree-reaped")
print("L0486-AC5 ok")

print(f"tree-cleanup: {N} checks pass")
