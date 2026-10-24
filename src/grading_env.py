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
import json
import time
from datetime import datetime, timedelta, timezone
import shlex, os, pathlib, re, shutil, subprocess, tomllib, urllib.parse  # noqa: E401
import verification
HERE = pathlib.Path(__file__).resolve().parent
DOIT_SRC = HERE.parent
ROOT = pathlib.Path(os.environ.get("DOIT_ROOT", pathlib.Path.home() / ".do-it"))
CONTENT = ROOT / "content"
DEPLOY_LOCK = ROOT / "deploy.lock"
DAILY_RELEASE_LOCK = pathlib.Path("/var/lock/albert-scott-daily-release.lock")
PROC_LOCKS = pathlib.Path("/proc/locks")

CAPABILITIES = ("env-file", "view-paths", "tools", "node_modules", "git",
                "db", "db-schema", "browser", "review-account", "deploy")
# Every preflight gap belongs to the spec whose view failed its proof.
SPEC_LOCAL = frozenset(CAPABILITIES)
# L-spec-8034/R12.g: capabilities a grader never has, by construction (R8's
# git-less invariant, no browser, no deploy target) — a criterion needing one
# of these routes to the reviewer or to owed instead of holding the grade.
GRADER_NEVER = ("browser", "deploy", "git")

# Unset in the environment of every process this module starts (Constraints).
PROD_DSN_VARS = ("SUPABASE_DB_URL", "SUPABASE_DB_URL_DIRECT",
                  "SUPABASE_DB_URL_RO", "SUPABASE_DB_URL_MIGRATIONS")
_PG_BIN = pathlib.Path("/usr/lib/postgresql/17/bin")
def failure_kind(text, exit_code=1):
    """Unknown/missing classifiers fail closed; no dependency API is inferred."""
    try:
        from nextstep import classify_failure
        result = classify_failure(text, exit_code)
        return result if result in {"infra", "work", "unknown"} else "unknown"
    except Exception:
        return "unknown"


def infra_class(text):
    if re.search(r"No space left|ENOSPC|Errno 28|Out of diskspace", text, re.I):
        return "no-space"
    if re.search(r"capacity|overloaded|429|quota|limit", text, re.I):
        return "capacity"
    if re.search(r"worktree|work tree", text, re.I):
        return "worktree"
    return "runner-or-network"


def infra_fields(attempt, backend, text, *, clock=None):
    clock = clock or datetime.now(timezone.utc)
    return dict(attempt=attempt, backend=backend, **{"class": infra_class(text)},
                retry_at=(clock + timedelta(minutes=5)).isoformat(timespec="seconds"))


class InfraFailure(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__("infra:" + category)


def _run(argv, **kw):
    return subprocess.run(argv, **kw)


_TOOL_TOKENS = {"python3", "python", "bash", "node", "alembic", "pytest"}
_NODE_TOKENS = {"npm", "npx", "node", "vitest", "tsc"}
_ABS_PATH_RE = re.compile(r'(?<![\w.:/\]})-])(/[^\s\'":]+)')  # ':' '/' ']' '}' ')' excluded: a URL's //host or a route dir like [...proxy]/x is not a path
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
    """`env-file`/`tools`/`node_modules`/`db`/`db-schema`/
    `browser`/`review-account` — the non-git, non-deploy token rules, applied
    to ONE block of text (Assumption 7): a criterion's own block, or one
    `&&`-split verify-script segment."""
    found = _tokens_tools_node(text)
    if ".env" in text:
        found.add("env-file")
    if any(tok in text for tok in _DB_TOKENS):
        found.update(("db", "db-schema"))
    if any(tok in text for tok in _BROWSER_TOKENS):
        found.update(("browser", "review-account"))
    return found


def _executed_path_tokens(text):
    """Paths outside quoted literals and HTTP command endpoint arguments.

    Keep offsets while masking so removing a literal cannot join two tokens.
    File-valued HTTP options still name filesystem paths, not endpoints.
    """
    blank = lambda m: re.sub(r"[^\n]", " ", m.group())
    text = re.sub(r"'(?:[^']*)'|\"(?:\\.|[^\"\\])*\"", blank, text)
    text = re.sub(r"https?://[^\s'\"]+", blank, text)
    file_options = {"-o", "-O", "--output", "--output-document", "--output-file",
                    "-K", "--config", "-T", "--upload-file", "--cacert",
                    "--ca-certificate", "--cert", "--key", "--netrc-file",
                    "--cookie-jar", "--input"}

    def mask_endpoints(match):
        command = match.group()
        words = list(re.finditer(r"\S+", command))
        chars = list(command)
        previous = None
        for word in words:
            token = word.group()
            if (token.startswith("/") and previous not in file_options
                    and not re.fullmatch(r"\d*[<>]+", previous or "")):
                chars[word.start():word.end()] = " " * len(token)
            previous = token
        return "".join(chars)

    text = re.sub(r"(?<![\w/-])(?:/[^\s;&|]+/)?(?:curl|wget|gh\s+api)\b[^\n;&|]*",
                  mask_endpoints, text)
    return [_clean_path_token(_trim_unmatched_closer(t)) for t in _ABS_PATH_RE.findall(text)]


def _needs_view_paths(text, project):
    # Grader views rewrite a checkout's working directory to their own tree.
    # Only the exact root used by cd is neutral; files under it still count.
    roots = (f"/opt/{project}", f"/home/albert/.do-it/repos/{project}",
             str(_project_checkout(project)))
    for root in roots:
        text = re.sub(r"\bcd\s+" + re.escape(root) + r"/?(?=\s|[;&]|$)", "cd .", text)
    return any(t not in _NEUTRAL_DEVICES and _looks_like_fs_path(t)
               and not t.startswith(("/usr/bin/", "/bin/", "/opt/albert-scott/.venv/bin/"))
               for t in _executed_path_tokens(text))


def _review_commands(text):
    """Extract commands from review fields, leaving file references/prose alone."""
    commands = []
    for line in _review_paths(text).splitlines():
        line = re.sub(r"^\s*review_paths?:\s*", "", line).strip()
        snippets = re.findall(r"`([^`]+)`", line)
        for candidate in snippets or [line]:
            candidate = re.sub(r"^run\s+", "", candidate).strip()
            if re.match(r"(?:log in as|go to|it\b)", candidate):
                continue
            if re.match(r"(?:[\w./-]+)\s+\S", candidate):
                commands.append(candidate)
    return "\n".join(commands)


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


def _review_paths(text):
    """Only AC review_path fields (including wrapped continuation lines)."""
    lines, active = [], False
    for line in text.splitlines():
        if re.match(r"^\s*review_paths?:", line):
            active = True
        elif re.match(r"^\s*[\w-]+_path:|^\s*#+\s|^\s*AC\d+\s*\[", line):
            active = False
        if active:
            lines.append(line)
    return "\n".join(lines)


def _verification_text(spec_text, verify_text):
    """The spec's Verification block plus its extracted verification script."""
    return "\n".join((verification.block(spec_text) or "", verify_text or ""))


def _tool_texts(spec_text, verify_text):
    """Executable capability evidence, attributed exactly like criterion_caps."""
    out = {cid: _review_paths(text) for cid, text in _criterion_blocks(spec_text).items()}
    for seg in _verification_text(spec_text, verify_text).split("&&"):
        match = re.search(r"\bAC(\d+)\b", seg)
        cid = f"AC{match.group(1)}" if match else "verify"
        out[cid] = out.get(cid, "") + "\n" + seg
    return out


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
        found = _caps_plain(text) - {"tools", "node_modules"}
        if _needs_view_paths(_review_commands(text), project):
            found.add("view-paths")
        found.update(_tokens_tools_node(_review_paths(text)))
        git = _git_need(text)
        if git and any(_GIT_RE.search(l) for l in text.splitlines() if "review_path:" in l):
            found.add(git)
        if _deploy_match(text, project):
            found.add("deploy")
        if found:
            out[cid] = found

    vtext = _verification_text(spec_text, verify_text).replace("$BASE", "BASE")
    for seg in (vtext.split("&&") if vtext else []):
        found = _caps_plain(seg)
        if _needs_view_paths(seg, project):
            found.add("view-paths")
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
    Pure: text and `look.toml` only. These are capability routes; dispatch uses
    routing.diff_routes with standing rejections to decide actual ownership.
    Re-judging a rejection does not grant the grader new capabilities."""
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
             mcp_config=False, review_account=False, bundled=False):
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
    if role == "grader" and bundled:
        from grader_packet import EXECUTION_CAPABILITIES
        found.difference_update(EXECUTION_CAPABILITIES)
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
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("SUPABASE_DB_URL") or k.startswith("PG") or "DSN" in k or "DATABASE" in k
                   or k in ("AS_LOCAL_PG_ADMIN", "SCRATCH_PG_URL"))}
    if extra:
        env.update(extra)
    return env


def _prove_db(view, *, admin=False, avoid_port=None):
    import provision
    return provision._grading_db(view, admin=admin, avoid_port=avoid_port)


def _write_env_file(tree, row, dsn):
    """AC4: one NAME=DSN line per `db_env` name, mode 0600; empty if no `dsn`."""
    path, names = pathlib.Path(tree) / ".env", (row.get("db_env") or [])
    path.write_text("".join(f"{n}={dsn}\n" for n in names) if (dsn and names) else "")
    path.chmod(0o600)


def _env_file_error(row):
    names = row.get("env_vars", [])
    if not isinstance(names, list):
        return "env_vars must be a list of variable names"
    forbidden = set(PROD_DSN_VARS) | set(row.get("db_env") or []) | set(row.get("admin_env") or [])
    for name in names:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            return "env_vars contains an invalid variable name"
        if (re.search(r"KEY|TOKEN|SECRET|PASSWORD", name, re.I)
                or name in forbidden or name.upper().endswith(("_URL", "_DSN"))):
            return f"env_vars refuses secret or DSN name {name}"
    return None


def _env_values(text, names):
    """Read literal dotenv assignments; never expand or execute their values."""
    values = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)", line)
        if not match or match[1] not in names:
            continue
        value = match[2].strip()
        if value.startswith(("'", '"')):
            try:
                parts = shlex.split(value, comments=True)
            except ValueError:
                continue
            if len(parts) != 1:
                continue
            value = parts[0]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[match[1]] = value
    return values


def _provision_env_file(tree, row, dsn):
    """Provision only declared names, then prove the destination (no values in reasons)."""
    error = _env_file_error(row)
    if error:
        return False, error, {}
    if "env_file" not in row:
        _write_env_file(tree, row, dsn)
        return True, None, {}
    source = row["env_file"]
    if not isinstance(source, str) or not pathlib.Path(source).is_absolute():
        return False, "env_file must name an absolute source path", {}
    names = set(row.get("env_vars", []))
    try:
        values = _env_values(pathlib.Path(source).read_text(), names)
    except (OSError, UnicodeError):
        return False, f"env_file source {source} is missing or unreadable", {}
    missing = names - values.keys()
    if missing:
        return False, f"env_file source {source} lacks env_vars: {', '.join(sorted(missing))}", {}
    path = pathlib.Path(tree) / ".env"
    try:
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text("".join(f"{name}={shlex.quote(value)}\n" for name, value in values.items())
                        + ("".join(f"{name}={dsn}\n" for name in row.get("db_env", [])) if dsn else ""))
        proven = _env_values(path.read_text(), names)
    except (OSError, UnicodeError) as exc:
        if failure_kind(str(exc)) == "infra":
            raise InfraFailure(infra_class(str(exc))) from None
        return False, f"env_file destination {path} could not be written or read", {}
    missing = {name for name in names if name not in proven or proven[name] != values[name]}
    if missing:
        return False, f"env_file destination {path} lacks env_vars: {', '.join(sorted(missing))}", {}
    return True, None, values


def _db_setup_argv(row, project):
    setup = row.get("db_setup") or {}
    venv = row.get("venv") or ""
    venv_path = str(_project_checkout(project) / venv) if venv else ""
    argv = [a.replace("{venv}", venv_path) for a in (setup.get("argv") or [])]
    return argv, setup.get("cwd", "."), venv_path


def _prove_db_schema(view, row, project, dsn, *, admin_dsn=None):
    import provision
    return provision._grading_schema(view, row, project, dsn, admin_dsn=admin_dsn)


# L-spec-0667, 2026-10-05: shell command-substitution syntax around a path
# token (`test "$(wc -l < /path/to/file)" -le N`) is not excluded by
# `_ABS_PATH_RE`'s character class, so a trailing `)`/`]`/`}` with no
# matching opener inside the token rides along as if it were part of the
# path — a real interpreter path caught this way would fail the
# is_file()/X_OK check on account of a character the shell, not the path,
# put there. Same trim as `grader_view._trim_unmatched_closer` (duplicated,
# not imported, to avoid the mutual-import `grading_env`/`grader_view`
# already has elsewhere — `_sandboxed_run` imports `grader_view` lazily for
# the same reason).
_CLOSERS = {")": "(", "]": "[", "}": "{"}


def _trim_unmatched_closer(token):
    while token and token[-1] in _CLOSERS:
        closer = token[-1]
        if token.count(closer) <= token.count(_CLOSERS[closer]):
            break
        token = token[:-1]
    return token


# Thinker 2026-10-07: `/dev/null` (a redirect sink, e.g. `>/dev/null 2>&1`)
# reads nothing outside the view; refusing it wedged L-spec-0696 for 17 h.
_NEUTRAL_DEVICES = {"/dev/null"}


def _looks_like_fs_path(token):
    """Thinker 2026-10-12 (L-spec-0896): awk/sed patterns such as `/BEGIN`, `/#` or
    `/<<.CRON_SCRIPT.$/{o=NR}` matched the path regex and held a grade. A token is a
    filesystem path when its first component is a real top-level directory or looks
    like one (lowercase name characters); `/BEGIN`, `/#`, `/<<...` do not."""
    parts = token.lstrip("/").split("/")
    first = parts[0]
    if not first:
        return False
    if os.path.isdir("/" + first):
        return True
    # a single nonexistent component (`/rm`, `/BEGIN`) is a regex literal, not a path
    return len(parts) > 1 and re.fullmatch(r"[a-z0-9._-]+", first) is not None


def _clean_path_token(token):
    """Cut a token at a literal `\\n` (printf text) and drop shell punctuation glued to its end."""
    return token.split("\\n", 1)[0].rstrip(";,|&")


def _prove_view_paths(view):
    vs = pathlib.Path(view) / "verify.sh"
    text = vs.read_text() if vs.is_file() else ""
    # The whole view counts, not only view/tree: verify.sh sources the view's own
    # grading.env (AC7/AC11), which lives at the view root.
    inside = str(pathlib.Path(view)) + "/"
    bad = [t for t in _executed_path_tokens(text)
           if t != str(pathlib.Path(view)) and not t.startswith(inside) and t not in _NEUTRAL_DEVICES
           and _looks_like_fs_path(t)
           and not (pathlib.Path(t).is_file() and os.access(t, os.X_OK))]
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
    import provision
    return provision._grading_node(view, row, project)


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


def _release_lock_held(path):
    """Observe an exclusive flock by inode; never acquire a probe lock."""
    try:
        stat = path.stat()
        for line in PROC_LOCKS.read_text().splitlines():
            fields = line.split()
            if len(fields) < 6 or fields[1] != "FLOCK" or fields[3] != "WRITE":
                continue
            try:
                major, minor, inode = fields[5].split(":")
                identity = (int(major, 16), int(minor, 16), int(inode))
            except ValueError:
                continue
            if identity == (os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino):
                return True
    except (OSError, UnicodeError):
        pass
    return False


def release_in_progress(now=None):
    """Read the two kernel lock signals, then recent unterminated deploy events."""
    if _release_lock_held(DAILY_RELEASE_LOCK) or _release_lock_held(DEPLOY_LOCK):
        return True
    now = now if now is not None else datetime.now(timezone.utc)
    starts, terminals = {}, {}
    try:
        for path in (ROOT / "events").glob("*.jsonl"):
            for line in path.read_text().splitlines():
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict) or not isinstance(event.get("subject"), str):
                        continue
                    kind, subject = event.get("type"), event["subject"]
                    if kind not in ("deploy-started", "deploy-landed", "deploy-failed", "deploy-refused"):
                        continue
                    ts = datetime.fromisoformat(event["ts"].replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        continue
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                bucket = starts if kind == "deploy-started" else terminals
                if subject not in bucket or ts > bucket[subject]:
                    bucket[subject] = ts
    except (OSError, UnicodeError):
        return False
    return any(0 <= (now - ts).total_seconds() <= 3600
               and (subject not in terminals or terminals[subject] < ts)
               for subject, ts in starts.items())


def _prove_deploy(project, repo, *, clock=None, sleep=None):
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    row = next((p for p in _look_toml(repo).get("prod", []) if p.get("project") == project), None)
    if row is None:
        return False, f"no [[prod]] row for project {project!r} in look.toml"
    ssh_target, version_path = row.get("ssh_target"), row.get("version_path")
    if not ssh_target or not version_path:
        return False, "the [[prod]] row is missing ssh_target/version_path"
    # version_path needs the host in front of it or curl exits 3 (malformed URL).
    url = f"{row.get('base_url', '').rstrip('/')}{version_path}"
    started, waited = clock(), 0
    while True:
        try:
            r = _run(["ssh", ssh_target, f"curl -fsS -w '\\n%{{http_code}}' {shlex.quote(url)}"],
                     capture_output=True, text=True, timeout=20)
        except Exception as exc:
            return False, f"deploy check raised: {exc}; waited_s={int(waited)}"
        if r.returncode == 0:
            return True, f"waited_s={int(waited)}" if waited else None
        try:
            status = int((getattr(r, "stdout", "") or "").rsplit("\n", 1)[-1])
        except (ValueError, TypeError):
            status = 0
        transient = r.returncode == 7 or (r.returncode == 22 and 500 <= status < 600)
        if not transient or not release_in_progress():
            return False, f"deploy version check failed: exit {r.returncode}; waited_s={int(waited)}"
        remaining = 900 - (clock() - started)
        if remaining <= 0:
            return False, f"deploy version check failed: exit {r.returncode}; waited_s={int(waited)}"
        delay = min(30, remaining)
        before_sleep = clock()
        sleep(delay)
        waited += clock() - before_sleep


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
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps(state))


def preflight(role, subject, view, project, *, repo, mcp_config=None, owed=frozenset(), routed=frozenset(), bundled=False):
    import checks
    import provision
    record = checks.CheckRecord(subject + ":preflight", subject, "preflight", [], "",
        "scratch", False, "repo-scratch-pg", "pre-merge", "model")
    args = (role, subject, view, project)
    kwargs = dict(repo=repo, mcp_config=mcp_config, owed=owed, routed=routed, bundled=bundled)
    with provision.context(tree=pathlib.Path(view) / "tree", grading=(args, kwargs)):
        prepared = provision.prepare(record, lane="grading")
    return prepared.failures


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


def dsn_role(view):
    """Describe the private endpoints actually provisioned in this view."""
    try:
        state = json.loads((pathlib.Path(view) / ".grading_state.json").read_text())
    except (OSError, ValueError):
        return "none"
    if not isinstance(state, dict):
        return "none"
    return "private-admin" if state.get("admin_dsn") else "private" if state.get("dsn") else "none"


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
    env.update(state.get("env_file_vars") or {})
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
    if state.get("admin_dsn"):
        for name in state.get("admin_env_names") or []:
            env[name] = state["admin_dsn"]
    return env


def write_grading_env(view):
    """Write `<view>/grading.env` from `pane_env(view)` — the file every view's
    verify.sh already sources (grader_view's source line) and the grader packet
    names. The sandboxed pane got these values injected into its process; a
    relay-served grader sub-agent (operator ruling 2026-10-04) only sees them
    through this file, so without it a proven scratch DB was invisible and every
    live_db criterion came back cannot-assess. Mode 0600; returns the path."""
    view = pathlib.Path(view)
    env = pane_env(view)
    path = view / "grading.env"
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text("".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in env.items()))
    path.chmod(0o600)
    return path


def _stop_cluster(view, name):
    """Stop one view-owned cluster without preventing another one's cleanup."""
    try:
        data = pathlib.Path(view) / name / "data"
        if data.is_dir():
            _run([str(_PG_BIN / "pg_ctl"), "-D", str(data), "-m", "fast", "stop"],
                 capture_output=True, text=True, env=_clean_env(), timeout=30)
    except Exception:
        pass


def teardown(view):
    """Idempotent, never raises: stop both view-owned private clusters."""
    for name in ("pg", "pg-admin"):
        _stop_cluster(view, name)
    return None
