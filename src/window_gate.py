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
               exactly those, verify the remainder within a size-derived budget.
               Unattributed reds at any round split into bounded halves,
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
import io, json, math, os, pathlib, posixpath, re, shlex, shutil, signal, subprocess, sys, time, zipfile, zlib, ledger_files
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold  # noqa: E402
import rechain
import quarantine

PROJECT = "albert-scott"
PREDEPLOY = "scripts/ci/predeploy_gate.sh"
DEFAULT_TOML_PATH = pathlib.Path(__file__).resolve().parent.parent / "look.toml"
KILL_MARGIN_MIN = 10
BASE_RED_MARGIN_SECS = 600  # nested verify teardown, plus bounded log fetch
BASE_RED_COMMAND = "doit"
BASE_RED_WORKFLOWS = frozenset({"Python Tests", "Dashboard PR Quality"})
BASE_RED_KINDS = frozenset({"pytest", "pg-parity", "vitest", "suite-error"})
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
        for name, lines in ledger_files.iter_sources(fold.EVENTS):
            for line in lines:
                if line.strip():
                    try:
                        if not isinstance(json.loads(line), dict):
                            raise ValueError("event is not an object")
                    except ValueError as exc:
                        raise OSError("train ledger unreadable") from exc
    return fold.read_events()


def red_rate(events, now, hours=24):
    """Count non-green terminal windows; a dropped subset remains sticky red."""
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
            if name not in terminals or stamp >= terminals[name][0]:
                terminals[name] = stamp, event
            if event.get("dropped") or event.get("outcome") == "red":
                reds.add(name)
    windows = {name for name, (stamp, _) in terminals.items() if now - timedelta(hours=hours) <= stamp <= now}
    red = len(windows & reds)
    undetermined = other = 0
    for name in windows - reds:
        newest = terminals[name][1]
        if newest.get("type") == "window-verified" and not newest.get("dropped"):
            continue
        if newest.get("outcome") in ("undetermined", "base-not-releasable"):
            undetermined += 1
        else:
            other += 1
    non_green = red + undetermined + other
    return dict(rate=non_green / len(windows) if len(windows) >= 5 else None,
                red=red, undetermined=undetermined, other=other, non_green=non_green, windows=len(windows))


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
        health = red_rate(train_events() if events is None else events, now or datetime.now(timezone.utc))
        rate = health["red"] / health["windows"] if health["windows"] >= 5 else None
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
        if new is None and os.environ.get("DOIT_WINDOW_RECHAIN", "on") == "on":
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


def scope(repo, base, cand, ci_checks, *, fixes_base=False):
    """Classify bytes with the verified base's script; doubt requires all checks."""
    def fallback(cause):
        return dict(checks=list(ci_checks), everything=True, reason="scope-fallback: " + cause)

    try:
        if fixes_base:
            return fallback("fixes-base")
        diff = subprocess.run(["git", "-C", str(repo), "diff", "--name-only", "-z", f"{base}..{cand}"],
                              capture_output=True, timeout=60)
        if diff.returncode:
            return fallback("diff failed")
        if not diff.stdout:
            return fallback("empty diff")
        source = subprocess.run(["git", "-C", str(repo), "show", f"{base}:scripts/ci/actions_changes_gate.sh"],
                                capture_output=True, timeout=60)
        if source.returncode:
            return fallback("classifier absent at base")
        with tempfile.TemporaryDirectory(prefix="window-scope-") as tmp:
            paths, classifier = pathlib.Path(tmp) / "paths", pathlib.Path(tmp) / "classifier.sh"
            paths.write_bytes(diff.stdout)
            classifier.write_bytes(source.stdout)
            env = dict(PATH=os.environ.get("PATH", os.defpath), EVENT_NAME="window", CHANGED_PATHS_FILE=str(paths))
            proc = subprocess.Popen(["bash", str(classifier), "--json"], cwd=tmp, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                out, _ = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate()
                return fallback("classifier timeout")
            if proc.returncode:
                return fallback("classifier non-zero exit")
        lines = out.splitlines()
        if len(lines) != 1:
            return fallback("classifier output is not one JSON line")
        try:
            answer = json.loads(lines[0])
        except (ValueError, UnicodeError):
            return fallback("classifier output is not JSON")
        known = {PYTEST_CHECK, PG_CHECK, DASHBOARD_CHECK} | CI_VERIFY_EXCLUDED
        if (not isinstance(answer, dict) or not isinstance(answer.get("checks"), list)
                or any(not isinstance(c, str) or c not in known for c in answer["checks"])
                or not isinstance(answer.get("unclassified"), list)
                or any(not isinstance(p, str) for p in answer["unclassified"])
                or type(answer.get("everything")) is not bool or not isinstance(answer.get("reason"), str)):
            return fallback("ill-shaped classifier output")
        if answer["unclassified"] and not answer["everything"]:
            return fallback("unclassified paths")
        if answer["everything"]:
            return dict(checks=list(ci_checks), everything=True, reason=answer["reason"])
        selected = set(ci_checks) & set(answer["checks"])
        if not selected:
            return fallback("empty intersection")
        selected.add(PYTEST_CHECK)
        if any(p.startswith(b"dashboard/") for p in diff.stdout.split(b"\0")):
            selected.add(DASHBOARD_CHECK)
        return dict(checks=[c for c in ci_checks if c in selected], everything=False, reason=answer["reason"])
    except Exception as exc:
        return fallback(type(exc).__name__)


def base_pg(repo, root_base, ci_checks, window, events, *, fixes_base=False):
    """Inherit PG only across classified non-PG changes; a failed run still blocks."""
    baseline = verify(repo, root_base, [PG_CHECK], window)
    docs = [baseline.get("doc", {})]
    if fixes_base or baseline["state"] != "undetermined":
        return baseline, docs
    try:
        ancestors = git(repo, "rev-list", "--first-parent", "--max-count=31", root_base).stdout.splitlines()[1:]
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return baseline, docs

    # A scoped window may contain several --no-ff commits, but CI ran only
    # on its final tip. Its recorded base is the next verification checkpoint.
    # Do not dispatch PG on intermediate commits that this window never tested.
    def scoped_checkpoint(sha, older):
        for event in reversed(events):
            if event.get("type") == "window-verified" and event.get("sha") == sha:
                required = event.get("scope")
                if (isinstance(required, list) and required and all(isinstance(c, str) for c in required)
                        and PG_CHECK not in required and event.get("verified_on") in older):
                    return event["verified_on"]
                break
        return None

    checkpoint = scoped_checkpoint(root_base, ancestors)
    for ancestor in ancestors:
        # This also proves that PG was not required on the root base. Any
        # classifier failure requires PG and therefore stops inheritance.
        if PG_CHECK in scope(repo, ancestor, root_base, ci_checks)["checks"]:
            break
        if checkpoint is not None and ancestor != checkpoint:
            continue
        baseline = verify(repo, ancestor, [PG_CHECK], window)
        docs.append(baseline.get("doc", {}))
        if baseline["state"] == "green":
            ev("window-base-pg-inherited", window, base=root_base[:12], from_sha=ancestor[:12])
        # A red or undetermined checkpoint is never a reason to search for
        # an older green result.
        break
    return baseline, docs


def run_minutes(repo, docs, *, required=None):
    """Measure distinct dispatched runs, or retain the per-verification estimate."""
    estimate = dict(actions_minutes=0, selfhosted_minutes=0, minutes_source="estimate")
    try:
        scopes = required if required is not None else [doc.get("checks", {}) for doc in docs]
        estimate["actions_minutes"] = sum(RUN_MINUTES + (DASHBOARD_MINUTES if DASHBOARD_CHECK in checks else 0)
                                          for checks in scopes)
        ids = set()
        for doc in docs:
            checks = doc.get("checks")
            if not isinstance(checks, dict) or not checks:
                return estimate
            for check in checks.values():
                rid = check.get("run_id") if isinstance(check, dict) else None
                if isinstance(rid, (str, int)) and not isinstance(rid, bool) and re.fullmatch(r"[0-9]+", str(rid)):
                    ids.add(str(rid))
                elif check == "ran" or isinstance(check, dict) and check.get("state") == "ran":
                    return estimate
        totals = dict(actions_minutes=0, selfhosted_minutes=0, minutes_source="measured")
        if not docs:
            return totals
        if not ids:
            return estimate
        # base_red imports us: resolve its repository helper only at call time.
        import base_red
        import ghlimit
        owner = base_red._owner(repo)
        if not isinstance(owner, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", owner):
            return estimate
        for rid in sorted(ids):
            if not ghlimit.gate(wait=False)["ok"]:
                return estimate
            try:
                proc = subprocess.run([ghlimit.real_gh(), "api",
                                       f"repos/{owner}/actions/runs/{rid}/jobs?filter=all&per_page=100",
                                       "--paginate"], cwd=repo, capture_output=True, text=True, timeout=60)
            finally:
                ghlimit._bump_spent()
            if proc.returncode:
                if ghlimit._matches_rate_limit(proc.stderr):
                    ghlimit._mark_exhausted()
                return estimate
            source = proc.stdout.lstrip()
            decoder, jobs = json.JSONDecoder(), []
            while source:
                page, end = decoder.raw_decode(source)
                if not isinstance(page, dict) or not isinstance(page.get("jobs"), list):
                    return estimate
                jobs.extend(page["jobs"])
                source = source[end:].lstrip()
            if not jobs:
                return estimate
            for job in jobs:
                if not isinstance(job, dict):
                    return estimate
                if "runner_name" not in job or not isinstance(job.get("status"), str):
                    return estimate
                labels, runner = job.get("labels"), job.get("runner_name")
                if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
                    return estimate
                if runner is None:
                    if job.get("status") == "completed":
                        continue  # skipped jobs never acquired a runner
                    return estimate
                if not isinstance(runner, str) or not runner:
                    return estimate
                start = datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00"))
                if start.tzinfo is None or end.tzinfo is None or end < start:
                    return estimate
                minutes = max(1, math.ceil((end - start).total_seconds() / 60))
                totals["selfhosted_minutes" if "self-hosted" in labels else "actions_minutes"] += minutes
        return totals
    except Exception:
        return estimate


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


TREE_GUARDS = (
    "tests/test_820_no_abs_test_imports.py::",
    "tests/test_496_r3_guard.py::",
    "tests/test_207_monitoring_denoise.py::TestDigestFoldCronStale::test_query_cron_runs_stale_returns_data_trust_when_failed",
    "tests/deploy/test_ship_snapshot.py::test_ac14_line_counts",
    "tests/deploy/test_protected_stamp.py::test_ac12_deploy_sh_line_count_not_grown",
    "api/tests/profit_v2/test_read_model_refresh.py::test_ac9_deploy_sh_net_zero",
)


def tree_guards(env, toml_path=None):
    try:
        import tomllib
        path = pathlib.Path(toml_path) if toml_path is not None else DEFAULT_TOML_PATH
        guards = tomllib.loads(path.read_text())["window_gate"]["tree_guards"]
        if isinstance(guards, list) and guards and all(isinstance(g, str) and g for g in guards):
            return tuple(guards)
    except (OSError, ValueError, KeyError, TypeError, ImportError):
        pass
    return TREE_GUARDS


def guard_prefixes(repo, candidate, base, live):
    """Read the already-built ordinary merge chain, without creating any refs."""
    commits = git(repo, "rev-list", "--first-parent", f"{base}..{candidate}").stdout.splitlines()[::-1]
    if len(commits) != len(live):
        return None
    previous = base
    for spec, commit in zip(live, commits):
        parents = git(repo, "show", "-s", "--format=%P", commit).stdout.split()
        message = git(repo, "show", "-s", "--format=%s", commit).stdout.strip()
        if len(parents) != 2 or parents[0] != previous or not message.startswith(f"merge({spec}): window "):
            return None
        previous = commit
    return commits


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
    push = git(repo, "push", "--no-verify", "origin", f"{cand}:refs/heads/{ref}", check=False)
    pushed = push.returncode == 0
    try:
        if not pushed:
            return dict(state="undetermined", nodes=[], url="", doc={}, why="could not push the ci-verify ref: " + push.stderr[:200])
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
        if proc.returncode not in (0, 3):
            return incomplete
        answer = json.loads(out)
        if (not isinstance(answer, dict) or answer.get("sha") != base or
                answer.get("complete") is not (proc.returncode == 0) or answer.get("undetermined") or
                not isinstance(answer.get("ids"), dict) or
                not isinstance(answer.get("workflows"), list) or
                not all(isinstance(w, str) and w in BASE_RED_WORKFLOWS for w in answer["workflows"]) or
                not isinstance(answer.get("kinds_covered"), list) or
                not all(isinstance(k, str) and k in BASE_RED_KINDS for k in answer["kinds_covered"]) or
                (answer["complete"] and set(answer["workflows"]) != BASE_RED_WORKFLOWS)):
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
def resolve_entries(repo, spec_args, events):
    pairs = []
    if not spec_args:
        raise RuntimeError("missing candidate: explicit --specs required")
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
    out = []
    for s, b in pairs:
        p = git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}^{{commit}}", check=False)
        if p.returncode:
            raise RuntimeError(f"{s}: branch {b} does not resolve in {repo}")
        out.append((s, b, p.stdout.strip()))
    return out


def advance(repo, main, old, new, window, kept, dropped, runs, urls, entries, dashboard, rechained=(), pre_existing=(), actions_minutes=None, after=None, *, scope=None, minutes=None, quarantined=None, rechecked=None):
    replaced = git(repo, "rev-parse", f"refs/heads/{main}").stdout.strip()
    git(repo, "update-ref", f"refs/heads/{main}", new, old)       # old value asserted
    branch_of = {s: b for s, b, _ in entries}
    for s in kept:
        ev("shipped", s, sha=new, branch=branch_of[s], window=window,
           **({"rechained": True} if s in rechained else {}))
    ev("window-verified", window, sha=new, verified_on=old, replaced=replaced,
       **({"after": after} if after else {}), runs=urls, kept=kept, dropped=dropped,
       pre_existing=list(pre_existing),
       # Legacy direct helper callers retain their event shape; every gate
       # attempt below explicitly supplies a list, including when it is empty.
       **({"quarantined": list(quarantined)} if quarantined is not None else {}),
       **({"rechecked": rechecked, "recheck_undetermined": sorted(n for n, state in rechecked.items()
                                                               if state == "undetermined")} if rechecked else {}),
       actions_minutes=(minutes["actions_minutes"] if minutes is not None else
                        (runs * RUN_MINUTES + (runs * DASHBOARD_MINUTES if dashboard else 0)
                         if actions_minutes is None else actions_minutes)),
       **({"scope": scope["checks"], "scope_reason": scope["reason"]} if scope is not None else {}),
       **({k: minutes[k] for k in ("selfhosted_minutes", "minutes_source")} if minutes is not None else {}))


def _local_selector(node):
    if not isinstance(node, str) or "::" not in node:
        return False
    path, selector = node.split("::", 1)
    return (safe_path(path) and re.fullmatch(r"[\w./-]+\.py", path) is not None
            and all(part not in ("", ".", "..") for part in path.split("/"))
            and re.fullmatch(r"[\w][\w. :\[\],=+/-]*", selector) is not None
            and all(part and not part.startswith("-") for part in selector.split("::")))


def _local_command(python, ids):
    """Report out of the runner auxiliary directory before its cleanup.

    Collection establishes selector absence independently of a failing call.
    Candidate output and pytest exit status alone are never ownership evidence.
    """
    import base_red
    script = '''
import json, os, pathlib, subprocess, sys
aux = pathlib.Path(os.environ["HOME"]).parent
module = aux / "doit_recheck_report.py"
module.write_text(PLUGIN)
report = aux / "local-recheck.json"
env = dict(os.environ)
env.update(DOIT_RECHECK_REPORT=str(report), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
           PYTHONPATH=str(aux) + os.pathsep + str(pathlib.Path.cwd()))
env.pop("PYTEST_ADDOPTS", None)
env.pop("PYTEST_PLUGINS", None)
argv = [sys.executable, "-m", "pytest", "--noconftest", "-c", os.devnull,
        "--rootdir", str(pathlib.Path.cwd()), "-o", "addopts=", "-p", "doit_recheck_report", "-q"]
collect = subprocess.run(argv + ["--collect-only"] + list(dict.fromkeys(n.split("::", 1)[0] for n in IDS)), env=env)
inspection = json.loads(report.read_text())
report.unlink()
present = [n for n in IDS if n in inspection["collected"]]
code, phases = 0, {"collected": [], "reports": {}, "collection_error": False}
if collect.returncode in (0, 5) and not inspection["collection_error"] and present:
    run = subprocess.run(argv + present, env=env)
    code, phases = run.returncode, json.loads(report.read_text())
payload = dict(ids=IDS, inspection=inspection, collection_exit=collect.returncode,
               exit=code, phases=phases)
print("DOIT_LOCAL_RECHECK=" + json.dumps(payload, separators=(",", ":")), flush=True)
'''
    script = "PLUGIN = " + repr(base_red._RECHECK_PLUGIN) + "\nIDS = " + repr(ids) + "\n" + script
    return shlex.join([python, "-c", script])


def _local_phases(rows, ids):
    if len(rows) != 1 or rows[0].get("exit") != 0:
        raise ValueError("runner did not complete")
    lines = pathlib.Path(rows[0]["tail"]).read_text().splitlines()
    reports = [line.removeprefix("DOIT_LOCAL_RECHECK=") for line in lines
               if line.startswith("DOIT_LOCAL_RECHECK=")]
    if len(reports) != 1:
        raise ValueError("missing or truncated phase report")
    data = json.loads(reports[0])
    inspection, phases = data["inspection"], data["phases"]
    if (data["ids"] != ids or type(data["collection_exit"]) is not int
            or type(data["exit"]) is not int or data["collection_exit"] not in (0, 5)
            or data["exit"] not in (0, 1) or inspection["collection_error"] is not False
            or phases["collection_error"] is not False):
        raise ValueError("uncertain run")
    collected = inspection["collected"]
    if not isinstance(collected, list):
        raise ValueError("malformed collection")
    present = [n for n in ids if n in collected]
    if set(phases["collected"]) != set(present) or set(phases["reports"]) != set(present):
        raise ValueError("unexpected or missing runtime selection")
    states = {}
    for node in ids:
        if node not in collected:
            states[node] = "absent"
            continue
        phase = phases["reports"].get(node, {})
        if (node not in phases["collected"] or phase.get("setup") != "passed"
                or phase.get("teardown") != "passed" or phase.get("call") not in ("passed", "failed")):
            raise ValueError("uncertain test phases")
        states[node] = "red" if phase["call"] == "failed" else "green"
    if data["exit"] != int("red" in states.values()):
        raise ValueError("inconsistent process and phase results")
    return states


def local_owners(repo, project, entries, new_ids, *, window_base=None, deadline=None, cache=None):
    """Bounded sequential member/base proof, using the installed runner only.

    Any uncertainty disqualifies the whole member, without disqualifying a
    separately determined sibling. The cache belongs to one window attempt.
    """
    if not entries or not new_ids:
        return {}
    import build_runner
    import grading_env
    import loadgate
    if not all(isinstance(n, str) for n in new_ids):
        return {}
    ids = list(dict.fromkeys(new_ids))
    if not all(_local_selector(n) for n in ids):
        return {}
    try:
        row = grading_env._project_row(project)
        if not row or not row.get("venv"):
            return {}
        # Do not change the runner's project provisioning or temporary env.
        python = str(pathlib.Path(row["venv"]) / "bin/python")
        cache = {} if cache is None else cache
        result = {}
        for spec, branch, supplied_sha in entries:
            if not isinstance(spec, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", spec) is None:
                continue  # runner directory names must stay inside its scratch root
            until = min(time.monotonic() + 300, deadline if deadline is not None else float("inf"))
            env = grading_env._clean_env()
            def inspect(*args):
                return build_runner._required(["git", "-C", str(repo), *args], phase="local object inspection",
                                               env=env, deadline=until)
            try:
                inspect("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
                sha = build_runner._resolve(repo, supplied_sha, env, until)
                base = build_runner._resolve(repo, window_base, env, until)
                baseline = inspect("merge-base", sha, base)
                baseline = build_runner._resolve(repo, baseline, env, until)
                files = list(dict.fromkeys(n.split("::", 1)[0] for n in ids))
                # Member absence is not ownership; short circuit before clone.
                for path in files:
                    inspect("cat-file", "-e", f"{sha}:{path}")

                def snapshot(revision, *, member):
                    key = (revision, tuple(sorted(ids)))
                    if key in cache:
                        value = cache[key]
                        if value is None or (member and "absent" in value.values()):
                            raise ValueError("previous attempt undetermined")
                        return value
                    cache[key] = None  # includes denied, failed and expired attempts
                    present, states = [], {}
                    for path in files:
                        # A successful tree inspection is required for absence.
                        listing = inspect("ls-tree", "-z", revision, "--", path)
                        if listing:
                            if not re.fullmatch(r"100(?:644|755) blob [0-9a-f]{40}\t[^\x00]+\x00", listing):
                                raise ValueError("selected test is not a regular file")
                            inspect("cat-file", "-e", f"{revision}:{path}")
                            present.extend(n for n in ids if n.split("::", 1)[0] == path)
                        else:
                            states.update((n, "absent") for n in ids if n.split("::", 1)[0] == path)
                    if present:
                        import lane_driver
                        now = datetime.now(timezone.utc)
                        events = fold.read_events()
                        running = {role: lane_driver._in_flight_role(events, now, role)
                                   for role in ("builder", "reviewer")}
                        running["window_gate"] = 1
                        gate = loadgate.begin(running=running, allow_enforce=True)
                        if gate is not None and gate.check(spec, "grader", action="grade",
                                since=datetime.now(timezone.utc).isoformat()).held:
                            raise ValueError("admission held")
                        if time.monotonic() >= until:
                            raise ValueError("member deadline exhausted")
                        with tempfile.TemporaryDirectory(prefix="window-local-report-") as temp:
                            rows, _, _, _, _ = build_runner._run_tree(spec, revision, repo, row,
                                [("local-owner", _local_command(python, present))], pathlib.Path(temp),
                                0, build_runner.TAIL_CAP, until)
                            states.update(_local_phases(rows, present))
                    if member and any(v == "absent" for v in states.values()):
                        raise ValueError("member selector absent")
                    cache[key] = states
                    return states

                tip = snapshot(sha, member=True)
                before = snapshot(baseline, member=False)
                qualified = [n for n in ids if tip[n] == "red" and before[n] in ("green", "absent")]
                if qualified:
                    result[spec] = qualified
            except Exception:
                # Missing tooling, load hold, malformed evidence, red/unknown
                # phases and timeouts preserve the existing bisection fallback.
                continue
        return result
    except Exception:
        return {}


def bisect_window(window, live, prepare, judge, drop, conflict, local=None):
    """Visit both halves in entry order, returning the first verified candidate.

    All builds and verdict classification go through run_window's callbacks;
    a green sibling is never unioned into an unverified candidate.
    """
    chosen = None

    def judge_batch(batch):
        cand, kept, clashes = prepare(batch)
        conflict(clashes)
        if not kept:
            escalate(window, "bisection half has no mergeable specs; half remains pending", "undetermined")
            return
        visit(kept, cand, judge(cand, kept))

    def split(batch):
        k = len(batch) // 2
        halves = [batch[:k], batch[k:]]
        ev("window-bisected", window, window=window, halves=halves)
        for half in halves:
            judge_batch(half)

    def visit(kept, cand, result):
        nonlocal chosen
        if result["state"] == "green":
            if chosen is None:
                chosen = (cand, kept)
            return
        if result["state"] == "undetermined":
            return
        if len(kept) == 1:
            drop(kept, result, "bisect-single")
            return
        demonstrated = list(dict.fromkeys(
            owner for node in result["nodes"]
            for owner in owners(result["repo"], cand, node, result["paths"])))
        if not demonstrated and local is not None:
            result["local_owners"] = local(kept, result)
            demonstrated = [s for s in kept if s in result["local_owners"]]
        drop(demonstrated, result, "owner" if result.get("local_owners") else "bisect-owner")
        rest = [s for s in kept if s not in demonstrated]
        if not demonstrated:
            split(rest)
        elif rest:
            # Removing owners changes the tree. Judge before selecting/splitting.
            judge_batch(rest)

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


def infra_failure(window, project, text, code=1):
    import grading_env
    if grading_env.failure_kind(text, code) != "infra":
        return False
    fields = grading_env.infra_fields(window, "window-gate", text)
    ev("infra-failure", window, project=project, **fields)
    print("gate-window: infra hold — " + fields["class"])
    return True


def run_attempt(window, project, *args, **kwargs):
    try:
        return _run_attempt(window, project, *args, **kwargs)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        if infra_failure(window, project, str(exc), 124 if isinstance(exc, subprocess.TimeoutExpired) else 1):
            return 2
        raise


def _run_attempt(window, project, spec_args, main, fixes_base, base, root_base, after, slot, train, rebased=False):
    repo = fold.ROOT / "repos" / project
    if not repo.is_dir():
        sys.exit(f"gate-window: no repo at {repo}")
    quarantine.apply(project)
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
        if infra_failure(window, project, str(e)):
            return 2
        escalate(window, str(e), "undetermined")
        print("gate-window: " + str(e))
        return 2
    base_red_answer = base_red_snapshot(project, root_base)
    baseline, base_failures = None, {}
    enabled = os.environ.get("DOIT_WINDOW_RECHAIN", "on") == "on"
    migrations = {s for s, b, sha in entries
                  if enabled and len(rechain.added_versions(repo, base, sha)) == 1}
    opened = False
    touched = {s: set(git(repo, "diff", "--name-only", f"{base}...{b}").stdout.split()) for s, b, _ in entries}
    dropped, urls, runs, dashboard = [], [], 0, False
    pre_existing_all = set()
    quarantined_all, rechecked_all = set(), {}
    guard_seed = tree_guards(os.environ)
    actions_minutes = 0
    verdicts, verification_scopes, candidate_scopes = [], [], {}
    conflicted_all = {}
    fingerprint_failed = rechain.FailedFingerprints()
    judgments, judgment_limit = 0, 2 * len(entries) + 1

    def prepare(batch):
        batch = [e for e in batch if e[0] not in fingerprint_failed.specs]
        candidate, kept, clashes = build_candidate(repo, window, base, batch)
        migration_kept = [s for s in kept if s in migrations]
        if migration_kept:
            try:
                candidate = rechain.fingerprint(repo, candidate, window, ev=ev, project=project)
                git(repo, "update-ref", f"refs/heads/window/{window}", candidate)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                import grading_env
                if grading_env.failure_kind(str(exc)) == "infra":
                    raise
                fingerprint_failed.decline(migration_kept, exc, window, ev)
                rest = [e for e in batch if e[0] not in migrations]
                candidate, kept, more = build_candidate(repo, window, base, rest)
                clashes.update(more)
        if kept:
            if rebased:
                # Identical parents, message and second-resolution timestamps can
                # otherwise recreate the cancelled SHA. Give the fresh attempt
                # its own identity while retaining exactly the rebuilt tree.
                candidate = git(repo, *IDENT, "commit-tree", tree_of(repo, candidate), "-p", candidate,
                                "-m", f"verify({window}): rebuild after train cancellation").stdout.strip()
                git(repo, "update-ref", f"refs/heads/window/{window}", candidate)
        return candidate, kept, clashes

    by_id = {s: (b, sha) for s, b, sha in entries}
    local_cache = {}

    def local(batch, result):
        return local_owners(repo, project, [e for e in entries if e[0] in batch],
                            result["nodes"], window_base=base, cache=local_cache)

    # Unconfigured projects cannot run local proof. Retain the six-argument
    # route there, including existing judgment-budget callback wrappers.
    try:
        import grading_env
        local_available = bool(grading_env._project_row(project))
    except Exception:
        local_available = False

    def note_conflicts(conflicts):
        for s, paths in conflicts.items():
            ev("merge-conflict", s, files=paths, window=window)
        conflicted_all.update(conflicts)
        dropped.extend(s for s in conflicts if s not in dropped)

    def drop_specs(specs, result, reason):
        for s in specs:
            guard_nodes = result.get("guard_charges", {}).get(s, [])
            local_nodes = result.get("local_owners", {}).get(s)
            owned_nodes = (local_nodes if local_nodes is not None else [n for n in result["nodes"]
                           if s in owners(repo, result["candidate"], n, result["paths"])])
            reported_nodes = (owned_nodes if guard_nodes or local_nodes is not None else result["nodes"]) + guard_nodes
            ev("window-dropped", s, window=window, nodes=reported_nodes,
               **({"evidence": "local-repro", "tests": local_nodes} if local_nodes is not None else {}),
               quarantined=sorted(quarantined_all),
               **({"rechecked": dict(rechecked_all), "recheck_undetermined": sorted(
                   n for n, state in rechecked_all.items() if state == "undetermined")} if rechecked_all else {}),
               run_url=result["url"], branch=by_id[s][0], reason=reason,
               owned_nodes=owned_nodes + guard_nodes,
               unattributed_nodes=list(result["unattributed"]))
            print(f"gate-window: dropped {s} reason={reason} nodes={json.dumps(reported_nodes)}")
        dropped.extend(s for s in specs if s not in dropped)
        print(f"gate-window: dropped {len(specs)} spec(s), reason={reason}")

    def record(res, tree, required):
        if tree is not None:
            ev("window-tree-verified", window, window=window, sha=res["candidate"],
               tree=tree, green=res["state"] == "green", new_ids=res["nodes"], required=required)

    def judge(cand, kept, *, half=False):
        nonlocal runs, dashboard, actions_minutes, baseline, base_failures, opened, judgments
        if judgments >= judgment_limit:
            escalate(window, "candidate judgment budget exhausted; affected specs remain pending", "undetermined")
            return dict(state="undetermined", nodes=[], url="", doc={})
        judgments += 1
        candidate_scope = scope(repo, base, cand, ci_checks, fixes_base=fixes_base)
        candidate_scopes[cand] = candidate_scope
        candidate_required = candidate_scope["checks"]
        tree = None
        try:
            tree = tree_of(repo, cand)
            prior = prior_result(fold.read_events(), tree)
        except (OSError, RuntimeError, ValueError, TypeError, subprocess.SubprocessError):
            prior = None
        if PG_CHECK in candidate_required and baseline is None:
            baseline, baseline_docs = base_pg(repo, root_base, ci_checks, window, events, fixes_base=fixes_base)
            verdicts.extend(baseline_docs)
            if baseline["state"] != "green" and not (fixes_base and baseline["state"] == "red"):
                why = "base-not-releasable: pg-parity is " + baseline["state"]
                escalate(window, why, "base-not-releasable")
                print("gate-window: " + why)
                return dict(state="undetermined", nodes=[], url="", doc={})
            base_failures = baseline.get("pg_failures", {})
        if not opened:
            ev("window-opened", window, specs=[s for s, _, _ in entries], base=base[:12], fixes_base=fixes_base)
            opened = True
        ev("window-candidate", window, sha=cand, verified_on=base, specs=kept, slot=slot,
           **({"after": after} if after else {}))
        if (prior is not None and prior["green"] is True
                and isinstance(prior.get("required"), list)
                and all(isinstance(c, str) for c in prior["required"])
                and set(prior["required"]) >= set(candidate_required)):
            print(f"gate-window: tree {tree} already verified in {prior.get('window', prior.get('subject'))}")
            return dict(state="green", nodes=[], url="", doc={})
        res = dict(verify(repo, cand, candidate_required, window), candidate=cand)
        try:
            doc = res.get("doc", {})
            run_ids = sorted({str(check["run_id"]) for check in doc.get("checks", {}).values()
                              if isinstance(check, dict) and isinstance(check.get("run_id"), (str, int))
                              and not isinstance(check["run_id"], bool)
                              and re.fullmatch(r"[0-9]+", str(check["run_id"]))})
            if run_ids:
                ev("window-run-minutes", window, window=window, candidate=cand, run_ids=run_ids,
                   state=res["state"], **run_minutes(repo, [doc], required=[candidate_required]))
        except Exception as exc:
            print(f"gate-window: candidate minutes unavailable ({type(exc).__name__})")
        if res["state"] != "green" and infra_failure(window, project, res.get("output", "") + "\n" + res.get("why", "")):
            return dict(res, state="undetermined", why="infra: verification unavailable")
        # No attribution or shipment may escape a verification on an unmerged base.
        if train:
            wait_predecessor(train)
        runs += 1
        dashboard = dashboard or DASHBOARD_CHECK in candidate_required
        actions_minutes += RUN_MINUTES + (DASHBOARD_MINUTES if DASHBOARD_CHECK in candidate_required else 0)
        verdicts.append(res.get("doc", {}))
        verification_scopes.append(candidate_required)
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
        current = quarantine.active(fold.read_events(), project, datetime.now(timezone.utc))
        # Preserve the existing per-run base audit when an earlier run of this
        # attempt already proved these ids pre-existing and quarantined them.
        already_proved = sorted(n for n in nodes if n in current and n in pre_existing_all)
        if already_proved:
            ev("window-pre-existing", window, ids=already_proved, base_sha=root_base,
               count=len(already_proved), nodes=already_proved, run_url=res["url"])
        quarantined_all.update(n for n in nodes if n in current)
        excused = sorted(n for n in nodes if n in current)
        if excused:
            print(f"gate-window: {len(excused)} failure(s) excused by active quarantine: {json.dumps(excused)}")
        nodes = [n for n in nodes if n not in current]
        if not nodes:
            res["nodes"] = []
            record(res, tree, candidate_required)
            if fixes_base and res.get("pg_failed"):
                escalate(window, "base-not-releasable: --fixes-base window has not repaired pg-parity", "base-not-releasable")
                return dict(res, state="undetermined")
            return dict(res, state="green")
        classification = {"complete": False}
        if ((base_red_answer.get("complete") or base_red_answer.get("workflows"))
                and not base_red_answer.get("undetermined")):
            import base_red
            classification = base_red.classify(project, root_base, nodes, refresh=True)
        covered = set(classification.get("covered", BASE_RED_KINDS))
        uncovered = classification.get("uncovered", [])
        base_undetermined = classification.get("undetermined", False)
        pg_covered = (PG_CHECK in candidate_required and baseline is not None
                      and baseline["state"] == "green" and not base_undetermined)
        pg_complete = pg_covered and uncovered and all(n.startswith("pg-parity::") for n in uncovered)
        if base_undetermined or not (classification.get("complete") or pg_complete):
            outcome = "half remains pending" if half else "master unmoved"
            reason = "base incomplete"
            if base_undetermined:
                reason += " (base undetermined)"
            elif "needed" in classification and "covered" in classification:
                reason += f" (needs {classification['needed']}, base has {sorted(covered)})"
            escalate(window, f"{reason}; NEW ids={json.dumps(nodes)}; {outcome}, no spec dropped", "undetermined")
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
        for node in pre_existing:
            quarantine.record(project, node, "base-red", [window])
            quarantined_all.add(node)
        new_ids = [n for n in classification.get("new", nodes) if n not in pre_existing]
        rechecked = {}
        if new_ids:
            import base_red
            unseen = [n for n in new_ids if n not in rechecked_all]
            try:
                answer = base_red.recheck(project, root_base, unseen) if unseen else {}
            except Exception:
                answer = {}
            for node in new_ids:
                states = [k for k in ("red", "green", "absent", "undetermined") if node in answer.get(k, [])]
                state = rechecked_all.get(node, states[0] if len(states) == 1 else "undetermined")
                rechecked[node] = state
                rechecked_all[node] = state
                if state == "red":
                    quarantine.record(project, node, "base-red", [window])
                    quarantined_all.add(node)
                    print(f"gate-window: 1 failure excused by base-red quarantine: {node}")
        charged = [n for n in nodes if n not in pre_existing and rechecked.get(n) != "red"]
        guards = [n for n in charged if n.startswith(guard_seed)]
        res["nodes"] = [n for n in charged if n not in guards]
        res["guards"] = guards
        record(res, tree, candidate_required)
        if guards and (half or any(rechecked.get(n) == "undetermined" for n in guards)):
            escalate(window, "guard evidence is undetermined; affected specs remain pending", "undetermined")
            return dict(res, state="undetermined")
        if not charged:
            if fixes_base and res.get("pg_failed"):
                escalate(window, "base-not-releasable: --fixes-base window has not repaired pg-parity", "base-not-releasable")
                return dict(res, state="undetermined")
            return dict(res, state="green")  # complete base proves every id pre-existing
        paths = OwnershipPaths({s: touched[s] for s in kept}, failures["pg_files"])
        unattributed = unattributed_ids(repo, cand, res["nodes"], paths)
        return dict(res, repo=repo, paths=paths, unattributed=unattributed)

    def charge_guards(result, kept):
        guards = result.get("guards", [])
        if not guards or result["unattributed"] or (runs >= 2 and result["nodes"]):
            return True
        prefixes = guard_prefixes(repo, result["candidate"], base, kept)
        if prefixes is None:
            return False
        import base_red
        try:
            full = base_red.recheck(project, prefixes[-1], guards)
            if any(n not in full.get("red", []) for n in guards):
                return False
            charges, remaining = {}, list(guards)
            for spec, prefix in zip(kept, prefixes):
                answer = full if prefix == prefixes[-1] else base_red.recheck(project, prefix, remaining)
                for node in list(remaining):
                    if node in answer.get("red", []):
                        charges.setdefault(spec, []).append(node)
                        remaining.remove(node)
                    elif node not in answer.get("green", []) and node not in answer.get("absent", []):
                        return False
                if not remaining:
                    break
            result["guard_charges"] = charges
            return not remaining
        except Exception:
            return False

    def judge_half(cand, kept):
        return judge(cand, kept, half=True)

    def prepare_half(specs):
        return prepare([e for e in entries if e[0] in specs])

    cand, merged, conflicts = prepare(entries)
    note_conflicts(conflicts)
    if not merged:
        ev("window-opened", window, specs=[s for s, _, _ in entries], base=base[:12], fixes_base=fixes_base)
        print("gate-window: " + fingerprint_failed.why("every branch conflicts — nothing to verify"))
        escalate(window, fingerprint_failed.why("every branch in the window conflicts; builder re-dispatch via the merge-conflict flow"), "undetermined")
        return 2
    live = list(merged)
    while True:
        res = judge(cand, live)
        if res["state"] == "green":
            break
        if res["state"] == "undetermined":
            return 2
        if not charge_guards(res, live):
            escalate(window, "guard merge-prefix evidence is undetermined; no spec dropped", "undetermined")
            return 2
        paths = res["paths"]
        demonstrated = list(dict.fromkeys(
            s for node in res["nodes"] for s in owners(repo, cand, node, paths)))
        demonstrated = list(dict.fromkeys(demonstrated + list(res.get("guard_charges", {}))))
        if runs >= 2 and res.get("guard_charges") and not res["nodes"]:
            drop_specs(demonstrated, res, "guard-prefix")
            escalate(window, "second candidate guard is red; remainder pending", "red")
            return 2
        if len(live) == 1:
            drop_specs(live, res, "bisect-single" if not demonstrated else "owner")
            escalate(window, "isolated candidate is red; master unmoved", "red")
            return 2
        if not demonstrated:
            res["local_owners"] = local(live, res)
            demonstrated = [s for s in live if s in res["local_owners"]]
        drop_specs(demonstrated, res, "owner")
        live = [s for s in live if s not in demonstrated]
        if not live:
            escalate(window, "all demonstrated owners dropped; master unmoved", "red")
            return 2
        if enabled and any(s in migrations for s in dropped) and any(s in migrations for s in live):
            escalate(window, "migration ordering after owner removal is unsafe; remainder pending", "undetermined")
            return 2
        if demonstrated:
            cand, live, more = prepare_half(live)
            note_conflicts(more)
            if not live:
                escalate(window, fingerprint_failed.why("remaining merges conflict after the drop"), "undetermined")
                return 2
            continue
        chosen = bisect_window(window, live, prepare_half, judge_half, drop_specs, note_conflicts,
                               **({"local": local} if local_available else {}))
        if chosen is None:
            escalate(window, fingerprint_failed.why(f"window {window} bisection found no advanceable candidate; dropped specs={json.dumps(dropped)}; master unmoved"), "red")
            return 2
        cand, live = chosen
        pending = [s for s, _, _ in entries if s not in live and s not in dropped]
        print(f"gate-window: pending {len(pending)} spec(s), reason=bisection-unselected specs={json.dumps(pending)}")
        break
    if enabled and any(s in migrations for s in dropped) and any(s in migrations for s in live):
        escalate(window, "migration ordering after owner removal is unsafe; remainder pending", "undetermined")
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
                pre_existing=sorted(pre_existing_all), actions_minutes=actions_minutes, after=after,
                quarantined=sorted(quarantined_all), rechecked=dict(rechecked_all),
                scope=candidate_scopes[cand], minutes=run_minutes(repo, verdicts, required=verification_scopes))
    except RuntimeError:
        if train:
            raise TrainCancelled("base-moved")
        raise
    quarantine.apply(project)
    print(f"gate-window: advanced {main} -> {cand[:12]}  shipped={live} dropped={dropped}")
    return 1 if dropped or len(live) < len(entries) else 0


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
    if not specs and not bypass:
        print("gate-window: missing candidate: explicit --specs required", file=sys.stderr)
        return 2
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
            import merge_queue
            head = merge_queue.next_candidate(events) if not specs else None
            selected = specs or ([head] if head else [])
            for s in selected:
                ev("window-gate-bypassed", s.partition(":")[0], window=window)
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
