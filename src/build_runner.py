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
import secrets
import shlex
import shutil
import signal
import stat
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
import panes
import reap_tmp
import scratch
import verification

SOURCE = pathlib.Path(__file__).resolve().parent.parent
PRIVATE_PG = SOURCE / "scripts" / "private-pg.sh"
WALL_CAP = 30 * 60
VERSION = "1"
TAIL_CAP = 65536
DIRECTORY_CAP = 5 * 1024 * 1024
PROD_DATA_UNAVAILABLE = "cannot-observe: observed-data check needs a production read-only DSN; none supplied"
NO_PROD_ENV = {"pg": "none", "prod_dsns": "unset", "required_pg": "prod_ro"}
SHORT_TMP_ROOT = pathlib.Path("/tmp")
SHORT_TMP_BACKING_ROOT = None
_SHORT_TMP_NAME = re.compile(r"dr-([1-9][0-9]{0,9})-[a-z0-9]{8}")


def _tmp_backing_root():
    return (scratch.sub("runner-tmp") if SHORT_TMP_BACKING_ROOT is None
            else pathlib.Path(SHORT_TMP_BACKING_ROOT)).resolve()


def short_tmpdir():
    """Publish only an alias on /tmp; all payload stays on scratch."""
    backing = _tmp_backing_root() / f"dr-{os.getpid()}-{secrets.token_hex(4)}"
    backing.mkdir(mode=0o700)
    alias = SHORT_TMP_ROOT.absolute() / backing.name
    try:
        backing.chmod(0o700)
        alias.symlink_to(backing)
    except BaseException:
        backing.rmdir()
        raise
    return alias


def _tmp_pair(alias, backing):
    """Snapshot a validated pair (including interrupted, unpaired residue)."""
    result = []
    for path, link in ((alias, True), (backing, False)):
        try:
            info = path.lstat()
        except FileNotFoundError:
            result.append(None)
            continue
        if info.st_uid != os.geteuid():
            raise OSError("foreign temporary allocation")
        if link:
            if not stat.S_ISLNK(info.st_mode) or os.readlink(path) != str(backing):
                raise OSError("unexpected temporary alias")
        elif not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError("unexpected temporary backing")
        result.append((info.st_dev, info.st_ino, info.st_mode, info.st_uid))
    return tuple(result)


def _remove_tmp_pair(alias, backing, identity):
    if _tmp_pair(alias, backing) != identity:
        raise OSError("temporary allocation changed")
    if identity[0] is not None:
        alias.unlink()
    if identity[1] is not None:
        if _tmp_pair(alias, backing) != (None, identity[1]):
            raise OSError("temporary backing changed")
        # shutil's fd-based deletion does not follow nested symlinks.
        shutil.rmtree(backing)


def _tmp_unused(alias, backing, owner, proc_root):
    if panes.alive(owner):
        return False
    refs, _ = reap_tmp._own_uid_refs(proc_root, os.geteuid())
    if refs is None:
        raise OSError("unreadable process snapshot")
    # _bound_sockets treats a missing source as empty; recovery cannot.
    (proc_root / "net" / "unix").read_text()
    sockets = reap_tmp._bound_sockets(proc_root)
    for path in (alias, backing):
        if any(reap_tmp._under(ref, path) for ref in refs):
            return False
        if os.path.lexists(path):
            # _refs_d ignores unreadable pidfiles. Establish readability first,
            # then leave pid parsing and liveness to the shared predicate.
            for pidfile in reap_tmp._pidfiles_under(path):
                if pathlib.Path(pidfile).is_symlink():
                    raise OSError("symlinked pidfile")
                pathlib.Path(pidfile).read_text()
            if reap_tmp._refs_d(proc_root, path, sockets=sockets):
                return False
        elif any(reap_tmp._under(sock, path) for sock in sockets):
            return False
    return True


def sweep_short_tmpdirs(root=None, *, backing_root=None, proc_root=pathlib.Path("/proc")):
    """Best-effort recovery of this runner's namespace, using reaper evidence."""
    counts = dict(removed=0, retained=0, errors=0)
    try:
        root = pathlib.Path(root if root is not None else SHORT_TMP_ROOT).absolute()
        backing_root = pathlib.Path(backing_root) if backing_root is not None else _tmp_backing_root()
        if root.is_symlink() or backing_root.is_symlink():
            raise OSError("symlinked discovery root")
        backing_root = backing_root.resolve()
        names = {p.name for base in (root, backing_root) for p in base.iterdir()
                 if p.name.startswith("dr-")}
    except (OSError, scratch.ScratchRootError):
        counts["errors"] += 1
        return counts
    for name in sorted(names):
        match = _SHORT_TMP_NAME.fullmatch(name)
        if not match or int(match[1]) > 2147483647:
            counts["retained"] += 1
            continue
        alias, backing = root / name, backing_root / name
        try:
            identity = _tmp_pair(alias, backing)
            if identity == (None, None):
                continue
            if not _tmp_unused(alias, backing, int(match[1]), pathlib.Path(proc_root)):
                counts["retained"] += 1
                continue
            # Refresh both safety and identity immediately before removal.
            if not _tmp_unused(alias, backing, int(match[1]), pathlib.Path(proc_root)):
                counts["retained"] += 1
                continue
            _remove_tmp_pair(alias, backing, identity)
            counts["removed"] += 1
        except (OSError, UnicodeError):
            counts["retained"] += 1
            counts["errors"] += 1
    return counts


class Undetermined(Exception):
    pass


class InfraFailure(Undetermined):
    def __init__(self, category):
        self.category = category
        super().__init__("infra:" + category)


class WallCap(Undetermined):
    pass


class ProductionDataUnavailable(Undetermined):
    pass


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
    except OSError as exc:
        if grading_env.failure_kind(str(exc)) == "infra":
            raise InfraFailure(grading_env.infra_class(str(exc))) from None
        raise Undetermined(phase + " unavailable") from None
    if code:
        if grading_env.failure_kind(output.decode("utf-8", "replace"), code) == "infra":
            raise InfraFailure(grading_env.infra_class(output.decode("utf-8", "replace")))
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
    chain = verification.block(text) or ""
    tagged = _chain_covers(chain)
    joined = re.sub(r"\\\n", " ", chain)
    uncommented = re.sub(r"^[ \t]*#.*$", "", joined, flags=re.M)
    statements = {" ".join(statement.split())
                  for statement in re.split(r"\n|&&|\|\||;|\|", uncommented)}
    blocks = grading_env._criterion_blocks(text)
    commands, judgment = [], []
    for record in checks.records(text, project):
        criterion = record["criterion"]
        if criteria is not None and criterion not in criteria:
            continue
        if criterion in tagged:
            review = checks._review_command(blocks.get(criterion, ""))
            if review and " ".join(review.split()) not in statements:
                commands.append((criterion, review))
        elif record["runner"] != "build":
            judgment.append(criterion)
        else:
            commands.extend((criterion, command) for command in record["commands"])
    if chain and (criteria is None or set(tagged).intersection(criteria)):
        commands.append((None, chain))
    if not commands:
        raise Undetermined("no build command or Verification block")
    return commands, judgment


def _chain_covers(chain):
    return sorted(set(re.findall(r"^[ \t]*#\s*(AC\d+)\b", chain, re.M)))


def _source_paths(command):
    """Only tracked Python source, bound to the command's repo-relative cwd."""
    try:
        words = shlex.split(command.replace("$(git rev-parse --show-toplevel)", "."))
    except ValueError:
        return []
    directory, paths = pathlib.PurePosixPath("."), []
    for index, word in enumerate(words):
        if word == "cd" and index + 1 < len(words):
            directory = directory / words[index + 1]
        target = word.split("::", 1)[0]
        if not re.fullmatch(r"[\w./-]+\.py", target):
            continue
        path = directory / target
        if not path.is_absolute() and ".." not in path.parts:
            paths.append(str(path))
    return list(dict.fromkeys(paths))


def _missing_production_data(text, project, criteria, repo, sha, env, deadline, *, db_target="none"):
    # Full build Verification chains deliberately keep their existing behavior.
    # Only a criterion-selected owed observation can require production data.
    if criteria is None:
        return False
    # A private SUPABASE_DB_URL is not supplied production data. Future audited
    # RO execution must supply both a RO variable and explicit provenance.
    if db_target == "prod_ro" and env.get("SUPABASE_DB_URL_RO"):
        return False
    blocks = grading_env._criterion_blocks(text)
    sources = {}
    for criterion in criteria:
        block = blocks.get(criterion, "")
        anchor = block.splitlines()[0] if block else ""
        if not re.search(r"\[[^]]*\bobserved-data\b", anchor, re.I):
            continue
        commands, _ = _commands(text, project, {criterion})
        commands = [command for _, command in commands]
        if checks.production_data(block, commands):
            return True
        for command in commands:
            for path in _source_paths(command):
                if path not in sources:
                    code, output = _process(["git", "-C", str(repo), "show", f"{sha}:{path}"],
                                            env=env, deadline=deadline)
                    sources[path] = output.decode("utf-8", "replace") if code == 0 else ""
        if checks.production_data(block, commands, sources.values()):
            return True
    return False


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
    import provision
    if provision._ACTIVE.get() is not None:
        return provision._build_node(tree, row, env, deadline)
    record = checks.CheckRecord("node:setup", "node-setup", "setup", [], "", "scratch",
                                False, "repo-venv", "pre-merge", "model")
    with provision.context(tree=tree, env=env, node=(row, deadline)):
        prepared = provision.prepare(record, lane="build")
    return prepared.summary["node_modules"]


def _provision(tree, auxiliary, row, env, deadline):
    import provision
    record = checks.CheckRecord("build:setup", tree.name, "setup", [], "", "scratch", False,
        "repo-scratch-pg" if row.get("db_setup") else "repo-venv", "pre-merge", "script")
    with provision.context(tree=tree, env=env, build=(auxiliary, row, deadline)):
        prepared = provision.prepare(record, lane="build")
    env.update(prepared.env)
    return prepared.summary


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
    temporary = identity = None
    try:
        env = grading_env._clean_env()
        if row.get("venv"):
            # Match db_setup's venv resolution for absolute and tree-relative paths.
            venv_bin = tree / row["venv"] / "bin"
            env["PATH"] = str(venv_bin) + os.pathsep + env.get("PATH", os.defpath)
        for name in ("DOIT_LEDGER_FILE", "DOIT_SUPERVISED"):
            env.pop(name, None)
        for name, relative in (("HOME", "home"), ("DOIT_ROOT", "root")):
            path = auxiliary / relative
            path.mkdir()
            env[name] = str(path.resolve())
        temporary = short_tmpdir()
        backing = _tmp_backing_root() / temporary.name
        identity = _tmp_pair(temporary, backing)
        env["TMPDIR"] = env["TMP"] = env["TEMP"] = str(temporary)
        rows, parsed, missing, rewrites = [], [], [], []
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
            if criterion is None:
                rows[-1]["covers"] = _chain_covers(command)
            parsed.append(log)
        return rows, parsed, missing, rewrites, summary
    finally:
        try:
            grading_env.teardown(auxiliary)
        finally:
            try:
                if temporary is not None and identity is not None:
                    _remove_tmp_pair(temporary, backing, identity)
            finally:
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
    sweep_short_tmpdirs()
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
        row = grading_env._project_row(project)
        cap = row.get("runner_wall_cap_s", WALL_CAP)
        if "runner_wall_cap_s" in row and (type(cap) is not int or cap <= 0):
            raise Undetermined("runner_wall_cap_s must be a positive integer")
        deadline = time.monotonic() + cap
        previous = signal.signal(signal.SIGALRM, _expired)
        resources.callback(signal.signal, signal.SIGALRM, previous)
        resources.callback(signal.setitimer, signal.ITIMER_REAL, 0)
        signal.setitimer(signal.ITIMER_REAL, cap)
        repo = grading_env._project_checkout(project)
        env = grading_env._clean_env()
        done = next((e for e in reversed(events) if e["type"] == "build-done"), {})
        sha = _resolve(repo, requested_sha or done.get("ready_sha"), env, deadline)
        key = "all" if criteria is None else hashlib.sha256(",".join(sorted(criteria)).encode()).hexdigest()[:8]
        path = fold.ROOT / "content" / "evidence" / spec / f"{sha[:12]}-{key}.json"
        if criteria is not None:
            text = (fold.ROOT / "content" / f"{spec}.md").read_text()
            if _missing_production_data(text, project, criteria, repo, sha, env, deadline):
                raise ProductionDataUnavailable(PROD_DATA_UNAVAILABLE)
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
        text = (fold.ROOT / "content" / f"{spec}.md").read_text()
        commands, judgment = _commands(text, project, criteria)
        tails = path.with_suffix("")
        tails.mkdir(parents=True)
        limit = min(TAIL_CAP, DIRECTORY_CAP // (2 * len(commands)))
        tip, tip_logs, _, rewrites, summary = _run_tree(
            spec, sha, repo, row, commands, tails, 0, limit, deadline)
        try:
            base, base_logs, missing, base_rewrites, _ = _run_tree(
                spec, base_sha, repo, row, commands, tails, len(commands), limit, deadline)
            rewrites.extend(base_rewrites)
        except (WallCap, InfraFailure):
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
        category = getattr(exc, "category", None)
        if category is None and grading_env.failure_kind(str(exc)) == "infra":
            category = grading_env.infra_class(str(exc))
        if category:
            fields = grading_env.infra_fields(spec + ":" + sha, "build-runner", "")
            fields["class"] = category
            _event(spec, project, "infra-failure", **fields)
        reason = "infra:" + category if category else (str(exc) if isinstance(exc, Undetermined)
                  else "runner could not establish evidence (" + type(exc).__name__ + ")")
        fields = {"env": NO_PROD_ENV} if isinstance(exc, ProductionDataUnavailable) else {}
        if isinstance(exc, WallCap):
            fields["reason_code"] = "wall-cap"
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
    interrupts = {sig: signal.signal(sig, _interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        path = run(args.spec, args.sha, criteria)
        print(path.read_text().rstrip() if args.json else path)
        return 0
    except Undetermined as exc:
        print("run-checks: " + str(exc), file=sys.stderr)
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in interrupts.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
