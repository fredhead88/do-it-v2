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
import argparse, fcntl, json, os, pathlib, subprocess, sys, time
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


def _write_state(root, ok, remote, snapshot=None, ts=None):
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
def _untrack_outside_allowlist(root):
    ls = _git(["ls-files"], root)
    if ls.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {ls.stderr.strip()}")
    outside = [p for p in ls.stdout.splitlines() if p and p.split("/", 1)[0] not in ALLOWLIST]
    if outside:
        rm = _git(["rm", "-r", "-f", "--cached", "--quiet", "--"] + outside, root)
        if rm.returncode != 0:
            raise RuntimeError(f"git rm --cached failed: {rm.stderr.strip()}")


def _stage_allowlist(root):
    present = [p for p in ALLOWLIST if (root / p).exists()]
    if present:
        add = _git(["add"] + present, root)
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
        _repos_manifest(r)
        _untrack_outside_allowlist(r)
        _stage_allowlist(r)
        _ensure_remote(r, remote)
        _commit_if_staged(r, ts)
        head = _git(["rev-parse", "HEAD"], r).stdout.strip()
        pushed = _push_if_ahead(r, remote, branch, head)
        _write_state(r, ok=True, remote=remote, snapshot=head, ts=ts)
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
    p_cron = sub.add_parser("cron")
    p_cron.add_argument("--doit-bin")
    a = ap.parse_args(argv)
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
