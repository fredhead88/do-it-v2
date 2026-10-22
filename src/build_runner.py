#!/usr/bin/env python3
"""Run registered build checks in disposable tip/base clones, without a model."""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import base_red
import checks
import fold
import grader_view
import grading_env
import node_modules
import scratch
import verification

SOURCE = pathlib.Path(__file__).resolve().parent.parent
PRIVATE_PG = SOURCE / "scripts" / "private-pg.sh"
WALL_CAP = 30 * 60
VERSION = "1"
TAIL_CAP = 65536
DIRECTORY_CAP = 5 * 1024 * 1024


class Undetermined(Exception):
    pass


class WallCap(Undetermined):
    pass


class ProductionDataUnavailable(Undetermined):
    """No production observation was attempted, including on a cache hit."""


def _expired(*_):
    raise WallCap("wall cap exceeded")


def _interrupted(*_):
    raise Undetermined("runner interrupted")


def _process(argv, *, env, deadline, cwd=None):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _expired()
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    try:
        output, _ = proc.communicate(timeout=remaining)
    except BaseException as exc:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        if isinstance(exc, subprocess.TimeoutExpired):
            _expired()
        raise
    return proc.returncode, output


def _required(argv, *, phase, **kw):
    try:
        code, output = _process(argv, **kw)
    except OSError:
        raise Undetermined(phase + " unavailable") from None
    if code:
        # Never include potentially credential-bearing external output in errors.
        raise Undetermined(phase + " failed")
    return output.decode("utf-8", "replace").strip()


def _resolve(repo, sha, env, deadline):
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha or ""):
        raise Undetermined("unresolvable sha or base")
    value = _required(["git", "-C", str(repo), "rev-parse", "--verify", sha + "^{commit}"],
                      phase="resolve sha or base", env=env, deadline=deadline)
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise Undetermined("unresolvable sha or base")
    return value


def _main_branch(repo, project, env, deadline):
    configured = grading_env._look_toml(SOURCE).get("push", {}).get("main", {}).get(project)
    if os.environ.get("DOIT_MAIN") or configured:
        return os.environ.get("DOIT_MAIN") or configured
    ref = _required(["git", "-C", str(repo), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                    phase="resolve main", env=env, deadline=deadline)
    if not ref.startswith("origin/"):
        raise Undetermined("unresolvable main")
    return ref[len("origin/"):]


def _commands(text, project, criteria):
    # The registry alone maps criteria. Carry only the untagged preceding cds
    # for a tagged Verification statement, never for a review_path command.
    prefixes, cds = {}, []
    for criterion, statement in checks._verification_statements(text):
        if criterion is None and re.match(r"^cd\s+", statement):
            cds.append(statement)
        elif criterion is not None:
            prefixes[(criterion, statement)] = list(cds)
    commands, judgment = [], []
    for record in checks.records(text, project):
        criterion = record["criterion"]
        if criteria is not None and criterion not in criteria:
            continue
        if record["commands"] and "ro-prod-sql" in record["needs"]:
            raise ProductionDataUnavailable("cannot-observe: production data requires a read-only observation route; no database used")
        if record["runner"] != "build":
            judgment.append(criterion)
            continue
        for command in record["commands"]:
            prefix = prefixes.get((criterion, command), [])
            commands.append((criterion, " &&\n".join([*prefix, command])))
    chain = (verification.block(text) or "").strip()
    if criteria is None and chain:
        if checks.production_data("", [chain]):
            raise ProductionDataUnavailable("cannot-observe: live Verification requires a read-only observation route; no database used")
        commands.append((None, chain))
    if not commands:
        raise Undetermined("no build command or Verification block")
    return commands, judgment


def _rewrite(command, repo, tree, venv):
    # Shield absolute venv tokens: grader_view's own venv guard expects a
    # relative venv, while grading.toml supplies absolute venvs in production.
    tokens = grader_view._PATH_TOKEN_RE
    protected = {}
    def shield(match):
        token = match[1]
        core = grader_view._trim_unmatched_closer(token)
        if venv and pathlib.Path(venv).is_absolute() and (core == venv or core.startswith(venv + "/")):
            key = f"__RUNNER_VENV_{len(protected)}__"
            protected[key] = token
            return key
        return token
    masked = tokens.sub(shield, command)
    rewritten = grader_view._rewrite_repo_paths(masked, repo, tree, venv)
    for key, token in protected.items():
        rewritten = rewritten.replace(key, token)
    before, after = tokens.findall(command), tokens.findall(rewritten)
    changes = [{"from": old, "to": new} for old, new in zip(before, after) if old != new]
    return rewritten, changes


def _node(tree, row, env, deadline):
    keys = []
    for relative in row.get("node_dirs") or []:
        directory = tree / relative
        lock = directory / "package-lock.json"
        if not lock.is_file():
            raise Undetermined("node lockfile missing")
        key = hashlib.sha256(lock.read_bytes()).hexdigest()[:16]
        keys.append(key)
        root = scratch.sub("node-cache")
        cache = root / key
        with (root / (key + ".lock")).open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if not (cache / "node_modules").is_dir():
                with tempfile.TemporaryDirectory(prefix=key + "-", dir=root) as tmp:
                    stage = pathlib.Path(tmp)
                    work = stage / "work"
                    shutil.copytree(directory, work, ignore=shutil.ignore_patterns("node_modules", ".git"))
                    _required(["npm", "ci", "--prefer-offline"], phase="npm ci",
                              env=env, deadline=deadline, cwd=work)
                    if not (work / "node_modules").is_dir():
                        raise Undetermined("npm ci produced no node_modules")
                    try:
                        node_modules.validate(work / "node_modules")
                    except node_modules.UnsafeNodeModules as exc:
                        raise Undetermined(str(exc)) from None
                    ready = stage / "ready"
                    ready.mkdir()
                    shutil.move(str(work / "node_modules"), ready / "node_modules")
                    ready.rename(cache)
        target = directory / "node_modules"
        if target.exists() or target.is_symlink():
            raise Undetermined("node_modules present in committed tree")
        # A private copy: caches and live checkouts cannot receive test writes.
        try:
            node_modules.copy_tree(cache / "node_modules", target)
        except node_modules.UnsafeNodeModules as exc:
            raise Undetermined(str(exc)) from None
    return "cache:" + ",".join(keys) if keys else "none"


def _provision(tree, auxiliary, row, env, deadline):
    summary = {"node_modules": "none", "pg": "none", "admin_pg": "none", "prod_dsns": "unset"}
    summary["node_modules"] = _node(tree, row, env, deadline)
    if row.get("db_setup"):
        for name, admin in (("pg", False), ("pg-admin", True)):
            if admin and not row.get("admin_env"):
                continue
            argv = ["/bin/bash", str(PRIVATE_PG), str(auxiliary / name)]
            if admin:
                argv.append("loopback")
            output = _required(argv, phase="private-pg.sh", env=env, deadline=deadline)
            field = "ADMIN_DSN" if admin else "SUPABASE_DB_URL"
            match = re.search(r"\b" + field + r"='([^']+)'", output)
            if not match:
                raise Undetermined("private-pg.sh produced no private endpoint")
            for variable in row.get("admin_env" if admin else "db_env") or []:
                env[variable] = match[1]
            summary["admin_pg" if admin else "pg"] = "loopback" if admin else "private"
        setup = row["db_setup"]
        venv = str(tree / row.get("venv", ""))
        argv = [arg.replace("{venv}", venv) for arg in setup.get("argv", [])]
        if not argv:
            raise Undetermined("db_setup has no argv")
        _required(argv, phase="db_setup", env=env, deadline=deadline,
                  cwd=tree / setup.get("cwd", "."))
    return summary


def _targets_missing(command, tree):
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    targets, directory = [], tree
    for index, word in enumerate(words):
        if word == "cd" and index + 1 < len(words):
            destination = words[index + 1]
            if any(c in destination for c in "$`~"):
                return False
            directory = directory / destination
        if not word.startswith("-") and re.fullmatch(r"[^\s;()]+\.(?:py|[cm]?js|tsx?|sh)(?:::.*)?", word):
            target = (directory / word.split("::")[0]).resolve()
            if not target.is_relative_to(tree):
                return False
            targets.append(target)
    return bool(targets) and all(not path.exists() for path in targets)


def _run_tree(spec, sha, repo, row, commands, tail_dir, offset, tail_limit, deadline):
    tree = scratch.sub("runner") / f"{spec}-{sha[:12]}-{os.getpid()}"
    auxiliary = tree.with_name(tree.name + "-env")
    if tree.exists() or auxiliary.exists():
        raise Undetermined("runner scratch already exists")
    auxiliary.mkdir()
    env = grading_env._clean_env()
    for name in ("DOIT_LEDGER_FILE", "DOIT_SUPERVISED"):
        env.pop(name, None)
    for name, relative in (("HOME", "home"), ("DOIT_ROOT", "root")):
        path = auxiliary / relative
        path.mkdir()
        env[name] = str(path)
    rows, parsed, missing, rewrites = [], [], [], []
    try:
        _required(["git", "clone", "--shared", "--no-checkout", str(repo), str(tree)],
                  phase="clone", env=env, deadline=deadline)
        _required(["git", "-C", str(tree), "checkout", "--detach", sha],
                  phase="checkout", env=env, deadline=deadline)
        summary = _provision(tree, auxiliary, row, env, deadline)
        for index, (criterion, command) in enumerate(commands):
            command, changes = _rewrite(command, repo, tree, row.get("venv"))
            rewrites.extend(changes)
            missing.append(_targets_missing(command, tree))
            started = time.monotonic()
            code, output = _process(["/bin/bash", "-c", command], env=env, deadline=deadline, cwd=tree)
            log = base_red.parse_log(output.decode("utf-8", "replace"))
            ids = sorted(set().union(*(set(v) for k, v in log.items() if k != "undetermined")))
            tail = tail_dir / f"{offset + index}.tail"
            tail.write_bytes(output[-tail_limit:])
            rows.append({"criterion": criterion, "cmd": command, "cwd": str(tree), "exit": code,
                         "wall_s": round(time.monotonic() - started, 3),
                         "out_sha256": hashlib.sha256(output).hexdigest(), "out_bytes": len(output),
                         "tail": str(tail), "failing_ids": ids})
            parsed.append(log)
        return rows, parsed, missing, rewrites, summary
    finally:
        grading_env.teardown(auxiliary)
        shutil.rmtree(tree, ignore_errors=True)
        shutil.rmtree(auxiliary, ignore_errors=True)


def _compare(project, base_sha, tip, base, tip_logs, base_logs, missing):
    existing, new, undetermined = set(), set(), set()
    for index, row in enumerate(tip):
        ids = set(row["failing_ids"])
        if not row["exit"] and not ids and not tip_logs[index]["undetermined"]:
            continue
        if tip_logs[index]["undetermined"] or (row["exit"] and not ids):
            undetermined.add(index)
            continue
        if not base:
            undetermined.add(index)
            continue
        baseline = base[index]
        if base_logs[index]["undetermined"] or (baseline["exit"] and not baseline["failing_ids"]):
            if missing[index] and not base_logs[index]["undetermined"]:
                new.update(ids)
            else:
                undetermined.add(index)
            continue
        known = set(baseline["failing_ids"])
        existing.update(ids & known)
        new.update(ids - known)
    if base and (existing or new):
        classified = base_red.classify(project, base_sha, sorted(existing | new))
        if classified["complete"]:
            # The direct rerun is authoritative when CI disagrees about an id.
            existing.update(set(classified["pre_existing"]) - new)
    return {"pre_existing": sorted(existing), "new": sorted(new), "undetermined": sorted(undetermined)}


def _event(spec, project, kind, **fields):
    env = grading_env._clean_env({"DOIT_LEDGER_FILE": "L-build-runner-local.jsonl"})
    if project:
        env["DOIT_PROJECT"] = project
    argv = [str(SOURCE / "doit"), "append", kind, spec]
    argv.extend(f"{key}:={json.dumps(value)}" for key, value in fields.items())
    _required(argv, phase="ledger append", env=env, deadline=time.monotonic() + 30)


def run(spec, requested_sha=None, criteria=None):
    deadline = time.monotonic() + WALL_CAP
    started = datetime.now(timezone.utc).isoformat()
    sha, project = requested_sha or "unresolved", None
    tails = path = None
    resources = contextlib.ExitStack()
    try:
        if not re.fullmatch(r"L-spec-\d+", spec):
            raise Undetermined("invalid spec id")
        events = fold.fold(fold.read_events())[3].get(spec, [])
        project = next((e["project"] for e in events if e.get("project")), None)
        if not project or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", project):
            raise Undetermined("spec has no ledger project")
        repo = grading_env._project_checkout(project)
        env = grading_env._clean_env()
        done = next((e for e in reversed(events) if e["type"] == "build-done"), {})
        sha = _resolve(repo, requested_sha or done.get("ready_sha"), env, deadline)
        key = "all" if criteria is None else hashlib.sha256(",".join(sorted(criteria)).encode()).hexdigest()[:8]
        path = fold.ROOT / "content" / "evidence" / spec / f"{sha[:12]}-{key}.json"
        # Validate today's DB requirements before returning any cached bundle.
        # Legacy live_db evidence may have been collected against scratch PG.
        text = (fold.ROOT / "content" / f"{spec}.md").read_text()
        commands, judgment = _commands(text, project, criteria)
        locks = fold.ROOT / "evidence" / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock = resources.enter_context((locks / f"{spec}-{sha}.lock").open("a"))
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            return path
        if done.get("base_sha"):
            base_sha = _resolve(repo, done["base_sha"], env, deadline)
        else:
            main = _main_branch(repo, project, env, deadline)
            base_sha = _required(["git", "-C", str(repo), "merge-base", sha, "origin/" + main],
                                 phase="merge base", env=env, deadline=deadline)
            base_sha = _resolve(repo, base_sha, env, deadline)
        row = grading_env._project_row(project)
        tails = path.with_suffix("")
        tails.mkdir(parents=True)
        limit = min(TAIL_CAP, DIRECTORY_CAP // (2 * len(commands)))
        tip, tip_logs, _, rewrites, summary = _run_tree(
            spec, sha, repo, row, commands, tails, 0, limit, deadline)
        try:
            base, base_logs, missing, base_rewrites, _ = _run_tree(
                spec, base_sha, repo, row, commands, tails, len(commands), limit, deadline)
            rewrites.extend(base_rewrites)
        except WallCap:
            raise
        except Undetermined:
            # AC7: a base which could not provision supplies no clean ids.
            base, base_logs, missing = [], [], []
        comparison = _compare(project, base_sha, tip, base, tip_logs, base_logs, missing)
        bundle = {"spec": spec, "project": project, "sha": sha, "base_sha": base_sha,
                  "runner_version": VERSION, "started_at": started, "env": summary,
                  "rewrites": rewrites, "commands": tip, "base": base,
                  "comparison": comparison, "judgment_only": judgment}
        data = (json.dumps(bundle, indent=2) + "\n").encode()
        if time.monotonic() >= deadline:
            _expired()
        # Publication/failure recording has its own bounded subprocess;
        # the check deadline cannot interrupt a ledger append mid-write.
        signal.setitimer(signal.ITIMER_REAL, 0)
        temporary = path.with_suffix(".pending")
        temporary.write_bytes(data)
        temporary.replace(path)
        _event(spec, project, "evidence-bundle", path=str(path), sha=sha, base_sha=base_sha,
               bundle_sha256=hashlib.sha256(data).hexdigest(), n_commands=len(tip),
               n_failed=sum(r["exit"] != 0 for r in tip), n_new=len(comparison["new"]))
        return path
    except (Undetermined, OSError, ValueError, KeyError, subprocess.SubprocessError,
            scratch.ScratchRootError) as exc:
        if tails:
            shutil.rmtree(tails, ignore_errors=True)
            path.unlink(missing_ok=True)
            path.with_suffix(".pending").unlink(missing_ok=True)
        # The evidence deadline must not interrupt recording its own failure.
        signal.setitimer(signal.ITIMER_REAL, 0)
        reason = str(exc) if isinstance(exc, Undetermined) else "runner could not establish evidence (" + type(exc).__name__ + ")"
        fields = {"env": {"pg": "none", "prod_dsns": "unset", "required_pg": "prod_ro"}} \
            if isinstance(exc, ProductionDataUnavailable) else {}
        _event(spec, project, "evidence-bundle-failed", sha=sha, reason=reason, **fields)
        raise Undetermined(reason) from None
    finally:
        resources.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec")
    parser.add_argument("--sha")
    parser.add_argument("--criteria")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    criteria = set(args.criteria.split(",")) if args.criteria is not None else None
    if criteria is not None and any(not re.fullmatch(r"AC\d+", c) for c in criteria):
        parser.error("criteria must be comma-separated AC ids")
    previous = signal.signal(signal.SIGALRM, _expired)
    interrupts = {sig: signal.signal(sig, _interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    signal.setitimer(signal.ITIMER_REAL, WALL_CAP)
    try:
        path = run(args.spec, args.sha, criteria)
        print(path.read_text().rstrip() if args.json else path)
        return 0
    except Undetermined as exc:
        print("run-checks: " + str(exc), file=sys.stderr)
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        for sig, handler in interrupts.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
