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
import json, os, pathlib, re, subprocess, sys, tempfile, threading, time

TMP = pathlib.Path(tempfile.mkdtemp(prefix="backup-test-"))
os.environ["DOIT_ROOT"] = str(TMP / "doit-root-unused")
os.environ["DOIT_PROJECT"] = "pinned-by-the-suite"
os.environ.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                  GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import backup  # noqa: E402

REAL_LEDGER = pathlib.Path.home() / ".do-it" / "events"
REAL_LEDGER_BEFORE = sorted(REAL_LEDGER.glob("*.jsonl")) if REAL_LEDGER.is_dir() else []
REAL_LEDGER_SIZES_BEFORE = {p: p.stat().st_size for p in REAL_LEDGER_BEFORE}

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

# ══ §5 · the operator's real ledger is never touched by a fixture-root test ═
after_sizes = ({p: p.stat().st_size for p in REAL_LEDGER.glob("*.jsonl")}
              if REAL_LEDGER.is_dir() else {})
check(after_sizes == REAL_LEDGER_SIZES_BEFORE,
      f"§5: the real ~/.do-it/events ledger must be byte-unchanged: {REAL_LEDGER_SIZES_BEFORE} -> {after_sizes}")

print(f"backup: {N} checks pass")
