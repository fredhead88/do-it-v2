#!/usr/bin/env python3
"""window_gate — verify a merge WINDOW's candidate tip before master moves (L-spec-0765).

    doit gate-window <window-id> --project albert-scott [--specs A,B,C] [--main BRANCH] [--fixes-base]

Today nobody runs the full Python suite until a deploy, so one bad merge hides
behind twenty-four good ones. For project `albert-scott` the Executor instead
gates each branch (`doit gate`, unchanged), collects the window, and calls this:

  1. build the candidate tip — master plus each gate-clean branch, in order,
     `--no-ff` — with git PLUMBING only (`merge-tree --write-tree`,
     `commit-tree`, `update-ref`): no checkout, no index, no working tree;
  2. push it to an ephemeral `ci-verify/<sha12>-win<pid>` ref and hand it to
     `scripts/ci/predeploy_gate.sh` (dispatch, poll, run_verdict, cancel at the
     deadline are ITS mechanism; none of that is re-implemented here);
  3. green  -> fast-forward master to the candidate sha (old value asserted);
     red    -> map the NEW failing node ids to the specs that own them, drop
               exactly those, verify the remainder ONCE more, advance to the green
               subset. At most two runs per window. A second red drops the window.

Exit codes (the Executor contract, agents/executor.md, mirrors these):
  0  advanced: every spec in the window shipped
  1  partially advanced: some specs dropped (window-dropped / merge-conflict), the rest shipped
  2  red or undetermined: master did not move (an escalation-blocking is filed)
  3  another window holds the lock: do nothing, the next pass retries
  4  DOIT_WINDOW_GATE=off: bypassed, one window-gate-bypassed event per spec;
     use the direct merge path (operator-declared CI outage only)

Env: DOIT_WINDOW_PREDEPLOY (override the script path), DOIT_WINDOW_VERIFY_TIMEOUT_SECS.
"""
import io, json, math, os, pathlib, posixpath, re, shlex, signal, subprocess, sys, time, zipfile, zlib
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold  # noqa: E402
import rechain

PROJECT = "albert-scott"
PREDEPLOY = "scripts/ci/predeploy_gate.sh"
DEFAULT_TOML_PATH = pathlib.Path(__file__).resolve().parent.parent / "look.toml"
KILL_MARGIN_MIN = 10
BASE_RED_MARGIN_SECS = 600  # nested verify teardown, plus bounded log fetch
BASE_RED_COMMAND = "doit"
MAX_MERGES = 6
PYTEST_CHECK = "pytest"
PG_CHECK = "pg-parity (Postgres)"
# These workflows only run on master pushes/PRs, not ci-verify dispatches.
# Every new deploy requirement must be classified here before a window can open.
CI_VERIFY_EXCLUDED = frozenset({
    "Auth Coverage · Secret Scan · CVE Check",
    "lint PR-changed migrations + sql/core freeze + single head",
    "Repo lint guards",
})
DASHBOARD_CHECK = "TypeScript · Biome · Tests · File Sizes"
RUN_MINUTES, DASHBOARD_MINUTES = 45, 5
NODE_RE = re.compile(r"[\w./-]+\.py::[^\s\"',\]]+")
IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import\s+([\w, ()*]+)|import\s+([\w., ]+))", re.M)
IMPORT_ROOTS = ("api/", "pipelines/", "agents/", "tests/")
IDENT = ("-c", "user.name=doit-window", "-c", "user.email=doit-window@localhost")


class Red(Exception):
    pass


def git(repo, *args, check=True, env=None, inp=None):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       timeout=60, env=env, input=inp)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip() or 'failed'}")
    return p


def ev(typ, subject, **kv):
    """Append one executor event. Every value goes in as JSON (`k:=`), so a
    list/number/string round-trips exactly. The actor is the filename (D90)."""
    os.environ["DOIT_LEDGER_FILE"] = os.environ.get("DOIT_GATE_LEDGER_FILE", "L-executor-0001.jsonl")
    kv.setdefault("project", PROJECT)
    fold.append([typ, subject] + [f"{k}:={json.dumps(v)}" for k, v in kv.items()])


def escalate(window, why):
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
    ev("escalation-blocking", window, why=why,
       default="master stays where it is; the window's specs stay unshipped until an operator decides",
       deadline=due,
       revert="rerun `doit gate-window` once the cause is fixed, or DOIT_WINDOW_GATE=off for a declared CI outage")


# ── lock ────────────────────────────────────────────────────────────────────
def lock_path():
    return fold.ROOT / "state" / "window-gate.lock"


def acquire():
    p = lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode()), os.close(fd)
            return True
        except FileExistsError:
            try:
                os.kill(int(p.read_text().strip() or 0), 0)
                return False                       # holder alive
            except (ValueError, ProcessLookupError):
                p.unlink(missing_ok=True)          # stale: its owner is gone
            except PermissionError:
                return False
    return False


def release():
    lock_path().unlink(missing_ok=True)


# ── candidate build (plumbing only) ─────────────────────────────────────────
def merge_into(repo, cur, branch_sha, msg):
    """-> (new_commit, None) or (None, conflicted_paths). Never touches a tree."""
    p = git(repo, "merge-tree", "--write-tree", "--name-only", cur, branch_sha, check=False)
    lines = p.stdout.split("\n")
    if p.returncode == 1:
        paths = []
        for ln in lines[1:]:
            if not ln.strip():
                break
            paths.append(ln.strip())
        return None, paths
    if p.returncode:
        raise RuntimeError(f"merge-tree: {p.stderr.strip()}")
    env = {**os.environ, "GIT_AUTHOR_DATE": f"{int(time.time())} +0000", "GIT_COMMITTER_DATE": f"{int(time.time())} +0000"}
    c = git(repo, *IDENT, "commit-tree", lines[0].strip(), "-p", cur, "-p", branch_sha, "-m", msg, env=env)
    return c.stdout.strip(), None


def build_candidate(repo, window, base, entries):
    """entries: [(spec, branch, branch_sha)]. -> (candidate, merged_specs, conflicts{spec: paths})."""
    cur, merged, conflicts = base, [], {}
    for spec, branch, bsha in entries:
        new, paths = merge_into(repo, cur, bsha, f"merge({spec}): window {window}")
        if new is None and os.environ.get("DOIT_WINDOW_RECHAIN", "off") == "on":
            new, _ = rechain.resolve(repo, cur, (spec, branch, bsha), window, ev=ev)
        if new is None:
            conflicts[spec] = paths
        else:
            cur = new
            merged.append(spec)
    if merged:
        git(repo, "update-ref", f"refs/heads/window/{window}", cur)
    return cur, merged, conflicts


# ── verification: ONE call into the existing deploy gate ─────────────────────
def required_checks(repo, base):
    """Read literal Bash array data from the base, without executing repo code.

    Only quoted literal entries are supported. Dynamic or malformed Bash is
    deliberately unreadable rather than a reason to weaken the gate.
    """
    source = git(repo, "show", f"{base}:{PREDEPLOY}", check=False)
    array = re.search(r"(?m)^REQUIRED_CHECKS=\(", source.stdout)
    if source.returncode or array is None:
        raise RuntimeError("required-checks-unreadable")
    lexer = shlex.shlex(source.stdout[array.end():], posix=False, punctuation_chars="();")
    lexer.whitespace_split = True
    checks = []
    try:
        for token in lexer:
            if token == ")":
                break
            if (len(token) < 2 or token[0] not in "\"'" or token[-1] != token[0]
                    or any(c in token[1:-1] for c in "$`\\\n")):
                raise ValueError("non-literal array entry")
            checks.append(token[1:-1])
        else:
            raise ValueError("unterminated array")
    except ValueError as e:
        raise RuntimeError("required-checks-unreadable") from e
    supported = {PYTEST_CHECK, PG_CHECK, DASHBOARD_CHECK}
    if not checks or len(set(checks)) != len(checks) or any(not c for c in checks):
        raise RuntimeError("required-checks-unreadable")
    unknown = set(checks) - supported - CI_VERIFY_EXCLUDED
    if unknown:
        raise RuntimeError("required-checks-unclassified: " + ", ".join(sorted(unknown)))
    if not supported <= set(checks):
        raise RuntimeError("required-checks-unreadable: missing ci-verify requirements")
    return [c for c in checks if c in supported]


def pg_result(repo, doc):
    """Read the named job, not the workflow conclusion; retain failure signatures.

    The deploy gate enumerates pytest's baseline-aware NEW nodes, but pg-parity
    needs its own log. No workflow dispatch/poll mechanism is duplicated here.
    """
    check = doc.get("checks", {}).get(PG_CHECK)
    if check == "ran":                 # legacy verdicts without job metadata
        return False, {}
    if not isinstance(check, dict) or not str(check.get("run_id", "")).isdigit():
        raise RuntimeError("pg-parity job metadata unavailable")
    rid = str(check["run_id"])
    def gh(*args):
        proc = subprocess.run(["gh", "run", "view", *args], cwd=repo,
                              capture_output=True, text=True, timeout=60)
        if proc.returncode:
            raise RuntimeError("pg-parity job evidence unavailable")
        return proc.stdout
    try:
        jobs = json.loads(gh(rid, "--json", "jobs"))["jobs"]
        matches = [j for j in jobs if j.get("name") == PG_CHECK]
        if len(matches) != 1 or matches[0].get("conclusion") not in ("success", "failure"):
            raise RuntimeError("pg-parity job absent, ambiguous or not completed")
        job = matches[0]
        if job["conclusion"] == "success":
            return False, {}
        jid = str(job.get("databaseId", ""))
        if not jid.isdigit():
            raise RuntimeError("pg-parity job id unavailable")
        log = gh("--job", jid, "--log-failed")
        failures = {}
        for line in log.splitlines():
            line = re.sub(r"\x1b\[[0-9;]*m", "", line)
            m = re.search(r"\b(FAILED|ERROR)\s+([\w./-]+\.py::\S+)(.*)$", line)
            if m:
                failures[m[2]] = m[1] + " " + m[3].strip()
        return True, failures
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as e:
        raise RuntimeError("pg-parity job evidence unreadable") from e


def _positive_timeout(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def dispatch_timeout_min(env, toml_path=None):
    """Resolve a positive finite budget: process env, versioned TOML, then 45."""
    override = _positive_timeout(env.get("PREDEPLOY_GATE_DISPATCH_TIMEOUT_MIN"))
    if override is not None:
        return override
    path = pathlib.Path(toml_path) if toml_path is not None else DEFAULT_TOML_PATH
    try:
        import tomllib
        with open(path, "rb") as f:
            table = tomllib.load(f).get("window_gate")
    except (OSError, ValueError, ImportError):
        table = None
    configured = _positive_timeout(table.get("dispatch_timeout_min")) if isinstance(table, dict) else None
    return configured if configured is not None else 45.0


def verify_timeout_secs(env, toml_path=None):
    """Keep the gate's cancellation deadline inside our process kill deadline."""
    override = _positive_timeout(env.get("DOIT_WINDOW_VERIFY_TIMEOUT_SECS"))
    return override if override is not None else (dispatch_timeout_min(env, toml_path) + KILL_MARGIN_MIN) * 60


def parse_verdict(out):
    """The gate's JSON verdict line (last line that parses as an object), tolerant
    of shape: node ids from `new_failures`/`failures`, url from `run_url`/`url`."""
    doc = {}
    for ln in reversed(out.strip().splitlines()):
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                doc = json.loads(ln)
                break
            except ValueError:
                continue
    nodes = doc.get("new_failures") or doc.get("failures") or []
    if isinstance(nodes, str):
        nodes = [nodes]
    nodes = [n for n in nodes if isinstance(n, str)]
    if not nodes:
        nodes = list(dict.fromkeys(NODE_RE.findall(out)))
    url = doc.get("run_url") or doc.get("url") or ""
    if not url:
        m = re.search(r"https://\S+/actions/runs/\d+", out)
        url = m.group(0) if m else ""
    return doc, nodes, url


def verify(repo, cand, required, window):
    """-> dict(state=green|red|undetermined, nodes, url, doc). Anything that is not
    an unambiguous green is not green."""
    script = os.environ.get("DOIT_WINDOW_PREDEPLOY") or str(repo / PREDEPLOY)
    ref = f"ci-verify/{cand[:12]}-win{os.getpid()}"
    env = {**os.environ, "PREDEPLOY_GATE_CI_ONLY": "1", "PREDEPLOY_GATE_ONLY_GREEN": "1",
           "PREDEPLOY_GATE_DEPLOY_SHA": cand, "PREDEPLOY_GATE_REPO_ROOT": str(repo),
           "PREDEPLOY_GATE_CI_CHECKS": "\n".join(required),
           "PREDEPLOY_GATE_REQUIRED_CHECKS": "\n".join(required)}
    dispatch_minutes = dispatch_timeout_min(env)
    env["PREDEPLOY_GATE_DISPATCH_TIMEOUT_MIN"] = (str(int(dispatch_minutes)) if dispatch_minutes.is_integer()
                                                 else str(dispatch_minutes))
    timeout = verify_timeout_secs(env)
    # Ephemeral ci-verify ref: skip the repo pre-push guardrails (fix-the-fix breaker walks every
    # pushed fix: commit and exceeded the 60s git timeout on multi-branch windows, 2026-10-16).
    pushed = git(repo, "push", "--no-verify", "origin", f"{cand}:refs/heads/{ref}", check=False).returncode == 0
    try:
        if not pushed:
            return dict(state="undetermined", nodes=[], url="", doc={}, why="could not push the ci-verify ref")
        proc = subprocess.Popen(["bash", script], cwd=str(repo), env=env, text=True, start_new_session=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)       # the script's own trap cancels + deletes its ref
            try:
                proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
            return dict(state="undetermined", nodes=[], url="", doc={}, why=f"verification timed out after {timeout:.0f}s")
        doc, nodes, url = parse_verdict(out)
        checks = doc.get("checks")
        # predeploy_gate.sh (L-spec-0764) reports each check as {"state": "ran", "run_id", "url"};
        # older fixtures use the bare string "ran". Accept both: a shape mismatch here turned
        # every green CI verdict into a red window with nodes=[] (windows 0101 and 0201, 2026-10-10).
        def _ran(v):
            return v == "ran" or (isinstance(v, dict) and v.get("state") == "ran")
        all_ran = (isinstance(checks, dict) and all(_ran(checks.get(c)) for c in required))
        pg_failed, pg_failures = False, {}
        if all_ran and PG_CHECK in required:
            try:
                pg_failed, pg_failures = pg_result(repo, doc)
            except RuntimeError as e:
                return dict(state="undetermined", nodes=[], url=url, doc=doc, why=str(e))
        detail = dict(pg_failed=pg_failed, pg_failures=pg_failures, other_nodes=list(nodes), output=out)
        nodes = list(dict.fromkeys(nodes + list(pg_failures)))
        if proc.returncode == 0 and all_ran and not nodes and not pg_failed and doc.get("verdict", "green") == "green":
            return dict(state="green", nodes=[], url=url, doc=doc, **detail)
        if proc.returncode == 2 or (proc.returncode == 0 and not doc):
            return dict(state="undetermined", nodes=nodes, url=url, doc=doc,
                        why=f"gate exit {proc.returncode}: " + (out.strip().splitlines() or [""])[-1][:200], **detail)
        return dict(state="red", nodes=nodes, url=url, doc=doc,
                    why=f"gate exit {proc.returncode}", **detail)
    finally:
        if pushed:
            git(repo, "push", "--no-verify", "origin", f":refs/heads/{ref}", check=False)


# ── failing node -> owning specs ─────────────────────────────────────────────
def safe_path(path):
    return bool(path and not path.startswith(("/", "-")) and ".." not in path.split("/")
                and "\\" not in path and "\x00" not in path)


def _exists(repo, rev, path):
    return safe_path(path) and git(repo, "cat-file", "-e", f"{rev}:{path}", check=False).returncode == 0


def _resolve(repo, rev, mod, here):
    base = mod.replace(".", "/")
    for cand in (f"{base}.py", f"{base}/__init__.py"):
        if cand.startswith(IMPORT_ROOTS) and _exists(repo, rev, cand):
            return cand
    return None


def import_closure(repo, rev, path, limit=400):
    """Files reachable from `path` by import (and sibling/ancestor conftest.py), read from the
    candidate commit's blobs — never the working tree. Bounded; only under IMPORT_ROOTS."""
    seen, todo = set(), [path]
    while todo and len(seen) < limit:
        cur = todo.pop()
        if cur in seen or not _exists(repo, rev, cur):
            continue
        seen.add(cur)
        parts = cur.split("/")[:-1]
        for i in range(len(parts) + 1):
            cf = "/".join(parts[:i] + ["conftest.py"])
            if cf not in seen and _exists(repo, rev, cf):
                todo.append(cf)
        text = git(repo, "show", f"{rev}:{cur}", check=False).stdout
        for frm, names, imp in IMPORT_RE.findall(text):
            mods = [frm] if frm else [m.strip().split(" as ")[0] for m in imp.split(",")]
            for m in mods:
                hit = _resolve(repo, rev, m, cur)
                if hit:
                    todo.append(hit)
                if frm:
                    for n in re.findall(r"\w+", names):
                        sub = _resolve(repo, rev, f"{frm}.{n}", cur)
                        if sub:
                            todo.append(sub)
    return seen


JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs")
JS_IMPORT_RE = re.compile(
    r"(?:\b(?:import|export)\b[^;\n]*?\bfrom\s*|\bimport\s*(?:\(\s*)?|\brequire\s*\(\s*)"
    r"[\"'](\.{1,2}/[^\"']+)[\"']")


def js_import_closure(repo, rev, path, limit=400):
    """Relative imports only, bounded and read exclusively from candidate blobs."""
    seen, todo = set(), [path]
    while todo and len(seen) < limit:
        cur = todo.pop()
        if cur in seen or not _exists(repo, rev, cur):
            continue
        seen.add(cur)
        text = git(repo, "show", f"{rev}:{cur}", check=False).stdout
        for specifier in JS_IMPORT_RE.findall(text):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(cur), specifier))
            if not safe_path(target):
                continue
            candidates = ([target] if target.endswith(JS_EXTENSIONS) else
                          [target + ext for ext in JS_EXTENSIONS] +
                          [target + "/index" + ext for ext in JS_EXTENSIONS])
            for candidate in candidates:
                if _exists(repo, rev, candidate):
                    todo.append(candidate)
                    break
    return seen


class OwnershipPaths(dict):
    """A window's diffs plus run-local pg step file evidence; no global cache."""
    def __init__(self, touched, pg_files):
        super().__init__(touched)
        self.pg_files = pg_files


def owners(repo, rev, node, touched):
    """Direct file owners, then import owners; an unknown id has no owners."""
    if node.startswith("pg-parity::"):
        files = getattr(touched, "pg_files", {}).get(node, set())
        found = {s for f in files for s in owners(repo, rev, f, touched)}
        return [s for s in touched if s in found]
    path = node.split("::")[0]
    if not safe_path(path) or not path.endswith((".py", *JS_EXTENSIONS)):
        return []
    direct = [s for s, paths in touched.items() if path in paths]
    if direct:
        return direct
    closure = (import_closure if path.endswith(".py") else js_import_closure)(repo, rev, path)
    return [s for s, paths in touched.items() if paths & closure]


def unattributed_ids(repo, rev, ids, touched):
    return [node for node in ids if not owners(repo, rev, node, touched)]


def call_base_red(project, base, ensure):
    """The ensure wait must be killable, including its nested verify children."""
    argv = [BASE_RED_COMMAND, "base-red", project, base]
    if ensure:
        argv.append("--ensure")
    argv.append("--json")
    incomplete = {"sha": base, "complete": False}
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=verify_timeout_secs(os.environ) + BASE_RED_MARGIN_SECS)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return incomplete
        if proc.returncode != 0:
            return incomplete
        answer = json.loads(out)
        if (not isinstance(answer, dict) or answer.get("sha") != base or
                answer.get("complete") is not True or answer.get("undetermined") or
                not isinstance(answer.get("ids"), dict)):
            return incomplete
        return answer
    except (OSError, ValueError, subprocess.SubprocessError):
        return incomplete


def base_red_snapshot(project, base):
    answer = call_base_red(project, base, True)
    if not answer.get("complete") or answer.get("undetermined"):
        answer = call_base_red(project, base, False)
    return answer


def candidate_logs(repo, doc):
    """Fetch unique candidate run zips via the shared quota gate, never log-view."""
    import ghlimit
    checks = doc.get("checks", {})
    runs = {str(c["run_id"]) for c in checks.values()
            if isinstance(c, dict) and str(c.get("run_id", "")).isdigit()}
    if not runs:
        return []
    remote = git(repo, "remote", "get-url", "origin").stdout.strip()
    match = re.fullmatch(r"(?:https://github.com/|git@github.com:|ssh://git@github.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?", remote)
    if not match:
        raise RuntimeError("candidate logs repository unavailable")
    owner, logs = match[1], []
    for rid in sorted(runs):
        if not ghlimit.gate()["ok"]:
            raise RuntimeError("candidate log quota unavailable")
        proc = subprocess.run([ghlimit.real_gh(), "api", f"repos/{owner}/actions/runs/{rid}/logs"],
                              capture_output=True, timeout=120)
        if proc.returncode:
            raise RuntimeError("candidate logs unavailable")
        with zipfile.ZipFile(io.BytesIO(proc.stdout)) as archive:
            members = [(m.filename, archive.read(m).decode("utf-8-sig"))
                       for m in archive.infolist() if not m.is_dir()]
        if not members:
            raise RuntimeError("empty candidate logs zip")
        # Raw step members need the actual job/step display names for parse_log.
        pg_members = [name for name, _ in members if "pg-parity" in name.lower()]
        jobs = []
        if pg_members:
            rc, out, _ = ghlimit.run(["gh", "api", f"repos/{owner}/actions/runs/{rid}/jobs?per_page=100"])
            if rc:
                raise RuntimeError("candidate step metadata unavailable")
            jobs = json.loads(out)["jobs"]
        named_steps = set()
        for name, text in members:
            if name in pg_members:
                number = re.match(r"(\d+)_", pathlib.PurePosixPath(name).name)
                steps = [step["name"] for job in jobs if "pg-parity" in job["name"].lower()
                         for step in job.get("steps", []) if number and step["number"] == int(number[1])]
                if len(steps) == 1:
                    named_steps.add(steps[0])
                    text = "\n".join("pg-parity\t" + steps[0] + "\t" + line for line in text.splitlines())
            logs.append((rid, text))
        # Some archives contain only aggregate job logs. Metadata still names
        # failed steps, even when their output cannot safely identify a file.
        for job in jobs:
            if "pg-parity" not in job["name"].lower():
                continue
            for step in job.get("steps", []):
                if step.get("conclusion") == "failure" and step["name"] not in named_steps:
                    logs.append((rid, "pg-parity\t" + step["name"] + "\t##[error]failed step"))
    return logs


def candidate_failures(repo, res):
    """Canonical typed ids. All log format knowledge belongs to base_red."""
    import base_red
    kinds = ("pytest", "pg-parity", "vitest", "suite-error")
    result = {k: set() for k in kinds}
    result.update(undetermined=[], pg_files={})
    pg_check = res.get("doc", {}).get("checks", {}).get(PG_CHECK, {})
    pg_run = str(pg_check.get("run_id", "")) if isinstance(pg_check, dict) else ""
    pg_nodes = set(res.get("pg_failures", {}))
    # Retain structured verdict ids for older gate versions as well as NEW logs.
    for node in res.get("nodes", []):
        if node.startswith("pg-parity::") and pg_nodes:
            continue
        kind = ("pg-parity" if node.startswith("pg-parity::") else "pytest" if ".py::" in node else
                "vitest" if "::" in node else "suite-error")
        result[kind].add(node)
    texts = [(pg_run, res.get("output", ""))] + candidate_logs(repo, res.get("doc", {}))
    for rid, text in texts:
        parsed = base_red.parse_log(text)
        for kind in kinds:
            ids = parsed[kind]
            if kind == "pg-parity" and pg_nodes and rid == pg_run:
                ids = set()
            result[kind].update(ids)
        result["undetermined"].extend(parsed.get("undetermined", []))
        files = {p for p in re.findall(r"[\w./-]+\.py\b", text) if safe_path(p)
                 and pathlib.PurePosixPath(p).name.startswith("test")}
        for step in parsed["pg-parity"]:
            result["pg_files"].setdefault(step, set()).update(files)
    result["pytest"].update(pg_nodes)
    return result


# ── orchestration ────────────────────────────────────────────────────────────
def pending_from_ledger(events):
    """Specs whose newest merge-gate-clean is newer than their newest shipped/window-dropped."""
    clean, done = {}, {}
    for e in events:
        if e.get("project") not in (PROJECT, None):
            continue
        s, t = e.get("subject"), fold.ts(e.get("ts"))
        if e.get("type") == "merge-gate-clean" and e.get("branch"):
            clean[s] = (t, e["branch"])
        elif e.get("type") in ("shipped", "window-dropped"):
            done[s] = max(done.get(s, t), t)
    rows = sorted((v[0], s, v[1]) for s, v in clean.items() if s not in done or v[0] > done[s])
    return [(s, b) for _, s, b in rows]


def resolve_entries(repo, spec_args, events):
    pairs = []
    if spec_args:
        newest = {}
        for e in events:
            if e.get("type") == "merge-gate-clean" and e.get("branch"):
                newest[e.get("subject")] = e["branch"]
        for a in spec_args:
            s, _, b = a.partition(":")
            if not (b or newest.get(s)):
                raise RuntimeError(f"{s}: no merge-gate-clean branch on file (pass {s}:<branch>)")
            pairs.append((s, b or newest[s]))
    else:
        pairs = pending_from_ledger(events)
    out = []
    for s, b in pairs:
        p = git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}^{{commit}}", check=False)
        if p.returncode:
            raise RuntimeError(f"{s}: branch {b} does not resolve in {repo}")
        out.append((s, b, p.stdout.strip()))
    return out


def advance(repo, main, old, new, window, kept, dropped, runs, urls, entries, dashboard, rechained=(), pre_existing=()):
    git(repo, "update-ref", f"refs/heads/{main}", new, old)       # old value asserted
    branch_of = {s: b for s, b, _ in entries}
    for s in kept:
        ev("shipped", s, sha=new, branch=branch_of[s], window=window,
           **({"rechained": True} if s in rechained else {}))
    ev("window-verified", window, sha=new, runs=urls, kept=kept, dropped=dropped,
       pre_existing=list(pre_existing),
       actions_minutes=runs * RUN_MINUTES + (runs * DASHBOARD_MINUTES if dashboard else 0))


def run_window(window, project, spec_args, main, fixes_base=False):
    repo = fold.ROOT / "repos" / project
    if not repo.is_dir():
        sys.exit(f"gate-window: no repo at {repo}")
    events = fold.read_events()
    entries = resolve_entries(repo, spec_args, events)
    if not entries:
        print("gate-window: nothing gate-clean is waiting"); return 2
    if len(entries) > MAX_MERGES:
        sys.exit(f"gate-window: {len(entries)} merges exceeds the window cap of {MAX_MERGES}")
    base = git(repo, "rev-parse", "--verify", f"refs/heads/{main}^{{commit}}").stdout.strip()
    try:
        ci_checks = required_checks(repo, base)
    except RuntimeError as e:
        escalate(window, str(e))
        print("gate-window: " + str(e))
        return 2
    base_red_answer = base_red_snapshot(project, base)
    baseline = verify(repo, base, [PG_CHECK], window)
    if baseline["state"] != "green" and not (fixes_base and baseline["state"] == "red"):
        why = "base-not-releasable: pg-parity is " + baseline["state"]
        escalate(window, why)
        print("gate-window: " + why)
        return 2
    base_failures = baseline.get("pg_failures", {})
    enabled = os.environ.get("DOIT_WINDOW_RECHAIN", "off") == "on"
    migrations, deferred = set(), []
    if enabled:
        cap = int(os.environ.get("DOIT_WINDOW_MAX_MIGRATIONS", "3"))
        if cap < 1:
            raise RuntimeError("DOIT_WINDOW_MAX_MIGRATIONS must be positive")
        for s, b, sha in entries:
            if len(rechain.added_versions(repo, base, sha)) == 1:
                if len(migrations) >= cap:
                    deferred.append(s)
                    ev("window-deferred", s, window=window, branch=b, reason="migration-cap", cap=cap)
                else:
                    migrations.add(s)
        entries = [e for e in entries if e[0] not in deferred]
        if deferred:
            print(f"gate-window: deferred {len(deferred)} migration(s), reason=migration-cap cap={cap}")
    ev("window-opened", window, specs=[s for s, _, _ in entries], base=base[:12], fixes_base=fixes_base)
    touched = {s: set(git(repo, "diff", "--name-only", f"{base}...{b}").stdout.split()) for s, b, _ in entries}
    dropped, urls, runs, dashboard = [], [], 0, False
    pre_existing_all = set()
    conflicted_all = {}

    def prepare(batch):
        candidate, kept, clashes = build_candidate(repo, window, base, batch)
        migration_kept = [s for s in kept if s in migrations]
        if migration_kept:
            try:
                candidate = rechain.fingerprint(repo, candidate, window, ev=ev)
                git(repo, "update-ref", f"refs/heads/window/{window}", candidate)
            except (ValueError, OSError, subprocess.SubprocessError):
                for s in migration_kept:
                    ev("rechain-declined", s, window=window, why="final-tree fingerprint or private PG failed")
                    clashes[s] = [rechain.FINGERPRINT]
                rest = [e for e in batch if e[0] not in migrations]
                candidate, kept, more = build_candidate(repo, window, base, rest)
                clashes.update(more)
                print(f"gate-window: excluded {len(migration_kept)} migration(s), reason=fingerprint-failed")
        return candidate, kept, clashes

    cand, merged, conflicts = prepare(entries)
    for s, paths in conflicts.items():
        ev("merge-conflict", s, files=paths, window=window)
    conflicted_all.update(conflicts)
    dropped += list(conflicts)
    if not merged:
        print("gate-window: every branch conflicts — nothing to verify")
        escalate(window, "every branch in the window conflicts; builder re-dispatch via the merge-conflict flow")
        return 2
    live = list(merged)
    while True:
        changed = git(repo, "diff", "--name-only", f"{base}..{cand}").stdout.split()
        has_dash = any(p.startswith("dashboard/") for p in changed)
        dashboard = dashboard or has_dash
        required = [c for c in ci_checks if c != DASHBOARD_CHECK or has_dash]
        res = verify(repo, cand, required, window)
        runs += 1
        if res["url"]:
            urls.append(res["url"])
        if res["state"] == "green":
            break
        if res["state"] == "undetermined":
            escalate(window, f"window {window} could not be verified ({res.get('why')}); master unmoved, no spec dropped")
            print(f"gate-window: undetermined — {res.get('why')}"); return 2
        try:
            failures = candidate_failures(repo, res)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, zipfile.BadZipFile,
                zipfile.LargeZipFile, zlib.error, subprocess.SubprocessError) as exc:
            escalate(window, f"unnamed new failures: candidate log evidence unavailable ({type(exc).__name__}); named ids={json.dumps(res.get('nodes', []))}; master unmoved, no spec dropped")
            return 2
        nodes = sorted(set().union(*(failures[k] for k in ("pytest", "pg-parity", "vitest", "suite-error"))))
        if failures["undetermined"] or not nodes:
            escalate(window, f"unnamed new failures: {failures['undetermined']}; NEW ids={json.dumps(nodes)}; master unmoved, no spec dropped")
            return 2
        classification = {"complete": False}
        if base_red_answer.get("complete") and not base_red_answer.get("undetermined"):
            import base_red
            classification = base_red.classify(project, base, nodes)
        if not classification.get("complete"):
            escalate(window, f"base incomplete; NEW ids={json.dumps(nodes)}; master unmoved, no spec dropped")
            return 2
        pre_existing = set(classification["pre_existing"])
        # A red --fixes-base baseline has stronger, signature-level evidence.
        for node, signature in res.get("pg_failures", {}).items():
            if node in base_failures:
                if base_failures[node] == signature:
                    pre_existing.add(node)
                else:
                    pre_existing.discard(node)
        pre_existing = sorted(pre_existing)
        if pre_existing:
            ev("window-pre-existing", window, ids=pre_existing, base_sha=base,
               count=len(pre_existing), nodes=pre_existing, run_url=res["url"])
            print(f"gate-window: {len(pre_existing)} failure(s) pre-existing at base: {json.dumps(pre_existing)}")
            pre_existing_all.update(pre_existing)
        res["nodes"] = [n for n in nodes if n not in pre_existing]
        if not res["nodes"]:
            if fixes_base and res.get("pg_failed"):
                escalate(window, "base-not-releasable: --fixes-base window has not repaired pg-parity")
                return 2
            break  # all-pre-existing takes precedence over the two-run cap
        # red
        by_id = {s: (b, sha) for s, b, sha in entries}
        paths = OwnershipPaths({s: touched[s] for s in live}, failures["pg_files"])
        unattributed = unattributed_ids(repo, cand, res["nodes"], paths)
        if runs >= 2 or unattributed:
            drop = list(live)
        else:
            drop = []
            for n in res["nodes"]:
                drop += [s for s in owners(repo, cand, n, paths) if s not in drop]
        bisected = []
        if enabled and not unattributed and runs == 1 and any(s in migrations for s in drop):
            stack = [s for s in live if s in migrations]
            bisected = stack[1:]
            drop = [s for s in drop if s not in migrations] + bisected
        for s in drop:
            ev("window-dropped", s, window=window, nodes=res["nodes"], run_url=res["url"], branch=by_id[s][0],
               **({"reason": "unattributed"} if unattributed else {"reason": "migration-bisect"} if s in bisected else {}))
            print(f"gate-window: dropped {s} nodes={json.dumps(res['nodes'])}")
        dropped += drop
        live = [s for s in live if s not in drop]
        if not live or runs >= 2:
            why = "unattributed " + json.dumps(unattributed) if unattributed else "new ids " + json.dumps(res["nodes"])
            escalate(window, f"window {window} is red after {runs} run(s); {why}; dropped specs={json.dumps(drop)}; whole window dropped, master unmoved")
            return 2
        rest = [e for e in entries if e[0] in live]
        cand, live, more = prepare(rest)
        for s, paths in more.items():
            ev("merge-conflict", s, files=paths, window=window)
        dropped += list(more)
        if not live:
            escalate(window, "remaining merges conflict after the drop")
            return 2
    rechained = set()
    if enabled:
        # Only merges reachable from the final candidate count as shipped re-chains.
        for sha in git(repo, "rev-list", "--first-parent", f"{base}..{cand}").stdout.splitlines():
            message = git(repo, "show", "-s", "--format=%s", sha).stdout.strip()
            if message.endswith("(mechanical re-chain)"):
                rechained.update(s for s in live if message.startswith(f"merge({s}):"))
    advance(repo, main, base, cand, window, live, dropped, runs, urls, entries, dashboard, rechained,
            pre_existing=sorted(pre_existing_all))
    print(f"gate-window: advanced {main} -> {cand[:12]}  shipped={live} dropped={dropped}")
    return 1 if dropped or deferred else 0


def cleanup(repo, window):
    git(repo, "update-ref", "-d", f"refs/heads/window/{window}", check=False)


def main_(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__); return 0
    window, project, specs, main = argv[0], None, [], os.environ.get("DOIT_MAIN")
    fixes_base = False
    it = iter(argv[1:])
    for a in it:
        if a == "--project": project = next(it, None)
        elif a == "--specs": specs = [x for x in (next(it, "") or "").split(",") if x]
        elif a == "--main": main = next(it, None)
        elif a == "--fixes-base": fixes_base = True
        else: sys.exit(f"gate-window: unknown argument {a!r}\n{__doc__}")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", window):
        sys.exit(f"gate-window: {window!r} is not a window id")
    if project != PROJECT:
        sys.exit(f"gate-window: only project {PROJECT} uses merge windows (got {project!r}); other projects merge directly")
    if os.environ.get("DOIT_WINDOW_GATE") == "off":
        for s in specs or [s for s, _ in pending_from_ledger(fold.read_events())]:
            ev("window-gate-bypassed", s, window=window)
        print("gate-window: DOIT_WINDOW_GATE=off — bypassed; use the direct merge path"); return 4
    if not main:
        try:
            import look, push_origin
            main = dict(push_origin.targets(look.load_toml())).get(PROJECT)
        except Exception:
            main = None
    main = main or "master"
    if not acquire():
        print("gate-window: another window holds the lock"); return 3
    repo = fold.ROOT / "repos" / project
    try:
        return run_window(window, project, specs, main, fixes_base)
    except RuntimeError as e:
        print(f"gate-window: {e}", file=sys.stderr)
        try:
            escalate(window, f"gate-window failed before a verdict: {e}"[:300])
        except SystemExit:
            pass
        return 2
    finally:
        cleanup(repo, window)
        release()


if __name__ == "__main__":
    sys.exit(main_(sys.argv[1:]))
