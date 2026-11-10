"""Deterministic execution of verification-chain owed checks (L-spec-2240).

An owed `verify` or `ACn` row is observed by running its own command, not by
asking a model: the whole linted Verification chain (`packet.verify_script`) or
that AC's tagged commands (`checks._tagged_commands`) run at the criterion's
shipped sha in a private clone, with a clean credential-free environment inside
a bubblewrap boundary. The exit code decides: 0 is owed-met, any other exit is
owed-failed kind=unmet, and only an identified environment inability is
owed-unobservable with a named cause.

Entry points: run() (called by sweep_owed.run before script_rows), provision()
(the reusable private environment), replay() and audit_report() (the R7
historical reconciliation, driven from src/test_chain_runner.py).
"""
import contextlib
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import build_runner
import checks
import dispatch
import fold
import grading_env
import owed
import packet
import provision as provision_lib
import scratch
import sweep_provision

ROW_CAP = build_runner.WALL_CAP
OPEN = ("due", "blocked", "failed")
SPEC_ID = re.compile(r"L-spec-\d{1,9}")
CRITERION = re.compile(r"verify|AC\d{1,3}")
PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SHA = re.compile(r"[0-9a-f]{7,40}")
LEADING = re.compile(r"(?:cd|export|set|source|\.)\s|[A-Za-z_][A-Za-z0-9_]*=")
PG_NEEDS = re.compile(sweep_provision.PG_MARKERS.pattern + r"|private-pg|psql|alembic|SUPABASE_DB_URL|DATABASE_URL")
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|DSN|DATABASE|^PG|COOKIE|SESSION", re.I)
NETWORK_FAILURE = re.compile(r"temporary failure in name resolution|could not resolve host|"
                             r"network is unreachable|name or service not known|"
                             r"connection refused.*https?://", re.I)
VENV_PATH = r"(?<![\w./-])(/[\w./-]*venv[\w./-]*/bin/[\w.-]+)"
ENV_KEEP = ("PATH", "LANG", "LC_ALL", "TZ")
_CONFINE_OK = {}


class Pending(list):
    """Rows the caller must still route; `held` are chain rows deferred by budget."""

    def __init__(self, rows=()):
        super().__init__(rows)
        self.held = []
        self.appended = 0


class Unobservable(Exception):
    def __init__(self, cause, detail=""):
        self.cause, self.detail = cause, detail
        super().__init__(cause)


# ------------------------------------------------------------------ selection

def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _spec_text(spec):
    if not SPEC_ID.fullmatch(str(spec)):
        return None
    try:
        return (dispatch.ROOT / "content" / (spec + ".md")).read_text()
    except OSError:
        return None


def _projection(statements):
    return "## Verification\n\n```bash\n" + " &&\n".join(statements) + "\n```\n"


def _shipped(events, spec):
    found = None
    for event in events:
        if (event.get("type") == "shipped" and event.get("subject") == spec
                and SHA.fullmatch(str(event.get("sha") or ""))):
            found = event["sha"]
    return found


def select(row, events):
    """-> (plan, None) or (None, reason). A reason leaves the row pending untouched."""
    spec, criterion = row.get("spec"), row.get("criterion")
    if not SPEC_ID.fullmatch(str(spec)) or not CRITERION.fullmatch(str(criterion)):
        return None, "not a verification-chain criterion"
    if not PROJECT.fullmatch(str(row.get("project"))):
        return None, "project identifier invalid"
    text = _spec_text(spec)
    if text is None:
        return None, "spec text unreadable"
    if criterion == "verify":
        try:
            if packet.verify_script(text) is None:
                return None, "no Verification block"
        except packet.VerifyLint as exc:
            return None, "malformed Verification chain: " + str(exc)[:120]
        statements, provenance = [build_runner.verification.block(text).strip()], "verify-chain"
    else:
        tagged = checks._tagged_commands(text).get(criterion)
        if not tagged:
            return None, f"{criterion} has no tagged command"
        context = []
        for tag, statement in checks._verification_statements(text):
            if tag is not None:
                break
            if LEADING.match(statement):
                context.append(statement)
        statements = context + [f"# {criterion}\n{statement}" for statement in tagged]
        provenance = "tagged:" + criterion
    try:
        commands, _ = build_runner._commands(_projection(statements), row["project"], None)
    except build_runner.Undetermined as exc:
        return None, "no executable command: " + str(exc)
    chain = "\n".join(command for criterion_id, command in commands if criterion_id is None)
    if not chain.strip():
        return None, "no executable command"
    return {"row": row, "spec": spec, "criterion": criterion, "project": row["project"],
            "sha": _shipped(events, spec), "commands": chain, "provenance": provenance,
            "spec_digest": _digest(text), "command_digest": _digest(chain)}, None


def _eligible(row, events):
    if row.get("status", "due") not in OPEN:
        return False
    record = checks.record_for(events, row.get("spec"), row.get("criterion"))
    if record is not None and (record.profile == "prod-readonly" or record.data_source == "production"
                               or record.answered_by == "script"):
        return False  # a declared route is not replaced by the chain
    return True


# --------------------------------------------------------------- environment

def _child_env(parent, base):
    env = {key: os.environ[key] for key in ENV_KEEP if key in os.environ}
    home, tmp = parent / "home", parent / "tmp"
    for path in (home, tmp):
        path.mkdir(mode=0o700, exist_ok=True)
    env.update(HOME=str(home), TMPDIR=str(tmp), BASE=base, CI="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def _classify(exc):
    text = str(exc).lower()
    for needle, cause in (("first parent", "base-unavailable"), ("repo clone", "repo-unavailable"),
                          ("unresolvable sha", "sha-missing"), ("resolve sha", "sha-missing"), ("repo checkout", "sha-missing"),
                          ("unresolvable", "sha-missing"), ("main", "main-branch-unavailable"),
                          ("node", "node_modules"), ("npm", "node_modules"),
                          ("postgres", "scratch-postgres"), ("private-pg", "scratch-postgres"),
                          ("db_setup", "scratch-postgres"), ("venv", "venv"),
                          ("wall cap", "timeout")):
        if needle in text:
            return cause
    if text.startswith("infra:"):
        return "infra-" + re.sub(r"[^a-z0-9-]", "", text[6:])[:30]
    return "provisioning-failed-" + (re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:30] or "unknown")


def provision(project, sha, sweep_id):
    """Private shipped-sha clone as a provision.Env. A failure is a named
    Env.unobservable, never a usable-looking empty tree."""
    env = provision_lib.Env("repo-venv", None, {}, summary={"prod_dsns": "unset"})
    if not (PROJECT.fullmatch(str(project)) and SHA.fullmatch(str(sha or ""))
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(sweep_id))):
        env.unobservable = "invalid-identifier"
        return env
    import sweep_owed
    try:
        tree = sweep_owed.make_repo_cwd(project, sha, sha + "^1", sweep_id)
    except (build_runner.Undetermined, OSError, subprocess.SubprocessError, scratch.ScratchRootError) as exc:
        env.unobservable = _classify(exc) if not isinstance(exc, scratch.ScratchRootError) else "scratch-root-unavailable"
        return env
    parent = tree.parent
    env.tree, env.resources = tree, [str(parent)]
    env._cleanup.append(lambda: shutil.rmtree(parent, ignore_errors=True))
    full = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    base = subprocess.run(["git", "-C", str(tree), "rev-parse", "--verify", "HEAD^1"],
                          capture_output=True, text=True).stdout.strip()
    try:
        env.summary.update(json.loads((parent / "provisioning.json").read_text()))
    except (OSError, ValueError):
        env.summary["node_modules"] = "unknown"
    env.summary.update(sha=full, base=base, project=project)
    env.env = _child_env(parent, base)
    return env


def _confine(tree, project, pg, commands=""):
    """bwrap prefix: read-only host, writable scratch parent only, no home, no
    ledger, no credentials; network only when a scratch Postgres needs loopback."""
    if shutil.which("bwrap") is None:
        raise Unobservable("sandbox-unavailable", "bwrap not installed")
    parent = tree.parent
    home = pathlib.Path.home().resolve()
    root = pathlib.Path(os.environ.get("DOIT_ROOT") or home / ".do-it").expanduser().resolve()
    repo = (dispatch.ROOT / "repos" / project).resolve()
    argv = ["bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts"]
    if not pg:
        argv.append("--unshare-net")
    argv += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for hidden in sorted({str(home), str(root), "/tmp", "/var/tmp"}, key=len):
        argv += ["--tmpfs", hidden]
    if repo.is_dir():
        argv += ["--ro-bind", str(repo), str(repo)]
    for link in [tree / "node_modules", *tree.glob("*/node_modules")]:
        if link.is_symlink():
            target = link.resolve()
            argv += ["--ro-bind", str(target), str(target)]
    for interpreter in re.findall(VENV_PATH, commands):
        venv = pathlib.Path(interpreter.rsplit("/bin/", 1)[0])
        if venv.is_dir() and not str(venv).startswith(str(parent)):
            argv += ["--ro-bind", str(venv), str(venv)]
    argv += ["--bind", str(parent), str(parent), "--chdir", str(tree)]
    return argv


def _confinement_works():
    if "ok" not in _CONFINE_OK:
        try:
            _CONFINE_OK["ok"] = subprocess.run(
                ["bwrap", "--die-with-parent", "--ro-bind", "/", "/", "--tmpfs", "/tmp", "true"],
                capture_output=True, timeout=20).returncode == 0
        except (OSError, subprocess.SubprocessError):
            _CONFINE_OK["ok"] = False
    return _CONFINE_OK["ok"]


def _reset(tree, sha):
    """Every row starts from the pristine shipped tree, whatever the last row did."""
    for argv in (["git", "-C", str(tree), "reset", "--hard", "-q", sha],
                 ["git", "-C", str(tree), "clean", "-fdxq", "-e", "node_modules"]):
        done = subprocess.run(argv, capture_output=True, timeout=120)
        if done.returncode:
            raise Unobservable("clone-reset-failed", "git " + argv[3])


def _missing_venv(commands):
    for path in re.findall(VENV_PATH, commands):
        if not pathlib.Path(path).exists():
            return path
    return None


# ------------------------------------------------------------------- logging

def redact(text):
    text = re.sub(r"(?i)postgres(?:ql)?://[^\s\"'<>]+", "[redacted database URL]", text)
    text = re.sub(r"(https?://)[^\s/@]+@", r"\1[redacted]@", text, flags=re.I)
    text = re.sub(r"(?i)\b(password|token|secret|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+",
                  r"\1=[redacted]", text)
    for name, value in os.environ.items():
        if len(value) >= 6 and SECRET_NAME.search(name):
            text = text.replace(value, "[redacted]")
    return text


def _write_log(plan, header, output):
    directory = dispatch.ROOT / "state" / "chain-logs"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = directory / f"{plan['spec']}-{plan['criterion']}-{(plan['sha'] or 'nosha')[:12]}-{stamp}-{secrets.token_hex(3)}.log"
    body = redact(header + "\n" + output.decode("utf-8", "replace")[-200000:])
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(body)
    return path


# ----------------------------------------------------------------- execution

def _execute(plan, env):
    """-> dict(outcome, why, exit, output). The outcome is the command's own exit,
    except for a named environment inability."""
    commands = plan["commands"]
    if env.unobservable or env.tree is None:
        raise Unobservable(env.unobservable or "environment-missing")
    if missing := _missing_venv(commands):
        raise Unobservable("venv-unavailable", "missing interpreter " + os.path.basename(missing))
    if not _confinement_works():
        raise Unobservable("sandbox-unavailable", "bwrap probe failed")
    _reset(env.tree, env.summary["sha"])
    record = env.tree.parent / ("record-" + secrets.token_hex(4) + ".json")
    record.write_text(json.dumps({"subject": plan["spec"], "project": plan["project"]}))
    pg = bool(PG_NEEDS.search(commands))
    base = env.summary.get("base") or ""
    try:
        code, output = sweep_provision.run_check(
            env.tree, base, commands, record, timeout=ROW_CAP, env=env.env,
            profile="repo-scratch-pg" if pg else "repo-venv",
            confine=lambda has_pg: _confine(env.tree, plan["project"], has_pg, commands))
    except build_runner.WallCap:
        return {"outcome": "unobservable", "why": "timeout", "exit": None, "output": b"timeout after %d s\n" % ROW_CAP}
    except build_runner.Undetermined as exc:
        if "interrupted" in str(exc):
            raise
        cause = _classify(exc)
        raise Unobservable(cause, str(exc)[:160]) from None
    if code == 2 and re.fullmatch(rb"infra:[a-z0-9_-]{1,40}\n", output):
        return {"outcome": "unobservable", "why": "infra-" + output[6:-1].decode(), "exit": None, "output": output}
    if code and NETWORK_FAILURE.search(output.decode("utf-8", "replace")):
        return {"outcome": "unobservable", "why": "network-unavailable", "exit": code, "output": output}
    return {"outcome": "met" if code == 0 else "failed", "why": "", "exit": code, "output": output}


def _open_now(context, spec, criterion, now=None):
    """Check the caller's current snapshot, refreshing through its existing reader."""
    if context.refresh is not None:
        context.events = context.refresh()
    evs = fold.fold(context.events)[0].get(spec, {}).get("evs", [])
    for row in owed.checks(evs, now or datetime.now(timezone.utc)):
        if row["criterion"] == criterion:
            return row["status"] in OPEN, row["status"]
    return False, "unknown"


def _append(plan, result, path, log):
    exit_code = result["exit"]
    base = {"subject": plan["spec"]}
    fields = dict(criterion=plan["criterion"], runner="chain", capability="repo", sha=plan["sha"],
                  exit=exit_code, log=str(log))
    evidence = f"chain {plan['provenance']} exit {exit_code}; sha {plan['sha']}; log {log}"
    if result["outcome"] == "unobservable":
        fields.update(why=result["why"], evidence=f"{result['why']}: {evidence}")
    elif result["outcome"] == "failed":
        fields.update(kind="unmet", evidence=evidence)
    else:
        fields.update(evidence=evidence)
    path.parent.mkdir(parents=True, exist_ok=True)
    return dispatch.emit(path, base, "owed-" + result["outcome"], strict=True, **fields)


class _Pass:
    def __init__(self, events, refresh=None):
        self.events, self.refresh = events, refresh
        self.envs, self.count = {}, 0
        self.tag = f"L-chain-{os.getpid()}-{secrets.token_hex(3)}"

    def env(self, plan):
        key = (plan["project"], plan["sha"])
        if key not in self.envs:
            self.count += 1
            self.envs[key] = provision(plan["project"], plan["sha"], f"{self.tag}-{self.count}") if plan["sha"] else None
        return self.envs[key]

    def cleanup(self):
        errors = []
        for env in self.envs.values():
            if env is not None:
                try:
                    env.cleanup()
                except Exception as exc:  # keep cleaning the rest
                    errors.append(exc)
        self.envs.clear()
        if errors:
            raise errors[0]


def attempt(plan, path, context, *, append, now=None):
    """Observe one planned row. -> dict(outcome, why, exit, log, appended, refusal, closed)."""
    result = {"outcome": "unobservable", "why": "", "exit": None, "output": b""}
    try:
        if not plan["sha"]:
            raise Unobservable("sha-missing", "no shipped sha recorded")
        env = context.env(plan)
        result = _execute(plan, env)
    except Unobservable as exc:
        result = {"outcome": "unobservable", "why": exc.cause, "exit": None,
                  "output": (exc.detail or exc.cause).encode() + b"\n"}
    header = f"{plan['spec']}/{plan['criterion']} {plan['provenance']} sha={plan['sha']} outcome={result['outcome']} exit={result['exit']}"
    log = _write_log(plan, header, result["output"])
    out = {**result, "log": str(log), "appended": False, "refusal": None, "closed": None}
    out.pop("output")
    if append:
        still_open, status = _open_now(context, plan["spec"], plan["criterion"], now)
        if not still_open:
            out["closed"] = status
            return out
        refusal = _append(plan, result, path, log)
        out.update(appended=refusal is None, refusal=refusal)
    return out


@contextlib.contextmanager
def _interruptible():
    previous = {}
    if threading.current_thread() is threading.main_thread():
        previous = {sig: signal.signal(sig, build_runner._interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run(rows, events, path, *, limit, dry_run=False, refresh=None):
    """-> (pending_rows, lines). Resolved rows leave `pending`; budget-deferred
    eligible chain rows stay in it and are also listed in `pending.held`."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    fate, plans, lines = {}, [], []
    for row in rows:
        key = (row["spec"], row["criterion"])
        if key in fate or not _eligible(row, events):
            fate.setdefault(key, "pending")
            continue
        plan, reason = select(row, events)
        if plan is None:
            fate[key] = "pending"
            if reason != "not a verification-chain criterion":
                lines.append(f"chain: {key[0]}/{key[1]}: left pending: {reason}")
        else:
            fate[key] = "planned"
            plans.append(plan)
    chosen, deferred = plans[:limit], plans[limit:]
    for plan in deferred:
        fate[(plan["spec"], plan["criterion"])] = "held"
    if deferred:
        lines.append(f"chain: {len(deferred)} row(s) deferred to the next pass: limit {limit}")
    if dry_run:
        for plan in chosen:
            fate[(plan["spec"], plan["criterion"])] = "held"
            lines.append(f"chain: DRY RUN would run {plan['spec']}/{plan['criterion']} at {plan['sha']}")
    else:
        context = _Pass(events, refresh)
        result_appended = 0
        try:
            with _interruptible():
                for plan in chosen:
                    key = (plan["spec"], plan["criterion"])
                    outcome = attempt(plan, path, context, append=True)
                    if outcome["refusal"]:
                        fate[key] = "pending"
                        lines.append(f"chain: {key[0]}/{key[1]}: append refused: {outcome['refusal']}")
                    elif outcome["closed"]:
                        fate[key] = "closed"
                        lines.append(f"chain: {key[0]}/{key[1]}: closed ({outcome['closed']}) before append; not re-appended")
                    else:
                        fate[key] = "resolved"
                        result_appended += 1
                        lines.append(f"chain: {key[0]}/{key[1]}: {outcome['outcome']}"
                                     + (f" ({outcome['why']})" if outcome["why"] else ""))
        finally:
            context.cleanup()
    result = Pending()
    result.events = events if dry_run else context.events
    result.appended = 0 if dry_run else result_appended
    seen_held = set()
    for row in rows:
        key = (row["spec"], row["criterion"])
        if fate[key] in ("pending", "held"):
            result.append(row)
            if fate[key] == "held":
                result.held.append(row)
                seen_held.add(key)
    return result, lines


# --------------------------------------------------------------------- replay

VERDICT = {"MET": "met", "FAILED": "failed", "CANNOT": "unobservable"}


def parse_inventory(spec_text):
    """The fixed D cohort from the spec's Proposal inventory: [(spec, criterion, VERDICT)]."""
    return re.findall(r"^- (L-spec-\d+)/(verify|AC\d+): (MET|FAILED|CANNOT)$", spec_text, re.M)


def _key(plan):
    return f"{plan['spec']}/{plan['criterion']}/{plan['sha']}/{plan['spec_digest'][:16]}/{plan['command_digest'][:16]}"


def replay(proposals, events, path, report_path, *, append=False, budget_s=24 * 3600, now=None, clock=time.monotonic, refresh=None):
    """Re-execute a proposal cohort through the production attempt path.

    Resumable: a saved entry keyed by spec/criterion/sha/spec digest/command
    digest is reused, never re-run, and its open state is rechecked before any
    append. Rows already met/waived are compared, never appended."""
    report_path = pathlib.Path(report_path)
    try:
        saved = json.loads(report_path.read_text())
    except (OSError, ValueError):
        saved = {"rows": {}, "used_s": 0.0}
    rows, used, start = saved.get("rows", {}), float(saved.get("used_s", 0.0)), clock()
    now = now or datetime.now(timezone.utc)
    by_spec = fold.fold(events)[0]
    context, remaining = _Pass(events, refresh), []

    def persist(complete):
        data = {"rows": rows, "used_s": used + clock() - start, "complete": complete, "remaining": remaining,
                "cohort": len(proposals), "written_at": now.isoformat()}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")

    try:
        with _interruptible():
            for spec, criterion, proposed in proposals:
                ident = f"{spec}/{criterion}"
                if used + clock() - start > budget_s:
                    remaining.append(ident)
                    continue
                status_row = next((r for r in owed.checks(by_spec.get(spec, {}).get("evs", []), now)
                                   if r["criterion"] == criterion), None)
                status = status_row["status"] if status_row else "unknown"
                base = {"spec": spec, "criterion": criterion, "proposal": proposed, "runtime_status": status}
                if status_row is None:
                    rows[ident] = {**base, "outcome": "none", "explanation": "no owed row at runtime; nothing executed"}
                    continue
                plan, reason = select({**status_row, "project": status_row.get("project")}, events)
                if plan is None:
                    rows[ident] = {**base, "outcome": "pending", "appended": False,
                                   "explanation": "not executed: " + reason}
                    continue
                saved_row = rows.get(ident)
                if saved_row and saved_row.get("key") == _key(plan) and saved_row.get("outcome") in ("met", "failed", "unobservable"):
                    result = {k: saved_row[k] for k in ("outcome", "why", "exit", "log")}
                    result.update(appended=saved_row.get("appended", False), refusal=None, closed=None)
                    if append and status in OPEN and not result["appended"]:
                        open_now, current = _open_now(context, spec, criterion, now)
                        result["closed"] = None if open_now else current
                        if open_now:
                            result["refusal"] = _append(plan, result, path, result["log"])
                            result["appended"] = result["refusal"] is None
                else:
                    result = attempt(plan, path, context, append=append and status in OPEN, now=now)
                differs = VERDICT[proposed] != result["outcome"]
                note = (f"proposal {proposed} but executed {result['outcome']} (exit {result['exit']}"
                        + (f", cause {result['why']}" if result["why"] else "") + ")") if differs else "matches proposal"
                if status not in OPEN:
                    note += f"; compared only: row already {status}"
                rows[ident] = {**base, "key": _key(plan), "sha": plan["sha"], "spec_digest": plan["spec_digest"],
                               "command_digest": plan["command_digest"], "provenance": plan["provenance"],
                               "outcome": result["outcome"], "why": result["why"], "exit": result["exit"],
                               "log": result["log"], "appended": result["appended"],
                               "closed_before_append": result["closed"], "explanation": note}
                persist(False)
    finally:
        context.cleanup()
    persist(not remaining)
    return rows, remaining


def audit_report(report_path, inventory, events):
    """Read-only completeness audit of a saved replay. -> list of problems."""
    try:
        report = json.loads(pathlib.Path(report_path).read_text())
    except (OSError, ValueError):
        return ["no readable replay report at " + str(report_path)]
    rows, problems = report.get("rows", {}), []
    idents = [f"{s}/{c}" for s, c, _ in inventory]
    if len(idents) != 155 or len(set(idents)) != 155:
        problems.append(f"inventory has {len(set(idents))} unique identities, expected 155")
    verify = sum(1 for _, c, _ in inventory if c == "verify")
    if (verify, len(inventory) - verify) != (51, 104):
        problems.append(f"inventory is {verify} verify and {len(inventory) - verify} AC, expected 51 and 104")
    if not report.get("complete") or report.get("remaining"):
        problems.append(f"report incomplete: {len(report.get('remaining', []))} id(s) remaining")
    verdicts = {(e.get("subject"), e.get("criterion")) for e in events
                if e.get("runner") == "chain" and str(e.get("type", "")).startswith("owed-")}
    for spec, criterion, proposed in inventory:
        ident = f"{spec}/{criterion}"
        row = rows.get(ident)
        if row is None:
            problems.append(ident + ": absent from report")
            continue
        if row.get("proposal") != proposed:
            problems.append(ident + ": proposal mismatch")
        if row.get("outcome") in ("met", "failed", "unobservable"):
            if not (row.get("sha") and row.get("log") and row.get("command_digest")):
                problems.append(ident + ": executed without shipped-sha evidence")
            if not row.get("explanation"):
                problems.append(ident + ": difference unexplained")
            if row.get("runtime_status") in OPEN and not row.get("closed_before_append") and (spec, criterion) not in verdicts:
                problems.append(ident + ": open at execution but no admitted chain verdict")
            if row.get("runtime_status") not in OPEN and row.get("appended"):
                problems.append(ident + ": closed row was re-appended")
        elif not str(row.get("explanation", "")).startswith(("not executed:", "no owed row")):
            problems.append(ident + ": neither executed nor explained")
    return problems
