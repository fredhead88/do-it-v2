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

print(f"tree-cleanup: {N} checks pass")
