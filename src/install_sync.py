#!/usr/bin/env python3
"""install_sync — agent-linking and the once-per-shipped-sha cron sync
(L-charter-0042 R5).

  install_sync.py link-agents        symlink every agents/*.md into ~/.claude/agents

`link_agents` is `install.sh`'s own agent-linking step, factored out of the
bare per-file loop that used to be the only place it existed (R5a) — the
destination defaults to `~/.claude/agents`, matching `up.AGENTS_HOME`'s own
value (duplicated here, not imported, so importing this module can never pull
in `up` -> `tick` -> this module again).

`run()` is `tick._record()`'s once-per-tick call (R5b). The FIRST tick with no
`install-synced` event anywhere in the ledger links every agent and records a
baseline sha, replaying no `shipped` history. Every tick after that walks the
`shipped` events for `project == "do-it-v2"` newer than the newest
`install-synced` and not already named by one, oldest first, and for each
one's sha installs (or, for a `where = "cron.d"` row, prints and briefs)
whatever `crons.toml` row that commit added or changed — `crons.install`'s own
contract installs HEAD's current text, never the historical one, so
`changed_rows` only NAMES which rows to act on.
"""
import os, pathlib, pwd, subprocess, sys
import tomllib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fold  # noqa: E402 — fold.ROOT/fold.ts/fold.NOW read live, at call time (crons.py's own precedent)


class LinkRefused(Exception):
    """Raised by `link_agents(agents_dir=None)` when `source` is not the
    canonical do-it-v2 checkout (R8.3). Refuse, never redirect (SD-R16-2):
    nothing is created or linked before this is raised."""


# ── R8.3: the one place that decides whether a checkout may hijack the real ──
# ── agent links ────────────────────────────────────────────────────────────

def _canonical(source):
    """True only when the resolved `source` equals the resolved canonical
    do-it-v2 tree: `<passwd home>/.do-it/repos/do-it-v2`, or the resolved
    `DOIT_CANONICAL_REPO` when that is set explicitly. The passwd home comes
    from `pwd.getpwuid(os.getuid()).pw_dir` — NEVER `HOME` or `DOIT_ROOT`
    (SD-R16-1 as amended) — so a test's own temp HOME or a temp `DOIT_ROOT`
    that happens to hold a `repos/do-it-v2` subtree cannot satisfy this. Pure:
    resolves both sides (symlinks followed), reads passwd and one env var,
    touches nothing."""
    resolved_source = pathlib.Path(source).resolve()
    override = os.environ.get("DOIT_CANONICAL_REPO")
    if override:
        return resolved_source == pathlib.Path(override).resolve()
    passwd_home = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
    canonical = (passwd_home / ".do-it" / "repos" / "do-it-v2").resolve()
    return resolved_source == canonical


class MachineInstallRefused(Exception):
    """A whole-machine preflight failed; no file may be installed."""

    def __init__(self, name, detail):
        self.name = name
        super().__init__(f"{name}: {detail}")


def install_machine(repo: str, *, root=None, crontab_path=None) -> list[str]:
    import machine, models
    root = pathlib.Path(root) if root is not None else models.PATH.parent
    if (crontab_path is None or root.resolve() == models.PATH.parent.resolve()) and not _canonical(repo):
        raise MachineInstallRefused("crontab.txt" if crontab_path is None else "models.toml",
                                    "real destination requires the canonical checkout")
    prepared = []
    # Validate BOTH files before creating even a directory or refreshing a marker.
    for name in sorted(machine.NAMES):
        saved = machine.read_text(pathlib.Path(repo) / "machine" / name)
        problem = machine.input_problem(saved, name)
        if problem:
            raise MachineInstallRefused(name, f"repo {problem.kind}: {problem.detail}")
        dest = root / "models.toml" if name == "models.toml" else crontab_path
        live = machine.read_text(dest) if dest is not None else machine.crontab_text()
        problem = machine.input_problem(live, name)
        if problem:
            raise MachineInstallRefused(name, f"live {problem.kind}: {problem.detail}")
        marker = root / "machine-last" / name
        previous = machine.read_text(marker)
        if live != saved and (previous is None or live != previous):
            raise MachineInstallRefused(name, "live differs from repo and " +
                                        ("last-installed marker is absent or unreadable" if previous is None
                                         else "live differs from last-installed marker"))
        prepared.append((name, dest, live, saved, marker))
    changed = []
    for name, dest, live, saved, marker in prepared:
        if live != saved:
            (root / (name + ".bak")).write_bytes(live.encode("utf-8"))
            if dest is not None:
                pathlib.Path(dest).write_bytes(saved.encode("utf-8"))
            else:
                import tempfile
                with tempfile.NamedTemporaryFile(prefix="machine-crontab-", dir=root) as staged:
                    staged.write(saved.encode("utf-8"))
                    staged.flush()
                    subprocess.run(["crontab", staged.name], check=True, capture_output=True)
            changed.append(name)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(saved.encode("utf-8"))
    return changed


# ── R5a: the one place agent-linking exists ──────────────────────────────────

def link_agents(agents_dir=None, *, source=None):
    """Symlink every `agents/*.md` in `source` (default `HERE.parent`, i.e.
    THIS repo) into `agents_dir` (default `~/.claude/agents`). With
    `agents_dir=None` — the real destination — refuses before creating
    anything (`LinkRefused`) unless `_canonical(source)` is true; an explicit
    `agents_dir` is never refused (R8.3). Creates `agents_dir` if absent. For
    each source file: an already-correct symlink (present, a symlink,
    resolving to that exact source) is left alone and uncounted; anything
    else (absent, not a symlink, or resolving elsewhere) is (re)linked.
    Returns the sorted names actually (re)linked."""
    source = HERE.parent if source is None else source
    if agents_dir is None and not _canonical(source):
        raise LinkRefused(
            f"install_sync: refusing to link {pathlib.Path(source).resolve()} "
            f"into ~/.claude/agents — not the canonical do-it-v2 checkout "
            f"(pass agents_dir= explicitly, or set DOIT_CANONICAL_REPO)")
    dest_dir = pathlib.Path(agents_dir) if agents_dir is not None else (pathlib.Path.home() / ".claude" / "agents")
    dest_dir.mkdir(parents=True, exist_ok=True)
    src_dir = pathlib.Path(source) / "agents"
    linked = []
    for src in sorted(src_dir.glob("*.md")):
        dest = dest_dir / src.name
        correct = False
        if dest.is_symlink():
            try:
                correct = dest.resolve() == src.resolve()
            except OSError:
                correct = False
        if correct:
            continue
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        dest.symlink_to(src)
        linked.append(src.name)
    return sorted(linked)


# ── R5b: which crons.toml rows a shipped sha added or changed ────────────────

def _crons_toml_at(repo, ref):
    """`{name: (schedule, command)}` for `crons.toml` at REF — `tomllib.loads`
    on `git show <ref>:crons.toml`, the same `[[row]]` shape `crons.rows`
    reads. `None` when the ref, or the path at it, is absent (a sha with no
    parent, or a commit that predates crons.toml)."""
    try:
        text = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:crons.toml"],
                              capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        return None
    doc = tomllib.loads(text)
    return {r["name"]: (r["schedule"], r["command"]) for r in doc.get("row", [])}


def changed_rows(repo, sha):
    """Names of every `crons.toml` row present at `sha` whose `(schedule,
    command)` pair differs from (or is absent at) `sha^1` — manifest order, a
    row removed between the two never returned. A `sha` with no parent, or
    whose `sha^1:crons.toml` is absent, counts every row at `sha` as changed."""
    cur = _crons_toml_at(repo, sha)
    if cur is None:
        return []
    parent = _crons_toml_at(repo, f"{sha}^1")
    if parent is None:
        return list(cur.keys())
    return [name for name, val in cur.items() if parent.get(name) != val]


def sync_realm(repo, sha):
    """Ship the tracked unit when it changes; otherwise restart changed Realm code."""
    import realm
    result = subprocess.run(['git', '-C', str(repo), 'diff-tree', '--root', '--no-commit-id',
                             '--name-only', '-r', '--diff-merges=first-parent', sha], capture_output=True, text=True, check=True)
    paths = result.stdout.splitlines()
    if 'ops/doit-realm.service' in paths:
        realm.install_realm(repo)
        return 'installed'
    if any(path.startswith('src/realm') for path in paths):
        realm.systemctl_user('restart', 'doit-realm')
        return 'restarted'
    return None


def _row_where(name, manifest):
    import crons
    for row in crons.rows(manifest=manifest):
        if row["name"] == name:
            return row["where"]
    raise ValueError(f"install_sync: no such crons.toml row {name!r}")


# ── R5b: the once-per-tick call ───────────────────────────────────────────────

def run(events, *, now=None, root=None, repo=None, agents_dir=None, manifest=None, crontab_path=None):
    """Once per tick. Every keyword defaults exactly as its own callee already
    defaults it and threads straight through, so a test supplying all five
    never touches the real `~/.claude/agents`, crontab, or `$DOIT_ROOT`
    ledger. `now` is accepted for the same call shape every other `run()` in
    this codebase carries; every timestamp actually recorded is
    `dispatch.emit`'s own, as everywhere else."""
    import crons, dispatch, look
    root = pathlib.Path(root) if root is not None else fold.ROOT
    repo = pathlib.Path(repo) if repo is not None else HERE.parent
    events = list(events or ())
    dst = root / "events" / "L-tick-installsync.jsonl"
    dst.parent.mkdir(parents=True, exist_ok=True)

    synced = [e for e in events if e.get("type") == "install-synced"]
    if not synced:
        linked = link_agents(agents_dir=agents_dir)
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
        dispatch.emit(dst, {}, "install-synced", sha=sha, baseline=True, linked=linked,
                      installed=[], print_only=[], subject="do-it-v2")
        return linked

    newest_ts = max(fold.ts(e.get("ts")) for e in synced)
    known_shas = {e.get("sha") for e in synced}
    shipped = sorted(
        [e for e in events if e.get("type") == "shipped" and e.get("project") == "do-it-v2"
         and fold.ts(e.get("ts")) > newest_ts and e.get("sha") not in known_shas],
        key=lambda e: fold.ts(e.get("ts")))

    machine_changes = None
    if shipped:
        try:
            machine_changes = install_machine(repo, root=root, crontab_path=crontab_path)
        except MachineInstallRefused as exc:
            look.emit_once("machine-install-refused", exc.name, "operator",
                           {"file": exc.name, "reason": str(exc)}, events, root=root)
    touched = []
    for e in shipped:
        sha = e.get("sha")
        names = changed_rows(repo, sha)
        installed, print_only, failed = [], [], []
        for name in names:
            where = _row_where(name, manifest)
            if where == "cron.d":
                print_only.append(name)
                look.emit_once("cron-row-needs-root", name, "operator",
                               {"line": crons.print_line(name, manifest=manifest)}, events, root=root)
            elif where == "user":
                try:
                    crons.install(name, manifest=manifest, crontab_path=crontab_path)
                    installed.append(name)
                except Exception as exc:
                    failed.append({"name": name, "error": str(exc)})
        kv = dict(sha=sha, linked=[], installed=installed, print_only=print_only, subject="do-it-v2")
        if e is shipped[-1] and machine_changes is not None:
            kv["machine"] = machine_changes
        try:
            action = sync_realm(repo, sha)
            if action:
                kv['realm'] = action
        except Exception as exc:
            detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
            failed.append({'name': 'doit-realm', 'error': detail})
        if failed:
            kv["failed"] = failed
        dispatch.emit(dst, {}, "install-synced", **kv)
        touched.extend(installed + print_only)
    return touched


# ── CLI ────────────────────────────────────────────────────────────────────

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "link-agents":
        try:
            for name in link_agents():
                print(name)
        except LinkRefused as e:
            print(str(e), file=sys.stderr)
            return 1
        return 0
    print("usage: install_sync.py link-agents", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
