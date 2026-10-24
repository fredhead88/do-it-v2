#!/usr/bin/env python3
"""backup — one push that keeps the git mirror's tree to exactly four names.

  backup.py push | watch --for N [--poll S] | status | cron [--doit-bin B]

Replaces `~/.do-it/backup-to-git.sh` (a personal-crontab `git add -A`, broken by
a `.gitignore` whose `repos/`/`worktrees/`/`seat/` lines carry a trailing comment
`.gitignore` does not honor — only a line whose FIRST character is `#` is a
comment). `push()` untracks then stages exactly `events/ content/ models.toml
env.sh`, EVERY push, so a still-broken `.gitignore` can never let anything else
back in (AC1) — this is a property of the allow-list, not of the ignore file.

The destination — remote and branch — is `~/.config/do-it/backup.toml`,
deliberately OUTSIDE `$DOIT_ROOT`, so it survives `rm -rf $DOIT_ROOT` and
`restore.py` needs no live root to learn where to pull from (AC6). A missing
config (and no `--remote` override) is a named refusal, never a guessed
destination.

`watch()` is the cron body (ADR-0028-11): one lock for the whole `--for` window,
never re-acquired per tick; a run that finds the lock already held exits 0.
"""
import argparse, fcntl, json, os, pathlib, stat, subprocess, sys, time
import shutil
import signal
import tempfile
import traceback
import tomllib
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fold  # noqa: E402 — fixes fold.ROOT/EVENTS/NOW from $DOIT_ROOT at import,
             # once per process (§5's cross-root rule): every append this module
             # makes therefore lands on the CALLING PROCESS's own live root.

ALLOWLIST = ("events", "content", "models.toml", "env.sh")
CONFIG_PATH = pathlib.Path.home() / ".config" / "do-it" / "backup.toml"


def _root(root=None):
    return pathlib.Path(root) if root else fold.ROOT


def _config_path(config_path=None):
    return pathlib.Path(config_path) if config_path else CONFIG_PATH


def load_config(config_path=None):
    """{} when the file is absent — a missing destination is loud, never a
    fallback to a hardcoded remote (design §9.9, Assumption 4)."""
    p = _config_path(config_path)
    if not p.is_file():
        return {}
    d = tomllib.loads(p.read_text())
    out = {}
    if d.get("remote"):
        out["remote"] = d["remote"]
    out["branch"] = d.get("branch") or "main"
    return out


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


# ── state and lock: colocated under the pushed root, outside the allow-list ──
# (Assumption 9) — never under ~/.config/do-it/, which is shared config, not
# per-root runtime bookkeeping.
def _state_path(root):
    return _root(root) / "backup-state.json"


def _lock_path(root):
    return _root(root) / "backup.lock"


def last_push(root=None):
    """{ts, snapshot, remote, age_s, ok} — ts/snapshot/remote name the last
    SUCCESSFUL push; ok reflects the last ATTEMPT (AC2)."""
    p = _state_path(root)
    if not p.is_file():
        cfg = load_config()
        return {"ts": None, "snapshot": None, "remote": cfg.get("remote"),
                "age_s": None, "ok": False}
    try:
        d = json.loads(p.read_text())
    except json.JSONDecodeError:
        d = {}
    ts = d.get("ts")
    age = ((datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
           if ts else None)
    return {"ts": ts, "snapshot": d.get("snapshot"), "remote": d.get("remote"),
            "age_s": age, "ok": bool(d.get("ok"))}


def _write_state(root, ok, remote, snapshot=None, ts=None, exclusions=()):
    p = _state_path(root)
    prev = {}
    if p.is_file():
        try:
            prev = json.loads(p.read_text())
        except json.JSONDecodeError:
            prev = {}
    d = ({"ts": ts, "snapshot": snapshot, "remote": remote} if ok else
         {"ts": prev.get("ts"), "snapshot": prev.get("snapshot"),
          "remote": remote or prev.get("remote")})
    d["ok"] = ok
    d["excluded"] = list(exclusions) if ok else prev.get("excluded", [])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d))


def _append_backup_failed(remote, error):
    """fold.append(), exactly as backup.sh did (Assumption 1) — never a hand-
    written .jsonl line. Actor is the filename (D90): L-backup-local.jsonl."""
    prev = os.environ.get("DOIT_LEDGER_FILE")
    os.environ["DOIT_LEDGER_FILE"] = "L-backup-local.jsonl"
    try:
        fold.append(["backup-failed", "-", f"remote={remote or ''}", f"error={error}"])
    finally:
        if prev is None:
            os.environ.pop("DOIT_LEDGER_FILE", None)
        else:
            os.environ["DOIT_LEDGER_FILE"] = prev


# ── the manifest (Assumption 7) ───────────────────────────────────────────────
def _repos_manifest(root):
    """{name: target} of every real repos/<name> symlink, regenerated on every
    push, written inside the already-allow-listed content/ (no fifth path)."""
    repos_dir = root / "repos"
    manifest = {}
    if repos_dir.is_dir():
        for entry in sorted(repos_dir.iterdir()):
            if entry.is_symlink():
                manifest[entry.name] = os.readlink(entry)
    out = root / "content" / ".repos-manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return manifest


# ── the allow-list operation (AC1) ────────────────────────────────────────────
def excluded(root):
    """Sorted content exclusions; never follow links or enter excluded trees.

    Index failures and an unreadable content root abort the scan. Errors on a
    child name exclude that entire path as undetermined instead of treating it
    as clean. Ledger parts must remain mirrored, so their 90 MB cap is fatal.
    """
    root = pathlib.Path(root)
    index = _git(["ls-files", "--stage", "-z", "--", "content"], root)
    if index.returncode:
        raise RuntimeError(f"git ls-files failed: {index.stderr.strip()}")
    found = {}
    for record in index.stdout.split("\0"):
        if record:
            meta, path = record.split("\t", 1)
            if meta.split()[0] == "160000" and path.startswith("content/"):
                found[path] = "gitlink"

    def walk(path):
        rel = path.relative_to(root).as_posix()
        if rel in found:
            return
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                return
            if stat.S_ISREG(info.st_mode):
                if rel.startswith("content/ledger-mirror/"):
                    if info.st_size > 94_371_840:
                        raise RuntimeError(f"ledger mirror file exceeds 90 MB: {rel}")
                elif info.st_size > 52_428_800:
                    found[rel] = "over-50MB"
                return
            if not stat.S_ISDIR(info.st_mode):
                return
            with os.scandir(path) as scan:
                entries = {entry.name for entry in scan}
        except OSError:
            found[rel] = "undetermined"
            return

        uncertain = False
        if ".git" in entries:
            try:
                git = path / ".git"
                mode = git.lstat().st_mode
                nested = False
                if stat.S_ISDIR(mode):
                    try:
                        nested = stat.S_ISREG((git / "HEAD").lstat().st_mode)
                    except FileNotFoundError:
                        pass
                elif stat.S_ISREG(mode):
                    with git.open("rb") as f:
                        nested = f.readline(7).startswith(b"gitdir:")
                if nested:
                    found[rel] = "nested-repo"
                    return
            except OSError:
                uncertain = True
        if "PG_VERSION" in entries:
            try:
                if stat.S_ISREG((path / "PG_VERSION").lstat().st_mode):
                    found[rel] = "pg-data-dir"
                    return
            except OSError:
                uncertain = True
        if uncertain:
            found[rel] = "undetermined"
            return
        for name in sorted(entries - {".git"}):
            walk(path / name)

    content = root / "content"
    try:
        mode = content.lstat().st_mode
    except FileNotFoundError:
        mode = 0
    if stat.S_ISDIR(mode):
        # content itself (including its empty .git) is never classified.
        with os.scandir(content) as scan:
            names = sorted(entry.name for entry in scan if entry.name != ".git")
        for name in names:
            walk(content / name)
    return [(p, found[p]) for p in sorted(found)
            if not any(parent.as_posix() in found for parent in pathlib.PurePosixPath(p).parents)]


def _untrack_excluded(root, exclusions):
    paths = [f":(top,literal){path}" for path, _ in exclusions]
    if not paths:
        return
    ls = _git(["ls-files", "-z", "--", *paths], root)
    if ls.returncode:
        raise RuntimeError(f"git ls-files failed: {ls.stderr.strip()}")
    tracked = [f":(top,literal){p}" for p in ls.stdout.split("\0") if p]
    if tracked:
        rm = _git(["rm", "-r", "-f", "--cached", "--quiet", "--", *tracked], root)
        if rm.returncode:
            raise RuntimeError(f"git rm --cached failed: {rm.stderr.strip()}")


def _untrack_outside_allowlist(root):
    ls = _git(["ls-files"], root)
    if ls.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {ls.stderr.strip()}")
    outside = [p for p in ls.stdout.splitlines() if p and p.split("/", 1)[0] not in ALLOWLIST]
    if outside:
        rm = _git(["rm", "-r", "-f", "--cached", "--quiet", "--"] + outside, root)
        if rm.returncode != 0:
            raise RuntimeError(f"git rm --cached failed: {rm.stderr.strip()}")


def _stage_allowlist(root, exclusions=()):
    present = [p for p in ALLOWLIST if (root / p).exists()]
    if present:
        skips = [f":(top,exclude,literal){path}" for path, _ in exclusions]
        add = _git(["add", "--", *present, *skips], root)
        if add.returncode != 0:
            raise RuntimeError(f"git add failed: {add.stderr.strip()}")


def _ensure_remote(root, remote):
    cur = _git(["remote", "get-url", "origin"], root)
    if cur.returncode != 0:
        r = _git(["remote", "add", "origin", remote], root)
        if r.returncode != 0:
            raise RuntimeError(f"git remote add failed: {r.stderr.strip()}")
    elif cur.stdout.strip() != remote:
        r = _git(["remote", "set-url", "origin", remote], root)
        if r.returncode != 0:
            raise RuntimeError(f"git remote set-url failed: {r.stderr.strip()}")


def _commit_if_staged(root, ts):
    # `diff --cached --quiet` works even pre-first-commit (an unborn HEAD diffs
    # against the empty tree) — checked BEFORE committing so a true no-op push
    # never needs a configured git identity at all.
    if _git(["diff", "--cached", "--quiet"], root).returncode == 0:
        return False
    cm = _git(["commit", "-m", f"backup: {ts}"], root)
    if cm.returncode != 0:
        raise RuntimeError(f"git commit failed: {(cm.stderr or cm.stdout).strip()}")
    return True


def _push_if_ahead(root, remote, branch, head):
    _git(["fetch", "origin", branch], root)         # best-effort: branch may not exist upstream yet
    rh = _git(["rev-parse", f"origin/{branch}"], root)
    remote_head = rh.stdout.strip() if rh.returncode == 0 else None
    if remote_head == head:
        return False
    pu = _git(["push", "origin", f"HEAD:{branch}"], root)
    if pu.returncode != 0:
        raise RuntimeError(f"git push failed: {pu.stderr.strip()}")
    return True


def _push_locked(root=None, config_path=None):
    """The unlocked core (Interfaces). Never call this without holding the
    caller's own lock first — push()/watch() are the only two callers."""
    r = _root(root)
    cfg = load_config(config_path)
    remote, branch = cfg.get("remote"), cfg.get("branch") or "main"
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not remote:
        err = ("no destination configured: set remote/branch in "
               "~/.config/do-it/backup.toml, or pass --remote")
        _write_state(r, ok=False, remote=remote)
        _append_backup_failed(remote, err)
        return {"ts": ts, "snapshot": None, "remote": remote, "ok": False,
                "error": err, "pushed": False}
    try:
        exclusions = excluded(r)
        _repos_manifest(r)
        _untrack_outside_allowlist(r)
        _untrack_excluded(r, exclusions)
        _stage_allowlist(r, exclusions)
        _ensure_remote(r, remote)
        _commit_if_staged(r, ts)
        head = _git(["rev-parse", "HEAD"], r).stdout.strip()
        pushed = _push_if_ahead(r, remote, branch, head)
        _write_state(r, ok=True, remote=remote, snapshot=head, ts=ts, exclusions=exclusions)
        return {"ts": ts, "snapshot": head, "remote": remote, "ok": True,
                "error": None, "pushed": pushed}
    except Exception as e:                                    # noqa: BLE001
        err = str(e)
        _write_state(r, ok=False, remote=remote)
        _append_backup_failed(remote, err)
        return {"ts": ts, "snapshot": None, "remote": remote, "ok": False,
                "error": err, "pushed": False}


# ── the lock (ADR-0028-11) ────────────────────────────────────────────────────
class _Lock:
    """One flock per (root), held for the caller's whole scope. `acquired` is
    False when another push/watch already holds it — the caller returns
    cleanly rather than raising or blocking (ADR-0028-11)."""
    def __init__(self, root):
        self.path = _lock_path(root)
        self.fh = None
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except BlockingIOError:
            self.fh.close()
            self.fh = None
            self.acquired = False
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
        return False

    def acquire_blocking(self, seconds):
        """Wait on the same lock, with a raising alarm (PEP 475 retries EINTR)."""
        def expired(*_):
            raise _MaintainLockTimeout()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        previous = signal.signal(signal.SIGALRM, expired)
        try:
            signal.alarm(max(1, int(seconds)))
            fcntl.flock(self.fh, fcntl.LOCK_EX)
            self.acquired = True
        except _MaintainLockTimeout:
            self.fh.close()
            self.fh = None
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        return self.acquired


class _MaintainLockTimeout(Exception):
    pass


class _MaintainStop(Exception):
    def __init__(self, reason):
        self.reason = reason


class _MaintainGitError(Exception):
    pass


def _maintain_test_path(name):
    if os.environ.get("DOIT_BACKUP_MAINTAIN_TEST") == "1":
        value = os.environ.get("DOIT_BACKUP_MAINTAIN_TEST_" + name)
        if value:
            return pathlib.Path(value)
    return None


def _maintain_number(name, default):
    return int(os.environ.get("DOIT_BACKUP_MAINTAIN_" + name, default))


class _MaintainRun:
    """One budget and process supervisor for all maintenance git commands."""
    CAPS = ("pack.windowMemory=256m", "pack.threads=1",
            "pack.deltaCacheSize=64m", "core.packedGitLimit=256m")

    def __init__(self, root):
        self.root = _root(root).resolve()
        self.packdir = self.root / ".git/objects/pack"
        self.path = self.root / "backup-maintain-state.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.default_n = _maintain_number("CHUNK", 20_000)
        self.default_b = _maintain_number("CHUNK_BYTES", 1024**3)
        self.n = int(self.state.get("chunk", self.default_n))
        self.b = int(self.state.get("chunk_bytes", self.default_b))
        if min(self.n, self.b, self.default_n, self.default_b) <= 0:
            raise ValueError("chunk bounds must be positive")
        self.history = self.state.get("history", [])[-3:]
        self.budget = float(os.environ.get("DOIT_BACKUP_MAINTAIN_TIMEOUT_S", 2400))
        self.started = None
        self.done = 0
        self.left = []
        self.chunks_left = 0
        self.before = self.after = 0
        self.behind = "unknown"
        self.stopped = "none"
        self.nothing = False
        self.halve = False

    def clock(self):
        path = _maintain_test_path("CLOCK")
        return float(path.read_text()) if path else time.monotonic()

    def remaining(self):
        return self.budget - (self.clock() - self.started)

    def memory_ok(self):
        path = _maintain_test_path("MEMINFO") or pathlib.Path("/proc/meminfo")
        for line in path.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024 >= _maintain_number("MEM_MIN", 4 * 1024**3)
        raise ValueError("MemAvailable missing")

    def rss(self, pgid):
        path = _maintain_test_path("RSS")
        if path:
            return int(path.read_text())
        return sum(int(fields[21]) for fields in self.group_members(pgid)) * os.sysconf("SC_PAGE_SIZE")

    @staticmethod
    def group_members(pgid):
        for proc in pathlib.Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                # comm may contain spaces and parentheses; fields after its last
                # ')' start with state, ppid, pgrp, ... rss (proc_pid_stat(5)).
                fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) == pgid and fields[0] not in {"Z", "X"}:
                    yield fields
            except (FileNotFoundError, ProcessLookupError):
                continue

    def kill_group(self, proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        # wait() reaps only the direct child. SIGKILL delivery to descendants
        # is asynchronous; do not snapshot/remove their files while they exit.
        deadline = time.monotonic() + 150
        while any(self.group_members(proc.pid)):
            if time.monotonic() >= deadline:
                raise TimeoutError("process group did not exit after SIGKILL")
            time.sleep(.01)

    def git(self, args, stopped="listing", data=None, chunk=False):
        if self.remaining() <= 0:
            raise _MaintainStop(stopped)
        argv = ["git", "-C", str(self.root)]
        for cap in self.CAPS:
            argv.extend(["-c", cap])
        argv.extend(args)
        if chunk:
            argv = ["nice", "-n", "19", *argv]
        # Files avoid pipe backpressure during a large listing or batch-check.
        with tempfile.TemporaryFile(mode="w+") as inp, tempfile.TemporaryFile(mode="w+") as out, tempfile.TemporaryFile(mode="w+") as err:
            inp.write(data or "")
            inp.seek(0)
            proc = subprocess.Popen(argv, stdin=inp, stdout=out, stderr=err, start_new_session=True)
            sample_at = time.monotonic() + 1
            try:
                while proc.poll() is None:
                    remaining = self.remaining()
                    if remaining <= 0:
                        raise _MaintainStop(stopped)
                    if chunk and time.monotonic() >= sample_at:
                        sample_at = time.monotonic() + 1
                        if self.rss(proc.pid) > _maintain_number("RSS_MAX", 1536 * 1024**2):
                            raise _MaintainStop("rss")
                    try:
                        proc.wait(timeout=min(.1, remaining))
                    except subprocess.TimeoutExpired:
                        pass
                if proc.returncode:
                    raise _MaintainGitError(f"{args[0]} failed rc={proc.returncode}")
                out.seek(0)
                return out.read()
            finally:
                # Always reap the entire group on exceptional exits, including
                # Python errors in resource sampling; children inherit this pgid.
                if sys.exc_info()[0] is not None or proc.poll() is None:
                    self.kill_group(proc)
                err.seek(0)
                sys.stdout.write(err.read())

    def loose_count(self):
        objects = self.root / ".git/objects"
        return sum(1 for directory in objects.iterdir()
                   if len(directory.name) == 2 and all(c in "0123456789abcdef" for c in directory.name)
                   and directory.is_dir() for p in directory.iterdir()
                   if len(p.name) in (38, 62) and all(c in "0123456789abcdef" for c in p.name) and p.is_file())

    def chunk(self, work):
        size = 0
        end = 0
        for _, byte_count in work:
            if end and (end >= self.n or size + byte_count > self.b):
                break
            end += 1
            size += byte_count
        return work[:end], size

    def save(self):
        self.state.update(chunk=self.n, chunk_bytes=self.b, history=self.history)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state) + "\n")
        tmp.replace(self.path)

    def count_chunks_left(self):
        chunks = count = size = 0
        for _, byte_count in self.left:
            if count and (count >= self.n or size + byte_count > self.b):
                chunks += 1
                count = size = 0
            count += 1
            size += byte_count
        return chunks + bool(count)


def _maintain_locked(run):
    run.started = run.clock()
    if not run.memory_ok():
        raise _MaintainStop("memory")
    run.git(["prune-packed"], "prune")
    counts = dict(line.split(": ", 1) for line in run.git(["count-objects", "-v"]).splitlines())
    run.before = int(counts["count"])
    if run.before < int(os.environ.get("DOIT_BACKUP_LOOSE_MAX", 20_000)):
        run.nothing = True
        return
    if not run.memory_ok():
        raise _MaintainStop("memory")
    listing = run.git(["rev-list", "--objects", "--all", "--reflog", "--indexed-objects", "--unpacked"])
    oids = list(dict.fromkeys(line.split()[0] for line in listing.splitlines()))
    print(f"unlisted_loose={max(0, run.before - len(oids))} "
          "reason=outside reachable-unpacked listing; left untouched")
    run.behind = ("yes" if all(a < b for a, b in zip(run.history, run.history[1:] + [len(oids)]))
                  else "no") if len(run.history) == 3 else "unknown"
    run.history = (run.history + [len(oids)])[-3:]
    if not oids:
        run.nothing = True
        return
    sizes = {oid: (run.root / ".git/objects" / oid[:2] / oid[2:]).stat().st_size for oid in oids}
    run.left = [(oid, sizes[oid]) for oid in oids]
    typed = run.git(["cat-file", "--batch-check=%(objectname) %(objecttype)"], data="\n".join(oids) + "\n")
    rows = [line.split() for line in typed.splitlines()]
    if len(rows) != len(oids) or any(len(row) != 2 or row[1] not in {"blob", "tree", "tag", "commit"} for row in rows):
        raise ValueError("incomplete object typing")
    ordered = [oid for oid, kind in rows if kind != "commit"] + [oid for oid, kind in rows if kind == "commit"]
    run.left = [(oid, sizes[oid]) for oid in ordered]
    run.packdir.mkdir(exist_ok=True)
    while run.left:
        if run.remaining() <= 0:
            raise _MaintainStop("budget")
        if not run.memory_ok():
            raise _MaintainStop("memory")
        work, size = run.chunk(run.left)
        free_path = _maintain_test_path("FREE")
        free = int(free_path.read_text()) if free_path else shutil.disk_usage(run.root).free
        need = size + 2 * 1024**3
        if free < need:
            print(f"insufficient free space ({free} < {need} bytes)")
            run.halve = True
            raise _MaintainStop("space")
        existing = set(run.packdir.iterdir())
        chunk_started = run.clock()
        try:
            run.git(["pack-objects", "-q", str(run.packdir / "pack")], "killed",
                    data="\n".join(oid for oid, _ in work) + "\n", chunk=True)
        except BaseException:
            # The group is dead before we take this second directory snapshot.
            # Count the exact deletion candidates; never infer ownership by mtime.
            created = set(run.packdir.iterdir()) - existing
            print(f"backup-maintain cleanup: created_files={len(created)} reason=incomplete-chunk")
            for path in created:
                path.unlink()
            reason = sys.exc_info()[1]
            run.halve = isinstance(reason, _MaintainStop) and (
                reason.reason == "rss" or (reason.reason == "killed" and run.clock() - chunk_started > run.budget / 2))
            raise
        run.done += 1
        run.left = run.left[len(work):]
        run.git(["prune-packed"], "prune")


def maintain(root=None):
    """Bounded, resumable packing; normal backup writers use this same lock."""
    run = None
    error = None
    lock = _Lock(root)
    started = time.monotonic()
    try:
        # Read state only after acquiring the lock: another maintenance may have
        # updated the adaptive bounds while this caller was waiting.
        if not lock.acquire_blocking(_maintain_number("LOCK_WAIT_S", 120)):
            run = _MaintainRun(root)
            run.stopped = "lock"
        else:
            run = _MaintainRun(root)
            try:
                _maintain_locked(run)
            except _MaintainStop as stop:
                run.stopped = stop.reason
            except _MaintainGitError as exc:
                error = str(exc)
            # Count without starting a git process after the budget expires.
            run.after = run.loose_count()
            run.chunks_left = run.count_chunks_left()
            if run.halve:
                run.n = max(min(run.n, _maintain_number("CHUNK_MIN", 1000)), run.n // 2)
                run.b = max(min(run.b, 64 * 1024**2), run.b // 2)
            elif run.done and run.stopped in {"none", "budget"} and error is None:
                run.n = min(run.default_n, run.n * 2)
                run.b = min(run.default_b, run.b * 2)
            run.save()
    except Exception as exc:
        traceback.print_exc(file=sys.stdout)
        error = f"internal error {type(exc).__name__}"
    finally:
        lock.__exit__()
    if run is not None:
        print(f"backup-maintain: loose_before={run.before} loose_after={run.after} "
              f"chunks_done={run.done} chunks_left={run.chunks_left} chunk={run.n} chunk_bytes={run.b} "
              f"stopped={run.stopped} behind={run.behind} seconds={time.monotonic() - started:.2f}")
    if error:
        print(f"backup-maintain: {error}", file=sys.stderr)
        return 5
    if run.stopped == "lock" or (not run.nothing and not run.done) or run.behind == "yes":
        print("backup-maintain: not keeping up (see ledger-repack.log)", file=sys.stderr)
        return 3
    return 0


def push(root=None, config_path=None):
    """The public wrapper (Interfaces): acquires the lock once, then the
    unlocked core. A held lock returns cleanly — never raises."""
    with _Lock(root) as lock:
        if not lock.acquired:
            cfg = load_config(config_path)
            return {"ts": None, "snapshot": None, "remote": cfg.get("remote"),
                    "ok": False, "error": "lock held by another backup process"}
        return _push_locked(root, config_path)


def watch(for_s=60.0, poll_s=10.0, root=None, config_path=None):
    """One lock for the whole window (Assumption 10). A held lock -> 0
    immediately. Otherwise polls every poll_s, calling the UNLOCKED core each
    tick (never the public push(), which would re-acquire this same lock and
    silently no-op)."""
    with _Lock(root) as lock:
        if not lock.acquired:
            return 0
        n = 0
        end = time.monotonic() + for_s
        while True:
            res = _push_locked(root, config_path)
            if res.get("ok") and res.get("pushed"):
                n += 1
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_s, remaining))
        return n


def cron_line(doit_bin=None):
    """Prints only; installs nothing (Boundaries)."""
    b = doit_bin or str(HERE.parent / "doit")
    log = str(fold.ROOT / "backup.log")
    return f"* * * * * {b} backup watch --for 60 >> {log} 2>&1"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="backup")
    sub = ap.add_subparsers(dest="cmd")
    p_push = sub.add_parser("push")
    p_push.add_argument("--config")
    p_watch = sub.add_parser("watch")
    p_watch.add_argument("--for", dest="for_s", type=float, default=60.0)
    p_watch.add_argument("--poll", dest="poll_s", type=float, default=10.0)
    p_watch.add_argument("--config")
    sub.add_parser("status")
    sub.add_parser("maintain")
    p_cron = sub.add_parser("cron")
    p_cron.add_argument("--doit-bin")
    a = ap.parse_args(argv)
    if a.cmd == "maintain":
        return maintain()
    if a.cmd == "push":
        r = push(config_path=a.config)
        print(json.dumps(r))
        return 0 if r["ok"] else 1
    if a.cmd == "watch":
        n = watch(for_s=a.for_s, poll_s=a.poll_s, config_path=a.config)
        print(f"backup watch: {n} push(es)")
        return 0
    if a.cmd == "status":
        print(json.dumps(last_push()))
        return 0
    if a.cmd == "cron":
        print(cron_line(a.doit_bin))
        return 0
    ap.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
