#!/usr/bin/env python3
"""restore — rebuild `$DOIT_ROOT`'s four-path backup set onto an empty root from
the configured git mirror, and prove two roots derive "the same board".

  restore.py <snapshot> [--root DIR] [--dry-run] [--remote URL]

`snapshot` is "latest" or a sha on the configured branch (ADR-0028-4/10). A
non-empty target is refused unless `dry_run` is true AND an explicit `--root`
was given — never the bare `$DOIT_ROOT` default (Assumption 5). On an EMPTY
target the four paths are written for real regardless of `--dry-run` (there is
nothing destructive to protect against); on a NON-empty target the only allowed
case never touches it — it clones to scratch, proves the clone works, and
leaves the target exactly as it was, so the weekly drill's own scratch
directory is reusable call after call (design §9.9, Boundaries).

`board_diff()` never calls `fold` in-process against a foreign root — §5's
cross-root isolation rule — it always shells out with `DOIT_ROOT=<root>` set.
"""
import argparse, json, os, pathlib, re, shutil, subprocess, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import backup, fold  # noqa: E402

FOLD_PY = HERE / "fold.py"


class Refusal(Exception):
    """A by-name refusal. main() prints it and exits non-zero."""


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _is_empty(path):
    p = pathlib.Path(path)
    if not p.exists():
        return True
    return not any(p.iterdir())


def recreate_repo_links(root):
    """Reads content/.repos-manifest.json, recreates each repos/<name> symlink
    whose recorded target still exists on this host. Never raises for a
    missing target (AC9)."""
    root = pathlib.Path(root)
    manifest_path = root / "content" / ".repos-manifest.json"
    out = []
    if not manifest_path.is_file():
        return out
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        return out
    repos_dir = root / "repos"
    for name, tgt in sorted(manifest.items()):
        if pathlib.Path(tgt).exists():
            repos_dir.mkdir(parents=True, exist_ok=True)
            link = repos_dir / name
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(tgt)
                out.append(f"{name}: recreated")
            except OSError as e:                               # noqa: BLE001
                out.append(f"{name}: skipped — {e}")
        else:
            out.append(f"{name}: skipped — target {tgt} absent on this host")
    return out


def _append_restore_verified(sha, event_count):
    """fold.append(), to the CALLING PROCESS's own live ledger — never under
    `root` (§5, AC7). L-drill-local.jsonl unless DOIT_LEDGER_FILE is already
    set (backup.sh's old convention)."""
    prev = os.environ.get("DOIT_LEDGER_FILE")
    if prev is None:
        os.environ["DOIT_LEDGER_FILE"] = "L-drill-local.jsonl"
    try:
        fold.append(["restore-verified", "-", f"snapshot_id={sha}",
                     f"event_count:={event_count}"])
    finally:
        if prev is None:
            os.environ.pop("DOIT_LEDGER_FILE", None)


def restore(snapshot, root=None, dry_run=False, remote=None, config_path=None):
    explicit_root = root is not None
    target = pathlib.Path(root) if explicit_root else fold.ROOT
    cfg = backup.load_config(config_path)
    use_remote = remote or cfg.get("remote")
    branch = cfg.get("branch") or "main"
    if not use_remote:
        raise Refusal("restore: no destination configured — set remote in "
                      "~/.config/do-it/backup.toml, or pass --remote")
    empty = _is_empty(target)
    if not empty and not (dry_run and explicit_root):
        raise Refusal(
            f"restore: refuses a non-empty target ({target}) unless --dry-run "
            "AND an explicit --root are both given — never the bare $DOIT_ROOT default")

    scratch = pathlib.Path(tempfile.mkdtemp(prefix="restore-"))
    try:
        clone_dir = scratch / "clone"
        cl = _git(["clone", "--quiet", "--branch", branch, "--", use_remote, str(clone_dir)],
                  scratch)
        if cl.returncode != 0:
            raise Refusal(f"restore: git clone failed — {cl.stderr.strip()}")
        if snapshot != "latest":
            co = _git(["checkout", "--quiet", snapshot], clone_dir)
            if co.returncode != 0:
                raise Refusal(f"restore: git checkout {snapshot} failed — {co.stderr.strip()}")
        sha = _git(["rev-parse", "HEAD"], clone_dir).stdout.strip()
        tree = _git(["ls-tree", "-r", "--name-only", "HEAD"], clone_dir).stdout.splitlines()
        event_count = len([p for p in tree if p.startswith("events/")])
        if empty:
            target.mkdir(parents=True, exist_ok=True)
            for item in clone_dir.iterdir():
                shutil.move(str(item), str(target / item.name))
            recreate_repo_links(target)
        _append_restore_verified(sha, event_count)
        return {"root": str(target), "snapshot_sha": sha, "dry_run": dry_run}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ── board_diff (ADR-0028-4) ───────────────────────────────────────────────────
# Wave-1 entries only. Wave-3's `board-shows-each-signal-as-itself` extends this
# constant with its own "last mirror push age" line — never a second one.
VOLATILE = (
    ("header", re.compile(r"^# board · .*$"), "drop"),
    ("live_panes_header", re.compile(r"^## LIVE PANES \(\d+\).*$"), "drop"),
    ("live_panes_row", re.compile(r"^  .* · last event .+$"), "drop"),
    ("live_panes_unavailable", re.compile(r"^  unavailable — .*$"), "drop"),
    ("spend_header", re.compile(r"^## SPEND \(\d+\).*$"), "drop"),
    ("spend_row", re.compile(r"^  .* · \d+ spawns.*$"), "drop"),
    ("spend_unmeasured", re.compile(r"^  unmeasured: \d+ spawn\(s\) carry no usage at all$"),
     "drop"),
    ("health_spend", re.compile(r"^  spend · .*$"), "drop"),
    ("health_restore_drill", re.compile(r"^  restore drill: .*$"), "drop"),
    ("health_last_tick", re.compile(r"^  last tick: .*$"), "drop"),
    ("day_age", re.compile(r" · \d+\.\d+d"), "normalize"),
    # Reserved, inert today (ADR-0028-4): the future HEALTH line naming the
    # mirror's own push age is itself a clock-derived value and belongs here
    # the day it exists, not in a second constant.
    ("mirror_push_age_reserved", re.compile(r"^  last mirror push age: .*$"), "drop"),
)


def _normalize(text):
    out = []
    for line in text.splitlines():
        dropped = False
        for _name, pattern, mode in VOLATILE:
            if mode == "drop" and pattern.match(line):
                dropped = True
                break
            if mode == "normalize":
                line = pattern.sub(" · <AGE>d", line)
        if not dropped:
            out.append(line)
    return "\n".join(out)


def _fold_env(root):
    return {**os.environ, "DOIT_ROOT": str(root)}


def _states(root):
    r = subprocess.run([sys.executable, str(FOLD_PY), "states"],
                       capture_output=True, text=True, env=_fold_env(root))
    return dict(line.split("\t", 1) for line in r.stdout.splitlines() if "\t" in line)


def _render(root):
    r = subprocess.run([sys.executable, str(FOLD_PY)],
                       capture_output=True, text=True, env=_fold_env(root))
    return r.stdout


def board_diff(live_root, restored_root):
    """[] when both roots derive the same board (ADR-0028-4's full test:
    fold.states equality PLUS render() byte-equal after VOLATILE), else one
    string per differing line/state key. Read-only: no subprocess is given
    `append`."""
    diffs = []
    ls, rs = _states(live_root), _states(restored_root)
    for k in sorted(set(ls) | set(rs)):
        if ls.get(k) != rs.get(k):
            diffs.append(f"state {k}: live={ls.get(k, 'ABSENT')} restored={rs.get(k, 'ABSENT')}")
    lb, rb = _normalize(_render(live_root)), _normalize(_render(restored_root))
    if lb != rb:
        import difflib
        diffs.append("board render differs after VOLATILE normalization:")
        diffs.extend(difflib.unified_diff(lb.splitlines(), rb.splitlines(), lineterm=""))
    return diffs


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="restore")
    ap.add_argument("snapshot")
    ap.add_argument("--root")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--remote")
    a = ap.parse_args(argv)
    try:
        r = restore(a.snapshot, root=a.root, dry_run=a.dry_run, remote=a.remote)
    except Refusal as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
