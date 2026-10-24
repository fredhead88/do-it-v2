"""Shared environment profiles and script verdicts. No model launch boundary."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import grading_env
import resources as resource_ledger

SOURCE = Path(__file__).resolve().parent.parent
PG_CTL = "/usr/lib/postgresql/17/bin/pg_ctl"
CMD_TIMEOUT_S = 60
PROFILES = ("repo-scratch-pg", "repo-venv", "prod-readonly", "browser-review")
_OPTIONS = ContextVar("provision_options", default={})
_ACTIVE = ContextVar("provision_active", default=None)


class Undetermined(RuntimeError):
    pass


@dataclass
class Env:
    profile: str
    tree: Path
    env: dict[str, str]
    resources: list[str] = field(default_factory=list)
    unobservable: str | None = None
    summary: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)
    _cleanup: list = field(default_factory=list, repr=False)

    def cleanup(self):
        # Keep a failing cleanup on the stack; a dirty stop must be retriable.
        while self._cleanup:
            self._cleanup[-1]()
            self._cleanup.pop()


@dataclass(frozen=True)
class Verdict:
    outcome: str
    reason: str = ""
    evidence: str = ""
    capability: str = "repo"


@contextmanager
def context(**options):
    """Lane-owned paths/configuration; the CheckRecord remains authoritative."""
    token = _OPTIONS.set({**_OPTIONS.get(), **options})
    try:
        yield
    finally:
        _OPTIONS.reset(token)


def _record(kind, path):
    active = _ACTIVE.get()
    if active is None:
        raise Undetermined("resource created outside prepare")
    env, lane, subject = active
    resource_ledger.record(kind, str(path), lane=lane, subject=subject)
    env.resources.append(str(path))


def _stop_pg(path, env, ctl=None):
    data = Path(path) / "data"
    if data.exists():
        result = subprocess.run([ctl or PG_CTL, "-w", "-t", "15", "-D", str(data),
            "-m", "fast", "stop"], env=env, capture_output=True, timeout=20)
        if result.returncode and (data / "postmaster.pid").exists():
            raise Undetermined("scratch Postgres stop failed; directory retained")
    if Path(path).exists():
        shutil.rmtree(path)


def _repo(env, record):
    options = _OPTIONS.get()
    if "tree" in options:
        return
    import dispatch
    import fold
    project = fold.fold(fold.read_events())[0].get(record.spec, {}).get("project")
    repo = dispatch.ROOT / "repos" / project if project else None
    if repo is None or not repo.is_dir() or not re.fullmatch(r"[0-9a-f]{40}", record.sha):
        raise Undetermined("exact check repository or sha unavailable")
    parent = Path(tempfile.mkdtemp(prefix="check-"))
    _record("tmpdir", parent)
    env._cleanup.append(lambda: shutil.rmtree(parent))
    env.tree = parent / "tree"
    _record("worktree", env.tree)
    for argv in (["git", "clone", "--shared", "--no-checkout", str(repo), str(env.tree)],
                 ["git", "-C", str(env.tree), "checkout", "--detach", record.sha]):
        process = subprocess.run(argv, env=env.env, capture_output=True, timeout=60)
        if process.returncode:
            raise Undetermined("exact check checkout unavailable")


def _repo_setup(env, record):
    options = _OPTIONS.get()
    _repo(env, record)
    if options.get("dashboard"):
        root, live = options["dashboard"]
        env.summary["node_modules"] = _sweep_dashboard(env.tree, root, live=live)
    if options.get("grading"):
        env.failures = _grading_preflight(*options["grading"][0], **options["grading"][1])
    elif options.get("build"):
        auxiliary, row, deadline = options["build"]
        with context(record=record):
            env.summary = _build_setup(env.tree, auxiliary, row, env.env, deadline)
    elif options.get("node"):
        row, deadline = options["node"]
        env.summary["node_modules"] = _build_node(env.tree, row, env.env, deadline)


def repo_venv(record, env):
    _repo_setup(env, record)
    return env


def repo_scratch_pg(record, env):
    _repo_setup(env, record)
    options = _OPTIONS.get()
    if options.get("grading") or options.get("build"):
        return env
    path = Path(tempfile.mkdtemp(prefix=options.get("pg_prefix", "check-pg-"), dir=options.get("pg_parent")))
    _record("scratch-db", path)
    env._cleanup.append(lambda: _stop_pg(path, grading_env._clean_env(), options.get("pg_ctl")))
    output = br._required(["/bin/bash", str(options.get("pg_script", SOURCE / "scripts/private-pg.sh")), str(path), "loopback"],
        phase="scratch Postgres", env=env.env, deadline=options.get("pg_deadline", time.monotonic() + 60))
    match = re.search(r"^ADMIN_DSN='(postgresql://albert:[0-9a-f]{32}@127\.0\.0\.1:\d+/postgres)'$",
                      output, re.M)
    if not match:
        raise Undetermined("scratch Postgres produced no loopback admin endpoint")
    row = options.get("row", {})
    for name in set(row.get("db_env", ())) | set(row.get("admin_env", ())) | {
            "SUPABASE_DB_URL", "DATABASE_URL", "PG_PARITY_DB_URL", "SCRATCH_PG_URL", "AS_LOCAL_PG_ADMIN"}:
        env.env[name] = match[1]
    env.summary.update(pg="loopback", admin_pg="loopback", prod_dsns="unset")
    setup = row.get("db_setup")
    if setup:
        argv = [arg.replace("{venv}", str(env.tree / row.get("venv", ""))) for arg in setup.get("argv", [])]
        if not argv:
            raise Undetermined("db_setup has no argv")
        process = subprocess.run(argv, cwd=env.tree / setup.get("cwd", "."), env=env.env,
                                 capture_output=True, timeout=120)
        if process.returncode:
            raise Undetermined("db_setup failed")
    return env


def prod_readonly(record, env):
    head = Path(record.argv[0]).name if record.argv else ""
    wrapper = SOURCE / "scripts/sweep" / head
    if head not in ("ro-sql", "droplet-read") or not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        env.unobservable = "no-approved-access"
    # Production wrappers obtain their own scoped access. Never inherit keys.
    env.env = {key: value for key, value in env.env.items()
               if not re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", key, re.I)}
    return env


def browser_review(record, env):
    # Browser driving remains a seat responsibility; provision only repo/tools.
    _repo_setup(env, record)
    return env


def script_rows(rows, events, path, *, lane, dry_run=False):
    """Resolve declared script checks inline; model and unknown rows stay visible."""
    import checks
    pending, lines = [], []
    for row in rows:
        record = checks.record_for(events, row["spec"], row["criterion"])
        if record is None or record.answered_by == "model":
            pending.append(row)
            if record is None:
                lines.append(f"{lane}: {row['spec']}/{row['criterion']}: data-source-unknown")
            continue
        if dry_run:
            lines.append(f"{lane}: DRY RUN would run script {record.spec}/{record.criterion}")
            continue
        prepared = None
        try:
            prepared = prepare(record, lane=lane)
            verdict = run_script_check(record, prepared)
        except (Undetermined, br.Undetermined, OSError, subprocess.SubprocessError):
            verdict = Verdict("unobservable", "environment unavailable", "environment unavailable", "repo")
        finally:
            if prepared is not None:
                prepared.cleanup()
        emit_verdict(record, verdict, path)
        lines.append(f"{lane}: {record.spec}/{record.criterion}: script {verdict.outcome}")
    return pending, lines


def prepare(record, *, lane):
    profile = getattr(record, "profile", None)
    functions = dict(zip(PROFILES, (repo_scratch_pg, repo_venv, prod_readonly, browser_review)))
    if profile not in functions:
        raise Undetermined("unknown or missing check profile")
    options = _OPTIONS.get()
    child_env = (dict(options["env"]) if "env" in options else grading_env._clean_env())
    child_env = {key: value for key, value in child_env.items()
                 if not (key.startswith(("SUPABASE_DB_URL", "PG")) or "DSN" in key or "DATABASE" in key
                         or key in ("AS_LOCAL_PG_ADMIN", "SCRATCH_PG_URL"))}
    env = Env(profile, Path(options.get("tree", Path.cwd())), child_env,
              summary={"prod_dsns": "unset"})
    token = _ACTIVE.set((env, lane, record.spec))
    try:
        return functions[profile](record, env)
    except BaseException:
        env.cleanup()
        raise
    finally:
        _ACTIVE.reset(token)


def run_script_check(record, env):
    import prod_runner
    capability = "ro-dsn" if record.profile == "prod-readonly" else "repo"
    def unavailable(reason):
        return Verdict("unobservable", reason, reason, capability)
    if env.unobservable:
        return unavailable(env.unobservable)
    try:
        argv = list(record.argv)
        if not argv or not all(isinstance(arg, str) for arg in argv):
            return unavailable("invalid command")
        forbidden = {"keepa-fetch", "scrapingbee-fetch", "portal-e2e", "claude", "codex"}
        if any(re.search(r"(?:^|[\s/;|&])(?:" + "|".join(forbidden) + r")(?:$|[\s;|&])", word)
               for word in argv):
            return unavailable("command refused")
        if record.data_source == "production":
            argv = prod_runner._argv(argv)
        # Preserve the existing grading sandbox when a view was provided.
        view = _OPTIONS.get().get("view")
        if view is not None and grading_env._bwrap_available():
            import grader_view
            argv = grader_view.bwrap_argv(view, doit_src=SOURCE) + ["--"] + argv
        process = subprocess.run(argv, cwd=env.tree, env=env.env, shell=False,
            capture_output=True, text=True, timeout=CMD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return unavailable("command timeout")
    except (OSError, ValueError, TypeError):
        return unavailable("command unavailable or refused")
    output = (process.stdout or "") + "\n" + (process.stderr or "")
    if Path(argv[0]).name == "ro-sql" and "permission denied" in output.lower():
        return Verdict("unobservable", "read-only database permission denied",
                       "read-only database permission denied", "ro-dsn-grant")
    lower = output.lower()
    if process.returncode and any(token in lower for token in (
            "connection refused", "could not connect", "couldn't connect", "could not resolve",
            "timed out", "timeout", "connection reset", "network is unreachable")):
        return unavailable(f"command could not answer (exit {process.returncode})")
    if argv[0] == "curl" and process.returncode not in (0, 22):
        return unavailable(f"curl could not answer (exit {process.returncode})")
    if process.returncode in (2, 3):
        return unavailable(f"command could not answer (exit {process.returncode})")
    evidence = prod_runner.redact(f"{Path(argv[0]).name} exit {process.returncode}: {output}")
    return Verdict("met" if process.returncode == 0 else "failed", "", evidence, capability)


def emit_verdict(record, verdict, path):
    import dispatch
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = dict(criterion=record.criterion, evidence=verdict.evidence or verdict.reason)
    if verdict.outcome == "unobservable":
        fields.update(why=verdict.reason, capability=verdict.capability)
    elif verdict.outcome == "failed":
        fields["kind"] = "unmet"
    refusal = dispatch.emit(path, {"subject": record.spec}, "owed-" + verdict.outcome, strict=True, **fields)
    if refusal:
        raise Undetermined("verdict append refused: " + str(refusal))
# Existing setup implementations, moved from their lane adapters.
import build_runner as br
import grading_env as ge


def _build_node(tree, row, env, deadline):
    keys = []
    for relative in row.get('node_dirs') or []:
        directory = tree / relative
        lock = directory / 'package-lock.json'
        if not lock.is_file():
            raise br.Undetermined('node lockfile missing')
        key = br.hashlib.sha256(lock.read_bytes()).hexdigest()[:16]
        keys.append(key)
        root = br.scratch.sub('node-cache')
        cache = root / key
        with (root / (key + '.lock')).open('a') as handle:
            br.fcntl.flock(handle, br.fcntl.LOCK_EX)
            if not (cache / 'node_modules').is_dir():
                with br.tempfile.TemporaryDirectory(prefix=key + '-', dir=root) as tmp:
                    stage = br.pathlib.Path(tmp)
                    _record('tmpdir', stage)
                    work = stage / 'work'
                    br.shutil.copytree(directory, work, ignore=br.shutil.ignore_patterns('node_modules', '.git'))
                    br._required(['npm', 'ci', '--prefer-offline'], phase='npm ci', env=env, deadline=deadline, cwd=work)
                    if not (work / 'node_modules').is_dir():
                        raise br.Undetermined('npm ci produced no node_modules')
                    try:
                        br.node_modules.validate(work / 'node_modules')
                    except br.node_modules.UnsafeNodeModules as exc:
                        raise br.Undetermined(str(exc)) from None
                    ready = stage / 'ready'
                    ready.mkdir()
                    br.shutil.move(str(work / 'node_modules'), ready / 'node_modules')
                    ready.rename(cache)
        target = directory / 'node_modules'
        if target.exists() or target.is_symlink():
            raise br.Undetermined('node_modules present in committed tree')
        try:
            br.node_modules.copy_tree(cache / 'node_modules', target)
        except br.node_modules.UnsafeNodeModules as exc:
            raise br.Undetermined(str(exc)) from None
    return 'cache:' + ','.join(keys) if keys else 'none'


def _build_setup(tree, auxiliary, row, env, deadline):
    summary = {'node_modules': 'none', 'pg': 'none', 'admin_pg': 'none', 'prod_dsns': 'unset'}
    summary['node_modules'] = br._node(tree, row, env, deadline)
    if row.get('db_setup'):
        for name, admin in (('pg', False), ('pg-admin', True)):
            if admin and (not row.get('admin_env')):
                continue
            argv = ['/bin/bash', str(br.PRIVATE_PG), str(auxiliary / name)]
            if admin:
                argv.append('loopback')
            _record('scratch-db', auxiliary / name)
            output = br._required(argv, phase='private-pg.sh', env=env, deadline=deadline)
            field = 'ADMIN_DSN' if admin else 'SUPABASE_DB_URL'
            match = br.re.search('\\b' + field + "='([^']+)'", output)
            if not match:
                raise br.Undetermined('private-pg.sh produced no private endpoint')
            for variable in row.get('admin_env' if admin else 'db_env') or []:
                env[variable] = match[1]
            summary['admin_pg' if admin else 'pg'] = 'loopback' if admin else 'private'
        setup = row['db_setup']
        venv = str(tree / row.get('venv', ''))
        argv = [arg.replace('{venv}', venv) for arg in setup.get('argv', [])]
        if not argv:
            raise br.Undetermined('db_setup has no argv')
        br._required(argv, phase='db_setup', env=env, deadline=deadline, cwd=tree / setup.get('cwd', '.'))
    return summary




def _grading_db(view, *, admin=False, avoid_port=None):
    """Private cluster -> (ok, reason, dsn); admin mode leaves it role-clean."""
    script = ge.DOIT_SRC / 'scripts' / 'private-pg.sh'
    prefix = 'admin cluster: ' if admin else ''
    if not script.is_file():
        if admin:
            ge._stop_cluster(view, 'pg-admin')
        return (False, f'{prefix}missing {script}', None)
    argv = ['/bin/bash', str(script), str(ge.pathlib.Path(view) / ('pg-admin' if admin else 'pg'))]
    if admin:
        argv.append('loopback')
    env = ge._clean_env({'PRIVATE_PG_AVOID_PORT': str(avoid_port)} if admin and avoid_port else None)
    if _ACTIVE.get() is not None:
        _record('scratch-db', ge.pathlib.Path(view) / ('pg-admin' if admin else 'pg'))
    try:
        r = ge._run(argv, capture_output=True, text=True, env=env, timeout=60)
    except Exception as exc:
        if ge.failure_kind(str(exc), 124 if isinstance(exc, ge.subprocess.TimeoutExpired) else 1) == 'infra':
            raise ge.InfraFailure(ge.infra_class(str(exc))) from None
        if admin:
            ge._stop_cluster(view, 'pg-admin')
            return (False, f'{prefix}private-pg.sh raised {type(exc).__name__}', None)
        return (False, f'private-pg.sh raised: {exc}', None)
    if r.returncode != 0:
        if ge.failure_kind((r.stderr or '') + (r.stdout or ''), r.returncode) == 'infra':
            raise ge.InfraFailure(ge.infra_class((r.stderr or '') + (r.stdout or '')))
        if admin:
            ge._stop_cluster(view, 'pg-admin')
            return (False, f'{prefix}private-pg.sh exit {r.returncode}', None)
        return (False, f"private-pg.sh exit {r.returncode}: {(r.stderr or '')[-200:]}", None)
    if admin:
        m = ge.re.search("ADMIN_DSN='(postgresql://albert:[0-9a-f]{32}@127\\.0\\.0\\.1:\\d+/postgres)'", r.stdout or '')
        if not m:
            ge._stop_cluster(view, 'pg-admin')
            return (False, 'admin cluster: no valid ADMIN_DSN in private-pg.sh output', None)
    else:
        m = ge.re.search("SUPABASE_DB_URL='([^']*)'", r.stdout or '')
    return (True, None, m.group(1)) if m else (False, 'no SUPABASE_DB_URL in private-pg.sh output', None)


def _grading_schema(view, row, project, dsn, *, admin_dsn=None):
    argv, cwd_rel, _ = ge._db_setup_argv(row, project)
    if not argv:
        return (False, 'no db_setup configured for this project')
    if not dsn or (row.get('admin_env') and (not admin_dsn)):
        return (False, 'private db/admin endpoint was not proven')
    private_env = {n: dsn for n in row.get('db_env') or []}
    private_env.update({n: admin_dsn for n in row.get('admin_env') or []})
    env = ge._clean_env(private_env)
    try:
        r = ge._run(argv, cwd=str(ge.pathlib.Path(view) / 'tree' / cwd_rel), env=env, capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return (False, f'db_setup raised: {exc}')
    return (False, f"db_setup exit {r.returncode}: {(r.stderr or '')[-200:]}") if r.returncode else (True, None)


def _grading_node(view, row, project):
    dirs = row.get('node_dirs') or []
    if not dirs:
        if 'node_dirs' in row and row['node_dirs'] == []:
            return (True, None)
        return (False, 'no node_dirs configured for this project')
    checkout, tree = (ge._project_checkout(project), ge.pathlib.Path(view) / 'tree')
    for d in dirs:
        declared = (row.get('node_modules') or {}).get(d)
        src = ge.pathlib.Path(declared) if declared is not None else checkout / d / 'node_modules'
        key = f'node_modules.{d}'
        if not src.is_absolute() or not src.is_dir() or (not ge.os.access(src, ge.os.R_OK | ge.os.X_OK)):
            return (False, f'{key} source {src} is missing, unreadable or not absolute')
        dst = tree / d / 'node_modules'
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not (dst.is_symlink() or dst.exists()):
                dst.symlink_to(src)
            if not dst.is_symlink() or not dst.is_dir() or dst.resolve() != src.resolve():
                return (False, f'{key} destination {dst} does not resolve to source {src}')
        except OSError as exc:
            if ge.failure_kind(str(exc)) == 'infra':
                raise ge.InfraFailure(ge.infra_class(str(exc))) from None
            return (False, f'{key} source {src} could not be linked at {dst}')
    return (True, None)


def _grading_preflight(role, subject, view, project, *, repo, mcp_config=None, owed=frozenset(), routed=frozenset(), bundled=False):
    """Provisions/proves every capability `required()` needs, in `view/` only.
    Returns `(capability, reason)` FAILURES only, in `CAPABILITIES` order.
    `owed`/`routed` (criterion ids) pass straight through to `required()`
    (R12.g/i) — this function reads no ledger itself; dispatch computes them."""
    view = ge.pathlib.Path(view)
    tree = view / 'tree'
    spec_text, verify_text = ge._spec_verify_text(subject, role)
    caps = ge.required(role, spec_text, verify_text, project, owed=owed, routed=routed, bundled=bundled)
    if role == 'grader' and (not routed):
        if any((cap == 'git' for cap, _ in ge.route(spec_text, verify_text, project).values())):
            caps = [cap for cap in caps if cap != 'git']
    row = ge._project_row(project)
    reasons = {}
    env_file_vars = {}
    if role != 'reviewer' and (not (role == 'grader' and bundled)) and (error := ge._env_file_error(row)):
        reasons['env-file'] = error
    dsn = None
    admin_dsn = None
    if 'db' in caps:
        ok, reason, dsn = ge._prove_db(view)
        if not ok:
            reasons['db'] = reason
    if role == 'grader' and (not bundled) and row.get('admin_env') and ('db' not in reasons):
        first_port = (ge.urllib.parse.parse_qs(ge.urllib.parse.urlparse(dsn or '').query).get('port') or [None])[0]
        ok, reason, admin_dsn = ge._prove_db(view, admin=True, avoid_port=first_port)
        if not ok:
            reasons['db'] = reason
    if 'env-file' in caps and 'env-file' not in reasons:
        ok, reason, env_file_vars = ge._provision_env_file(tree, row, None if 'db' in reasons else dsn)
        if not ok:
            reasons['env-file'] = reason
    if 'db-schema' in caps and row.get('db_setup'):
        if 'db' in reasons:
            reasons['db-schema'] = f"db was not proven: {reasons['db']}"
        else:
            ok, reason = ge._prove_db_schema(view, row, project, dsn, admin_dsn=admin_dsn)
            if not ok:
                reasons['db-schema'] = reason
    if 'view-paths' in caps:
        ok, reason = ge._prove_view_paths(view)
        if not ok:
            reasons['view-paths'] = reason
    venv = row.get('venv') or ''
    venv_path = str(ge._project_checkout(project) / venv) if venv else ''
    caps_text = '\n'.join((text for cid, text in ge._tool_texts(spec_text, verify_text).items() if cid not in set(owed) | set(routed)))
    if 'node_modules' in caps:
        ok, reason = ge._prove_node_modules(view, row, project)
        if not ok:
            reasons['node_modules'] = reason
    if 'tools' in caps:
        ok, reason = ge._prove_tools(view, project, repo, caps_text, venv_path)
        if not ok:
            reasons['tools'] = reason
    has_prod = any((p.get('project') == project for p in ge._look_toml(ge.DOIT_SRC).get('prod', [])))
    if 'browser' in caps and has_prod:
        ok, reason = ge._prove_browser(mcp_config)
        if not ok:
            reasons['browser'] = reason
    if 'review-account' in caps and has_prod:
        ok, reason = ge._prove_review_account(project)
        if not ok:
            reasons['review-account'] = reason
    if 'deploy' in caps and any((p.get('project') == project for p in ge._look_toml(ge.DOIT_SRC).get('prod', []))):
        ok, reason = ge._prove_deploy(project, repo)
        if not ok:
            reasons['deploy'] = reason
    if role != 'reviewer':
        ge._write_state(view, venv=venv_path, db_env_names=row.get('db_env') or [] if dsn else [], dsn=dsn or '', admin_env_names=row.get('admin_env') or [] if admin_dsn else [], admin_dsn=admin_dsn or '', env_file_vars=env_file_vars)
    for reason in reasons.values():
        if ge.failure_kind(reason) == 'infra':
            raise ge.InfraFailure(ge.infra_class(reason))
    return [(c, reasons[c]) for c in ge.CAPABILITIES if c in reasons]


import fcntl, hashlib, pathlib, time
import build_runner

def _sweep_dashboard(tree, root, *, live):
    """Prefer the lock-keyed cache; use the live tree only with identical locks."""
    directory = pathlib.Path(tree) / "dashboard"
    lock = directory / "package-lock.json"
    if not lock.is_file():
        return "none"
    target = directory / "node_modules"
    if target.exists() or target.is_symlink():
        raise build_runner.Undetermined("node_modules present in committed tree")
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    cache_root = pathlib.Path(root) / "cache/node_modules"
    cache_root.mkdir(parents=True, exist_ok=True)
    cache = cache_root / digest
    with (cache_root / (digest + ".lock")).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if cache.exists():
            if (not (cache / "node_modules").is_dir()
                    or not (cache / "package-lock.json").is_file()
                    or (cache / "package-lock.json").read_bytes() != lock.read_bytes()):
                raise build_runner.Undetermined("node cache lock mismatch or incomplete cache")
            source, used = cache / "node_modules", "cache:" + digest
        elif ((live / "node_modules").is_dir() and (live / "package-lock.json").is_file()
              and (live / "package-lock.json").read_bytes() == lock.read_bytes()):
            source, used = live / "node_modules", "matching-lock:" + str(live)
        else:
            # Stage atomically: a failed install must never become a cache hit.
            stage = pathlib.Path(tempfile.mkdtemp(prefix=digest[:12] + "-", dir=cache_root))
            _record("tmpdir", stage)
            promoted = False
            try:
                for name in ("package.json", "package-lock.json"):
                    if (directory / name).is_file():
                        shutil.copyfile(directory / name, stage / name)
                build_runner._required(["npm", "ci", "--prefer-offline"], phase="dashboard npm ci",
                    env=grading_env._clean_env(), deadline=time.monotonic() + build_runner.WALL_CAP,
                    cwd=stage)
                if not (stage / "node_modules").is_dir() or (stage / "package-lock.json").read_bytes() != lock.read_bytes():
                    raise build_runner.Undetermined("npm ci produced no lock-matched node_modules")
                stage.rename(cache)
                promoted = True
            finally:
                if not promoted:
                    shutil.rmtree(stage)
            source, used = cache / "node_modules", "cache:" + digest
        target.symlink_to(source.resolve(), target_is_directory=True)
    return used
