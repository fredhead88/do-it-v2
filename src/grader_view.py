#!/usr/bin/env python3
"""grader_view.py — L-spec-0426, charter L-charter-0042 R8.

Four pure library functions a later dispatch step (`grader-dispatch-view`,
`grader-pane-serve`, both wave 2) uses to run a grader pane inside a real
`bwrap` sandbox whose filesystem view holds only one grader spawn's build
artifacts, never `$HOME`, `~/.claude`, `$DOIT_ROOT` (seat included), any
repository, or any `.git`:

    build(spec, repo, base_sha, ready_sha, spawn) -> pathlib.Path
    config_dir() -> pathlib.Path
    bwrap_argv(view, *, doit_src, venvs=()) -> list[str]
    smoke(view) -> dict

No CLI, no ledger event, no import of `packet`/`dispatch` (see the spec's
Boundaries / Out-of-scope). Writes only under `scratch.sub("grade")/<spawn>/`
and `scratch.sub("grader-claude")`.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
from datetime import datetime, timezone

# Same DOIT_ROOT-env idiom as src/packet.py:21-22 and src/dispatch.py:386
# (Assumption 1) — no import of either module, to avoid a cycle.
ROOT = pathlib.Path(os.environ.get("DOIT_ROOT", pathlib.Path.home() / ".do-it"))
CONTENT = ROOT / "content"
SEAT = ROOT / "seat"


def _scratch_sub(name: str) -> pathlib.Path:
    """`scratch-root` (sibling wave-1 unit, `src/scratch.py`) is not merged in
    this tree yet — a minimal local stub for the one call site this module
    needs (Assumption 2), using the identical DOIT_SCRATCH-env idiom the
    constants above use for DOIT_ROOT. Default matches AC4's own worked
    example (`$HOME/doit-scratch/grade/<spawn>`)."""
    root = pathlib.Path(os.environ.get("DOIT_SCRATCH", pathlib.Path.home() / "doit-scratch"))
    p = root / name
    p.mkdir(parents=True, exist_ok=True)
    return p


_PATH_TOKEN_RE = re.compile(r"(?<![\w.-])(/[^\s'\"]+)")


def _rewrite_repo_paths(text: str, repo, tree_dir, venv_rel) -> str:
    """L-spec-0481/AC7 (SD-R12-3a): rewrites a `repo`-absolute path in the
    verify script to the view-relative path under `tree_dir` — a
    checkout-relative absolute path is otherwise unreachable from inside
    `bwrap_argv`'s sandbox, which never binds the checkout at all. Per
    occurrence, not a blind substring replace: (a) the `repo` argument's own
    symlink form AND its resolved target both count (a symlinked
    `ROOT/repos/<project>` repo argument, verify naming the resolved path);
    (b) a path under the project's configured `venv` is left byte-identical —
    it is bound into the sandbox separately, never copied into `tree_dir`;
    (c) a path that does not actually exist under `tree_dir` (deleted,
    gitignored, or simply foreign) is left as-is, so `_prove_view_paths`
    catches it as a failure rather than pointing at nothing."""
    tree_s = str(pathlib.Path(tree_dir))
    repo_p = pathlib.Path(repo)
    variants = [str(repo_p)]
    try:
        resolved = str(repo_p.resolve())
        if resolved not in variants:
            variants.append(resolved)
    except OSError:
        pass
    venv_prefixes = [f"{v}/{venv_rel}" for v in variants] if venv_rel else []

    def repl(m):
        token = m.group(1)
        for v in variants:
            if token == v or token.startswith(v + "/"):
                if any(token == vp or token.startswith(vp + "/") for vp in venv_prefixes):
                    return token
                candidate = tree_s + token[len(v):]
                return candidate if pathlib.Path(candidate).exists() else token
        return token

    return _PATH_TOKEN_RE.sub(repl, text)


def _rewrite_verify(text: str, repo, tree_dir, view, venv_rel=None) -> bytes:
    """Applies `_rewrite_repo_paths` (AC7), then inserts ONE static line,
    first thing after the shebang and before the script's own `set -e...`
    (so a `grading.env` not yet written, or written empty, never trips
    `set -e` on a false `[ -s ... ]`): a conditional source of
    `<view>/grading.env` when it exists and is non-empty (R12.6/AC9), THEN an
    unconditional `export HOME=<view>/home DOIT_ROOT=<view>/.doit` (R8.5/
    AC9) — so every check `verify.sh` runs sees the VIEW's own HOME/DOIT_ROOT,
    never the caller's, whether or not `grading.env` is present, and whether
    or not it sets its own HOME/DOIT_ROOT (this export runs after the
    conditional source, so it always wins). A verify script naming no
    checkout-absolute path and run before any `grading.env` exists comes back
    byte-identical but for that one inserted line."""
    rewritten = _rewrite_repo_paths(text, repo, tree_dir, venv_rel)
    lines = rewritten.split("\n", 1)
    shebang, rest = (lines[0], lines[1]) if rewritten.startswith("#!") else ("", rewritten)
    source_line = (f'[ -s "{view}/grading.env" ] && . "{view}/grading.env"; '
                   f'export HOME="{view}/home" DOIT_ROOT="{view}/.doit"')
    body = "\n".join([source_line, rest]) if rest else source_line
    out = f"{shebang}\n{body}" if shebang else body
    return out.encode()


def build(spec: str, repo, base_sha: str, ready_sha: str, spawn: str) -> pathlib.Path:
    """Populate `scratch.sub("grade")/<spawn>/` per the spec's Boundaries and
    return it. Raises — never writes a placeholder — when the spec/verify
    inputs are missing, or a git subprocess against `repo` fails."""
    spec_path = CONTENT / f"{spec}.md"
    verify_path = CONTENT / f"verify-{spec}-grader.sh"
    if not spec_path.is_file():
        raise FileNotFoundError(f"grader_view.build: missing {spec_path}")
    if not verify_path.is_file():
        raise FileNotFoundError(f"grader_view.build: missing {verify_path}")

    repo = pathlib.Path(repo)
    view = _scratch_sub("grade") / spawn
    view.mkdir(parents=True, exist_ok=True)

    # R8.5/AC9: the isolated HOME/DOIT_ROOT every `verify.sh` check runs
    # under — `_rewrite_verify`'s inserted line exports these two paths.
    (view / "home").mkdir(parents=True, exist_ok=True)
    (view / ".doit").mkdir(parents=True, exist_ok=True)

    tree_dir = view / "tree"
    if tree_dir.exists():
        shutil.rmtree(tree_dir)
    tree_dir.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", ready_sha], stdout=subprocess.PIPE, check=True
    )
    subprocess.run(["tar", "-x", "-C", str(tree_dir)], input=archive.stdout, check=True)

    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", f"{base_sha}..{ready_sha}"],
        stdout=subprocess.PIPE,
        check=True,
    )
    (view / "diff.patch").write_bytes(diff.stdout)

    (view / "spec.md").write_bytes(spec_path.read_bytes())

    verify_dst = view / "verify.sh"
    import grading_env  # project name = repo's own basename (ROOT/repos/<project> convention)
    venv_rel = grading_env.project_venv(repo.name)
    verify_dst.write_bytes(_rewrite_verify(verify_path.read_text(), repo, tree_dir, view, venv_rel=venv_rel))
    verify_dst.chmod(0o755)

    seat_dir = view / "seat"
    seat_dir.mkdir(parents=True, exist_ok=True)
    live_packet = SEAT / f"{spawn}.packet.md"
    if live_packet.is_file():
        (seat_dir / f"{spawn}.packet.md").write_bytes(live_packet.read_bytes())

    view_json = {
        "spec": spec,
        "base_sha": base_sha,
        "ready_sha": ready_sha,
        "spawn": spawn,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (view / "view.json").write_text(json.dumps(view_json))

    return view


def config_dir() -> pathlib.Path:
    """The shared, per-box Claude config directory every sandboxed grader
    pane binds rw (SD8). Idempotent: only the `agents/grader.md` link, the
    credentials copy, and settings/first-run state are (re)written on each
    call — a `projects/` subtree a live pane has already seeded is never
    touched."""
    d = _scratch_sub("grader-claude")
    try:
        d.chmod(0o700)
    except PermissionError:
        pass

    agents_dir = d / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    grader_md_src = pathlib.Path(__file__).resolve().parents[1] / "agents" / "grader.md"
    grader_md_dst = agents_dir / "grader.md"
    if grader_md_dst.is_symlink() or grader_md_dst.exists():
        grader_md_dst.unlink()
    grader_md_dst.symlink_to(grader_md_src)

    # ~/.claude resolves through pathlib.Path.home() (the HOME env var, same
    # idiom as ROOT above) — a fixture HOME redirects this read with no extra
    # parameter (Assumption 6). A missing source is not a config_dir()
    # failure (Assumption 6): it just degrades to "no credentials copied".
    creds_src = pathlib.Path.home() / ".claude" / ".credentials.json"
    if creds_src.is_file():
        creds_dst = d / ".credentials.json"
        creds_dst.write_bytes(creds_src.read_bytes())
        creds_dst.chmod(0o600)

    # Minimal first-run state so a non-interactive `claude --agent grader`
    # launch (grader-pane-serve, wave 2) does not block on onboarding.
    settings_path = d / "settings.json"
    settings_path.write_text(json.dumps({"hasCompletedOnboarding": True}) + "\n")

    return d


def _claude_launcher():
    """The `claude` launcher path and the real file it resolves to (on this
    box, `/home/albert/.local/bin/claude` -> a versioned single-file build
    under `/home/albert/.local/share/claude/versions/`)."""
    exe = shutil.which("claude") or str(pathlib.Path.home() / ".local" / "bin" / "claude")
    resolved = str(pathlib.Path(exe).resolve())
    return exe, resolved


def bwrap_argv(view, *, doit_src, venvs: tuple = (), binds: tuple = ()) -> list:
    """A true allowlist per SD8 (AC4): binds only `/usr`, `/bin`, `/lib`,
    `/lib64`, `/etc`, the claude install dir + launcher, `doit_src`'s own
    `src/`, `agents/`, `scripts/` and `doit` (individually, never the bare
    checkout root), any path in `venvs`, every `(src, dst)` pair in `binds`
    (L-spec-0481/R12.2 — `grading_env.sandbox_binds`'s own node_modules/venv
    pairs, always `--ro-bind`, never `--bind`), `view` and `config_dir()`'s
    own directory (rw) — nothing else. `$HOME`, `~/.claude`, `$DOIT_ROOT`
    (seat included), any repository, and any `.git` are simply absent."""
    view = pathlib.Path(view)
    doit_src = pathlib.Path(doit_src)
    cfg = config_dir()
    launcher, resolved = _claude_launcher()

    ro_pairs = [
        ("/usr", "/usr"),
        ("/bin", "/bin"),
        ("/lib", "/lib"),
        ("/lib64", "/lib64"),
        ("/etc", "/etc"),
        (launcher, launcher),
        (resolved, resolved),
        (str(doit_src / "src"), str(doit_src / "src")),
        (str(doit_src / "agents"), str(doit_src / "agents")),
        (str(doit_src / "scripts"), str(doit_src / "scripts")),
        (str(doit_src / "doit"), str(doit_src / "doit")),
    ]
    for v in venvs:
        ro_pairs.append((str(v), str(v)))
    for src, dst in binds:
        ro_pairs.append((str(src), str(dst)))

    seen, ro = set(), []
    for pair in ro_pairs:
        if pair not in seen:
            seen.add(pair)
            ro.append(pair)

    # `--tmpfs /tmp` (and /dev, /proc) go FIRST: `view`/`cfg` legitimately
    # live under `$HOME/doit-scratch`, which on this box resolves under
    # `/tmp` for a test fixture's `tempfile.mkdtemp()` — a later mount always
    # wins at its own target path, so their binds must come AFTER the tmpfs
    # mount or it silently shadows them (`bwrap: Can't chdir ... No such
    # file or directory`, observed).
    argv = ["bwrap", "--unshare-all", "--share-net", "--die-with-parent"]
    argv += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    for src, dst in ro:
        argv += ["--ro-bind", src, dst]
    argv += ["--bind", str(view), str(view)]
    argv += ["--bind", str(cfg), str(cfg)]
    argv += ["--setenv", "CLAUDE_CONFIG_DIR", str(cfg)]
    argv += ["--setenv", "HOME", str(view)]
    argv += ["--setenv", "PATH", "/usr/bin:/bin"]
    argv += ["--chdir", str(view)]
    return argv


_NAMESPACE_REFUSAL = ("permission", "namespace")


def _sandbox_unavailable(proc) -> bool:
    """True when `bwrap` itself refused to start (already nested, no
    unprivileged userns) rather than the *command* inside it failing —
    e.g. `bwrap: No permissions to create a new namespace, likely because
    the kernel does not allow non-privileged user namespaces.` (observed,
    exit 1)."""
    stderr = (proc.stderr or "").lower()
    return proc.returncode == 1 and all(tok in stderr for tok in _NAMESPACE_REFUSAL)


def smoke(view) -> dict:
    """Run `bwrap_argv`'s own argv for real on this box and report four
    discriminating checks (AC5). `skipped` names any key this environment
    could not exercise (missing `bwrap`, or a nested-sandbox refusal) —
    those keys are set to `None` rather than asserted."""
    view = pathlib.Path(view)
    keys = ("card_path_unreadable", "git_unreachable", "verify_started", "claude_version_ok")
    result = {k: None for k in keys}
    result["skipped"] = []

    if shutil.which("bwrap") is None:
        result["skipped"] = list(keys)
        return result

    # smoke() always tests build()'s own output, never a caller-supplied
    # doit_src (fix-list item 5) — this checkout's own root, fixed.
    doit_src = pathlib.Path(__file__).resolve().parents[1]
    argv = bwrap_argv(view, doit_src=doit_src, venvs=())

    def run(cmd):
        return subprocess.run(argv + ["--"] + cmd, capture_output=True, text=True)

    r = run(["/bin/ls", str(CONTENT)])
    if _sandbox_unavailable(r):
        result["skipped"].append("card_path_unreadable")
    else:
        result["card_path_unreadable"] = r.returncode != 0

    r1 = run(["git", "-C", str(doit_src), "log", "-1"])
    r2 = run(["git", "-C", str(ROOT), "log", "-1"])
    if _sandbox_unavailable(r1) or _sandbox_unavailable(r2):
        result["skipped"].append("git_unreachable")
    else:
        result["git_unreachable"] = r1.returncode != 0 and r2.returncode != 0

    verify_path = view / "verify.sh"
    r = run([str(verify_path)])
    if _sandbox_unavailable(r):
        result["skipped"].append("verify_started")
    else:
        # "Started" = not a not-executable/not-found failure AND it produced
        # some stdout before whatever happened next (the marker line an
        # AC5-fixture verify.sh prints first) — a script that errors out
        # before reaching its first real line does not count.
        result["verify_started"] = r.returncode not in (126, 127) and bool((r.stdout or "").strip())

    _, resolved = _claude_launcher()
    r = run([resolved, "--version"])
    if _sandbox_unavailable(r):
        result["skipped"].append("claude_version_ok")
    else:
        result["claude_version_ok"] = r.returncode == 0 and bool((r.stdout or "").strip())

    return result
