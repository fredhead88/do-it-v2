#!/usr/bin/env python3
"""backup.py against a real git fixture — every push is a real `git commit`/
`git push` into a real bare "remote" repo on disk, never a stubbed subprocess,
because the property under test (the allow-list survives a broken `.gitignore`,
AC1) is a property of what actually lands in a real tree.

`DOIT_ROOT` is pointed at a scratch tempdir BEFORE `backup`/`fold` import
(matching `test_carry.py`'s convention) — the operator's real `~/.do-it` is
never touched. Every fixture git repo gets its own local identity so a commit
never depends on (or pollutes) any global git config.
"""
import atexit, json, os, pathlib, re, shutil, subprocess, sys, tempfile, threading, time
from unittest.mock import patch

TMP = pathlib.Path(tempfile.mkdtemp(prefix="backup-test-"))
atexit.register(shutil.rmtree, TMP)
os.environ["DOIT_ROOT"] = str(TMP / "doit-root-unused")
os.environ["DOIT_PROJECT"] = "pinned-by-the-suite"
os.environ.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                  GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

REAL_LEDGER = pathlib.Path.home() / ".do-it" / "events"
REAL_LEDGER_BEFORE = sorted(REAL_LEDGER.glob("*.jsonl")) if REAL_LEDGER.is_dir() else []
REAL_LEDGER_SIZES_BEFORE = {p: p.stat().st_size for p in REAL_LEDGER_BEFORE}
os.environ["HOME"] = str(TMP / "home")
(TMP / "home").mkdir()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import backup  # noqa: E402

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


def sh(cwd, *cmd):
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"{cmd}: {p.stderr}"
    return p.stdout.strip()


def bare_remote(name):
    p = TMP / f"{name}.git"
    sh(TMP, "git", "init", "-q", "--bare", str(p))
    return p


def config(remote, branch="main", name="backup"):
    p = TMP / f"{name}-{len(list(TMP.glob('backup-*.toml')))}.toml"
    p.write_text(f'remote = "{remote}"\nbranch = "{branch}"\n')
    return p


def seed_repo(name, files=None):
    """An initialized git repo carrying the four allow-listed paths (whichever
    of `files` are given) plus one real commit."""
    r = TMP / name
    r.mkdir(parents=True)
    sh(r, "git", "init", "-q", "-b", "main")
    for rel, content in (files or {}).items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-qm", "seed")
    return r


def ls_tree(repo, ref="HEAD"):
    return sh(repo, "git", "ls-tree", "-r", "--name-only", ref).splitlines()


def head(repo):
    return sh(repo, "git", "rev-parse", "HEAD")


DEFAULT_FILES = {"events/L-op.jsonl": '{"a": 1}\n', "content/README.md": "x\n",
                 "models.toml": "# models\n", "env.sh": "export X=1\n"}

# ══ AC1 · the allow-list untrack, every push, against a real gitlink/seat bug ═
repo1 = seed_repo("ac1", DEFAULT_FILES)
# simulate the measured HEAD state: worktrees/ and seat/ already tracked.
wt = repo1 / "worktrees" / "some-branch"
wt.mkdir(parents=True)
sh(wt, "git", "init", "-q")
(wt / "file.txt").write_text("wt\n")
sh(wt, "git", "add", "-A")
sh(wt, "git", "commit", "-qm", "x")
(repo1 / "seat" / "old-spawn.json").parent.mkdir(parents=True, exist_ok=True)
(repo1 / "seat" / "old-spawn.json").write_text("seat\n")
sh(repo1, "git", "add", "worktrees/some-branch", "seat")
sh(repo1, "git", "commit", "-qm", "the measured bug, baked into HEAD")
check("worktrees/some-branch" in ls_tree(repo1) and "seat/old-spawn.json" in ls_tree(repo1),
      "AC1: fixture setup — the bug really is on HEAD before push() runs")
# an unstaged change under events/, the way a live append would leave one.
(repo1 / "events" / "L-op.jsonl").write_text('{"a": 1}\n{"b": 2}\n')
remote1 = bare_remote("ac1-remote")
cfg1 = config(remote1)
r = backup.push(root=repo1, config_path=cfg1)
check(r["ok"] is True and r["error"] is None, f"AC1: push() must succeed: {r}")
tree = ls_tree(repo1)
check(not any(p.startswith("worktrees/") or p.startswith("seat/") for p in tree),
      f"AC1: zero worktrees/ or seat/ entries survive: {tree}")
check((repo1 / "events" / "L-op.jsonl").read_text() == sh(repo1, "git", "show", "HEAD:events/L-op.jsonl") + "\n",
      "AC1: the events/ change is in the new commit")
check("content" in {p.split("/")[0] for p in tree} and "events" in {p.split("/")[0] for p in tree},
      f"AC1: the allow-listed content survives: {tree}")

# ══ AC2 · last_push()'s exact five keys, before and after ══════════════════
repo2 = seed_repo("ac2", DEFAULT_FILES)
remote2 = bare_remote("ac2-remote")
cfg2 = config(remote2)
before = backup.last_push(root=repo2)
check(set(before) == {"ts", "snapshot", "remote", "age_s", "ok"},
      f"AC2: exactly five keys before any push: {before}")
check(before["ok"] is False and before["ts"] is None, f"AC2: no push yet: {before}")
r2 = backup.push(root=repo2, config_path=cfg2)
check(r2["ok"] is True, f"AC2: fixture push must succeed: {r2}")
after = backup.last_push(root=repo2)
check(set(after) == {"ts", "snapshot", "remote", "age_s", "ok"}, f"AC2: five keys after: {after}")
check(after["ok"] is True, f"AC2: ok True after a successful push: {after}")
check(after["snapshot"] == head(repo2), f"AC2: snapshot is the new HEAD sha: {after}")
check(isinstance(after["age_s"], float) and 0 <= after["age_s"] < 30, f"AC2: small non-negative age_s: {after}")

# ══ AC3 · watch() catches a change made AFTER it started ═══════════════════
repo3 = seed_repo("ac3", DEFAULT_FILES)
remote3 = bare_remote("ac3-remote")
cfg3 = config(remote3)


def writer():
    time.sleep(0.15)
    with open(repo3 / "events" / "L-op.jsonl", "a") as fh:
        fh.write('{"mid_call": true}\n')


threading.Thread(target=writer, daemon=True).start()
n3 = backup.watch(for_s=0.6, poll_s=0.1, root=repo3, config_path=cfg3)
check(n3 >= 1, f"AC3: at least one push happened inside the window: {n3}")
remote_events = sh(TMP, "git", "--git-dir", str(remote3), "show", "main:events/L-op.jsonl")
check("mid_call" in remote_events, f"AC3: the remote's HEAD includes the mid-call change: {remote_events!r}")

# ══ AC4 · a lock already held: push() and watch() both return cleanly ══════
repo4 = seed_repo("ac4", DEFAULT_FILES)
remote4 = bare_remote("ac4-remote")
cfg4 = config(remote4)
import fcntl  # noqa: E402
lockfile = backup._lock_path(repo4)
lockfile.parent.mkdir(parents=True, exist_ok=True)
held = open(lockfile, "w")
fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
commits_before = sh(repo4, "git", "log", "--oneline")
rp = backup.push(root=repo4, config_path=cfg4)
check(rp["ok"] is False and "lock" in (rp.get("error") or ""), f"AC4: push() returns cleanly, no raise: {rp}")
nw = backup.watch(for_s=0.1, poll_s=0.05, root=repo4, config_path=cfg4)
check(nw == 0, f"AC4: watch() returns 0 immediately when the lock is held: {nw}")
commits_after = sh(repo4, "git", "log", "--oneline")
check(commits_before == commits_after, "AC4: no second commit was attempted while the lock was held")
fcntl.flock(held, fcntl.LOCK_UN)
held.close()

# ══ AC5 · cron_line()'s shape, and that it mutates nothing ═════════════════
_real_run = subprocess.run


def no_crontab(cmd, *a, **k):
    assert not (isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "crontab"), \
        f"AC5: cron_line() must never invoke crontab: {cmd}"
    return _real_run(cmd, *a, **k)


subprocess.run = no_crontab
try:
    line = backup.cron_line()
    check(bool(re.match(r"^\* \* \* \* \* .*backup watch --for 60", line)), f"AC5: shape: {line!r}")
finally:
    subprocess.run = _real_run

# ══ AC6 (push half) · a missing config is a named refusal, never a guess ═══
repo6 = seed_repo("ac6", DEFAULT_FILES)
missing_cfg = TMP / "no-such-backup.toml"
r6 = backup.push(root=repo6, config_path=missing_cfg)
check(r6["ok"] is False and ("backup.toml" in r6["error"] or "--remote" in r6["error"]),
      f"AC6: push() names the missing config, never guesses a remote: {r6}")

# ══ AC9 (push half) · the manifest regenerates from real repos/ symlinks ═══
repo9 = seed_repo("ac9", DEFAULT_FILES)
(repo9 / "repos").mkdir()
present_target = TMP / "present-target"
present_target.mkdir()
(repo9 / "repos" / "albert-scott").symlink_to(present_target)
(repo9 / "repos" / "gone").symlink_to(TMP / "never-created")
remote9 = bare_remote("ac9-remote")
cfg9 = config(remote9)
r9 = backup.push(root=repo9, config_path=cfg9)
check(r9["ok"] is True, f"AC9: fixture push must succeed: {r9}")
manifest = json.loads((repo9 / "content" / ".repos-manifest.json").read_text())
check(manifest.get("albert-scott") == str(present_target), f"AC9: manifest names the real target: {manifest}")
check("gone" in manifest, f"AC9: manifest also names the entry whose target will vanish: {manifest}")
check("content/.repos-manifest.json" in ls_tree(repo9), "AC9: the manifest is committed inside content/")

# ══ AC10 · doit's rewiring, read from the file ══════════════════════════════
DOIT = (pathlib.Path(__file__).resolve().parent.parent / "doit").read_text()
check('exec python3 "$SRC/backup.py"' in DOIT, "AC10: doit's backup) case invokes backup.py")
check("restore)" in DOIT, "AC10: a restore) case exists")
check('exec bash "$SRC/backup.sh"' not in DOIT, "AC10: no remaining shell-out to backup.sh")
sh_check = subprocess.run(["bash", str(pathlib.Path(__file__).resolve().parent / "backup.sh")],
                          capture_output=True, text=True)
check(sh_check.returncode != 0 and "retired" in sh_check.stderr,
      f"AC10: bash src/backup.sh refuses, naming itself retired: rc={sh_check.returncode} {sh_check.stderr!r}")

# ══ R9 mirror guard · equality, real remote round trips, and failure recovery ═
MB50 = 52_428_800


def sparse(repo, rel, size):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("wb") as f:
        f.truncate(size)
    return p


def text(repo, rel, value="small\n"):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value)
    return p


def gitlink(repo, rel):
    sh(repo, "git", "update-index", "--add", "--cacheinfo", f"160000,{head(repo)},{rel}")


scan = seed_repo("excluded", DEFAULT_FILES)
(scan / "content/.git").mkdir()
sparse(scan, "content/big", MB50 + 1)
sparse(scan, "content/boundary", MB50)
text(scan, "content/pg/PG_VERSION", "17\n")
sparse(scan, "content/pg/deep/big", MB50 + 1)
text(scan, "content/repo/.git/HEAD", "ref: refs/heads/main\n")
text(scan, "content/worktree/.git", "gitdir: elsewhere\n")
(scan / "content/empty/.git").mkdir(parents=True)
text(scan, "content/empty/ordinary")
text(scan, "content/invalid/.git", "not a repo\n")
text(scan, "content/invalid/ordinary")
text(scan, "content/both/PG_VERSION", "17\n")
text(scan, "content/both/.git/HEAD", "ref: refs/heads/main\n")
gitlink(scan, "content/missing-link")
gitlink(scan, "content/existing-link")
text(scan, "content/existing-link/.git/HEAD", "ref: refs/heads/main\n")
sparse(scan, "content/ledger-mirror/large-part", MB50 + 1)
text(scan, "content/ledger-mirror/pg/PG_VERSION", "17\n")
text(scan, "content/ledger-mirror/repo/.git/HEAD", "ref: refs/heads/main\n")
gitlink(scan, "content/ledger-mirror/missing-link")
outside = sparse(TMP, "outside-big", MB50 + 1)
(scan / "content/symlink").symlink_to(outside)
(scan / "content/dir-symlink").symlink_to(scan / "content/pg", target_is_directory=True)
text(scan, "content/unreadable/small")
expected = [("content/big", "over-50MB"), ("content/both", "nested-repo"),
            ("content/existing-link", "gitlink"),
            ("content/ledger-mirror/missing-link", "gitlink"),
            ("content/ledger-mirror/pg", "pg-data-dir"),
            ("content/ledger-mirror/repo", "nested-repo"), ("content/missing-link", "gitlink"),
            ("content/pg", "pg-data-dir"), ("content/repo", "nested-repo"),
            ("content/worktree", "nested-repo")]
check(backup.excluded(scan) == expected, "R9 AC1: exact classes, precedence, boundaries, no .git children")
real_scan = os.scandir


def unreadable(path):
    if pathlib.Path(path) == scan / "content/unreadable":
        raise PermissionError("fixture scan denied")
    return real_scan(path)


with patch.object(os, "scandir", unreadable):
    check(backup.excluded(scan) == sorted(expected + [("content/unreadable", "undetermined")]),
          "R9 AC1: scan errors are named, never clean")
real_stat = pathlib.Path.lstat


def stat_denied(path):
    if path == scan / "content/boundary":
        raise PermissionError("fixture stat denied")
    return real_stat(path)


with patch.object(pathlib.Path, "lstat", stat_denied):
    check(backup.excluded(scan) == sorted(expected + [("content/boundary", "undetermined")]),
          "R9 AC1: stat errors are named")
cap = seed_repo("ledger-cap", DEFAULT_FILES)
sparse(cap, "content/ledger-mirror/part", 94_371_840)
check(backup.excluded(cap) == [], "R9 AC1: ledger cap boundary is allowed")
sparse(cap, "content/ledger-mirror/part", 94_371_841)
try:
    backup.excluded(cap)
except RuntimeError as e:
    check("90 MB" in str(e), "R9 AC1: oversized ledger copy fails loudly")
else:
    check(False, "R9 AC1: ledger cap must raise")
with patch.object(backup, "_git", return_value=subprocess.CompletedProcess([], 1, "", "index denied")):
    try:
        backup.excluded(scan)
    except RuntimeError as e:
        check("index denied" in str(e), "R9 AC1: index scan failure raises")
    else:
        check(False, "R9 AC1: failed index scan must raise")


def guard_fixture(name):
    repo = seed_repo(name, DEFAULT_FILES)
    (repo / "content/.git").mkdir()
    nested = repo / "content/nested/repo"
    nested.mkdir(parents=True)
    sh(nested, "git", "init", "-q")
    text(nested, "ordinary")
    sh(nested, "git", "add", "--", "ordinary")
    sh(nested, "git", "commit", "-qm", "nested seed")
    text(repo, "content/pg/data/PG_VERSION", "17\n")
    sparse(repo, "content/pg/data/deep/big", MB50 + 1)
    sparse(repo, "content/oversize/big", MB50 + 1)
    # Pathspec metacharacters must stay literal, including for excluded files.
    sparse(repo, "content/odd[1]*", MB50 + 1)
    text(repo, "content/odd1-ok")
    for parent in ("nested", "pg", "oversize"):
        text(repo, f"content/{parent}/sibling.txt")
    sparse(repo, "content/ledger-mirror/part", 60 * 1024 * 1024)
    return repo


def clone_guard(repo, remote, label):
    clone = TMP / label
    sh(TMP, "git", "clone", "-q", "-b", "main", str(remote), str(clone))
    excluded_paths = ["content/nested/repo", "content/pg/data", "content/oversize/big", "content/odd[1]*"]
    tree = ls_tree(clone)
    check(not any(p == x or p.startswith(x + "/") for p in tree for x in excluded_paths),
          f"R9 AC2/AC3: fresh clone has no excluded paths: {tree}")
    wanted = list(DEFAULT_FILES) + ["content/.repos-manifest.json", "content/ledger-mirror/part", "content/odd1-ok"]
    wanted += [f"content/{parent}/sibling.txt" for parent in ("nested", "pg", "oversize")]
    check(set(wanted) <= set(tree), f"R9 AC2: clone preserves ordinary content and allowlist: {tree}")
    check((clone / "content/ledger-mirror/part").stat().st_size == 60 * 1024 * 1024,
          "R9 AC2: ledger part survives the remote round trip")
    check(all((repo / x).exists() for x in excluded_paths), "R9 AC2/AC3: working files remain")
    print(f"R9 {label} fresh-clone listing: {tree}")
    shutil.rmtree(clone)


for broken in (False, True):
    label = "broken-ignore" if broken else "round-trip"
    guarded = guard_fixture(label)
    if broken:
        text(guarded, ".gitignore", "content/ # trailing comment is not an ignore\n")
    remote = bare_remote(label + "-remote")
    cfg = config(remote, name=label)
    for attempt in range(2):
        text(guarded, "content/README.md", f"attempt {attempt}\n")
        result = backup.push(root=guarded, config_path=cfg)
        check(result["ok"], f"R9 AC2: guarded push succeeds: {result}")
        clone_guard(guarded, remote, f"{label}-{attempt}")
    text(guarded, "content/README.md", "watch update\n")
    check(backup.watch(for_s=0, root=guarded, config_path=cfg) == 1, "R9 AC2: watch pushes safely")
    clone_guard(guarded, remote, label + "-watch")
    state = json.loads((guarded / "backup-state.json").read_text())
    check(state["excluded"] == [list(pair) for pair in backup.excluded(guarded)],
          "R9 AC4: exact sorted JSON exclusion pairs")
    check(len(state["excluded"]) == 4, "R9 AC4: every skipped path is named")
    failed = backup.push(root=guarded, config_path=missing_cfg)
    failed_state = json.loads((guarded / "backup-state.json").read_text())
    check(not failed["ok"] and failed_state == dict(state, ok=False),
          "R9 AC4: failed attempt preserves exclusions and successful snapshot fields")

tracked = guard_fixture("untrack_tracked")
sh(tracked, "git", "add", "--", "content")
gitlink(tracked, "content/missing-link")
text(tracked, "content/later-deleted")
sh(tracked, "git", "add", "--", "content/later-deleted")
sh(tracked, "git", "commit", "-qm", "pre-guard tracked scratch")
old_tree = ls_tree(tracked)
print(f"R9 untrack_tracked pre-guard tracked content count: {len([p for p in old_tree if p.startswith('content/')])}")
check(all(p in old_tree for p in ["content/nested/repo", "content/pg/data/PG_VERSION", "content/oversize/big"]),
      "R9 AC3: dangerous entries really were tracked before guard")
remote_tracked = bare_remote("untrack-remote")
sh(tracked, "git", "remote", "add", "origin", str(remote_tracked))
sh(tracked, "git", "push", "-q", "origin", "main")
(tracked / "content/later-deleted").unlink()
old_head = head(tracked)
result = backup.push(root=tracked, config_path=config(remote_tracked, name="untrack"))
check(result["ok"] and head(tracked) != old_head, "R9 AC3: untracking is a successful new commit")
clone_guard(tracked, remote_tracked, "untrack-clone")
check("content/missing-link" not in ls_tree(tracked), "R9 AC3: absent indexed gitlink is untracked")
index = sh(tracked, "git", "ls-files", "-s", "--", "content")
check(not any(line.startswith("160000 ") or line.endswith("/PG_VERSION") for line in index.splitlines()),
      "R9 AC3: index has no gitlinks or PG markers")
sizes = sh(tracked, "git", "ls-tree", "-rl", "HEAD", "--", "content")
check(all(int(line.split()[3]) <= MB50 or "\tcontent/ledger-mirror/" in line for line in sizes.splitlines()),
      "R9 AC3: index has no oversized ordinary blobs")

recover = seed_repo("excluded_raises", DEFAULT_FILES)
recover_remote = bare_remote("recover-remote")
recover_cfg = config(recover_remote, name="recover")
check(backup.push(root=recover, config_path=recover_cfg)["ok"], "R9 AC5: initial push")
old_local = head(recover)
old_remote = sh(TMP, "git", "--git-dir", str(recover_remote), "rev-parse", "main")
old_state = json.loads((recover / "backup-state.json").read_text())
check(old_state["excluded"] == [], "R9 AC4: clean push records empty exclusions")
text(recover, "content/pending")
ledger = backup.fold.EVENTS / "L-backup-local.jsonl"
events_before = ledger.read_text().splitlines()
with patch.object(backup, "excluded", side_effect=RuntimeError("fixture exclusion scan failed")):
    result = backup.push(root=recover, config_path=recover_cfg)
check(not result["ok"] and "fixture exclusion scan failed" in result["error"], "R9 AC5: named failure")
check(head(recover) == old_local and sh(TMP, "git", "--git-dir", str(recover_remote), "rev-parse", "main") == old_remote,
      "R9 AC5: no local commit and no remote change on scan failure")
check(json.loads((recover / "backup-state.json").read_text()) == dict(old_state, ok=False),
      "R9 AC5: state preserves last known exclusions on scan failure")
new_events = [json.loads(line) for line in ledger.read_text().splitlines()[len(events_before):]]
check(len(new_events) == 1 and new_events[0]["type"] == "backup-failed" and "actor" not in new_events[0],
      "R9 AC5: exactly one failure through fold in the temp ledger, no actor")
check(backup.push(root=recover, config_path=recover_cfg)["ok"] and head(recover) != old_local,
      "R9 AC5: next attempt recovers")

# ══ §5 · the operator's real ledger is never touched by a fixture-root test ═
after_sizes = ({p: p.stat().st_size for p in REAL_LEDGER.glob("*.jsonl")}
              if REAL_LEDGER.is_dir() else {})
check(after_sizes == REAL_LEDGER_SIZES_BEFORE,
      f"§5: the real ~/.do-it/events ledger must be byte-unchanged: {REAL_LEDGER_SIZES_BEFORE} -> {after_sizes}")

print(f"backup: {N} checks pass")
