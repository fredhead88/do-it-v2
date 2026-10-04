#!/usr/bin/env python3
"""grading_env — L-spec-0481, charter L-charter-0042 R12(a, b-entry, c-entry).

Derives which capabilities a grader/reviewer's criteria need (`required`),
provisions and proves only those in the spawn's own `view/` (never the
shared checkout), and reports every one that could not be proven so
dispatch refuses before any spend. No CLI, no ledger event (dispatch's job).

`required` reads only its text args, the `grading.toml` row and `look.toml`'s
`[[prod]]` rows — no subprocess. `db_env`/`db_setup`/`venv`/`node_dirs` come
only from `grading.toml`; spec/verify text contributes capability NAMES
only, never an argv element. `_run` is the one subprocess indirection every
call here goes through; tests monkeypatch it (mirrors `grader_serve.py`'s
own injected `runner`).
"""
import json, os, pathlib, re, shutil, subprocess, tomllib, urllib.parse  # noqa: E401
HERE = pathlib.Path(__file__).resolve().parent
DOIT_SRC = HERE.parent
ROOT = pathlib.Path(os.environ.get("DOIT_ROOT", pathlib.Path.home() / ".do-it"))
CONTENT = ROOT / "content"

CAPABILITIES = ("env-file", "view-paths", "tools", "node_modules", "git",
                "db", "db-schema", "browser", "review-account", "deploy")
# SD-R12-6a: the two capabilities whose hold is scoped to one spec, never shared.
SPEC_LOCAL = frozenset({"view-paths", "git"})
# L-spec-8034/R12.g: capabilities a grader never has, by construction (R8's
# git-less invariant, no browser, no deploy target) — a criterion needing one
# of these routes to the reviewer or to owed instead of holding the grade.
GRADER_NEVER = ("browser", "deploy", "git")

# Unset in the environment of every process this module starts (Constraints).
PROD_DSN_VARS = ("SUPABASE_DB_URL", "SUPABASE_DB_URL_DIRECT",
                  "SUPABASE_DB_URL_RO", "SUPABASE_DB_URL_MIGRATIONS")
_PG_BIN = pathlib.Path("/usr/lib/postgresql/17/bin")
def _run(argv, **kw): return subprocess.run(argv, **kw)


_TOOL_TOKENS = {"python3", "python", "bash", "node", "alembic", "pytest"}
_NODE_TOKENS = {"npm", "npx", "node", "vitest", "tsc"}
_ABS_PATH_RE = re.compile(r'(?<![\w.:/-])(/[^\s\'"]+)')  # ':' and '/' excluded so a URL's //host is not a path
_GIT_RE = re.compile(r'(?<![\w-])git(?![\w-])')
_GIT_HISTORY_RE = re.compile(r'\bgit\b[^\n]*\b(merge-base|log|show|rev-list|diff|blame|cat-file|--is-ancestor)\b')
_DB_TOKENS = ("live_db", "DB_URL")
# L-spec-8034/R12.j: narrowed to tokens that NAME the deployed system — never
# the bare prose words `origin`/`deployed`, which a Constraints paragraph or a
# rollback note uses without meaning "this criterion needs a live deploy".
_DEPLOY_TOKENS = ("/version", "deployed at")
_BROWSER_TOKENS = ("log in as", "go to /")
_AC_ANCHOR = re.compile(r"^\s*\**AC(\d+)\s*\[")  # same shape packet._AC_ANY matches
_HEADING_RE = re.compile(r'^\s*#+\s')  # a `##` section heading always closes the current block


def _git_need(text):
    """Assumption 4: `"git"` (the builder branch's own history/ancestry — `git`
    followed by `merge-base`/`log`/`show`/`rev-list`/`diff`/`blame`/`cat-file`/
    `--is-ancestor`, or any `$BASE`/`BASE` reference in a segment that also
    names `git`) vs `"tools"` (the bare program, e.g. a test's own `git init`
    in a tmp dir) vs `None` (no `git` word at all) for one block of text."""
    if not _GIT_RE.search(text):
        return None
    based = text.replace("$BASE", "BASE")
    if _GIT_HISTORY_RE.search(based) or re.search(r'\bgit\b[^\n]*\bBASE\b', based):
        return "git"
    return "tools"


def _tokens_tools_node(text):
    tokens = re.findall(r"[A-Za-z0-9_./-]+", text)
    basenames = {t.rsplit("/", 1)[-1] for t in tokens}
    found = set()
    if basenames & _TOOL_TOKENS:
        found.add("tools")
    if basenames & _NODE_TOKENS:  # returned regardless of node_dirs (Assumption 2)
        found.add("node_modules")
    return found


def _caps_plain(text):
    """`env-file`/`view-paths`/`tools`/`node_modules`/`db`/`db-schema`/
    `browser`/`review-account` — the non-git, non-deploy token rules, applied
    to ONE block of text (Assumption 7): a criterion's own block, or one
    `&&`-split verify-script segment."""
    found = _tokens_tools_node(text)
    if ".env" in text:
        found.add("env-file")
    if _ABS_PATH_RE.search(text):
        found.add("view-paths")
    if any(tok in text for tok in _DB_TOKENS):
        found.update(("db", "db-schema"))
    if any(tok in text for tok in _BROWSER_TOKENS):
        found.update(("browser", "review-account"))
    return found


def _deploy_match(text, project):
    """R12.j: `/version`, `"deployed at"`, or the project's own `[[prod]]
    base_url` — never the bare words `origin`/`deployed` alone."""
    if any(tok in text for tok in _DEPLOY_TOKENS):
        return True
    base_url = next((p.get("base_url") for p in _look_toml(DOIT_SRC).get("prod", [])
                     if p.get("project") == project), None)
    return bool(base_url and base_url in text)


def _criterion_blocks(spec_text):
    """`{criterion_id: block_text}` — the same `AC<n> [` anchor/grouping
    `routing.group_blocks`/`packet._drop_owed_blocks` use (Assumption 3): a
    block runs from its anchor line through the line before the next anchor;
    text before the first anchor (and any Constraints/rollback_path/
    security_path prose after the last one, since nothing there re-anchors)
    belongs to no criterion and contributes nothing."""
    lines = (spec_text or "").splitlines()
    blocks, cur_id, cur = {}, None, []

    def flush():
        if cur_id is not None:
            blocks[cur_id] = "\n".join(cur)
    for l in lines:
        m = _AC_ANCHOR.match(l)
        if m:
            flush()
            cur_id, cur = f"AC{m.group(1)}", [l]
        elif _HEADING_RE.match(l):  # a `##` heading (Constraints/rollback_path/
            flush()                # security_path/…) always closes the block —
            cur_id, cur = None, []  # nothing after it re-anchors without a fresh AC<n>
        elif cur_id is not None:
            cur.append(l)
    flush()
    return blocks


def criterion_caps(spec_text, verify_text, project):
    """R12.f: `{criterion_id or "verify": [capabilities, CAPABILITIES order]}`,
    derived from each criterion's own block (git scoped to its `review_path:`
    line(s), matching the old spec-level `_review_path_git` rule but now
    per-criterion) plus verify-script segments (split on `&&`, Assumption 5)
    attributed to the `AC<n>` they name, else to the pseudo-criterion
    `"verify"` — present only when a segment actually yields a capability.
    Pure: no subprocess; `look.toml` read only for the project's own
    `[[prod]] base_url`."""
    out = {}
    for cid, text in _criterion_blocks(spec_text).items():
        found = _caps_plain(text)
        git = _git_need(text)
        if git and any(_GIT_RE.search(l) for l in text.splitlines() if "review_path:" in l):
            found.add(git)
        if _deploy_match(text, project):
            found.add("deploy")
        if found:
            out[cid] = found

    vtext = (verify_text or "").replace("$BASE", "BASE")
    for seg in (vtext.split("&&") if vtext else []):
        found = _caps_plain(seg)
        git = _git_need(seg)
        if git:
            found.add(git)
        if _deploy_match(seg, project):
            found.add("deploy")
        if not found:
            continue
        m = re.search(r'\bAC(\d+)\b', seg)
        target = f"AC{m.group(1)}" if m else "verify"
        out[target] = out.get(target, set()) | found

    return {cid: [c for c in CAPABILITIES if c in caps] for cid, caps in out.items()}


def route(spec_text, verify_text, project):
    """R12.g/AC5: `{criterion_id: (capability, "reviewer"|"owed")}` for every
    grader criterion (or the pseudo-criterion `"verify"`) whose block needs a
    `GRADER_NEVER` capability. Browser and deploy route to `"reviewer"` when
    the project has a `[[prod]]` row, to `"owed"` when it has none; `git`
    always routes to `"owed"` (nothing is deployed to drive a git-less grader
    into having history). A criterion needing several `GRADER_NEVER`
    capabilities routes on the first in `GRADER_NEVER` order (Assumption 6).
    Pure: text and `look.toml` only."""
    has_prod = any(p.get("project") == project for p in _look_toml(DOIT_SRC).get("prod", []))
    out = {}
    for cid, caps in criterion_caps(spec_text, verify_text, project).items():
        hit = next((c for c in GRADER_NEVER if c in caps), None)
        if hit is None:
            continue
        to = "owed" if hit == "git" else ("reviewer" if has_prod else "owed")
        out[cid] = (hit, to)
    return out


def required(role, spec_text, verify_text, project, *, owed=frozenset(), routed=frozenset(),
             mcp_config=False, review_account=False):
    """Pure/textual subsequence of `CAPABILITIES`. Reviewer needs are
    conditional (SD-R12-2a/Assumption 10) — never a blanket three; the
    reviewer branch is unchanged by this unit. The grader branch is now the
    union over the role's criteria (`criterion_caps`, including the pseudo-
    criterion `"verify"`) minus any criterion id in `owed` or `routed` —
    spec-level derivation is gone (R12.f)."""
    if role == "reviewer":
        found = set()
        if mcp_config:
            found.add("browser")
        if review_account:
            found.add("review-account")
        if any(p.get("project") == project for p in _look_toml(DOIT_SRC).get("prod", [])):
            found.add("deploy")
        return [c for c in CAPABILITIES if c in found]
    skip = set(owed) | set(routed)
    found = set()
    for cid, caps in criterion_caps(spec_text or "", verify_text or "", project).items():
        if cid in skip:
            continue
        found.update(caps)
    return [c for c in CAPABILITIES if c in found]


def _toml_path():
    return pathlib.Path(os.environ.get("DOIT_GRADING_TOML", str(DOIT_SRC / "grading.toml")))


def _grading_toml():
    p = _toml_path()
    return tomllib.loads(p.read_text()) if p.is_file() else {}


def _project_row(project):
    return _grading_toml().get("project", {}).get(project, {})


def project_venv(project):
    """`grading.toml`'s `venv` row for `project`, or `None` (AC7)."""
    return _project_row(project).get("venv") or None


def _look_toml(repo):
    p = pathlib.Path(repo) / "look.toml" if repo else None
    return tomllib.loads(p.read_text()) if p and p.is_file() else {}


def _project_checkout(project):
    return ROOT / "repos" / project


def _spec_verify_text(subject, role):
    sp, vp = CONTENT / f"{subject}.md", CONTENT / f"verify-{subject}-{role}.sh"
    return (sp.read_text() if sp.is_file() else "", vp.read_text() if vp.is_file() else "")


def _clean_env(extra=None):
    env = {k: v for k, v in os.environ.items() if k not in PROD_DSN_VARS}
    if extra:
        env.update(extra)
    return env


def _prove_db(view):
    """Real private cluster via `scripts/private-pg.sh` -> (ok, reason, dsn)."""
    script = DOIT_SRC / "scripts" / "private-pg.sh"
    if not script.is_file():
        return False, f"missing {script}", None
    try:
        r = _run(["/bin/bash", str(script), str(pathlib.Path(view) / "pg")],
                  capture_output=True, text=True, env=_clean_env(), timeout=60)
    except Exception as exc:
        return False, f"private-pg.sh raised: {exc}", None
    if r.returncode != 0:
        return False, f"private-pg.sh exit {r.returncode}: {(r.stderr or '')[-200:]}", None
    m = re.search(r"SUPABASE_DB_URL='([^']*)'", r.stdout or "")
    return (True, None, m.group(1)) if m else (False, "no SUPABASE_DB_URL in private-pg.sh output", None)


def _write_env_file(tree, row, dsn):
    """AC4: one NAME=DSN line per `db_env` name, mode 0600; empty if no `dsn`."""
    path, names = pathlib.Path(tree) / ".env", (row.get("db_env") or [])
    path.write_text("".join(f"{n}={dsn}\n" for n in names) if (dsn and names) else "")
    path.chmod(0o600)


def _db_setup_argv(row, project):
    setup = row.get("db_setup") or {}
    venv = row.get("venv") or ""
    venv_path = str(_project_checkout(project) / venv) if venv else ""
    argv = [a.replace("{venv}", venv_path) for a in (setup.get("argv") or [])]
    return argv, setup.get("cwd", "."), venv_path


def _prove_db_schema(view, row, project, dsn):
    argv, cwd_rel, _ = _db_setup_argv(row, project)
    if not argv:
        return False, "no db_setup configured for this project"
    env = _clean_env({n: dsn for n in (row.get("db_env") or [])})
    try:
        r = _run(argv, cwd=str(pathlib.Path(view) / "tree" / cwd_rel), env=env,
                  capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return False, f"db_setup raised: {exc}"
    return (False, f"db_setup exit {r.returncode}: {(r.stderr or '')[-200:]}") if r.returncode else (True, None)


def _prove_view_paths(view):
    vs = pathlib.Path(view) / "verify.sh"
    text = vs.read_text() if vs.is_file() else ""
    # The whole view counts, not only view/tree: verify.sh sources the view's own
    # grading.env (AC7/AC11), which lives at the view root.
    inside = str(pathlib.Path(view)) + "/"
    bad = [t for t in _ABS_PATH_RE.findall(text)
           if not t.startswith(inside) and not (pathlib.Path(t).is_file() and os.access(t, os.X_OK))]
    if bad:
        return False, f"absolute path(s) outside the view, not an interpreter: {', '.join(sorted(set(bad))[:3])}"
    return True, None


def _resolve_interp(token, venv_path):
    if venv_path and (pathlib.Path(venv_path) / "bin" / token).is_file():
        return str(pathlib.Path(venv_path) / "bin" / token)
    # With no project venv, resolve against the sandbox's own PATH (pane_env's
    # /usr/bin:/bin), not the dispatcher's, which may put an unbound venv first.
    found = shutil.which(token, path="/usr/bin:/bin")
    if not found and token == "python":  # no bare `python` on this box; verify uses python3
        found = shutil.which("python3", path="/usr/bin:/bin")
    return found or token


def _bwrap_available():
    return shutil.which("bwrap") is not None


def sandbox_mode():
    """Which lane `_sandboxed_run` uses — dispatch's `sandbox=` field (AC10/AC21)."""
    return "bwrap" if _bwrap_available() else "host"


def _sandboxed_run(view, project, repo, argv):
    """Inside `bwrap_argv`'s sandbox, or on the host with `pane_env` (Assumption 6)."""
    if _bwrap_available():
        import grader_view
        wrap = grader_view.bwrap_argv(view, doit_src=DOIT_SRC, binds=sandbox_binds(view, project))
        try:
            r = _run(wrap + ["--"] + argv, capture_output=True, text=True, timeout=30)
        except Exception as exc:
            return False, f"sandboxed run raised: {exc}"
        return r.returncode == 0, ((r.stdout or "") + (r.stderr or ""))[-200:]
    try:
        r = _run(argv, capture_output=True, text=True, env=pane_env(view), timeout=30)
    except Exception as exc:
        return False, f"host run raised: {exc}"
    return r.returncode == 0, ((r.stdout or "") + (r.stderr or ""))[-200:]


def _prove_tools(view, project, repo, caps_text, venv_path):
    found = {t for t in _TOOL_TOKENS if re.search(rf'(?<![\w./-]){re.escape(t)}(?![\w-])', caps_text)}
    for token in sorted(found):
        ok, tail = _sandboxed_run(view, project, repo, [_resolve_interp(token, venv_path), "--version"])
        if not ok:
            return False, f"{token} --version failed: {tail}"
    return True, None


def _prove_node_modules(view, row, project):
    dirs = row.get("node_dirs") or []
    if not dirs:
        return False, "no node_dirs configured for this project"
    checkout, tree = _project_checkout(project), pathlib.Path(view) / "tree"
    for d in dirs:
        src = checkout / d / "node_modules"
        if not src.is_dir():
            return False, f"{src} does not exist in the checkout"
        dst = tree / d / "node_modules"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not (dst.is_symlink() or dst.exists()):
            dst.symlink_to(src)
    return True, None


def _prove_browser(mcp_config):
    if not mcp_config:
        return False, "no --mcp-config supplied for the browser proof"
    path = pathlib.Path(mcp_config)
    if not path.is_file():
        return False, f"--mcp-config {mcp_config!r} does not exist"
    try:
        cfg = json.loads(path.read_text())
    except ValueError as exc:
        return False, f"--mcp-config is not valid JSON: {exc}"
    return (True, None) if cfg.get("mcpServers") else (False, "--mcp-config carries no mcpServers")


def _prove_review_account(project):
    path = ROOT / f"review-account-{project}"
    return (True, None) if path.exists() else (False, f"{path} does not exist")


def _prove_deploy(project, repo):
    row = next((p for p in _look_toml(repo).get("prod", []) if p.get("project") == project), None)
    if row is None:
        return False, f"no [[prod]] row for project {project!r} in look.toml"
    ssh_target, version_path = row.get("ssh_target"), row.get("version_path")
    if not ssh_target or not version_path:
        return False, "the [[prod]] row is missing ssh_target/version_path"
    try:
        # version_path is a path ("/version"); curl needs the host in front of it,
        # or it exits 3 (malformed URL) on every call.
        url = f"{row.get('base_url', '').rstrip('/')}{version_path}"
        r = _run(["ssh", ssh_target, f"curl -fsS {url}"], capture_output=True, text=True, timeout=20)
    except Exception as exc:
        return False, f"deploy check raised: {exc}"
    return (True, None) if r.returncode == 0 else (False, f"deploy version check failed: exit {r.returncode}")


def _write_state(view, **kv):
    path = pathlib.Path(view) / ".grading_state.json"
    state = {}
    if path.is_file():
        try:
            state = json.loads(path.read_text())
        except ValueError:
            state = {}
    state.update(kv)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def preflight(role, subject, view, project, *, repo, mcp_config=None, owed=frozenset(), routed=frozenset()):
    """Provisions/proves every capability `required()` needs, in `view/` only.
    Returns `(capability, reason)` FAILURES only, in `CAPABILITIES` order.
    `owed`/`routed` (criterion ids) pass straight through to `required()`
    (R12.g/i) — this function reads no ledger itself; dispatch computes them."""
    view = pathlib.Path(view)
    tree = view / "tree"
    spec_text, verify_text = _spec_verify_text(subject, role)
    caps = required(role, spec_text, verify_text, project, owed=owed, routed=routed)
    row = _project_row(project)
    reasons = {}

    dsn = None
    if "db" in caps:
        ok, reason, dsn = _prove_db(view)
        if not ok:
            reasons["db"] = reason
    if "env-file" in caps:
        _write_env_file(tree, row, None if "db" in reasons else dsn)
    # A project with no db_setup row in grading.toml has no schema to migrate,
    # so no criterion of it can need a schema proof (same rule as deploy below).
    if "db-schema" in caps and row.get("db_setup"):
        if "db" in reasons:
            reasons["db-schema"] = f"db was not proven: {reasons['db']}"
        else:
            ok, reason = _prove_db_schema(view, row, project, dsn)
            if not ok:
                reasons["db-schema"] = reason
    if "view-paths" in caps:
        ok, reason = _prove_view_paths(view)
        if not ok:
            reasons["view-paths"] = reason

    venv = row.get("venv") or ""
    venv_path = str(_project_checkout(project) / venv) if venv else ""
    caps_text = f"{spec_text}\n{verify_text}"

    if "node_modules" in caps:
        ok, reason = _prove_node_modules(view, row, project)
        if not ok:
            reasons["node_modules"] = reason
    if "tools" in caps:
        ok, reason = _prove_tools(view, project, repo, caps_text, venv_path)
        if not ok:
            reasons["tools"] = reason
    if "git" in caps:  # SD-R12-3: detected, never provisioned (R8 git-less invariant) — always a failure
        reasons["git"] = "git is never provisioned into a grader view (R8 git-less invariant)"
    # Browser and review-account proofs apply only to a project with a
    # [[prod]] row: a project with no deployed site has no page for any
    # criterion to open (the same rule as deploy below).
    has_prod = any(p.get("project") == project for p in _look_toml(DOIT_SRC).get("prod", []))
    if "browser" in caps and has_prod:
        ok, reason = _prove_browser(mcp_config)
        if not ok:
            reasons["browser"] = reason
    if "review-account" in caps and has_prod:
        ok, reason = _prove_review_account(project)
        if not ok:
            reasons["review-account"] = reason
    # A project with no [[prod]] row in look.toml is never deployed, so no
    # criterion of it can need a live deploy; the reviewer branch of
    # required() already applies the same rule.
    if "deploy" in caps and any(p.get("project") == project
                                for p in _look_toml(DOIT_SRC).get("prod", [])):
        ok, reason = _prove_deploy(project, repo)
        if not ok:
            reasons["deploy"] = reason

    if role != "reviewer":  # Constraints: a reviewer's cwd is never written to, even this state file
        _write_state(view, venv=venv_path,
                     db_env_names=(row.get("db_env") or []) if dsn else [], dsn=dsn or "")
    return [(c, reasons[c]) for c in CAPABILITIES if c in reasons]


def sandbox_binds(view, project):
    """Extra ro `(src, dst)` pairs: `node_modules` symlink targets, the `venv`."""
    view, row, binds = pathlib.Path(view), _project_row(project), []
    for d in row.get("node_dirs") or []:
        target = view / "tree" / d / "node_modules"
        if target.exists() or target.is_symlink():
            resolved = str(target.resolve())
            binds.append((resolved, resolved))
    venv = row.get("venv")
    if venv:
        vpath = str(_project_checkout(project) / venv)
        binds.append((vpath, vpath))
    return binds


def pane_env(view):
    """The ONE env proof/`grading.env`/the pane agree on, rebuilt from state."""
    view = pathlib.Path(view)
    state = {}
    sp = view / ".grading_state.json"
    if sp.is_file():
        try:
            state = json.loads(sp.read_text())
        except ValueError:
            state = {}
    venv = state.get("venv") or ""
    env = {"HOME": str(view), "PATH": f"{venv}/bin:/usr/bin:/bin" if venv else "/usr/bin:/bin"}
    dsn = state.get("dsn")
    if dsn:
        for name in state.get("db_env_names") or []:
            env[name] = dsn
        parsed = urllib.parse.urlparse(dsn)
        qs = urllib.parse.parse_qs(parsed.query)
        env["PGHOST"] = (qs.get("host") or [""])[0]
        env["PGPORT"] = (qs.get("port") or [""])[0]
        env["PGUSER"] = parsed.username or "albert"
        env["PGDATABASE"] = parsed.path.lstrip("/") or "scratch"
    return env


def teardown(view):
    """Idempotent, never raises: stops the cluster under `view/pg` if started."""
    try:
        data = pathlib.Path(view) / "pg" / "data"
        if not data.is_dir():
            return None
        _run([str(_PG_BIN / "pg_ctl"), "-D", str(data), "-m", "fast", "stop"],
             capture_output=True, text=True, timeout=30)
    except Exception:
        pass
    return None
