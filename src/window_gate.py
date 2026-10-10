#!/usr/bin/env python3
"""window_gate — verify a merge WINDOW's candidate tip before master moves (L-spec-0765).

    doit gate-window <window-id> --project albert-scott [--specs A,B,C] [--main BRANCH]

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
import json, os, pathlib, re, signal, subprocess, sys, time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold  # noqa: E402

PROJECT = "albert-scott"
PREDEPLOY = "scripts/ci/predeploy_gate.sh"
MAX_MERGES = 6
PYTEST_CHECK = "pytest"
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
        if new is None:
            conflicts[spec] = paths
        else:
            cur = new
            merged.append(spec)
    if merged:
        git(repo, "update-ref", f"refs/heads/window/{window}", cur)
    return cur, merged, conflicts


# ── verification: ONE call into the existing deploy gate ─────────────────────
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
    timeout = float(os.environ.get("DOIT_WINDOW_VERIFY_TIMEOUT_SECS")
                    or (float(env.get("PREDEPLOY_GATE_DISPATCH_TIMEOUT_MIN", "45")) + 10) * 60)
    pushed = git(repo, "push", "origin", f"{cand}:refs/heads/{ref}", check=False).returncode == 0
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
        if proc.returncode == 0 and all_ran and not nodes and doc.get("verdict", "green") == "green":
            return dict(state="green", nodes=[], url=url, doc=doc)
        if proc.returncode == 2 or (proc.returncode == 0 and not doc):
            return dict(state="undetermined", nodes=nodes, url=url, doc=doc,
                        why=f"gate exit {proc.returncode}: " + (out.strip().splitlines() or [""])[-1][:200])
        return dict(state="red", nodes=nodes, url=url, doc=doc,
                    why=f"gate exit {proc.returncode}")
    finally:
        if pushed:
            git(repo, "push", "origin", f":refs/heads/{ref}", check=False)


# ── failing node -> owning specs ─────────────────────────────────────────────
def _exists(repo, rev, path):
    return git(repo, "cat-file", "-e", f"{rev}:{path}", check=False).returncode == 0


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


def owners(repo, rev, node, touched):
    """touched: {spec: set(paths)}. Test file touched by a spec; else a spec touching
    the node's import closure; else every spec (the safe direction)."""
    f = node.split("::")[0]
    direct = [s for s, p in touched.items() if f in p]
    if direct:
        return direct
    closure = import_closure(repo, rev, f)
    near = [s for s, p in touched.items() if p & closure]
    return near or list(touched)


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


def advance(repo, main, old, new, window, kept, dropped, runs, urls, entries, dashboard):
    git(repo, "update-ref", f"refs/heads/{main}", new, old)       # old value asserted
    branch_of = {s: b for s, b, _ in entries}
    for s in kept:
        ev("shipped", s, sha=new, branch=branch_of[s], window=window)
    ev("window-verified", window, sha=new, runs=urls, kept=kept, dropped=dropped,
       actions_minutes=runs * RUN_MINUTES + (runs * DASHBOARD_MINUTES if dashboard else 0))


def run_window(window, project, spec_args, main):
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
    ev("window-opened", window, specs=[s for s, _, _ in entries], base=base[:12])
    touched = {s: set(git(repo, "diff", "--name-only", f"{base}...{b}").stdout.split()) for s, b, _ in entries}
    dropped, urls, runs, dashboard = [], [], 0, False
    conflicted_all = {}

    cand, merged, conflicts = build_candidate(repo, window, base, entries)
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
        required = [PYTEST_CHECK] + ([DASHBOARD_CHECK] if has_dash else [])
        res = verify(repo, cand, required, window)
        runs += 1
        if res["url"]:
            urls.append(res["url"])
        if res["state"] == "green":
            break
        if res["state"] == "undetermined":
            escalate(window, f"window {window} could not be verified ({res.get('why')}); master unmoved, no spec dropped")
            print(f"gate-window: undetermined — {res.get('why')}"); return 2
        # red
        by_id = {s: (b, sha) for s, b, sha in entries}
        if runs >= 2:
            drop = list(live)
        else:
            drop = []
            for n in res["nodes"]:
                drop += [s for s in owners(repo, cand, n, {s: touched[s] for s in live}) if s not in drop]
            drop = drop or list(live)
        for s in drop:
            ev("window-dropped", s, window=window, nodes=res["nodes"], run_url=res["url"], branch=by_id[s][0])
            print(f"gate-window: dropped {s} nodes={json.dumps(res['nodes'])}")
        dropped += drop
        live = [s for s in live if s not in drop]
        if not live or runs >= 2:
            escalate(window, f"window {window} is red after {runs} run(s); whole window dropped, master unmoved")
            return 2
        rest = [e for e in entries if e[0] in live]
        cand, live, more = build_candidate(repo, window, base, rest)
        for s, paths in more.items():
            ev("merge-conflict", s, files=paths, window=window)
        dropped += list(more)
        if not live:
            escalate(window, "remaining merges conflict after the drop")
            return 2
    advance(repo, main, base, cand, window, live, dropped, runs, urls, entries, dashboard)
    print(f"gate-window: advanced {main} -> {cand[:12]}  shipped={live} dropped={dropped}")
    return 1 if dropped else 0


def cleanup(repo, window):
    git(repo, "update-ref", "-d", f"refs/heads/window/{window}", check=False)


def main_(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__); return 0
    window, project, specs, main = argv[0], None, [], os.environ.get("DOIT_MAIN")
    it = iter(argv[1:])
    for a in it:
        if a == "--project": project = next(it, None)
        elif a == "--specs": specs = [x for x in (next(it, "") or "").split(",") if x]
        elif a == "--main": main = next(it, None)
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
        return run_window(window, project, specs, main)
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
