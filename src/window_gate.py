#!/usr/bin/env python3
"""window_gate — verify a merge WINDOW's candidate tip before master moves (L-spec-0765).

    doit gate-window <window-id> --project albert-scott [--specs A,B,C] [--after WINDOW] [--main BRANCH] [--fixes-base]

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
               subset. At most two rounds on this path; a second red drops the window.
               A first red with only unattributed ids splits into bounded halves,
               shipping the first verified half and leaving other green halves pending.

Candidate trees with a green ledger result covering the required checks in the
last seven days reuse that result without another dispatch.

Exit codes (the Executor contract, agents/executor.md, mirrors these):
  0  advanced: every spec in the window shipped
  1  partially advanced: some specs dropped (window-dropped / merge-conflict), the rest shipped
  2  red or undetermined: master did not move (an escalation-blocking is filed)
  3  no slot free, predecessor absent/not yet a candidate/past its first verification: retry next pass
  4  DOIT_WINDOW_GATE=off: bypassed, one window-gate-bypassed event per spec;
     use the direct merge path (operator-declared CI outage only)

Env: DOIT_WINDOW_PREDEPLOY (override the script path), DOIT_WINDOW_VERIFY_TIMEOUT_SECS.
"""
import io, json, math, os, pathlib, posixpath, re, shlex, shutil, signal, subprocess, sys, time, zipfile, zlib
from contextvars import ContextVar
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
TRAIN_POLL_S = 15
_VERIFY_TRAIN = ContextVar("window_train", default=None)


class Red(Exception):
    pass


def git(repo, *args, check=True, env=None, inp=None):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       timeout=60, env=env, input=inp)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip() or 'failed'}")
    return p


def tree_of(repo, sha):
    """Resolve content identity; an unresolved or malformed tree is undetermined."""
    tree = git(repo, "rev-parse", f"{sha}^{{tree}}").stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise RuntimeError("candidate tree unavailable")
    return tree


def prior_result(events, tree):
    """Newest determined result within seven days (ledger order breaks ties)."""
    now = datetime.now(timezone.utc)
    newest, newest_ts = None, None
    for event in events:
        if (not isinstance(event, dict) or event.get("type") != "window-tree-verified" or event.get("tree") != tree
                or type(event.get("green")) is not bool):
            continue
        try:
            stamp = fold.ts(event.get("ts"))
            # fold.ts intentionally defaults malformed timestamps to NOW for other
            # consumers. A cache entry must have an actual timestamp instead.
            datetime.fromisoformat(str(event["ts"]).replace("Z", "+00:00"))
            if not now - timedelta(days=7) <= stamp <= now:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        if newest_ts is None or stamp >= newest_ts:
            newest, newest_ts = event, stamp
    return newest


def ev(typ, subject, **kv):
    """Append one executor event. Every value goes in as JSON (`k:=`), so a
    list/number/string round-trips exactly. The actor is the filename (D90)."""
    os.environ["DOIT_LEDGER_FILE"] = os.environ.get("DOIT_GATE_LEDGER_FILE", "L-executor-0001.jsonl")
    kv.setdefault("project", PROJECT)
    original_now = fold.NOW
    try:
        fold.NOW = datetime.now(timezone.utc)
        fold.append([typ, subject] + [f"{k}:={json.dumps(v)}" for k, v in kv.items()])
    finally:
        fold.NOW = original_now


def escalate(window, why, outcome=None):
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
    ev("escalation-blocking", window, why=why, outcome=outcome,
       default="master stays where it is; the window's specs stay unshipped until an operator decides",
       deadline=due,
       revert="rerun `doit gate-window` once the cause is fixed, or DOIT_WINDOW_GATE=off for a declared CI outage")


# ── lock ────────────────────────────────────────────────────────────────────
def lock_path(slot=0, root=None):
    return pathlib.Path(root if root is not None else fold.ROOT) / "state" / f"window-gate.{slot}.lock"


def lock_holder(slot, root=None):
    try:
        words = lock_path(slot, root).read_text().split()
        pid = int(words[0])
        if pid <= 0:
            return None
        os.kill(pid, 0)
        return pid, words[1] if len(words) == 2 else None
    except PermissionError:
        return (pid, words[1] if len(words) == 2 else None) if 'pid' in locals() else (None, None)
    except (OSError, ValueError, IndexError):
        return None


def acquire(window="", slot=0):
    p = lock_path(slot)
    p.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()} {window}".encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            if lock_holder(slot) is not None:
                return False                       # holder alive
            p.unlink(missing_ok=True)              # stale: its owner is gone
    return False


def release(slot=0):
    lock_path(slot).unlink(missing_ok=True)


def in_flight(events, root=None):
    """Candidates with a live, matching slot holder; cancellation is not terminal."""
    latest, terminal = {}, set()
    for event in events:
        name, kind = event.get("subject"), event.get("type")
        if kind == "window-candidate":
            latest[name] = event
        elif kind in ("window-verified", "escalation-blocking"):
            terminal.add(name)
    rows = []
    for name, event in latest.items():
        slot = event.get("slot")
        if name in terminal or type(slot) is not int or slot < 0:
            continue
        holder = lock_holder(slot, root)
        if holder is not None and holder[1] == name:
            rows.append(dict(window=name, **{key: event.get(key) for key in
                             ("sha", "verified_on", "after", "specs", "slot")}))
    return rows


def train_events():
    """A torn or unreadable ledger cannot establish a speculative merge base."""
    if fold.EVENTS.exists():
        for path in fold.EVENTS.iterdir():
            if path.suffix != ".jsonl":
                continue
            for line in path.read_text().splitlines():
                if line.strip():
                    try:
                        if not isinstance(json.loads(line), dict):
                            raise ValueError("event is not an object")
                    except ValueError as exc:
                        raise OSError("train ledger unreadable") from exc
    return fold.read_events()


def red_rate(events, now, hours=24):
    """Distinct terminal windows in the trailing day; a dropped subset is red."""
    now = datetime.fromisoformat(str(now).replace("Z", "+00:00")) if not isinstance(now, datetime) else now
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    known = {e.get("subject") for e in events if e.get("type") in ("window-opened", "window-candidate", "window-verified")}
    known.update(e.get("window") for e in events if e.get("type") in ("window-dropped", "merge-conflict"))
    terminals, reds = {}, set()
    for event in events:
        kind, name = event.get("type"), event.get("subject")
        if kind == "window-dropped" and event.get("nodes"):
            reds.add(event.get("window"))
        if kind == "window-verified" or (kind == "escalation-blocking" and
                (name in known or event.get("outcome") in ("red", "undetermined", "base-not-releasable"))):
            try:
                stamp = datetime.fromisoformat(str(event.get("ts")).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            terminals[name] = stamp
            if event.get("dropped") or event.get("outcome") == "red":
                reds.add(name)
    windows = {name for name, stamp in terminals.items() if now - timedelta(hours=hours) <= stamp <= now}
    red = len(windows & reds)
    return dict(rate=red / len(windows) if len(windows) >= 5 else None, red=red, windows=len(windows))


def train_depth(env, toml_path=None, root=None, events=None, now=None):
    """Pure configuration/health projection; reading it never emits or notifies."""
    value = 2
    try:
        import tomllib
        with open(toml_path if toml_path is not None else DEFAULT_TOML_PATH, "rb") as stream:
            value = tomllib.load(stream).get("window_gate", {}).get("train_depth")
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        value = None
    value = env.get("DOIT_WINDOW_TRAIN_DEPTH", value)
    configured = int(value) if ((type(value) is int or isinstance(value, str) and re.fullmatch(r"[0-9]+", value))
                                and int(value) >= 1) else 1
    disk_pct, reason, depth = None, "configured", configured
    try:
        disk = shutil.disk_usage(pathlib.Path(root if root is not None else env.get("DOIT_ROOT", fold.ROOT)))
        disk_pct = 100 * disk.used / disk.total
        if disk_pct > 90:
            depth, reason = 1, "disk-over-90"
    except (OSError, ValueError, ZeroDivisionError):
        depth, reason = 1, "disk-unreadable"
    try:
        rate = red_rate(train_events() if events is None else events, now or datetime.now(timezone.utc))["rate"]
    except (OSError, ValueError, TypeError):
        rate, depth, reason = None, 1, "ledger-unreadable"
    if rate is not None and rate > .40:
        depth, reason = 1, "red-rate-over-0.40" if depth != 1 or reason == "configured" else reason
    return dict(depth=depth, configured=configured, disk_pct=disk_pct, red_rate=rate, reason=reason)


def record_depth(depth_dict, events, env):
    previous = next((e.get("depth") for e in reversed(events) if e.get("type") == "train-depth"), depth_dict["configured"])
    if previous == depth_dict["depth"]:
        return
    telegram = "sent"
    try:
        script = pathlib.Path(env.get("DOIT_ROOT", fold.ROOT)) / "telegram" / "tg-send.sh"
        text = f"Merge train depth {previous} -> {depth_dict['depth']}: {depth_dict['reason']}"
        if subprocess.run(["bash", str(script), text], capture_output=True, timeout=30).returncode:
            telegram = "failed"
    except (OSError, subprocess.SubprocessError):
        telegram = "failed"
    ev("train-depth", "merge-train", depth=depth_dict["depth"], **{"from": previous},
       reason=depth_dict["reason"], disk_pct=depth_dict["disk_pct"], red_rate=depth_dict["red_rate"], telegram=telegram)


class TrainCancelled(Exception):
    pass


def predecessor_state(repo, main, after, verified_on, events=None):
    """None while pending, empty string when mergeable, otherwise a cancellation reason."""
    try:
        events = train_events() if events is None else events
        related = [e for e in events if e.get("subject") == after or e.get("window") == after]
        if any(e.get("type") == "window-dropped" and e.get("nodes") for e in related):
            return "predecessor-red"
        escalations = [e for e in related if e.get("subject") == after and e.get("type") == "escalation-blocking"]
        if escalations:
            return "predecessor-red" if escalations[-1].get("outcome") == "red" else "predecessor-undetermined"
        verified = [e for e in related if e.get("subject") == after and e.get("type") == "window-verified"]
        if not verified:
            return None if any(e["window"] == after for e in in_flight(events)) else "predecessor-dead"
        if verified[-1].get("dropped") or any(e.get("type") in ("window-deferred", "merge-conflict") for e in related):
            return "predecessor-partial"
        if any(e.get("type") == "shipped" and e.get("rechained") for e in related):
            return "predecessor-rechained"
        if verified[-1].get("sha") != verified_on:
            return "base-moved"
        if git(repo, "rev-parse", f"refs/heads/{main}").stdout.strip() != verified_on:
            return "base-moved"
        origin = git(repo, "ls-remote", "origin", f"refs/heads/{main}", check=False)
        lines = origin.stdout.splitlines()
        if origin.returncode or len(lines) != 1:
            return "base-moved"
        fields = lines[0].split()
        if len(fields) != 2 or fields[1] != f"refs/heads/{main}" or not re.fullmatch(r"[0-9a-f]{40}", fields[0]):
            return "base-moved"
        if fields[0] != verified_on and git(repo, "merge-base", "--is-ancestor", fields[0], verified_on, check=False).returncode:
            return "base-moved"
        return ""
    except (OSError, RuntimeError, ValueError, TypeError, subprocess.SubprocessError):
        return "base-moved"


def train_poll_secs():
    return _positive_timeout(os.environ.get("DOIT_WINDOW_TRAIN_POLL_S")) or TRAIN_POLL_S


def wait_predecessor(train, *, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + verify_timeout_secs(os.environ) + 600
    while True:
        reason = predecessor_state(**train)
        if reason is not None:
            if reason:
                raise TrainCancelled(reason)
            return
        left = deadline - clock()
        if left <= 0:
            raise TrainCancelled("predecessor-timeout")
        sleep(min(train_poll_secs(), left))


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


def max_merges(env, toml_path=None):
    """Resolve a positive integer cap: process env, versioned TOML, then 10."""
    def positive_integer(value):
        number = _positive_timeout(value)
        return int(number) if number is not None and number.is_integer() else None

    override = positive_integer(env.get("DOIT_WINDOW_MAX_MERGES"))
    if override is not None:
        return override
    try:
        import tomllib
        path = pathlib.Path(toml_path) if toml_path is not None else DEFAULT_TOML_PATH
        with open(path, "rb") as f:
            table = tomllib.load(f).get("window_gate")
    except (OSError, ValueError, TypeError, ImportError):
        table = None
    configured = positive_integer(table.get("max_merges")) if isinstance(table, dict) else None
    return configured if configured is not None else 10


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
            train = _VERIFY_TRAIN.get()
            if train is None or train["window"] != window:
                out, _ = proc.communicate(timeout=timeout)
            else:
                deadline = time.monotonic() + timeout
                while True:
                    reason = predecessor_state(**{k: v for k, v in train.items() if k != "window"})
                    if reason:
                        raise TrainCancelled(reason)
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise subprocess.TimeoutExpired(proc.args, timeout)
                    try:
                        out, _ = proc.communicate(timeout=min(train_poll_secs(), left))
                        break
                    except subprocess.TimeoutExpired:
                        continue
        except TrainCancelled:
            stop_verification(proc)
            raise
        except subprocess.TimeoutExpired:
            stop_verification(proc)
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


def stop_verification(proc):
    """The gate's SIGTERM trap cancels CI and removes its ref; always reap it."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()


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


def advance(repo, main, old, new, window, kept, dropped, runs, urls, entries, dashboard, rechained=(), pre_existing=(), actions_minutes=None, after=None):
    replaced = git(repo, "rev-parse", f"refs/heads/{main}").stdout.strip()
    git(repo, "update-ref", f"refs/heads/{main}", new, old)       # old value asserted
    branch_of = {s: b for s, b, _ in entries}
    for s in kept:
        ev("shipped", s, sha=new, branch=branch_of[s], window=window,
           **({"rechained": True} if s in rechained else {}))
    ev("window-verified", window, sha=new, verified_on=old, replaced=replaced,
       **({"after": after} if after else {}), runs=urls, kept=kept, dropped=dropped,
       pre_existing=list(pre_existing),
       actions_minutes=(runs * RUN_MINUTES + (runs * DASHBOARD_MINUTES if dashboard else 0)
                        if actions_minutes is None else actions_minutes))


def bisect_window(window, live, prepare, judge, drop, conflict):
    """Visit both halves in entry order, returning the first verified candidate.

    All builds and verdict classification go through run_window's callbacks;
    a green sibling is never unioned into an unverified candidate.
    """
    chosen = None

    def split(batch):
        nonlocal chosen
        k = len(batch) // 2
        halves = [batch[:k], batch[k:]]
        ev("window-bisected", window, window=window, halves=halves)
        for half in halves:
            cand, kept, clashes = prepare(half)
            conflict(clashes)
            if not kept:
                escalate(window, "bisection half has no mergeable specs; half remains pending", "undetermined")
                continue
            result = judge(cand, kept)
            if result["state"] == "green":
                if chosen is None:
                    chosen = (cand, kept)
            elif result["state"] == "undetermined":
                continue
            elif len(kept) > 1 and len(result["unattributed"]) == len(result["nodes"]):
                split(kept)
            else:
                if len(kept) == 1:
                    owners_to_drop, reason = kept, "bisect-single"
                else:
                    owners_to_drop = list(dict.fromkeys(
                        owner for node in result["nodes"]
                        for owner in owners(result["repo"], cand, node, result["paths"])))
                    reason = "bisect-owner"
                drop(owners_to_drop, result, reason)

    split(live)
    return chosen


def run_window(window, project, spec_args, main, fixes_base=False, after=None, slot=0):
    events = train_events()
    record_depth(train_depth(os.environ, events=events), events, os.environ)
    repo = fold.ROOT / "repos" / project
    master = git(repo, "rev-parse", "--verify", f"refs/heads/{main}^{{commit}}").stdout.strip()
    base, root_base = master, master
    if after:
        candidates = {e["subject"]: e for e in events if e.get("type") == "window-candidate"}
        candidate = candidates.get(after)
        if candidate is None:
            print("gate-window: predecessor has no window-candidate yet"); return 3
        base = candidate["sha"]
        seen = set()
        active = {entry["window"] for entry in in_flight(events)}
        while candidate.get("after") in active:
            if candidate["subject"] in seen or candidate["after"] not in candidates:
                raise RuntimeError("train root is unreadable")
            seen.add(candidate["subject"])
            candidate = candidates[candidate["after"]]
        root_base = candidate["verified_on"]
    for attempt in range(2):
        train = dict(repo=repo, main=main, after=after, verified_on=base) if after else None
        token = _VERIFY_TRAIN.set(dict(train, window=window) if train else None)
        try:
            return run_attempt(window, project, spec_args, main, fixes_base, base, root_base, after, slot, train,
                               rebased=bool(attempt))
        except TrainCancelled as exc:
            if attempt or not after:
                escalate(window, "second advance failed; master unmoved", "undetermined")
                return 2
            master = git(repo, "rev-parse", f"refs/heads/{main}").stdout.strip()
            ev("window-cancelled", window, after=after, reason=str(exc), verified_on=base, master=master)
            print(f"gate-window: cancelled after={after} reason={exc}; rebuilding on master in slot {slot}")
            base = root_base = master
            after = None
        finally:
            _VERIFY_TRAIN.reset(token)
    return 2


def run_attempt(window, project, spec_args, main, fixes_base, base, root_base, after, slot, train, rebased=False):
    repo = fold.ROOT / "repos" / project
    if not repo.is_dir():
        sys.exit(f"gate-window: no repo at {repo}")
    events = train_events()
    entries = resolve_entries(repo, spec_args, events)
    if not entries:
        print("gate-window: nothing gate-clean is waiting"); return 2
    cap = max_merges(os.environ)
    if len(entries) > cap:
        sys.exit(f"gate-window: {len(entries)} merges exceeds the window cap of {cap}")
    try:
        ci_checks = required_checks(repo, base)
    except RuntimeError as e:
        escalate(window, str(e), "undetermined")
        print("gate-window: " + str(e))
        return 2
    base_red_answer = base_red_snapshot(project, root_base)
    baseline = verify(repo, root_base, [PG_CHECK], window)
    if baseline["state"] != "green" and not (fixes_base and baseline["state"] == "red"):
        why = "base-not-releasable: pg-parity is " + baseline["state"]
        escalate(window, why, "base-not-releasable")
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
    actions_minutes = 0
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
        if kept:
            if rebased:
                # Identical parents, message and second-resolution timestamps can
                # otherwise recreate the cancelled SHA. Give the fresh attempt
                # its own identity while retaining exactly the rebuilt tree.
                candidate = git(repo, *IDENT, "commit-tree", tree_of(repo, candidate), "-p", candidate,
                                "-m", f"verify({window}): rebuild after train cancellation").stdout.strip()
                git(repo, "update-ref", f"refs/heads/window/{window}", candidate)
            ev("window-candidate", window, sha=candidate, verified_on=base, specs=kept, slot=slot,
               **({"after": after} if after else {}))
        return candidate, kept, clashes

    by_id = {s: (b, sha) for s, b, sha in entries}

    def note_conflicts(conflicts):
        for s, paths in conflicts.items():
            ev("merge-conflict", s, files=paths, window=window)
        conflicted_all.update(conflicts)
        dropped.extend(s for s in conflicts if s not in dropped)

    def drop_specs(specs, result, reason):
        for s in specs:
            ev("window-dropped", s, window=window, nodes=result["nodes"],
               run_url=result["url"], branch=by_id[s][0], reason=reason)
            print(f"gate-window: dropped {s} reason={reason} nodes={json.dumps(result['nodes'])}")
        dropped.extend(s for s in specs if s not in dropped)
        print(f"gate-window: dropped {len(specs)} spec(s), reason={reason}")

    def record(res, tree, required):
        if tree is not None:
            ev("window-tree-verified", window, window=window, sha=res["candidate"],
               tree=tree, green=res["state"] == "green", new_ids=res["nodes"], required=required)

    def judge(cand, kept, *, half=False):
        nonlocal runs, dashboard, actions_minutes
        changed = git(repo, "diff", "--name-only", f"{base}..{cand}").stdout.split()
        has_dash = any(p.startswith("dashboard/") for p in changed)
        dashboard = dashboard or has_dash
        candidate_required = [c for c in ci_checks if c != DASHBOARD_CHECK or has_dash]
        tree = None
        try:
            tree = tree_of(repo, cand)
            prior = prior_result(fold.read_events(), tree)
        except (OSError, RuntimeError, ValueError, TypeError, subprocess.SubprocessError):
            prior = None
        if (prior is not None and prior["green"] is True
                and isinstance(prior.get("required"), list)
                and all(isinstance(c, str) for c in prior["required"])
                and set(prior["required"]) >= set(candidate_required)):
            print(f"gate-window: tree {tree} already verified in {prior.get('window', prior.get('subject'))}")
            return dict(state="green", nodes=[], url="", doc={})
        res = dict(verify(repo, cand, candidate_required, window), candidate=cand)
        # No attribution or shipment may escape a verification on an unmerged base.
        if train:
            wait_predecessor(train)
        runs += 1
        actions_minutes += RUN_MINUTES + (DASHBOARD_MINUTES if has_dash else 0)
        if res["url"]:
            urls.append(res["url"])
        if res["state"] == "green":
            record(res, tree, candidate_required)
            return res
        if res["state"] == "undetermined":
            outcome = "half remains pending" if half else "master unmoved"
            escalate(window, f"window {window} could not be verified ({res.get('why')}); {outcome}, no spec dropped", "undetermined")
            print(f"gate-window: undetermined — {res.get('why')}")
            return res
        try:
            failures = candidate_failures(repo, res)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, zipfile.BadZipFile,
                zipfile.LargeZipFile, zlib.error, subprocess.SubprocessError) as exc:
            outcome = "half remains pending" if half else "master unmoved"
            escalate(window, f"unnamed new failures: candidate log evidence unavailable ({type(exc).__name__}); named ids={json.dumps(res.get('nodes', []))}; {outcome}, no spec dropped", "undetermined")
            if not half:
                record(res, tree, candidate_required)
            return dict(res, state="undetermined")
        nodes = sorted(set().union(*(failures[k] for k in ("pytest", "pg-parity", "vitest", "suite-error"))))
        if failures["undetermined"] or not nodes:
            outcome = "half remains pending" if half else "master unmoved"
            escalate(window, f"unnamed new failures: {failures['undetermined']}; NEW ids={json.dumps(nodes)}; {outcome}, no spec dropped", "undetermined")
            if not half:
                record(res, tree, candidate_required)
            return dict(res, state="undetermined")
        classification = {"complete": False}
        if base_red_answer.get("complete") and not base_red_answer.get("undetermined"):
            import base_red
            classification = base_red.classify(project, root_base, nodes)
        if not classification.get("complete"):
            outcome = "half remains pending" if half else "master unmoved"
            escalate(window, f"base incomplete; NEW ids={json.dumps(nodes)}; {outcome}, no spec dropped", "undetermined")
            if not half:
                record(res, tree, candidate_required)
            return dict(res, state="undetermined")
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
            ev("window-pre-existing", window, ids=pre_existing, base_sha=root_base,
               count=len(pre_existing), nodes=pre_existing, run_url=res["url"])
            print(f"gate-window: {len(pre_existing)} failure(s) pre-existing at base: {json.dumps(pre_existing)}")
            pre_existing_all.update(pre_existing)
        res["nodes"] = [n for n in nodes if n not in pre_existing]
        record(res, tree, candidate_required)
        if not res["nodes"]:
            if fixes_base and res.get("pg_failed"):
                escalate(window, "base-not-releasable: --fixes-base window has not repaired pg-parity", "base-not-releasable")
                return dict(res, state="undetermined")
            return dict(res, state="green")  # complete base proves every id pre-existing
        paths = OwnershipPaths({s: touched[s] for s in kept}, failures["pg_files"])
        unattributed = unattributed_ids(repo, cand, res["nodes"], paths)
        return dict(res, repo=repo, paths=paths, unattributed=unattributed)

    def judge_half(cand, kept):
        return judge(cand, kept, half=True)

    def prepare_half(specs):
        return prepare([e for e in entries if e[0] in specs])

    cand, merged, conflicts = prepare(entries)
    note_conflicts(conflicts)
    if not merged:
        print("gate-window: every branch conflicts — nothing to verify")
        escalate(window, "every branch in the window conflicts; builder re-dispatch via the merge-conflict flow", "undetermined")
        return 2
    live = list(merged)
    while True:
        res = judge(cand, live)
        if res["state"] == "green":
            break
        if res["state"] == "undetermined":
            return 2
        unattributed, paths = res["unattributed"], res["paths"]
        if runs == 1 and len(live) > 1 and len(unattributed) == len(res["nodes"]):
            chosen = bisect_window(window, live, prepare_half, judge_half, drop_specs, note_conflicts)
            if chosen is None:
                escalate(window, f"window {window} bisection found no advanceable candidate; dropped specs={json.dumps(dropped)}; master unmoved", "red")
                return 2
            cand, live = chosen
            pending = [s for s, _, _ in entries if s not in live and s not in dropped]
            print(f"gate-window: pending {len(pending)} spec(s), reason=bisection-unselected specs={json.dumps(pending)}")
            break
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
            escalate(window, f"window {window} is red after {runs} run(s); {why}; dropped specs={json.dumps(drop)}; whole window dropped, master unmoved", "red")
            return 2
        rest = [e for e in entries if e[0] in live]
        cand, live, more = prepare(rest)
        note_conflicts(more)
        if not live:
            escalate(window, "remaining merges conflict after the drop", "undetermined")
            return 2
    rechained = set()
    if enabled:
        # Only merges reachable from the final candidate count as shipped re-chains.
        for sha in git(repo, "rev-list", "--first-parent", f"{base}..{cand}").stdout.splitlines():
            message = git(repo, "show", "-s", "--format=%s", sha).stdout.strip()
            if message.endswith("(mechanical re-chain)"):
                rechained.update(s for s in live if message.startswith(f"merge({s}):"))
    if train:
        wait_predecessor(train)
    try:
        advance(repo, main, base, cand, window, live, dropped, runs, urls, entries, dashboard, rechained,
                pre_existing=sorted(pre_existing_all), actions_minutes=actions_minutes, after=after)
    except RuntimeError:
        if train:
            raise TrainCancelled("base-moved")
        raise
    print(f"gate-window: advanced {main} -> {cand[:12]}  shipped={live} dropped={dropped}")
    return 1 if dropped or deferred or len(live) < len(entries) else 0


def cleanup(repo, window):
    git(repo, "update-ref", "-d", f"refs/heads/window/{window}", check=False)


def main_(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__); return 0
    window, project, specs, main = argv[0], None, [], os.environ.get("DOIT_MAIN")
    fixes_base, after = False, None
    it = iter(argv[1:])
    for a in it:
        if a == "--project": project = next(it, None)
        elif a == "--specs": specs = [x for x in (next(it, "") or "").split(",") if x]
        elif a == "--main": main = next(it, None)
        elif a == "--fixes-base": fixes_base = True
        elif a == "--after": after = next(it, "")
        else: sys.exit(f"gate-window: unknown argument {a!r}\n{__doc__}")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", window):
        sys.exit(f"gate-window: {window!r} is not a window id")
    if after is not None and not re.fullmatch(r"[A-Za-z0-9._-]+", after):
        sys.exit(f"gate-window: {after!r} is not a window id")
    if project != PROJECT:
        sys.exit(f"gate-window: only project {PROJECT} uses merge windows (got {project!r}); other projects merge directly")
    bypass = os.environ.get("DOIT_WINDOW_GATE") == "off"
    if bypass or fixes_base:
        after = None
    if not main:
        try:
            import look, push_origin
            main = dict(push_origin.targets(look.load_toml())).get(PROJECT)
        except Exception:
            main = None
    main = main or "master"
    try:
        events = train_events()
    except (OSError, ValueError):
        print("gate-window: ledger unreadable; no slot admitted"); return 3
    flying = in_flight(events)
    if not after and flying:
        print("gate-window: another window is in flight"); return 3
    if after:
        candidates = [e for e in events if e.get("subject") == after and e.get("type") == "window-candidate"]
        if len(candidates) > 1 or any(e.get("window") == after and e.get("type") in ("window-dropped", "merge-conflict") for e in events):
            print("gate-window: predecessor is past its first verification"); return 3
        if not candidates:
            print("gate-window: predecessor has no window-candidate yet"); return 3
        if not any(e["window"] == after for e in flying):
            print("gate-window: predecessor is not in flight"); return 3
    depth = train_depth(os.environ, events=events)["depth"]
    if after and len(flying) >= depth:
        print("gate-window: no slot free at the effective train depth"); return 3
    slot = next((k for k in (range(depth) if after else (0,)) if acquire(window, k)), None)
    if slot is None:
        print("gate-window: no slot free"); return 3
    repo = fold.ROOT / "repos" / project
    try:
        if bypass:
            for s in specs or [s for s, _ in pending_from_ledger(fold.read_events())]:
                ev("window-gate-bypassed", s, window=window)
            print("gate-window: DOIT_WINDOW_GATE=off — bypassed; use the direct merge path"); return 4
        return run_window(window, project, specs, main, fixes_base, after, slot)
    except RuntimeError as e:
        print(f"gate-window: {e}", file=sys.stderr)
        try:
            escalate(window, f"gate-window failed before a verdict: {e}"[:300], "undetermined")
        except SystemExit:
            pass
        return 2
    finally:
        cleanup(repo, window)
        release(slot)


if __name__ == "__main__":
    sys.exit(main_(sys.argv[1:]))
