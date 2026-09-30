#!/usr/bin/env python3
"""reap_tmp — the scratch reaper decides by owner, age and liveness, never by name (SD7),
and (R9, L-charter-0042) watches the per-user quota instead of `/tmp`'s bare percentage.

  reap_tmp.user_quota(mount="/tmp", uid=None) -> {used_bytes, limit_bytes, limit_source}
  reap_tmp.run(root="/tmp", uid=None, min_age_min=120, dry_run=False, now=None,
               proc_root="/proc", scratch_root=None, events=None, kill_fn=None)
      -> {removed, kept, before_pct, after_pct, quota, mode, reaped_worktrees,
          stopped_servers, errors}
  doit reap-tmp [--dry-run] [--min-age MIN]

Replaces the retired ~/.do-it/tmp-reaper.sh, which decided by matching a
hardcoded, ever-growing list of name globs. This module never reads a name: a
top-level entry of `root` is removed only when ALL FOUR of SD7's conditions
hold — (a) it is owned by the resolved uid; (b) its newest mtime, over itself
and its descendants to depth 3, is older than the EFFECTIVE min-age minutes
(R9b: 30 in pressure mode, `min_age_min` otherwise); (c) no LIVE pid's
cwd/root/exe/fd/* target resolves under it; (d) no path under it is a bound
UNIX socket (`proc_root/net/unix`) or named by a live pid in a
`*.pid`/`postmaster.pid` file. "Under X" means X itself or any descendant of X.

R9a/R9b/R9e (probe `content/probe-L-charter-0042/q4.md`): `/tmp` here is a
quota-limited tmpfs with no readable kernel limit — `user_quota()` measures
uid 1000's real usage directly (walking `mount`, summing `st_blocks*512`) and
falls back to a CONFIGURED percentage of the filesystem's total when
`_quotactl_limit` cannot answer (this box, always). `run()` tightens its own
safety margin to 30 minutes once usage crosses 70% of that limit ("pressure"),
alarms once at 85% (`look.emit_once`), and — opted in via `scratch_root`,
`events`, `kill_fn` — also reaps the scratch root (R9b), reaps worktrees of
any spec that has reached a terminal state (R9c, via
`tree_cleanup.reap_merged_specs`), and stops orphaned local dev servers left
behind by finished work (R9d). `None` on any of those three means the caller
has not opted that surface in: `run()` skips it outright, so every
pre-existing hermetic call site (none of which names them) stays exactly as
hermetic as before this unit. `main()` is the ONLY caller that resolves live
values for them — `scratch.root()`, `fold.read_events()`, `os.kill` — imported
at module load exactly as `panes` already is.

Recursion (unchanged from before this unit) goes at most ONE level below a
top-level entry, and only when the entry fails (b)/(c)/(d) without (a) also
failing, and without any (c)/(d) disqualifying reference landing on the
entry's own top-level path — a reference there protects the whole entry, the
way a live database's data-directory cwd protects its own idle subdirectories.
Only then are the entry's immediate children evaluated independently by the
same four rules, each reported on its own; a grandchild is never evaluated on
its own, and the parent container itself is then reported via its children,
not itself.

A top-level entry that is a symlink is read with `lstat`, never `stat`, and is
never traversed into — its own link ownership/mtime decide it, never its
target's. Anything this module cannot read (`PermissionError`/`OSError`
anywhere in an entry's own evaluation) is kept, reason "undetermined" — never
removed: unreadable is fail-safe, not fail-open. A dead pid's `proc_root/<pid>`
entry is skipped entirely as evidence, mirroring a real `/proc` where it would
not exist at all.

`panes.alive(pid)` (src/panes.py) is the one liveness predicate this module
trusts, for both a live pid found under `proc_root` and a pid recovered from a
pidfile's contents. R9d never uses `panes.proc_start` — it hardcodes real
`/proc` and would break hermetic fixtures — parsing field 22 itself, under the
injectable `proc_root`, the identical way.
"""
import argparse, json, os, pathlib, re, shutil, signal, stat, subprocess, sys, time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold, look, panes, scratch, tree_cleanup  # noqa: E402

ROOT = pathlib.Path(os.environ.get("DOIT_ROOT", str(pathlib.Path.home() / ".do-it")))

# R9d condition (d) — SD17's closed list. Nothing else ever matches.
_SD17_SIGNATURES = (("next", "dev"), ("vite",), ("uvicorn", "--reload"), ("npm", "run", "dev"))

# R15c/SD-R15-2b — the closed pattern, pid-anchored (group 1 = the pid, the
# second-to-last underscore-separated segment). Observed 2026-09-29 on the
# box's own local cluster (33 `as_*` databases): `as_authority_runtime_v3_
# <pid>_<n>`, `as_943_<pid>_<n>`, `as_1058_test_<pid>_<hex>` — every one of
# these shapes matches; the old `^as_[0-9]+_test_[0-9]+_[0-9]+$` matched none
# of them and deleted nothing.
_TEST_DB_RE = re.compile(r"^as_[a-z0-9_]+_([0-9]+)_[0-9a-f]+$")


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


def _scan_dir(dirpath, uid, min_age_min, now, proc_root, dry_run):
    """SD7's decision, applied to every top-level entry of `dirpath`. Shared
    by `root` and (R9b) `scratch_root` — the identical scan, on a different
    directory. A failure listing `dirpath` itself propagates (the caller
    decides whether that is fatal, per surface)."""
    removed, kept = [], []
    for name in sorted(os.listdir(dirpath)):
        path = os.path.join(dirpath, name)
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
    return removed, kept


def _device_for(mount):
    """The block device backing `mount`, read from /proc/mounts — quotactl
    wants a device path, not a mount point. None when it cannot be
    determined; the caller treats that exactly like an unsupported syscall."""
    try:
        want = os.path.normpath(str(mount))
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == want:
                    return parts[0]
    except OSError:
        pass
    return None


def _quotactl_limit(mount, uid):
    """Kernel per-user block quota via quotactl(2), when the platform and
    this filesystem support it. Probe Q4 (2026-09-27): unsupported on this
    box — EPERM unprivileged, EINVAL even as root, no numeric limit
    configured anywhere. ANY failure of any kind returns None; the caller
    then falls back to the configured percentage (R9a). Untested beyond
    AC2's injection point (Boundaries: out of scope — an Assumption about
    present hardware, never guessed at by the caller)."""
    try:
        import ctypes

        dev = _device_for(mount)
        if dev is None:
            return None
        libc = ctypes.CDLL("libc.so.6", use_errno=True)

        class _DQBlk(ctypes.Structure):
            _fields_ = [("bhardlimit", ctypes.c_uint64), ("bsoftlimit", ctypes.c_uint64),
                        ("curspace", ctypes.c_uint64), ("ihardlimit", ctypes.c_uint64),
                        ("isoftlimit", ctypes.c_uint64), ("curinodes", ctypes.c_uint64),
                        ("btime", ctypes.c_uint64), ("itime", ctypes.c_uint64),
                        ("valid", ctypes.c_uint32)]

        USRQUOTA, Q_GETQUOTA = 0, 0x0700
        cmd = (Q_GETQUOTA << 8) | USRQUOTA
        buf = _DQBlk()
        rc = libc.quotactl(ctypes.c_int(cmd), dev.encode(), ctypes.c_int(uid), ctypes.byref(buf))
        if rc != 0:
            return None
        limit = buf.bhardlimit or buf.bsoftlimit
        return int(limit) * 1024 if limit else None
    except Exception:
        return None


def user_quota(mount="/tmp", uid=None):
    """R9a: uid's real usage under `mount` (every entry, any depth, whose
    OWN `lstat` reports `st_uid == uid` — symlinks never followed/traversed,
    only their own tiny link inode counted), against a limit that is the
    kernel's own quota when `_quotactl_limit` can answer, else a configured
    percentage of the filesystem total. An unreadable entry is skipped — an
    undercount only delays the alarm, never fabricates one."""
    uid = os.getuid() if uid is None else uid
    mount = str(mount)
    used = 0
    for dirpath, dirnames, filenames in os.walk(mount, onerror=lambda e: None):
        for name in dirnames + filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if st.st_uid == uid:
                used += st.st_blocks * 512

    limit = _quotactl_limit(mount, uid)
    if limit is not None:
        return {"used_bytes": used, "limit_bytes": int(limit), "limit_source": "quotactl"}

    pct = (look.load_toml().get("thresholds") or {}).get("tmp_user_quota_pct", 80)
    total = shutil.disk_usage(mount).total
    return {"used_bytes": used, "limit_bytes": round(pct / 100 * total), "limit_source": "configured"}


def _cmdline_tokens(proc_root, pid):
    try:
        raw = (pathlib.Path(proc_root) / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [t for t in raw.decode(errors="replace").split("\0") if t]


def _matches_sd17(tokens, scratch_root):
    """R9d condition (d): SD17's closed list, nothing else. `postgres -D`
    counts only when its OWN argument is under `scratch_root` — never a bare
    `postgres` process, and never checked at all when `scratch_root` was not
    opted in."""
    for sig in _SD17_SIGNATURES:
        if all(part in tokens for part in sig):
            return True
    if scratch_root is not None and "postgres" in tokens and "-D" in tokens:
        i = tokens.index("-D")
        if i + 1 < len(tokens) and _under(tokens[i + 1], str(scratch_root)):
            return True
    return False


def _orphan_servers(proc_root, uid, scratch_root, terminal_paths, kill_fn, dry_run):
    """R9d: every process meeting ALL FIVE of (a) ppid 1, (b) age over 2h by
    ticks+uptime — NEVER `st_mtime` — (c) cwd under a just-reaped or
    terminal-owned worktree, or under `scratch_root`, (d) SD17's cmdline,
    (e) owned by the SAME resolved `uid` `run()` already gates `root`/
    `scratch_root` on. A match gets exactly one `kill_fn(pid, SIGTERM)`
    unless `dry_run`."""
    stopped = []
    try:
        uptime_now = float((pathlib.Path(proc_root) / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return stopped
    clk_tck = os.sysconf("SC_CLK_TCK")
    for name in _pid_dir_names(proc_root):
        pid = int(name)
        pdir = pathlib.Path(proc_root) / name
        try:
            raw = (pdir / "stat").read_text()
            fields = raw[raw.rindex(")") + 2:].split()
            ppid, start_ticks = int(fields[1]), int(fields[19])
            pst = os.lstat(pdir)
            cwd = os.readlink(pdir / "cwd")
        except (OSError, ValueError, IndexError):
            continue
        if ppid != 1:
            continue
        if uptime_now - start_ticks / clk_tck <= 2 * 3600:
            continue
        if pst.st_uid != uid:
            continue
        under_terminal = any(_under(cwd, p) for p in terminal_paths)
        under_scratch = scratch_root is not None and _under(cwd, str(scratch_root))
        if not (under_terminal or under_scratch):
            continue
        tokens = _cmdline_tokens(proc_root, pid)
        if not _matches_sd17(tokens, scratch_root):
            continue
        stopped.append({"pid": pid, "cwd": cwd, "cmdline": tokens})
        if not dry_run:
            kill_fn(pid, signal.SIGTERM)
    return stopped


def _reap_grade_views(scratch_root, events, proc_root, dry_run):
    """L-charter-0042/L-spec-0486 (cleanup-on-finish), R15b: a grader/reviewer's
    own `grade/<spawn>/` scratch copy goes the instant its spawn is terminal
    (`fold.terminal_spawns`) — never age-gated like `_scan_dir`'s own rule, and
    covering any spawn id (`L-grader-*`, `L-reviewer-*`: a reviewer today has
    no separate scratch copy, so this covers whatever `grade/<spawn>/` a
    reviewer is given). `grader-claude/` (the shared config dir) is a SIBLING
    of `grade/` and is never even listed here — only `grade/`'s own immediate
    children are considered. A terminal entry's own private cluster is asked
    to stop FIRST, unconditionally (a live reference never blocks the ask,
    only the final removal) — the postgres process under it, if any, must not
    survive its own owning spawn regardless of who else's `cwd` still sits
    there. An entry whose spawn is not terminal, unknown, or not a directory
    is left exactly alone — not even looked at for liveness."""
    grade_dir = pathlib.Path(scratch_root) / "grade"
    try:
        names = sorted(os.listdir(grade_dir))
    except OSError:
        return [], {}
    terminal = fold.terminal_spawns(events)
    removed, errors = [], {}
    for name in names:
        if name not in terminal:
            continue
        path = grade_dir / name
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        if not dry_run:
            try:
                import grading_env
                grading_env.teardown(str(path))
            except ImportError:
                pass
            except Exception as e:
                errors["teardown"] = str(e)
        if _refs_c(proc_root, str(path)):
            continue
        if not dry_run:
            try:
                shutil.rmtree(path)
            except OSError as e:
                errors["scratch_grade"] = str(e)
                continue
        removed.append(str(path))
    return removed, errors


def _postmaster_candidates(proc_root, uid):
    """R15c/SD-R15-2a: every pid under `proc_root` matching a real postmaster —
    `os.path.basename(argv[0]) == "postgres"` (a live postmaster carries the
    full path, `/usr/lib/postgresql/18/bin/postgres`; a bare `postgres`
    argv[0] matches too — the basename is identical either way), carrying its
    own `-D <dir>`, owned by `uid`. `_matches_sd17`'s `"postgres" in tokens`
    exact-token test is NOT reused — it matches none of the full-path shape.
    Returns `[(pid, dir)]`."""
    out = []
    for name in _pid_dir_names(proc_root):
        pid = int(name)
        tokens = _cmdline_tokens(proc_root, pid)
        if not tokens or os.path.basename(tokens[0]) != "postgres" or "-D" not in tokens:
            continue
        i = tokens.index("-D")
        if i + 1 >= len(tokens):
            continue
        try:
            if os.lstat(pathlib.Path(proc_root) / name).st_uid != uid:
                continue
        except OSError:
            continue
        out.append((pid, tokens[i + 1]))
    return out


def _cluster_owner(d, scratch_root, terminal_worktrees):
    """`<scratch_root>/grade/<spawn>/...` -> `("spawn", spawn)`; a path under a
    TERMINAL-state spec's own worktree (`tree_cleanup.terminal_worktrees`,
    already scoped to terminal specs only) -> `("spec", spec)`; else
    `(None, None)` — unresolved."""
    if scratch_root is not None:
        grade_root = os.path.normpath(str(pathlib.Path(scratch_root) / "grade"))
        nd = os.path.normpath(d)
        if nd == grade_root or nd.startswith(grade_root + os.sep):
            rest = nd[len(grade_root) + 1:]
            spawn = rest.split(os.sep, 1)[0] if rest else None
            if spawn:
                return "spawn", spawn
    for wt_path, spec in terminal_worktrees.items():
        if _under(d, wt_path):
            return "spec", spec
    return None, None


def _has_client_backend(proc_root, ppid):
    """A CHILD of `ppid` whose own cmdline text starts `postgres:` and carries
    a `[local]` or `host(port)` token — a pure `/proc` read, no connection, no
    credential (Assumption 5). Read as ONE joined string, never per-token: a
    real postmaster child's title-rewritten cmdline is often a single blob
    with no embedded NUL at all."""
    for name in _pid_dir_names(proc_root):
        pid = int(name)
        try:
            raw = (pathlib.Path(proc_root) / name / "stat").read_text()
            fields = raw[raw.rindex(")") + 2:].split()
            cppid = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
        if cppid != ppid:
            continue
        text = " ".join(_cmdline_tokens(proc_root, pid))
        if text.startswith("postgres:") and ("[local]" in text or re.search(r"\(\d+\)", text)):
            return True
    return False


def _default_stop_cluster(d):
    pg_ctl = shutil.which("pg_ctl")
    if pg_ctl is None:
        raise FileNotFoundError("pg_ctl")
    subprocess.run([pg_ctl, "-D", str(d), "stop", "-m", "fast"], capture_output=True, timeout=30)


def _stop_cluster(d, stop_fn, kill_fn):
    """`pg_ctl -D <dir> stop -m fast` (injectable `stop_fn`), falling back to
    `kill_fn(<pid from <dir>/postmaster.pid>, SIGINT)` only when `pg_ctl` is
    genuinely absent (`stop_fn` raising `FileNotFoundError`) — never on any
    other failure, which is `stop_fn`'s own business to report."""
    fn = stop_fn or _default_stop_cluster
    try:
        fn(d)
        return
    except FileNotFoundError:
        pass
    try:
        pid = int(pathlib.Path(d, "postmaster.pid").read_text().splitlines()[0].strip())
    except (OSError, ValueError, IndexError):
        return
    kill_fn(pid, signal.SIGINT)


_CLUSTER_STATE_DEFAULT = lambda: ROOT / "state" / "reap-tmp-clusters.json"


def _reap_clusters(proc_root, uid, scratch_root, events, now, state_path, stop_fn, kill_fn,
                    wait_s, dry_run):
    """R15c/SD-R15-2a: an orphaned private Postgres cluster — a postmaster
    whose owning spawn/spec has already ended, or (no resolvable owner) one
    idle 6h+ across two consecutive passes with no client backend — is
    stopped and, once its pid is genuinely gone, its directory removed.
    Never touches `/var/lib/postgresql` or anything outside `/tmp`, `/dev/shm`,
    `$HOME` (the scratch root included) or `<ROOT>/worktrees`."""
    state_path = pathlib.Path(state_path) if state_path is not None else _CLUSTER_STATE_DEFAULT()
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}

    terminal_worktrees = tree_cleanup.terminal_worktrees(events) if events is not None else {}
    terminal_spawns = fold.terminal_spawns(events) if events is not None else set()
    try:
        uptime_now = float((pathlib.Path(proc_root) / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        uptime_now = None
    clk_tck = os.sysconf("SC_CLK_TCK")

    allowed_bases = ["/tmp", "/dev/shm", str(pathlib.Path.home())]
    if scratch_root is not None:
        allowed_bases.append(str(scratch_root))
    allowed_bases.append(str(ROOT / "worktrees"))

    stopped, seen = [], set()
    for pid, d in _postmaster_candidates(proc_root, uid):
        nd = os.path.normpath(d)
        if nd in seen:
            continue
        seen.add(nd)
        if _under(nd, "/var/lib/postgresql") or not any(_under(nd, b) for b in allowed_bases):
            continue

        kind, owner = _cluster_owner(nd, scratch_root, terminal_worktrees)
        if kind == "spawn":
            orphaned = owner in terminal_spawns
        elif kind == "spec":
            orphaned = True
        else:
            owner = "unresolved"
            has_client = _has_client_backend(proc_root, pid)
            age_s = None
            if uptime_now is not None:
                try:
                    raw = (pathlib.Path(proc_root) / str(pid) / "stat").read_text()
                    fields = raw[raw.rindex(")") + 2:].split()
                    age_s = uptime_now - int(fields[19]) / clk_tck
                except (OSError, ValueError, IndexError):
                    age_s = None
            if has_client or age_s is None or age_s < 6 * 3600:
                state.pop(nd, None)
                continue
            idle_passes = state.get(nd, {}).get("idle_passes", 0) + 1
            state[nd] = {"idle_passes": idle_passes}
            orphaned = idle_passes >= 2
        if not orphaned:
            continue

        # security_path: the STOP is asked for any orphaned cluster this
        # allowed_bases gate admitted (a stray postmaster must not survive its
        # own owning spawn/spec wherever it sits), but REMOVAL is narrower —
        # only `<dir>` under `scratch_root/grade/` or itself under a worktree,
        # never any other path, even once genuinely dead.
        grade_root = os.path.normpath(str(pathlib.Path(scratch_root) / "grade")) \
            if scratch_root is not None else None
        removable = (grade_root is not None and (nd == grade_root or nd.startswith(grade_root + os.sep))) \
            or any(_under(nd, wt) for wt in terminal_worktrees)

        if not dry_run:
            try:
                _stop_cluster(nd, stop_fn, kill_fn)
            except Exception:
                pass
            deadline = time.monotonic() + wait_s
            while panes.alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
        if panes.alive(pid):
            stopped.append({"dir": nd, "owner": owner, "why": "stopping"})
            continue
        if not removable:
            stopped.append({"dir": nd, "owner": owner,
                            "why": "stopped, not removed (outside scratch_root/grade/ and no worktree)"})
            continue
        if not dry_run:
            try:
                shutil.rmtree(nd)
            except OSError:
                stopped.append({"dir": nd, "owner": owner, "why": "stop-failed"})
                continue
        stopped.append({"dir": nd, "owner": owner, "why": "stopped"})

    for k in list(state):
        if k not in seen:
            state.pop(k, None)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state))
    except OSError:
        pass
    return stopped


def _reap_databases(pg_fn, dry_run):
    """R15c/SD-R15-2b: a test database is dropped only when its name FULLY
    matches the closed, pid-anchored pattern, that pid is not alive, and
    `pg_stat_activity` shows zero sessions on it — the name is validated
    BEFORE it ever reaches a `DROP DATABASE` statement, so an injection-shaped
    name never gets near one."""
    dropped = []
    for name in pg_fn("SELECT datname FROM pg_database").splitlines():
        name = name.strip()
        m = _TEST_DB_RE.match(name)
        if not m:
            continue
        if panes.alive(int(m.group(1))):
            continue
        try:
            count = int(pg_fn(
                f"SELECT count(*) FROM pg_stat_activity WHERE datname = '{name}'").strip().splitlines()[0])
        except (ValueError, IndexError):
            continue
        if count != 0:
            continue
        if not dry_run:
            pg_fn(f'DROP DATABASE "{name}"')
        dropped.append(name)
    return dropped


def _psql_pg_fn():
    """`main()`/`reap_now`'s own live `pg_fn`: the local Unix socket only (no
    `-h`/TCP), database `postgres` always named (`-d postgres` — without it
    `psql` targets db `albert`, which does not exist, and `errors["pg"]` would
    fire every run), and the environment scrubbed of every secret a production
    DSN could ride in on."""
    env = {k: v for k, v in os.environ.items()
           if "DB_URL" not in k and k != "DATABASE_URL" and not k.startswith("PG")}

    def fn(sql):
        p = subprocess.run(["psql", "-d", "postgres", "-Atc", sql], capture_output=True,
                           text=True, env=env, timeout=30)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip() or f"psql exited {p.returncode}")
        return p.stdout

    return fn


def run(root="/tmp", uid=None, min_age_min=120, dry_run=False, now=None, proc_root="/proc",
        scratch_root=None, events=None, kill_fn=None, state_path=None, pg_fn=None,
        stop_fn=None, wait_s=10):
    """SD7's decision, applied to `root` (and, opted in, `scratch_root`), plus
    R9's three additive surfaces. `scratch_root`/`events`/`kill_fn` do NOT
    resolve a live default when `None` — that means the caller has not opted
    that surface in: `run()` skips it outright (no scratch scan,
    `reaped_worktrees == []`, `stopped_servers == []`, zero `/proc` scanning
    for orphans). Only `main()` resolves live values for them. Raises (never
    returns a partial result) when the scan of `root` itself cannot be
    performed — the one contract unchanged from before this unit."""
    uid = os.getuid() if uid is None else uid
    now = now or datetime.now(timezone.utc)
    root = str(root)
    if not os.path.isdir(root):
        raise NotADirectoryError(f"reap_tmp: {root!r} is not a directory")

    before_pct = _disk_pct(root)
    quota = user_quota(mount=root, uid=uid)
    frac = (quota["used_bytes"] / quota["limit_bytes"]) if quota.get("limit_bytes") else 0.0
    mode = "pressure" if frac >= 0.70 else "normal"
    effective_min_age = 30 if mode == "pressure" else min_age_min

    removed, kept = _scan_dir(root, uid, effective_min_age, now, proc_root, dry_run)

    errors = {}
    if scratch_root is not None:
        try:
            s_removed, s_kept = _scan_dir(str(scratch_root), uid, effective_min_age, now, proc_root, dry_run)
            removed += s_removed
            kept += s_kept
        except Exception as e:
            errors["scratch"] = str(e)

    stopped_servers = []
    if kill_fn is not None:
        try:
            terminal_paths = tree_cleanup.terminal_worktrees(events) if events is not None else {}
            stopped_servers = _orphan_servers(proc_root, uid, scratch_root, terminal_paths, kill_fn, dry_run)
        except Exception as e:
            errors["servers"] = str(e)

    reaped_worktrees = []
    if events is not None:
        try:
            reaped_worktrees = tree_cleanup.reap_merged_specs(events, dry_run=dry_run)
        except tree_cleanup.GroupFailure as e:
            reaped_worktrees, errors["worktrees"] = e.reaped, str(e)
        except Exception as e:
            errors["worktrees"] = str(e)

    # L-charter-0042/L-spec-0486, R15b: a grader/reviewer's own `grade/<spawn>/`
    # scratch copy, removed the instant its spawn is terminal — both `scratch_root`
    # and `events` opted in (the same two surfaces R9c's worktree reap already
    # requires), never gated on `kill_fn`.
    removed_scratch = []
    if scratch_root is not None and events is not None:
        try:
            removed_scratch, g_errors = _reap_grade_views(scratch_root, events, proc_root, dry_run)
            removed += removed_scratch
            errors.update(g_errors)
        except Exception as e:
            errors["scratch_grade"] = str(e)

    # L-charter-0042/L-spec-0486, R15c: orphaned private Postgres clusters and
    # test databases with no live owner — opted in independently: clusters via
    # `kill_fn` (the same surface R9d's orphan-server stop already requires,
    # since both are "stop something SD17/SD-R15-2a decided is dead"), test
    # databases via `pg_fn` alone.
    stopped_clusters = []
    if kill_fn is not None:
        try:
            stopped_clusters = _reap_clusters(proc_root, uid, scratch_root, events, now,
                                              state_path, stop_fn, kill_fn, wait_s, dry_run)
        except Exception as e:
            errors["clusters"] = str(e)

    dropped_databases = []
    if pg_fn is not None:
        try:
            dropped_databases = _reap_databases(pg_fn, dry_run)
        except Exception as e:
            errors["pg"] = str(e)

    if frac >= 0.85:
        look.emit_once("tmp-quota-high", str(uid), "thinker",
                        {"used_bytes": quota["used_bytes"], "limit_bytes": quota["limit_bytes"],
                         "limit_source": quota["limit_source"]}, events)

    return {"removed": removed, "kept": kept, "before_pct": before_pct, "after_pct": _disk_pct(root),
            "quota": quota, "mode": mode, "reaped_worktrees": reaped_worktrees,
            "stopped_servers": stopped_servers, "removed_scratch": removed_scratch,
            "stopped_clusters": stopped_clusters, "dropped_databases": dropped_databases, "errors": errors}


def reap_now(dry_run=False, min_age_min=120):
    """The live-value resolver `main()` already was, extracted so `main()` and
    `dispatch.ensure_room` share one path: `scratch.root()`, `fold.read_events()`,
    `os.kill`, live `/proc`, and `main()`'s own new `_psql_pg_fn()` (R15c) — all
    resolved HERE, never by any hermetic caller. Raises exactly as `run()` does
    when `root` itself cannot be scanned; the caller decides what that means."""
    return run(root="/tmp", uid=os.getuid(), min_age_min=min_age_min, dry_run=dry_run,
              proc_root="/proc", scratch_root=scratch.root(), events=fold.read_events(),
              kill_fn=os.kill, pg_fn=_psql_pg_fn())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-age", type=int, default=120, dest="min_age")
    a = ap.parse_args(argv)

    try:
        result = reap_now(dry_run=a.dry_run, min_age_min=a.min_age)
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
