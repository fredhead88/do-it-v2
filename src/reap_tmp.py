#!/usr/bin/env python3
"""reap_tmp — the scratch reaper decides by owner, age and liveness, never by name (SD7).

  reap_tmp.run(root="/tmp", uid=None, min_age_min=120, dry_run=False, now=None,
               proc_root="/proc") -> {removed, kept, before_pct, after_pct}
  doit reap-tmp [--dry-run] [--min-age MIN]

Replaces the retired ~/.do-it/tmp-reaper.sh, which decided by matching a
hardcoded, ever-growing list of name globs. This module never reads a name: a
top-level entry of `root` is removed only when ALL FOUR of SD7's conditions
hold — (a) it is owned by the resolved uid; (b) its newest mtime, over itself
and its descendants to depth 3, is older than `min_age_min` minutes; (c) no
LIVE pid's cwd/root/exe/fd/* target resolves under it; (d) no path under it is
a bound UNIX socket (`proc_root/net/unix`) or named by a live pid in a
`*.pid`/`postmaster.pid` file. "Under X" means X itself or any descendant of X.

Recursion goes at most ONE level below a top-level entry, and only when the
entry fails (b)/(c)/(d) without (a) also failing, and without any (c)/(d)
disqualifying reference landing on the entry's own top-level path — a
reference there protects the whole entry, the way a live database's
data-directory cwd protects its own idle subdirectories. Only then are the
entry's immediate children evaluated independently by the same four rules,
each reported on its own; a grandchild is never evaluated on its own, and
the parent container itself is then reported via its children, not itself.

A top-level entry that is a symlink is read with `lstat`, never `stat`, and is
never traversed into — its own link ownership/mtime decide it, never its
target's. Anything this module cannot read (`PermissionError`/`OSError`
anywhere in an entry's own evaluation) is kept, reason "undetermined" — never
removed: unreadable is fail-safe, not fail-open. A dead pid's `proc_root/<pid>`
entry is skipped entirely as evidence, mirroring a real `/proc` where it would
not exist at all.

`panes.alive(pid)` (src/panes.py) is the one liveness predicate this module
trusts, for both a live pid found under `proc_root` and a pid recovered from a
pidfile's contents.
"""
import argparse, os, pathlib, shutil, stat, sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import panes  # noqa: E402

ROOT = pathlib.Path(os.environ.get("DOIT_ROOT", str(pathlib.Path.home() / ".do-it")))


class Undetermined(Exception):
    """Anything this module could not establish about one entry. Never removed."""


def _under(target, base):
    """X itself or a descendant of X — a string comparison, never a resolve:
    a symlink's target is never consulted, only the literal path a proc read
    or a socket/pidfile listing handed back."""
    t, b = os.path.normpath(str(target)), os.path.normpath(str(base))
    return t == b or t.startswith(b + os.sep)


def _same(a, b):
    return os.path.normpath(str(a)) == os.path.normpath(str(b))


def _newest_mtime(path, depth):
    """Newest mtime over `path` and its descendants, `depth` levels down.
    `lstat` throughout: a nested symlink's own mtime, never its target's, and
    never traversed into (its mode is never S_ISDIR)."""
    st = os.lstat(path)
    newest = st.st_mtime
    if depth <= 0 or not stat.S_ISDIR(st.st_mode):
        return newest
    for name in os.listdir(path):
        newest = max(newest, _newest_mtime(os.path.join(path, name), depth - 1))
    return newest


def _pid_dir_names(proc_root):
    return [n for n in os.listdir(proc_root) if n.isdigit()]


def _pid_refs(proc_root, pid):
    """cwd/root/exe/fd/* targets of a LIVE pid, read only for a live pid.
    A PermissionError (another user's process, unreadable without privilege)
    is raised, never swallowed — the caller turns it into "undetermined"."""
    base = pathlib.Path(proc_root) / str(pid)
    out = []
    for name in ("cwd", "root", "exe"):
        try:
            out.append(os.readlink(base / name))
        except PermissionError:
            raise
        except OSError:
            continue
    try:
        fds = os.listdir(base / "fd")
    except PermissionError:
        raise
    except OSError:
        fds = []
    for fdname in fds:
        try:
            out.append(os.readlink(base / "fd" / fdname))
        except PermissionError:
            raise
        except OSError:
            continue
    return out


def _refs_c(proc_root, x):
    """Rule (c): every reference of a live pid that resolves under `x`. A pid
    `panes.alive()` reports dead is skipped entirely — no read is attempted —
    a stale fixture `<pid>` directory is not evidence of anything."""
    out = []
    for name in _pid_dir_names(proc_root):
        pid = int(name)
        if not panes.alive(pid):
            continue
        for ref in _pid_refs(proc_root, pid):
            if _under(ref, x):
                out.append(ref)
    return out


def _bound_sockets(proc_root):
    try:
        text = (pathlib.Path(proc_root) / "net" / "unix").read_text()
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines()[1:]:  # header line
        parts = line.split()
        if parts and parts[-1].startswith("/"):
            out.append(parts[-1])
    return out


def _pidfiles_under(x):
    """`*.pid`/`postmaster.pid` files anywhere under `x` — (d) itself is not
    depth-limited, only the one-level recursion that REPORTS on it is. Never
    walks through a symlink (`os.walk`'s own default), and never walks at all
    when `x` is itself a symlink or not a directory."""
    st = os.lstat(x)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return []

    def _raise(e):
        raise e

    out = []
    for dirpath, _dirnames, filenames in os.walk(x, onerror=_raise):
        for fn in filenames:
            if fn.endswith(".pid") or fn == "postmaster.pid":
                out.append(os.path.join(dirpath, fn))
    return out


def _refs_d(proc_root, x):
    """Rule (d): a bound socket under `x`, or a `*.pid`/`postmaster.pid` file
    under `x` naming a pid `panes.alive()` reports live. A pid file naming a
    dead pid contributes nothing — its contents are read only for the
    integer, never as a path (security_path (c))."""
    out = [s for s in _bound_sockets(proc_root) if _under(s, x)]
    for pf in _pidfiles_under(x):
        try:
            pid = int(pathlib.Path(pf).read_text().strip())
        except (OSError, ValueError):
            continue
        if panes.alive(pid):
            out.append(pf)
    return out


def _reason(b_ok, c_refs, d_refs):
    parts = []
    if not b_ok:
        parts.append("too young")
    if c_refs:
        parts.append(f"a live process references {c_refs[0]}")
    if d_refs:
        parts.append(f"a bound socket or live pidfile at {d_refs[0]}")
    return "; ".join(parts) if parts else "undetermined"


def _decide(path, uid, min_age_min, now, proc_root, allow_recurse):
    """One entry (top-level, or an immediate child when `allow_recurse` is
    False), evaluated by SD7's four conditions in order. Returns
    ("remove", None) | ("keep", reason) | ("recurse", None) — the last only
    possible for a top-level directory entry. Raises Undetermined on any
    filesystem read this entry's own evaluation could not complete."""
    try:
        st = os.lstat(path)
        if st.st_uid != uid:
            return "keep", f"owned by uid {st.st_uid}, not the resolved uid {uid}"

        is_dir = stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
        newest = _newest_mtime(path, 3)
        c_refs = _refs_c(proc_root, path)
        d_refs = _refs_d(proc_root, path)
    except OSError as e:
        raise Undetermined(str(e))

    age = (now - datetime.fromtimestamp(newest, tz=timezone.utc)).total_seconds()
    b_ok = age > min_age_min * 60

    if b_ok and not c_refs and not d_refs:
        return "remove", None

    at_self = any(_same(r, path) for r in c_refs + d_refs)
    if at_self or not allow_recurse or not is_dir:
        return "keep", _reason(b_ok, c_refs, d_refs)
    return "recurse", None


def _disk_pct(root):
    """Same semantics as the retired script's `df --output=pcent`: a ceiling
    percentage of used/total, never a fabricated 0 on a read failure — the
    caller only ever sees this once `root` is already confirmed a directory."""
    u = shutil.disk_usage(root)
    if not u.total:
        return 0
    return -(-u.used * 100 // u.total)  # ceiling division


def _delete(path):
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        os.remove(path)
    else:
        shutil.rmtree(path)


def run(root="/tmp", uid=None, min_age_min=120, dry_run=False, now=None, proc_root="/proc"):
    """SD7's decision, applied to every top-level entry of `root`. Raises
    (never returns a partial result) when the scan of `root` itself cannot be
    performed — the CLI reads that as its own, separate failure mode."""
    uid = os.getuid() if uid is None else uid
    now = now or datetime.now(timezone.utc)
    root = str(root)
    if not os.path.isdir(root):
        raise NotADirectoryError(f"reap_tmp: {root!r} is not a directory")

    before_pct = _disk_pct(root)
    names = sorted(os.listdir(root))  # a failure here IS a scan failure

    removed, kept = [], []
    for name in names:
        path = os.path.join(root, name)
        try:
            kind, reason = _decide(path, uid, min_age_min, now, proc_root, True)
        except Undetermined:
            kept.append({"path": path, "reason": "undetermined"})
            continue
        if kind == "remove":
            removed.append(path)
            if not dry_run:
                _delete(path)
        elif kind == "keep":
            kept.append({"path": path, "reason": reason})
        else:  # "recurse" — the entry's immediate children, one level, each on its own
            try:
                child_names = sorted(os.listdir(path))
            except OSError:
                kept.append({"path": path, "reason": "undetermined"})
                continue
            for cname in child_names:
                cpath = os.path.join(path, cname)
                try:
                    ckind, creason = _decide(cpath, uid, min_age_min, now, proc_root, False)
                except Undetermined:
                    kept.append({"path": cpath, "reason": "undetermined"})
                    continue
                if ckind == "remove":
                    removed.append(cpath)
                    if not dry_run:
                        _delete(cpath)
                else:
                    kept.append({"path": cpath, "reason": creason})

    return {"removed": removed, "kept": kept, "before_pct": before_pct, "after_pct": _disk_pct(root)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-age", type=int, default=120, dest="min_age")
    a = ap.parse_args(argv)

    try:
        result = run(root="/tmp", uid=os.getuid(), min_age_min=a.min_age,
                     dry_run=a.dry_run, proc_root="/proc")
    except Exception as e:
        sys.stderr.write(f"reap-tmp: could not scan /tmp: {e}\n")
        return 1

    log_path = ROOT / "logs" / "tmp-reaper.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(log_path, "a") as f:
        f.write(f"{ts} /tmp {result['before_pct']}% -> {result['after_pct']}%\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
