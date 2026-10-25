#!/usr/bin/env python3
"""§4.11's reaper, against the two failures it is written for: destroying work that
is not dead, and trusting `git branch --merged` under squash-merge.

Every check builds a real repository with real worktrees and a real merge. The
squash case is the one that matters — it is the case where git's own answer is
confidently wrong (§8.11), and a mocked git would agree with the bug.
"""
import contextlib, hashlib, io, json, os, pathlib, subprocess, sys, tempfile
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import patch

# A dispatched builder's ledger filename must never escape into fixture runs.
os.environ.pop("DOIT_LEDGER_FILE", None)
os.environ.pop("DOIT_REAP_LEDGER_FILE", None)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold, tree_cleanup, disk_reaper  # noqa: E402
_PROC_FIXTURE = tempfile.TemporaryDirectory(prefix="tree-proc-")
# All legacy calls, including child interpreters, use this empty fake /proc.
disk_reaper._WORKTREE_PASS.set((_PROC_FIXTURE.name, []))

N = 0
HERE = pathlib.Path(__file__).resolve().parent


def legacy_rows(rows):
    return [{k: v for k, v in row.items() if k != "freed_bytes"} for row in rows]


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
    site stays byte-identical — and, when given, lands on the spec's FIRST
    event for the shared project resolver."""
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
    code = (f"import sys; sys.path.insert(0, {str(HERE)!r}); import tree_cleanup, disk_reaper;"
            f"disk_reaper._WORKTREE_PASS.set(({_PROC_FIXTURE.name!r}, []));"
            "raise SystemExit(tree_cleanup.main(sys.argv[1:]))")
    return subprocess.run([sys.executable, "-c", code, *argv],
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


# L1365: a shipped project's own witness overrides misfiled build metadata.
with tempfile.TemporaryDirectory() as d, patch.multiple(fold, ROOT=pathlib.Path(d), EVENTS=pathlib.Path(d) / "events"):
    for project in ("A", "B"):
        (pathlib.Path(d) / "repos" / project).mkdir(parents=True)
    check(tree_cleanup.spec_project([
        {"type": "spec-written", "project": "B"},
        {"type": "build-done", "project": "A"},
        {"type": "shipped", "project": "A"}], [], None) == "A",
        "L1365 regression: shipped project overrides conflicting build metadata")
    check(tree_cleanup.spec_project([
        {"type": "spec-written", "project": "A"},
        {"type": "shipped", "project": "A"},
        {"type": "shipped", "project": "B"}], [], None) == "A",
        "L1365: disagreeing shipped projects leave agreeing build metadata as fallback")
    check(tree_cleanup.spec_project([
        {"type": "spec-written", "project": "A"},
        {"type": "build-done", "project": "B"},
        {"type": "shipped", "project": "A"}], [], None) == "A",
        "L1365: own shipped project resolves without a path despite conflicting metadata")


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
    check(len(e) == 1 and e[0]["reaped"] == [b] and e[0]["retained_count"] == 0
          and "retained" not in e[0] and "retained_reason" not in e[0],
          f"one tree-reaped event carrying the reap summary (§4.11): {e}")

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
    """Give the shared resolver a charter-level project fallback."""
    write(d, "L-thinker-0001", {"type": "charter-filed", "subject": charter, "project": project})


def run_merged(root, dry_run=False):
    code = ("import json,sys;"
            f"sys.path.insert(0, {str(HERE)!r});"
            "import fold, tree_cleanup, disk_reaper;"
            f"disk_reaper._WORKTREE_PASS.set(({_PROC_FIXTURE.name!r}, []));"
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
    check(not (pathlib.Path(d) / "state" / "reap-retained.json").exists(),
          "L0941: dry-run creates no retained status file")

# ── L0440-AC12 / L1529-AC1,AC3 · real append and fresh-process read ──
with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0440-proj12")
    charter_project(d, "L-charter-0440d", "l0440-proj12")
    bA, wtA, readyA = branchwork(r, d, "L-spec-0001", "x")
    accepted(d, "L-charter-0440d", "L-spec-0001", bA, readyA, project="l0440-proj12")
    bB, wtB, readyB = branchwork(r, d, "L-spec-0002", "y")
    (pathlib.Path(wtB) / "dirty.txt").write_text("uncommitted\n")
    accepted(d, "L-charter-0440d", "L-spec-0002", bB, readyB, project="l0440-proj12")
    bC, wtC, readyC = branchwork(r, d, "L-spec-0003", "z")
    (pathlib.Path(wtC) / "dirty.txt").write_text("uncommitted\n")
    accepted(d, "L-charter-0440d", "L-spec-0003", bC, readyC, project="l0440-proj12")
    preflight_count = sum(b in branches(r) for b in (bA, bB, bC))
    check(preflight_count == 3, "L1529: three fixture branches before reap")
    out = run_merged(d)
    check(out == [bA], f"L0440-AC12: reaped list: {out}")
    e = read_via_fold(d, "tree-reaped")
    check(len(e) == 1, f"L0440-AC12: exactly one tree-reaped event: {e}")
    check(e[0]["actor"] == "executor", f"L0440-AC12: authoring actor is executor: {e[0]}")
    check(e[0]["reaped"] == [bA] and e[0]["retained_count"] == 2
          and type(e[0]["retained_count"]) is int and e[0]["repo"] == str(r)
          and e[0]["worktrees"] == [] and "retained" not in e[0]
          and "retained_reason" not in e[0], f"L1529-AC1: event shape: {e[0]}")
    state = json.loads((pathlib.Path(d) / "state/reap-retained.json").read_text())
    expected = {"project": "l0440-proj12", "repo": str(r), "retained": [bB, bC],
                "retained_reason": [f"{b}: worktree is not clean" for b in (bB, bC)]}
    check(state["projects"] == [expected]
          and len(expected["retained"]) == e[0]["retained_count"],
          f"L1529-AC3: full status agrees with event: {state}")
    check(run_merged(d) == [] and len(read_via_fold(d, "tree-reaped")) == 1,
          "L1529-AC3: retained-only rerun appends zero events")
    check(json.loads((pathlib.Path(d) / "state/reap-retained.json").read_text())["projects"] == [expected],
          "L1529-AC3: retained-only rerun still writes full status")
    for ac, body in (("AC1", {"event": e[0], "preflight_branch_count": preflight_count}),
                     ("AC3", {"projects": state["projects"], "retained_only_new_events": 0})):
        body = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url=f"cli://tree-cleanup/L1529-{ac}", status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

# Charter close goes through the public CLI and preserves the detailed stdout.
with tempfile.TemporaryDirectory() as d:
    r = repo(d)
    bA, wtA, readyA = branchwork(r, d, "L-spec-1529", "reap")
    bB, wtB, readyB = branchwork(r, d, "L-spec-1530", "retain")
    (pathlib.Path(wtB) / "dirty.txt").write_text("uncommitted\n")
    for sid, branch, ready in (("L-spec-1529", bA, readyA), ("L-spec-1530", bB, readyB)):
        accepted(d, "L-charter-1529", sid, branch, ready)
    close(d, "L-charter-1529")
    preflight_count = sum(b in branches(r) for b in (bA, bB))
    check(preflight_count == 2, "L1529-AC2: two fixture branches before reap")
    p = subprocess.run([str(HERE.parent / "doit"), "reap", "L-charter-1529", "--repo", str(r)],
                       capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if k != "DOIT_PROJECT"}
                       | {"DOIT_ROOT": d})
    check(p.returncode == 0, f"L1529-AC2: doit reap succeeds: {p.stderr}")
    out = json.loads(p.stdout)
    check(out == {"reaped": [bA], "retained": [bB],
                  "retained_reason": [f"{bB}: worktree is not clean"]},
          f"L1529-AC2: unchanged detailed stdout: {out}")
    e = read_via_fold(d, "tree-reaped")
    check(len(e) == 1 and e[0]["reaped"] == [bA] and e[0]["retained_count"] == 1
          and type(e[0]["retained_count"]) is int and e[0]["repo"] == str(r)
          and not {"retained", "retained_reason", "worktrees"}.intersection(e[0]),
          f"L1529-AC2: charter-close summary: {e}")
    body = json.dumps({"event": e[0], "stdout": out, "preflight_branch_count": preflight_count}, sort_keys=True)
    print(json.dumps(dict(url="cli://tree-cleanup/L1529-AC2", status=p.returncode,
                          body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

# Size boundary: real branches and append; synthetic long reasons isolate payload size.
with (tempfile.TemporaryDirectory() as d,
      patch.object(fold, "ROOT", pathlib.Path(d)),
      patch.object(fold, "EVENTS", pathlib.Path(d) / "events"),
      patch.dict(os.environ, DOIT_ROOT=d, DOIT_LEDGER_FILE="L-executor-0001.jsonl")):
    project = "l1529-size"
    r = repo_for(d, project)
    charter_project(d, "L-charter-1529", project)
    bA, wtA, readyA = branchwork(r, d, "L-spec-1529", "reap")
    accepted(d, "L-charter-1529", "L-spec-1529", bA, readyA, project=project)
    retained = [f"l-spec-{n}" for n in range(2000, 2200)]
    for b in retained:
        sh(r, "git", "branch", b)
        accepted(d, "L-charter-1529", b.replace("l-spec", "L-spec"), b, readyA, project=project)
    preflight_count = len([b for b in branches(r) if b != "main"])
    check(preflight_count == 201, "L1529-AC4: 201 fixture branches before reap")
    judge = tree_cleanup.judge
    def size_judge(repo, spec, *args):
        if spec.lower() in retained:
            return spec.lower(), None, False, "x" * 100
        return judge(repo, spec, *args)
    with patch.object(tree_cleanup, "judge", side_effect=size_judge):
        check(tree_cleanup.reap_merged_specs(fold.read_events()) == [bA], "L1529-AC4: one branch reaped")
    lines = (pathlib.Path(d) / "events/L-executor-0001.jsonl").read_bytes().splitlines(keepends=True)
    lines = [line for line in lines if json.loads(line)["type"] == "tree-reaped"]
    check(len(lines) == 1, "L1529-AC4: exactly one tree-reaped line")
    e = json.loads(lines[0])
    check(len(lines[0]) < 2048 and e["retained_count"] == 200
          and type(e["retained_count"]) is int
          and not {"retained", "retained_reason"}.intersection(e),
          f"L1529-AC4: bounded event size: {len(lines[0])} bytes: {e}")
    state = json.loads((pathlib.Path(d) / "state/reap-retained.json").read_text())["projects"][0]
    check(state["retained"] == retained
          and state["retained_reason"] == [f"{b}: {'x' * 100}" for b in retained],
          "L1529-AC4: full 200-branch list and long reasons remain in status")
    body = json.dumps({"event": e, "line_bytes": len(lines[0]),
                       "retained_reason_chars": 100, "preflight_branch_count": preflight_count}, sort_keys=True)
    print(json.dumps(dict(url="cli://tree-cleanup/L1529-AC4", status=0,
                          body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

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
            "import fold, tree_cleanup, disk_reaper;"
            f"disk_reaper._WORKTREE_PASS.set(({_PROC_FIXTURE.name!r}, []));"
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

# ── Thinker 2026-10-08 · shipped sha on origin, but a rework commit on the branch -> retained ──
with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "rework-proj")
    charter_project(d, "L-charter-rework", "rework-proj")
    b, wt, ready = branchwork(r, d, "L-spec-0002", "x")
    merge_sha = sh(r, "git", "rev-parse", "HEAD")
    bare_origin(r)
    shipped_trail(d, "L-charter-rework", "L-spec-0002", b, ready, merge_sha, project="rework-proj")
    (pathlib.Path(wt) / "rework.txt").write_text("rework after a failed owed check")
    sh(wt, "git", "add", "-A"); sh(wt, "git", "commit", "-qm", "rework L-spec-0002")
    out = run_reap_worktrees(d)
    check(not any(x["spec"] == "L-spec-0002" for x in out["reaped"]),
          f"rework: a shipped spec whose branch has newer unmerged commits is NOT reaped: {out}")
    row = next((x for x in out["retained"] if x["spec"] == "L-spec-0002"), None)
    check(row is not None and "rework in flight" in row["why"], f"rework: retained with a rework reason: {out}")
    check(pathlib.Path(wt).exists(), "rework: the worktree directory still exists")
print("rework-retained ok")

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
    row = next((x for x in out["reaped"] if x["spec"] == "L-spec-0001"), None)
    check(row is not None and row["proof"] == "origin-merged",
          f"L0486-AC2: a standing rejection alone permits terminal cleanup: {out}")
    check(not pathlib.Path(wt).exists(), "L0486-AC2: terminal worktree removed")
    write(d, "L-grader-0002", {"type": "criterion-cleared", "subject": "L-spec-0001", "criterion": "AC1"})
    out2 = run_reap_worktrees(d)
    row2 = next((x for x in out2["reaped"] if x["spec"] == "L-spec-0001"), None)
    check(row2 is None and out2 == {"reaped": [], "retained": []},
          f"L0486-AC2: a later pass has nothing left to remove: {out2}")

with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "l0486-proj2c")
    charter_project(d, "L-charter-0486b3", "l0486-proj2c")
    b, wt, ready = branchwork(r, d, "L-spec-0001", "x")
    merge_sha = sh(r, "git", "rev-parse", "HEAD")
    bare_origin(r)
    shipped_trail(d, "L-charter-0486b3", "L-spec-0001", b, ready, merge_sha, project="l0486-proj2c")
    write(d, "L-reviewer-0001", {"type": "must-fix", "subject": "L-spec-0001", "criterion": "AC2"})
    out = run_reap_worktrees(d)
    row = next((x for x in out["reaped"] if x["spec"] == "L-spec-0001"), None)
    check(row is not None and row["proof"] == "origin-merged",
          f"L0486-AC2: a standing must-fix alone permits terminal cleanup: {out}")

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
    check(not out["reaped"] and seen == {"L-spec-0001", "L-spec-0002"}
          and all("not finished" in row["why"] for row in out["retained"]),
          f"L1541: non-terminal worktrees retained with reasons: {out}")
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
    check(row is not None and "rework in flight" in row["why"],
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
    row = next((x for x in out["reaped"] if x["spec"] == "L-spec-0001"), None)
    check(row is not None and row["proof"] == "origin-merged" and not pathlib.Path(wt).exists(),
          f"L1541: finished dirty worktree removed: {out}")

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
    row = next((x for x in out["reaped"] if x["spec"] == "L-spec-0001"), None)
    check(row is not None and row["proof"] == "origin-branch" and not pathlib.Path(wt).exists(),
          f"L1541: dirty killed worktree removed: {out}")
    p = subprocess.run(["git", "-C", str(r), "ls-remote", "origin", f"refs/heads/{b}"],
                       capture_output=True, text=True)
    check(p.stdout.strip(), "L1541: dirty killed branch confirmed on origin")
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
    # worktree (via `reap_worktrees`). L0941 records that actual destruction
    # even though no terminal branch group exists, with no worktree-reaped
    # of its own.
    out2 = run_merged(d)
    check(out2 == [], f"L0486-AC5: reap_merged_specs's own branch-judge list is untouched: {out2}")
    check(not pathlib.Path(wt).exists(), "L0486-AC5: reap_merged_specs removed the worktree via reap_worktrees")
    e = events(d, "tree-reaped")
    check(len(e) == 1 and e[0]["reaped"] == [] and e[0]["worktrees"] == [wt],
          f"L0941: worktree-only destruction writes one tree-reaped event: {e}")
    run_merged(d)
    check(len(events(d, "tree-reaped")) == 1,
          "L0941: a worktree-only follow-up that destroys nothing appends no event")
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

# ── L0941-AC1 · retention is current status, never a destruction event ────────
with tempfile.TemporaryDirectory() as d:
    project = "l0941-retained"
    r = repo_for(d, project)
    charter_project(d, "L-charter-0941", project)
    b, wt, ready = branchwork(r, d, "L-spec-0941", "x")
    dirty = pathlib.Path(wt) / "dirty.txt"
    dirty.write_text("uncommitted\n")
    accepted(d, "L-charter-0941", "L-spec-0941", b, ready, project=project)
    state = pathlib.Path(d) / "state" / "reap-retained.json"
    for _ in range(2):
        check(run_merged(d) == [], "L0941: retained-only run reaps nothing")
        check(events(d, "tree-reaped") == [], "L0941: repeated retention writes ZERO events")
        snapshot = json.loads(state.read_text())
        measured = datetime.fromisoformat(snapshot["measured_at"])
        check(measured.tzinfo == timezone.utc and (datetime.now(timezone.utc) - measured).total_seconds() < 10,
              "L0941: status includes a current UTC measured_at")
        check(snapshot["projects"] == [{
            "project": project, "repo": str(r), "retained": [b],
            "retained_reason": [f"{b}: worktree is not clean"],
        }], "L0941: retained branches and reasons are in the status file")
    sh(wt, "git", "add", "dirty.txt")
    sh(wt, "git", "commit", "-qm", "unmerged work")
    run_merged(d)
    check(json.loads(state.read_text())["projects"][0]["retained_reason"] == [
        f"{b}: branch tip holds work not on main"],
          "L0941: changed retention reason overwrites status")
    check(events(d, "tree-reaped") == [], "L0941: changed reason writes no destruction event")
    b2, wt2, ready2 = branchwork(r, d, "L-spec-0942", "y", merge=None)
    accepted(d, "L-charter-0941", "L-spec-0942", b2, ready2, project=project)
    run_merged(d)
    check(json.loads(state.read_text())["projects"][0]["retained"] == [b, b2],
          "L0941: a new retained branch updates status")
    check(events(d, "tree-reaped") == [], "L0941: changed retention still writes no event")
    sh(r, "git", "merge", "-q", "--no-ff", b, "-m", "land remaining work")
    check(run_merged(d) == [b], "L0941: the newly clean merged branch is reaped")
    e = events(d, "tree-reaped")
    check(len(e) == 1 and e[0]["reaped"] == [b] and e[0]["retained_count"] == 1
          and e[0]["subject"] == project and e[0]["repo"] == str(r)
          and "retained" not in e[0] and "retained_reason" not in e[0]
          and e[0]["worktrees"] == [] and e[0]["actor"] == "L-executor-0001",
          f"L0941: actual branch destruction appends exactly one summary event: {e}")
    check(json.loads(state.read_text())["projects"][0]["retained"] == [b2],
          "L0941: status overwrites the previous retained list")
    before = state.read_bytes()
    run_merged(d, dry_run=True)
    check(state.read_bytes() == before, "L0941: dry-run leaves existing status untouched")

# The charter-close entry point still records its close marker with no reap.
with tempfile.TemporaryDirectory() as d:
    r = repo(d)
    b, wt, ready = branchwork(r, d, "L-spec-0941", "x")
    (pathlib.Path(wt) / "dirty.txt").write_text("uncommitted\n")
    accepted(d, "L-charter-0941", "L-spec-0941", b, ready)
    close(d, "L-charter-0941")
    state = pathlib.Path(d) / "state" / "reap-retained.json"
    for i in range(2):
        p = reap(d, "L-charter-0941", "--repo", str(r))
        check(p.returncode == 0, f"L0941: retained charter-close succeeds: {p.stderr}")
        out = json.loads(p.stdout)
        e = events(d, "tree-reaped")
        check(len(e) == i + 1 and e[-1]["subject"] == "L-charter-0941"
              and e[-1]["reaped"] == [] and e[-1]["retained_count"] == 1,
              "L0941: charter-close still emits its no-reap charter marker")
        check(not state.exists(), "L0941: charter-close does not write periodic status")
    reap(d, "L-charter-0941", "--repo", str(r), "--dry-run")
    check(len(events(d, "tree-reaped")) == 2,
          "L0941: charter-close dry-run still writes no event")
print("L0941-AC1 ok: retained-only runs emit zero events; one branch emits one; charter close unchanged")

# Status is a snapshot of all repo groups, including existing but invalid repos.
with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "z-healthy")
    charter_project(d, "L-charter-healthy", "z-healthy")
    b, wt, ready = branchwork(r, d, "L-spec-0941", "x")
    accepted(d, "L-charter-healthy", "L-spec-0941", b, ready, project="z-healthy")
    charter_project(d, "L-charter-missing", "a-missing")
    (pathlib.Path(d) / "repos" / "a-missing").mkdir()
    accepted(d, "L-charter-missing", "L-spec-0942", "l-spec-0942", ready, project="a-missing")
    code = (f"import sys; sys.path.insert(0, {str(HERE)!r});"
            "import fold, tree_cleanup, disk_reaper;"
            f"disk_reaper._WORKTREE_PASS.set(({_PROC_FIXTURE.name!r}, []));"
            "tree_cleanup.reap_merged_specs(fold.read_events())")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if k != "DOIT_PROJECT"}
                       | {"DOIT_ROOT": d})
    check(p.returncode != 0 and "GroupFailure" in p.stderr,
          "L0941: an existing invalid repo still reports GroupFailure")
    state = json.loads((pathlib.Path(d) / "state" / "reap-retained.json").read_text())["projects"]
    check([row["project"] for row in state] == ["a-missing", "z-healthy"],
          "L0941: status covers failed and healthy groups")
    check(state[0]["retained"] == ["l-spec-0942"] and state[0]["retained_reason"],
          "L0941: failed repo's branches have retained reasons in status")
    e = events(d, "tree-reaped")
    check(len(e) == 1 and e[0]["reaped"] == [b] and e[0]["subject"] == "z-healthy",
          "L0941: only the healthy repo's actual destruction writes an event")

with tempfile.TemporaryDirectory() as d:
    state = pathlib.Path(d) / "state" / "reap-retained.json"
    state.parent.mkdir()
    state.write_text('[{"retained": ["stale"]}]')
    check(run_merged(d) == [] and json.loads(state.read_text())["projects"] == [],
          "L0941: an empty run clears stale retained status")
    check(datetime.fromisoformat(json.loads(state.read_text())["measured_at"]).tzinfo == timezone.utc,
          "L0941: even an empty run writes measured_at")
    check(list(state.parent.iterdir()) == [state], "L0941: atomic write leaves no temporary file")

# L1040: path first, corroborated spec events second, charter fallback last.
with tempfile.TemporaryDirectory() as d, patch.multiple(fold, ROOT=pathlib.Path(d), EVENTS=pathlib.Path(d) / "events"):
    root = pathlib.Path(d)
    for project in ("A", "B", "C"):
        (root / "repos" / project).mkdir(parents=True)
    conventional = root / "worktrees" / "C" / "l-spec-0001"
    resolve = tree_cleanup.spec_project
    own = [{"type": "spec-written", "project": "A"}]
    charter = [{"project": "B"}]
    check(resolve(own, charter, conventional) == "C", "L1040-AC2: path takes precedence")
    check(resolve(own, charter, None) == "A", "L1040-AC2: spec beats charter")
    check(resolve([], charter, None) == "B", "L1040-AC2: charter fallback")
    check(resolve([], [], None) is None, "L1040-AC2: unresolved stays None")
    noise = [{"type": kind, "project": "B"} for kind in ("decision", "owed-waived", "lesson")]
    check(resolve(own + noise, charter, None) == "A", "L1040-AC2: other event projects ignored")
    check(resolve(own + [{"type": "build-done", "project": "A"}], [], None) == "A",
          "L1040-AC2: matching witnesses agree")
    check(resolve([{ "type": "build-done", "project": "A"}], [], None) == "A",
          "L1040-AC2: build-done is a second witness")
    check(resolve(own + [{"type": "build-done", "project": "B"}], charter, None) is None,
          "L1040-AC2: disagreement cannot fall back to charter")
    for invalid in ("", "..", "../escape", "a/b", ".", "missing", 3, ["A"], "bad\x00name"):
        check(resolve([{"type": "spec-written", "project": invalid}], charter, None) == "B",
              f"L1040-AC2: invalid spec component {invalid!r} is absent")
        check(resolve([], [{"project": invalid}], None) is None,
              "L1040-AC2: invalid charter component is absent")
        check(resolve([], [], root / "worktrees" / str(invalid) / "l-spec-0001") is None,
              "L1040-AC2: invalid path project is absent")
    for outside in (root / "elsewhere" / "C" / "l-spec-0001",
                    root / "worktrees" / "C" / "nested" / "l-spec-0001",
                    root / "worktrees" / ".." / "l-spec-0001"):
        check(resolve([], [], outside) is None, "L1040-AC2: only direct conventional paths qualify")
print("L1040-AC2 ok")

with tempfile.TemporaryDirectory() as d:
    r = repo_for(d, "spec-own")
    b, wt, ready = branchwork(r, d, "L-spec-1040", "x")
    bare_origin(r)
    shipped_trail(d, None, "L-spec-1040", b, ready, sh(r, "git", "rev-parse", "HEAD"),
                  project="spec-own")
    out = run_reap_worktrees(d, dry_run=True)
    check(legacy_rows(out["reaped"]) == [{"spec": "L-spec-1040", "project": "spec-own", "path": wt,
                              "proof": "origin-merged"}], "L1040-AC1: no charter needed")
    check(pathlib.Path(wt).exists(), "L1040-AC1: dry-run retains the directory")
print("L1040-AC1 ok")


def conventional_fixture(d):
    r = repo_for(d, "path-project")
    b, old, ready = branchwork(r, d, "L-spec-1040", "x")
    wt = pathlib.Path(d) / "worktrees" / "path-project" / b
    wt.parent.mkdir()
    sh(r, "git", "worktree", "move", old, str(wt))
    bare_origin(r)
    shipped_trail(d, "L-charter-1040", "L-spec-1040", b, ready,
                  sh(r, "git", "rev-parse", "HEAD"))
    return r, b, str(wt)


with (tempfile.TemporaryDirectory() as d,
      patch.multiple(fold, ROOT=pathlib.Path(d), EVENTS=pathlib.Path(d) / "events"),
      patch.object(fold, "EVENTS", pathlib.Path(d) / "events")):
    for project in ("A", "B"):
        (pathlib.Path(d) / "repos" / project).mkdir(parents=True)
    shipped_trail(d, None, "L-spec-1041", "l-spec-1041", "unknown", "unknown")
    shipped_trail(d, None, "L-spec-1042", "l-spec-1042", "unknown", "unknown", project="A")
    write(d, "L-executor-0001", {"type": "build-done", "subject": "L-spec-1042", "project": "B"})
    with patch.object(tree_cleanup, "git", side_effect=AssertionError("unresolved must not run git")):
        reaped, retained = tree_cleanup.reap_worktrees(fold.read_events())
        check(not reaped and retained == [],
              "L1040-AC3: no directory means nothing to remove without git")
    for project in ("A", "B"):
        (pathlib.Path(d) / "worktrees" / project / "l-spec-1041").mkdir(parents=True)
    with patch.object(tree_cleanup, "git", side_effect=AssertionError("duplicate paths must not run git")):
        check(tree_cleanup.reap_worktrees(fold.read_events(), dry_run=True)[1][0]["why"] == "project unresolved",
              "L1040-AC3: duplicate conventional paths give no answer")

with tempfile.TemporaryDirectory() as d:
    r, b, wt = conventional_fixture(d)
    check(len(sh(r, "git", "worktree", "list", "--porcelain").split("worktree ")) - 1 == 2,
          "L1040-AC3: pre-removal count is two registered worktrees")
    out = run_reap_worktrees(d)
    check(legacy_rows(out["reaped"]) == [{"spec": "L-spec-1040", "project": "path-project", "path": wt,
                              "proof": "origin-merged"}], "L1040-AC3: path project reaped")
    check(not pathlib.Path(wt).exists() and wt not in sh(r, "git", "worktree", "list", "--porcelain"),
          "L1040-AC3: directory and registration removed")
    check(b in branches(r), "L1040-AC3: branch preserved")
print("L1040-AC3 ok")

with (tempfile.TemporaryDirectory() as d,
      patch.multiple(fold, ROOT=pathlib.Path(d), EVENTS=pathlib.Path(d) / "events"),
      patch.object(fold, "EVENTS", pathlib.Path(d) / "events")):
    r, b, wt = conventional_fixture(d)
    repo_for(d, "other-project")
    write(d, "L-executor-0001", {"type": "shipped", "subject": "L-spec-1040",
                                "charter": "L-charter-1040", "project": "other-project"})
    write(d, "L-grader-0001", {"type": "verdict", "subject": "L-spec-1040", "confirmed": True})
    write(d, "L-reviewer-0001", {"type": "review", "subject": "L-spec-1040", "depth": "gates-only"})
    evs = read_via_fold(d)
    before = {str(p): p.read_bytes() for p in (pathlib.Path(d) / "events").glob("*")}
    with patch.object(tree_cleanup, "git", side_effect=AssertionError("mismatch must not run git")):
        reaped, retained = tree_cleanup.reap_worktrees(evs)
        check(not reaped and len(retained) == 1 and retained[0]["why"] == "project mismatch"
              and retained[0]["path"] == wt, "L1040-AC4: mismatch retained at actual path")
        check(tree_cleanup.terminal_worktrees(evs) == {}, "L1040-AC4: mismatch has no terminal lookup")
        check(tree_cleanup.reap_merged_specs(evs) == [], "L1040-AC4: branch reaper also retains mismatch")
    state = json.loads((pathlib.Path(d) / "state" / "reap-retained.json").read_text())
    check(state["projects"][0]["retained_reason"] == [f"{b}: project mismatch"],
          "L1040-AC4: retained status carries mismatch reason")
    check(pathlib.Path(wt).exists() and b in branches(r), "L1040-AC4: mismatch directory and branch survive")
    check(before == {str(p): p.read_bytes() for p in (pathlib.Path(d) / "events").glob("*")},
          "L1040-AC4: mismatch emits no event")
print("L1040-AC4 ok")

with (tempfile.TemporaryDirectory() as d,
      patch.multiple(fold, ROOT=pathlib.Path(d), EVENTS=pathlib.Path(d) / "events"),
      patch.object(fold, "EVENTS", pathlib.Path(d) / "events")):
    r, b, wt = conventional_fixture(d)
    write(d, "L-grader-0001", {"type": "verdict", "subject": "L-spec-1040", "confirmed": True})
    write(d, "L-reviewer-0001", {"type": "review", "subject": "L-spec-1040", "depth": "gates-only"})
    write(d, "L-executor-0001", {"type": "spec-written", "subject": "L-spec-1041"})
    write(d, "L-thinker-0001", {"type": "spec-killed", "subject": "L-spec-1041"})
    evs = read_via_fold(d)
    original_git = tree_cleanup.git
    def guarded_git(repo, *args, **kwargs):
        check("repos/None" not in str(repo), "L1040-AC5: never call git against repos/None")
        return original_git(repo, *args, **kwargs)
    with patch.object(tree_cleanup, "git", side_effect=guarded_git):
        check(tree_cleanup.terminal_worktrees(evs) == {wt: "L-spec-1040"},
              "L1040-AC5: only resolved terminal worktree returned")
        check(tree_cleanup.reap_merged_specs(evs, dry_run=True) == [b], "L1040-AC5: dry run processes healthy spec")
        check(not (pathlib.Path(d) / "state").exists(), "L1040-AC5: dry run writes no status")
        check(len(sh(r, "git", "worktree", "list", "--porcelain").split("worktree ")) - 1 == 2,
              "L1040-AC5: pre-removal count is two registered worktrees")
        check(tree_cleanup.reap_merged_specs(evs) == [b], "L1040-AC5: real run processes healthy spec")
    state = json.loads((pathlib.Path(d) / "state" / "reap-retained.json").read_text())["projects"]
    check(all(row["project"] is not None for row in state),
          "L1040-AC5: no-directory spec without a build has no null group")
    check(all(e["subject"] != "None" for e in events(d, "tree-reaped"))
          and len(events(d, "tree-reaped")) == 1, "L1040-AC5: only healthy group emits event")
print("L1040-AC5 ok")

# Charter-close inference deliberately ignores worktree paths and requires unanimity.
for projects in (("A", "A"), ("A", "B"), (None, None), ("A", None)):
    with tempfile.TemporaryDirectory() as d:
        r = repo_for(d, "A")
        repo_for(d, "B")
        bare_origin(r)
        for i, project in enumerate(projects):
            sid = f"L-spec-{1040 + i}"
            b, wt, ready = branchwork(r, d, sid, "x")
            accepted(d, "L-charter-1040", sid, b, ready, project=project)
        close(d, "L-charter-1040")
        before = {str(p): p.read_bytes() for p in (pathlib.Path(d) / "events").glob("*")}
        if projects == ("A", "A"):
            p = reap(d, "L-charter-1040", "--dry-run")
            check(p.returncode == 0 and len(json.loads(p.stdout)["reaped"]) == 2,
                  f"L1040-AC6: unanimous own projects resolve: {p.stderr}")
        else:
            with patch.multiple(fold, ROOT=pathlib.Path(d), EVENTS=pathlib.Path(d) / "events"), patch.object(fold, "read_events", return_value=read_via_fold(d)), \
                 patch.object(tree_cleanup, "git", side_effect=AssertionError("unresolved charter must not run git")):
                try:
                    tree_cleanup.main(["L-charter-1040"])
                except SystemExit as exc:
                    check("project unresolved" in str(exc), "L1040-AC6: unresolved charter fails explicitly")
                else:
                    check(False, "L1040-AC6: unresolved charter must exit")
            p = reap(d, "L-charter-1040")
            check(p.returncode != 0 and "project unresolved" in p.stderr,
                  "L1040-AC6: CLI refuses mixed or absent projects")
        check(before == {str(p): p.read_bytes() for p in (pathlib.Path(d) / "events").glob("*")},
              "L1040-AC6: no ledger changes")
        check(pathlib.Path(wt).exists(), "L1040-AC6: worktrees untouched")
print("L1040-AC6 ok")

with tempfile.TemporaryDirectory() as d:
    r, b, wt = conventional_fixture(d)
    unknown = pathlib.Path(d) / "worktrees" / "l-spec-1041"
    unknown.mkdir()
    shipped_trail(d, None, "L-spec-1041", "l-spec-1041", "unknown", "unknown")
    before = {str(p): p.read_bytes() for p in (pathlib.Path(d) / "events").glob("*")}
    for args in ((), ("L-charter-1040", "--all")):
        p = reap(d, *args)
        check(p.returncode != 0 and p.stderr.strip(), "L1040-AC7: invalid CLI combination explains failure")
        check(pathlib.Path(wt).exists() and b in branches(r), "L1040-AC7: invalid CLI touches nothing")
    p = reap(d, "--all", "--dry-run")
    check(p.returncode == 0, f"L1040-AC7: whole-root dry-run succeeds: {p.stderr}")
    out = json.loads(p.stdout)
    check(legacy_rows(out["reaped"]) == [{"spec": "L-spec-1040", "project": "path-project", "path": wt,
                              "proof": "origin-merged"}]
          and out["retained"] == [], "L1040-AC7: JSON omits specs without conventional directories")
    check(pathlib.Path(wt).exists() and not (pathlib.Path(d) / "state").exists(),
          "L1040-AC7: dry-run leaves worktree and writes no status")
    check(before == {str(p): p.read_bytes() for p in (pathlib.Path(d) / "events").glob("*")},
          "L1040-AC7: dry-run ledger byte-identical")
    check(len(out["reaped"]) == 1, "L1040-AC7: pre-removal count is one reapable worktree")
    p = reap(d, "--all")
    check(p.returncode == 0 and len(json.loads(p.stdout)["reaped"]) == 1,
          f"L1040-AC7: whole-root real run succeeds: {p.stderr}")
    check(not pathlib.Path(wt).exists() and unknown.exists() and b in branches(r),
          "L1040-AC7: only resolved worktree removed, branch preserved")
    check(all(pathlib.Path(p).read_bytes().startswith(data) for p, data in before.items())
          and len(events(d, "resource-reaped")) == 1, "L1541: real --all appends resource-reaped without editing history")
print("L1040-AC7 ok")


# R2: a legacy rebuild branch may keep the original spec directory. Resolve
# its project with NO project tags on any event, then prove dry-run ancestry.
with tempfile.TemporaryDirectory() as d:
    r, old_branch, wt = conventional_fixture(d)
    new_branch = old_branch + "-r2"
    sh(r, "git", "branch", "-m", old_branch, new_branch)
    trail = pathlib.Path(d) / "events" / "L-executor-0001.jsonl"
    # Rewrite only the private fixture ledger to model the historical branch.
    rows = [json.loads(line) for line in trail.read_text().splitlines()]
    for row in rows:
        if row.get("type") == "build-done":
            row["branch"] = new_branch
    trail.write_text("".join(json.dumps(row) + "\n" for row in rows))
    before = {str(p): p.read_bytes() for p in (pathlib.Path(d) / "events").glob("*")}
    out = json.loads(reap(d, "--all", "--dry-run").stdout)
    check(legacy_rows(out["reaped"]) == [{"spec": "L-spec-1040", "project": "path-project", "path": wt,
                             "proof": "origin-merged"}], "R2: legacy rebuild resolved from its spec path")
    check(pathlib.Path(wt).exists() and new_branch in branches(r), "R2: dry-run preserves branch and tree")
    check(before == {str(p): p.read_bytes() for p in (pathlib.Path(d) / "events").glob("*")},
          "R2: dry-run preserves every ledger byte")
    # The second conventional name must never make project choice arbitrary.
    duplicate = pathlib.Path(d) / "worktrees" / "other-project" / new_branch
    duplicate.mkdir(parents=True)
    with patch.multiple(fold, ROOT=pathlib.Path(d), EVENTS=pathlib.Path(d) / "events"):
        check(tree_cleanup._spec_worktree("L-spec-1040", rows) is None,
              "R2: duplicate conventional paths stay unresolved")
print("R2: renamed legacy branch dry-run and ambiguity checks pass")

# L1365 exercises the existing fixtures with the revised resolution and guards.
for shipped_project in ("path-project", None):
    with tempfile.TemporaryDirectory() as d, patch.multiple(fold, ROOT=pathlib.Path(d), EVENTS=pathlib.Path(d) / "events"):
        r, b, wt = conventional_fixture(d)
        repo_for(d, "B")
        evs = read_via_fold(d)
        for e in evs:
            if e["type"] == "spec-written":
                e["project"] = "B"
            if e["type"] == "build-done":
                e["project"] = "path-project"
            if e["type"] == "shipped" and shipped_project:
                e["project"] = shipped_project
        check(len(sh(r, "git", "worktree", "list", "--porcelain").split("worktree ")) - 1 == 2,
              "L1365-AC1: two registered worktrees before removal")
        reaped, retained = tree_cleanup.reap_worktrees(evs)
        check(legacy_rows(reaped) == [{"spec": "L-spec-1040", "project": "path-project", "path": wt,
                          "proof": "origin-merged"}] and not retained,
              f"L1365-AC1: shipped/path beats misfiled metadata: {reaped}, {retained}")
        print(f"resolved row project={reaped[0]['project']}")
        check(not pathlib.Path(wt).exists() and b in branches(r), "L1365-AC1: only worktree removed")
        check(tree_cleanup._project_resolution("L-spec-1365", [
            {"type": "spec-written", "project": "path-project"},
            {"type": "build-done", "project": "path-project"}], []) == ("path-project", None, None),
            "L1365-AC1: agreeing valid build metadata is the last own witness")
print("L1365-AC1 ok")

with tempfile.TemporaryDirectory() as d, patch.multiple(fold, ROOT=pathlib.Path(d), EVENTS=pathlib.Path(d) / "events"):
    root = pathlib.Path(d)
    for project in ("A", "B"):
        (root / "repos" / project).mkdir(parents=True)
    wt = root / "worktrees" / "B" / "l-spec-1365"
    wt.mkdir(parents=True)
    evs = [{"type": "spec-written", "subject": "L-spec-1365", "project": "B"},
           {"type": "shipped", "subject": "L-spec-1365", "project": "A"}]
    evs = [{**e, "actor": "executor"} for e in evs]
    with patch.object(tree_cleanup, "git", side_effect=AssertionError("resolution must precede git")):
        check(tree_cleanup.reap_worktrees(evs) == ([], [
            {"spec": "L-spec-1365", "project": None, "path": str(wt), "why": "project mismatch"}]),
            "L1365-AC2: own shipped project conflicts with actual path")
        (root / "worktrees" / "A" / "l-spec-1365").mkdir(parents=True)
        evs = [{"type": "spec-written", "subject": "L-spec-1365", "project": "A"},
               {"type": "build-done", "subject": "L-spec-1365", "project": "B"},
               {"type": "spec-killed", "subject": "L-spec-1365"}]
        evs = [{**e, "actor": "thinker" if e["type"] == "spec-killed" else "executor"} for e in evs]
        check(tree_cleanup.reap_worktrees(evs) == ([], [
            {"spec": "L-spec-1365", "project": None, "path": None, "why": "project ambiguous"}]),
            "L1365-AC2: conflicting fallback projects with duplicate directories retain")
    check(wt.is_dir() and (root / "worktrees" / "A" / wt.name).is_dir(),
          "L1365-AC2: both directories survive uncertainty")
print("L1365-AC2 ok")

with tempfile.TemporaryDirectory() as d, patch.multiple(fold, ROOT=pathlib.Path(d), EVENTS=pathlib.Path(d) / "events"):
    root = pathlib.Path(d)
    for project in ("A", "B"):
        (root / "repos" / project).mkdir(parents=True)
    evs = []
    for terminal in ("spec-killed", "shipped"):
        for shape in ("absent", "ambiguous", "unresolved"):
            sid = f"L-spec-{1365 + len(evs)}"
            evs.append({"type": "spec-written", "subject": sid})
            if shape == "ambiguous":
                # Disagreement can exist without any build-done/branch.
                evs.extend({"type": "spec-written", "subject": sid, "project": p} for p in ("A", "B"))
            if shape == "unresolved":
                evs.append({"type": "spec-written", "subject": sid, "project": "missing"})
            evs.append({"type": terminal, "subject": sid})
    evs = [{**e, "actor": "thinker" if e["type"] == "spec-killed" else "executor"} for e in evs]
    with patch.object(tree_cleanup, "git", side_effect=AssertionError("no directory must not run git")):
        check(tree_cleanup.reap_worktrees(evs) == ([], []), "L1365-AC3: no directories means no rows")
        check(tree_cleanup.reap_merged_specs(evs) == [], "L1365-AC3: no branches means no branch work")
    check(json.loads((root / "state" / "reap-retained.json").read_text())["projects"] == [],
          "L1365-AC3: no project-null retention without a directory or build")
    wt = root / "worktrees" / "missing" / "l-spec-1365"
    wt.mkdir(parents=True)
    with patch.object(tree_cleanup, "git", side_effect=AssertionError("unknown project must not run git")):
        check(tree_cleanup.reap_worktrees(evs)[1] == [
            {"spec": "L-spec-1365", "project": None, "path": str(wt), "why": "project unresolved"}],
            "L1365-AC3: an unresolved project with a directory remains visible")

with (tempfile.TemporaryDirectory() as d,
      patch.multiple(fold, ROOT=pathlib.Path(d), EVENTS=pathlib.Path(d) / "events"),
      patch.object(fold, "EVENTS", pathlib.Path(d) / "events")):
    r = repo_for(d, "A")
    bare_origin(r)
    b, old, ready = branch_only(r, d, "L-spec-1365", "unmerged work")
    wt = pathlib.Path(d) / "worktrees" / "A" / b
    wt.parent.mkdir()
    sh(r, "git", "worktree", "move", old, str(wt))
    evs = [{"type": "spec-written", "subject": "L-spec-1365"},
           {"type": "build-done", "subject": "L-spec-1365", "branch": b, "ready_sha": ready},
           {"type": "spec-killed", "subject": "L-spec-1365"}]
    evs = [{**e, "actor": "thinker" if e["type"] == "spec-killed" else "executor"} for e in evs]
    check(len(sh(r, "git", "worktree", "list", "--porcelain").split("worktree ")) - 1 == 2,
          "L1365-AC3: two worktrees before path-only removal")
    reaped, retained = tree_cleanup.reap_worktrees(evs)
    check(len(reaped) == 1 and reaped[0]["proof"] == "origin-branch" and not retained,
          "L1365-AC3: path-only killed worktree removed after push")
    tree_cleanup.reap_merged_specs(evs)
    state = json.loads((pathlib.Path(d) / "state" / "reap-retained.json").read_text())["projects"]
    check(state == [{"project": None, "repo": None, "retained": [b],
                     "retained_reason": [f"{b}: project unresolved"]}] and b in branches(r),
          "L1365-AC3: surviving branch remains listed after its path witness disappears")
print("L1365-AC3 ok")

# Each variant gets a fresh real repo, avoiding accidental dependence on a prior reap.
for variant in ("clean", "renamed", "build-live", "spawn-live", "ended", "other-spec", "dirty",
                "rework", "off-origin", "reviewing", "written", "killed-live", "killed", "no-sha",
                "missing-spawn", "grader-live", "failed", "stale", "unreadable"):
    with tempfile.TemporaryDirectory() as d, patch.multiple(fold, ROOT=pathlib.Path(d), EVENTS=pathlib.Path(d) / "events"):
        r, b, wt = conventional_fixture(d)
        evs = read_via_fold(d)
        evs.append({"type": "rejected-criterion", "subject": "L-spec-1040", "criterion": "AC1",
                    "actor": "grader"})
        if variant == "renamed":
            sh(r, "git", "branch", "-m", b, b + "-r2")
            next(e for e in evs if e["type"] == "build-done")["branch"] = b + "-r2"
        start = {"type": "build-started", "subject": "L-spec-1040", "spawn": "L-builder-1365",
                 "worktree": wt, "dsn_role": "builder", "backend": "codex", "issued_by": "test",
                 "base_sha": sh(r, "git", "rev-parse", "HEAD")}
        if variant in ("build-live", "spawn-live", "ended", "other-spec", "killed-live",
                       "missing-spawn", "grader-live", "failed", "stale"):
            if variant in ("spawn-live", "grader-live"):
                start["type"] = "spawn-started"
            if variant == "grader-live":
                start["spawn"] = "L-grader-1365"
            if variant == "other-spec":
                start["subject"] = "L-spec-9999"
            if variant == "missing-spawn":
                start.pop("spawn")
            evs.append(start)
        if variant in ("ended", "failed", "stale"):
            evs.append({"type": {"ended": "spawn-done", "failed": "spawn-failed", "stale": "spawn-stale"}[variant],
                        "spawn": "L-builder-1365"})
        if variant in ("killed", "killed-live", "reviewing", "written"):
            evs = [e for e in evs if e["type"] != "shipped"]
        if variant in ("killed", "killed-live"):
            evs.append({"type": "spec-killed", "subject": "L-spec-1040", "actor": "thinker"})
        if variant == "written":
            evs = [e for e in evs if e["type"] not in ("build-done", "rejected-criterion")]
        if variant == "dirty":
            (pathlib.Path(wt) / "untracked").write_text("keep")
        if variant == "rework":
            (pathlib.Path(wt) / "rework").write_text("keep")
            sh(wt, "git", "add", "-A")
            sh(wt, "git", "commit", "-qm", "rework")
        if variant == "off-origin":
            sh(r, "git", "update-ref", "refs/remotes/origin/main", sh(r, "git", "rev-parse", "HEAD~1"))
        if variant == "no-sha":
            next(e for e in evs if e["type"] == "shipped").pop("sha")
        expected = {"renamed": "standing rejection", "build-live": "builder spawn live",
                    "spawn-live": "builder spawn live", "killed-live": "builder spawn live",
                    "rework": "rework in flight",
                    "off-origin": "origin/main does not contain", "no-sha": "shipped event carries no sha",
                    "unreadable": "unreadable git"}.get(variant)
        check(len(sh(r, "git", "worktree", "list", "--porcelain").split("worktree ")) - 1 == 2,
              f"L1365-AC4: {variant} starts with two registered worktrees")
        if variant == "unreadable":
            with patch.object(tree_cleanup, "git", side_effect=tree_cleanup.Undetermined("unreadable git")):
                reaped, retained = tree_cleanup.reap_worktrees(evs)
        else:
            reaped, retained = tree_cleanup.reap_worktrees(evs)
        if expected:
            check(not reaped and len(retained) == 1 and expected in retained[0]["why"]
                  and pathlib.Path(wt).exists(), f"L1365-AC4: {variant} retains: {retained}")
        elif variant in ("reviewing", "written"):
            check(not reaped and len(retained) == 1 and "not finished" in retained[0]["why"] and pathlib.Path(wt).exists(),
                  "L1541: non-terminal worktree stays with a reason")
        else:
            proof = "origin-branch" if variant == "killed" else "origin-merged"
            check(len(reaped) == 1 and reaped[0]["proof"] == proof and not retained
                  and not pathlib.Path(wt).exists(), f"L1365-AC4: {variant} reaped safely")
print("L1365-AC4 ok")

with (tempfile.TemporaryDirectory() as d,
      patch.multiple(fold, ROOT=pathlib.Path(d), EVENTS=pathlib.Path(d) / "events"),
      patch.object(fold, "EVENTS", pathlib.Path(d) / "events")):
    r, b, wt = conventional_fixture(d)
    # Only the shipped event names the project; the charter has a misfiled fallback.
    write(d, "L-executor-0001", {"type": "shipped", "subject": "L-spec-1040", "charter": "L-charter-1040",
                                "sha": sh(r, "git", "rev-parse", "HEAD"), "project": "path-project"})
    write(d, "L-grader-0001", {"type": "verdict", "subject": "L-spec-1040", "confirmed": True})
    write(d, "L-reviewer-0001", {"type": "review", "subject": "L-spec-1040", "depth": "gates-only"})
    close(d, "L-charter-1040")
    repo_for(d, "B")
    charter_project(d, "L-charter-1040", "B")
    evs = read_via_fold(d)
    check(tree_cleanup.terminal_worktrees(evs) == {wt: "L-spec-1040"},
          "L1365-AC5: terminal lookup uses shipped project")
    with patch.object(fold, "read_events", return_value=evs), contextlib.redirect_stdout(io.StringIO()) as out:
        check(tree_cleanup.main(["L-charter-1040", "--dry-run"]) == 0,
              "L1365-AC5: per-charter caller resolves own shipped project")
    check(json.loads(out.getvalue())["reaped"] == [b], "L1365-AC5: charter judges the correct repository")
    # Make a separate branch with genuinely unmerged work; no path can resolve it.
    b2, wt2, ready2 = branch_only(r, d, "L-spec-1365", "keep unmerged")
    accepted(d, None, "L-spec-1365", b2, ready2)
    write(d, "L-executor-0001", {"type": "shipped", "subject": "L-spec-1365",
                                "project": "path-project", "sha": sh(r, "git", "rev-parse", "main")})
    check(len(sh(r, "git", "worktree", "list", "--porcelain").split("worktree ")) - 1 == 3,
          "L1365-AC5: three registered worktrees before cleanup")
    tree_cleanup.reap_merged_specs(read_via_fold(d))
    p = subprocess.run(["/usr/bin/python3", "-I", "-c",
                        "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))))",
                        str(pathlib.Path(d) / "state" / "reap-retained.json")], capture_output=True, text=True)
    check(p.returncode == 0, "L1365-AC5: separate isolated process reads retained status")
    state = json.loads(p.stdout)["projects"]
    check(len(state) == 1 and state[0]["project"] == "path-project"
          and all("project unresolved" not in why and "project ambiguous" not in why
                  for why in state[0]["retained_reason"]), "L1365-AC5: disk status has resolved project")
    check(state[0]["retained"] == [b2] and "holds work not on" in state[0]["retained_reason"][0]
          and b2 in branches(r) and pathlib.Path(wt2).exists(),
          "L1365-AC5: shared resolution preserves branch ancestry proof")
print("L1365-AC5 ok")

with tempfile.TemporaryDirectory() as d, patch.multiple(fold, ROOT=pathlib.Path(d), EVENTS=pathlib.Path(d) / "events"):
    import dispatch
    r, b, wt = conventional_fixture(d)
    evs = read_via_fold(d) + [{"type": "rejected-criterion", "subject": "L-spec-1040",
                             "criterion": "AC1", "actor": "grader"}]
    check(len(sh(r, "git", "worktree", "list", "--porcelain").split("worktree ")) - 1 == 2,
          "L1365-AC6: two registered worktrees before janitor cleanup")
    reaped, retained = tree_cleanup.reap_worktrees(evs)
    check(len(reaped) == 1 and not retained and not pathlib.Path(wt).exists() and b in branches(r),
          "L1365-AC6: janitor removes rejected terminal worktree but keeps branch")
    recorded = []
    def preflight_fail(*args, **kwargs):
        raise AssertionError((args, kwargs))
    with patch.object(dispatch, "ROOT", pathlib.Path(d)), \
         patch.object(dispatch, "emit", side_effect=lambda *args, **kwargs: recorded.append((args, kwargs))):
        a = SimpleNamespace(project="path-project", subject="L-spec-1040")
        for delete_branch in (False, True):
            if delete_branch:
                check(len(sh(r, "git", "worktree", "list", "--porcelain").split("worktree ")) - 1 == 2,
                      "L1365-AC6: two worktrees before second removal")
                sh(r, "git", "worktree", "remove", wt)
                check(branches(r).count(b) == 1, "L1365-AC6: exactly one branch before deletion")
                sh(r, "git", "branch", "-D", b)
            dispatch.builder_worktree_preflight(a, wt, pathlib.Path(d) / "events" / "fixture.jsonl", {}, preflight_fail)
            listing = sh(r, "git", "worktree", "list", "--porcelain")
            check(pathlib.Path(wt).is_dir() and f"worktree {wt}\n" in listing
                  and f"branch refs/heads/{b}" in listing, "L1365-AC6: real preflight restores conventional branch")
            check(recorded[-1][0][2] == "worktree-recreated" and recorded[-1][1]["branch"] == b,
                  "L1365-AC6: re-add records worktree-recreated")
            if delete_branch:
                check(sh(r, "git", "rev-parse", b) == sh(r, "git", "rev-parse", "origin/main"),
                      "L1365-AC6: missing branch recreated from origin/main")
print("L1365-AC6 ok")

print(f"tree-cleanup: {N} checks pass")

# Recorded worktrees retain the existing ancestry/dirty proofs and process refusals.
import resources
import reap_tmp
outcomes1496 = {}
for case1496 in ("terminal-idle", "owner-live", "live-pid", "unmerged", "unknown-owner"):
    with (tempfile.TemporaryDirectory(prefix="recorded-tree-") as d,
          patch.multiple(fold, ROOT=pathlib.Path(d), EVENTS=pathlib.Path(d) / "events"),
          patch.object(fold, "EVENTS", pathlib.Path(d) / "events"),
          patch.dict(os.environ, DOIT_ROOT=d, DOIT_LEDGER_FILE="L-executor-1496.jsonl")):
        r, b, wt = conventional_fixture(d)
        fold.append(["spec-killed", "L-spec-1040"], strict=True)
        proc1496 = pathlib.Path(d) / "proc"
        (proc1496 / "net").mkdir(parents=True)
        (proc1496 / "net/unix").write_text("Num RefCount Protocol Flags Type St Inode Path\n")
        row1496 = resources.record("worktree", wt, lane="build", subject="L-spec-1040")
        ev1496 = fold.read_events(use_cache=False)
        if case1496 == "owner-live":
            ev1496 = [e for e in ev1496 if e.get("type") in ("spec-written", "build-done", "resource-created")]
        elif case1496 == "unknown-owner":
            ev1496 = [e for e in ev1496 if e.get("type") == "resource-created"]
        elif case1496 == "live-pid":
            pid1496 = proc1496 / str(os.getpid())
            (pid1496 / "fd").mkdir(parents=True)
            (pid1496 / "cwd").symlink_to(wt)
        elif case1496 == "unmerged":
            (pathlib.Path(wt) / "extra").write_text("unmerged work")
            sh(wt, "git", "add", "extra")
            sh(wt, "git", "commit", "-qm", "unmerged")
        check(sum(pathlib.Path(wt).exists() for _ in [0]) == 1, "recorded worktree removal preflight count=1")
        result1496 = reap_tmp._reap_recorded(ev1496, scratch_root=pathlib.Path(d) / "scratch", proc_root=proc1496)
        check(len(result1496) == 1, "one result per recorded path")
        row1496 = result1496[0]
        check((row1496.action == "reaped") == (case1496 == "terminal-idle"), str(row1496))
        check(pathlib.Path(wt).exists() == (case1496 != "terminal-idle"), "only idle terminal worktree removed")
        if case1496 == "unknown-owner":
            check("undetermined" in row1496.reason, "unknown owner fails closed")
        outcomes1496[case1496] = dict(action=row1496.action, reason=row1496.reason)
body1496 = json.dumps(outcomes1496, sort_keys=True)
print(json.dumps(dict(url="cli://tree-cleanup/AC3", status=0, body_sha256=hashlib.sha256(body1496.encode()).hexdigest(), body_excerpt=body1496)))
print("L1496-AC3 ok")

# L1541: real git removals and append-only event readback; injected proc only.
import disk_reaper
outcomes1541 = {}
for case1541 in ('shipped', 'run-shipped', 'other-uid', 'killed', 'void', 'building', 'reworking', 'held', 'awaiting-merge',
                 'rejection', 'recorded', 'void-off-origin', 'cwd', 'fd', 'undetermined'):
    with (tempfile.TemporaryDirectory() as d,
          patch.object(fold, 'ROOT', pathlib.Path(d)),
          patch.object(fold, 'EVENTS', pathlib.Path(d) / 'events'),
          patch.dict(os.environ, DOIT_ROOT=d, DOIT_LEDGER_FILE='L-executor-1541.jsonl')):
        r, b, wt = conventional_fixture(d)
        proc1541 = pathlib.Path(d) / 'proc'
        proc1541.mkdir()
        evs1541 = read_via_fold(d)
        if case1541 in ('killed', 'void', 'building', 'reworking', 'held', 'awaiting-merge', 'void-off-origin'):
            evs1541 = [e for e in evs1541 if e['type'] != 'shipped']
        if case1541 == 'killed':
            evs1541.append(dict(type='spec-killed', subject='L-spec-1040', actor='thinker'))
        if case1541.startswith('void'):
            evs1541 = [e for e in evs1541 if e['type'] != 'spec-written']
            evs1541 += [dict(type=t, subject='L-spec-1040', actor='spec-writer', spawn='L-spec-writer-1541')
                        for t in ('spawn-started', 'spawn-failed')]
            if case1541 == 'void':
                sh(r, 'git', 'push', '-q', 'origin', b)
            else:
                sh(r, 'git', 'remote', 'set-url', 'origin', str(pathlib.Path(d) / 'absent-origin'))
        if case1541 == 'building':
            evs1541.append(dict(type='build-started', subject='L-spec-1040', spawn='L-builder-1541'))
        if case1541 == 'held':
            evs1541.append(dict(type='capability-hold', subject='capability:live_db:L-spec-1040',
                                capability='live_db', spec='L-spec-1040', actor='grader'))
        if case1541 == 'reworking':
            (pathlib.Path(wt) / 'work').write_text('unmerged')
            sh(wt, 'git', 'add', '-A')
            sh(wt, 'git', 'commit', '-qm', 'rework')
            check(tree_cleanup.git(r, 'merge-base', '--is-ancestor', b, 'origin/main', ok=True).returncode == 1, 'L1541 rework tip not on origin')
        if case1541 == 'rejection':
            sh(r, 'git', 'branch', '-m', b, b + '-r2')
            next(e for e in evs1541 if e['type'] == 'build-done')['branch'] = b + '-r2'
            evs1541.append(dict(type='rejected-criterion', subject='L-spec-1040', criterion='AC1', actor='grader'))
        if case1541 == 'recorded':
            evs1541.append(dict(type='resource-created', subject='L-spec-1040', actor='executor',
                                kind='worktree', ref=wt, lane='build'))
        if case1541 in ('cwd', 'fd', 'undetermined', 'other-uid'):
            pid1541 = proc1541 / str(os.getpid())
            (pid1541 / 'fd').mkdir(parents=True)
            (pid1541 / ('fd/4' if case1541 == 'fd' else 'cwd')).symlink_to(pathlib.Path(wt) / ('seed.txt' if case1541 == 'fd' else ''))
        (pathlib.Path(wt) / 'seed.txt').write_text('modified')
        (pathlib.Path(wt) / 'untracked').write_text('debris')
        expected_bytes1541 = sum(p.lstat().st_size for p in pathlib.Path(wt).iterdir())
        check(sum(pathlib.Path(wt).exists() for _ in [0]) == 1, 'L1541 pre-removal count=1')
        real_lstat1541 = os.lstat
        def other_uid1541(path, *args, **kwargs):
            st = real_lstat1541(path, *args, **kwargs)
            if pathlib.Path(path) == proc1541 / str(os.getpid()):
                fields = list(st)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return st
        with (disk_reaper.worktree_pass(proc1541),
              patch.object(os, 'lstat', side_effect=other_uid1541) if case1541 == 'other-uid' else contextlib.nullcontext()):
            with patch.object(reap_tmp, '_pid_refs', side_effect=PermissionError('proc')) if case1541 in ('undetermined', 'other-uid') else contextlib.nullcontext():
                if case1541 in ('run-shipped', 'other-uid'):
                    for name1541 in ('tmp', 'var', 'sessions'):
                        (pathlib.Path(d) / name1541).mkdir()
                    logfile1541 = pathlib.Path(d) / 'logs/tmp-reaper.log'
                    run1541 = reap_tmp.run(root=pathlib.Path(d) / 'tmp', proc_root=proc1541,
                        events=evs1541, var_tmp_root=pathlib.Path(d) / 'var',
                        session_roots=[pathlib.Path(d) / 'sessions'],
                        session_state_path=pathlib.Path(d) / 'state/date.json', log_path=logfile1541)
                    check(not run1541['errors'], 'L1541 run surfaces complete')
                    check(run1541['other_uid_skipped'] == int(case1541 == 'other-uid'),
                          'L1541 worktree process skip count reaches run result')
                    got1541, kept1541 = run1541['worktree_rows'], []
                    check(run1541['freed_bytes_worktrees'] == expected_bytes1541 and
                          f'worktrees removed=1 freed_bytes={expected_bytes1541} ' in logfile1541.read_text(),
                          'L1541 positive worktree bytes reach real log')
                else:
                    got1541, kept1541 = tree_cleanup.reap_worktrees(evs1541)
        if case1541 in ('shipped', 'run-shipped', 'other-uid', 'killed', 'void'):
            check(len(got1541) == 1 and not kept1541 and not pathlib.Path(wt).exists(), f'L1541 AC1 {case1541}: {got1541} {kept1541}')
            row1541 = got1541[0]
            check(row1541['freed_bytes'] == expected_bytes1541 > 0 and row1541['proof'] == ('origin-merged' if case1541 in ('shipped', 'run-shipped', 'other-uid') else 'origin-branch'), 'L1541 bytes and proof')
            fresh1541 = read_via_fold(d)
            reaped1541 = [e for e in fresh1541 if e['type'] == 'resource-reaped']
            check(len(reaped1541) == 1 and reaped1541[0]['ref'] == wt and reaped1541[0]['reason'] == 'worktree: ' + row1541['proof'], 'L1541 fresh-process event readback')
            outcomes1541[case1541] = row1541['proof']
        else:
            check(not got1541 and len(kept1541) == 1 and kept1541[0]['why'] and pathlib.Path(wt).exists(), f'L1541 AC2 {case1541}: {got1541} {kept1541}')
            check(not [e for e in read_via_fold(d) if e['type'] == 'resource-reaped'], 'L1541 retained appends no event')
            expected_reason1541 = {'building': 'builder spawn live', 'held': 'spec held',
                'reworking': 'not finished', 'awaiting-merge': 'not finished',
                'rejection': 'standing rejection', 'recorded': 'recorded resource',
                'void-off-origin': 'origin', 'cwd': 'live process', 'fd': 'live process',
                'undetermined': 'undetermined'}[case1541]
            check(expected_reason1541 in kept1541[0]['why'], f'L1541 {case1541} reason names its guard')
            if case1541 in ('cwd', 'fd', 'undetermined'):
                sh(wt, 'git', 'checkout', '--', 'seed.txt')
                (pathlib.Path(wt) / 'untracked').unlink()
                with disk_reaper.worktree_pass(proc1541):
                    with patch.object(reap_tmp, '_pid_refs', side_effect=PermissionError('proc')) if case1541 == 'undetermined' else contextlib.nullcontext():
                        tree_cleanup.reap_merged_specs(evs1541)
                check(pathlib.Path(wt).exists(), 'L1541 conservative second pass cannot bypass process guard')
            outcomes1541[case1541] = kept1541[0]['why']
body1541 = json.dumps(outcomes1541, sort_keys=True)
print(json.dumps(dict(url='cli://tree-cleanup/AC1-AC2-AC7', status=0, body_sha256=hashlib.sha256(body1541.encode()).hexdigest(), body_excerpt=body1541)))
print('L1541 AC1 AC2 AC7 pass; destructive fixture count=1 per case')
