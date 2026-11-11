"""Predeploy subprocess boundary and scoped candidate identity for window verification."""
from contextlib import contextmanager
from contextvars import ContextVar

_IDENTITY = ContextVar("window_dispatch_identity", default=None)


@contextmanager
def managed(tree, quick=(), known=None):
    """Carry candidate identity through callers that retain the legacy call shape.
    `known` (project, base_sha, specs) arms the known-red dispatch guard."""
    token = _IDENTITY.set((tree, quick, known))
    try:
        yield
    finally:
        _IDENTITY.reset(token)


def _undetermined(why):
    return dict(state="undetermined", nodes=[], url="", doc={}, why=why)


def verify(gate, repo, cand, required, window, *, rehearsal=False, tree=None, quick=()):
    """-> dict(state=green|red|undetermined, nodes, url, doc). Anything that is not
    an unambiguous green is not green."""
    context = _IDENTITY.get()
    known = context[2] if context is not None else None
    if tree is None and context is not None:
        tree, inherited_quick, _ = context
        if not quick:
            quick = inherited_quick
        if tree is None:
            return _undetermined("candidate tree unavailable")
    suite = gate.os.environ.get("PREDEPLOY_GATE_SUITE", "window")
    if suite not in ("window", "nightly"):
        return _undetermined("invalid verification suite")
    try:
        quick = tuple(quick)
    except TypeError:
        return _undetermined("quick ids must be nonempty single lines")
    if any(not isinstance(q, str) or not q or any(c in q for c in "\r\n\x00") for q in quick):
        return _undetermined("quick ids must be nonempty single lines")
    if tree is not None:
        try:
            resolved = gate.tree_of(repo, cand)
        except (OSError, RuntimeError, ValueError, TypeError, gate.subprocess.SubprocessError):
            return _undetermined("candidate tree unavailable")
        if tree != resolved:
            return _undetermined("supplied tree differs from candidate tree")
    if known is not None and tree is not None:
        import window_known_red
        skipped = window_known_red.guard(gate, repo, cand, tree, required, window, rehearsal=rehearsal, **known)
        if skipped is not None:
            return skipped
    script = gate.os.environ.get("DOIT_WINDOW_PREDEPLOY") or str(repo / gate.PREDEPLOY)
    ref = f"ci-verify/{cand[:12]}-win{gate.os.getpid()}"
    env = {**gate.os.environ, "PREDEPLOY_GATE_CI_ONLY": "1", "PREDEPLOY_GATE_ONLY_GREEN": "1",
           "PREDEPLOY_GATE_DEPLOY_SHA": cand, "PREDEPLOY_GATE_REPO_ROOT": str(repo),
           "PREDEPLOY_GATE_CI_CHECKS": "\n".join(required),
           "PREDEPLOY_GATE_REQUIRED_CHECKS": "\n".join(required),
           "PREDEPLOY_GATE_TREE": tree if tree is not None else "",
           "PREDEPLOY_GATE_WINDOW": window, "PREDEPLOY_GATE_SUITE": suite,
           "PREDEPLOY_GATE_QUICK_IDS": "\n".join(quick)}
    dispatch_minutes = gate.dispatch_timeout_min(env)
    env["PREDEPLOY_GATE_DISPATCH_TIMEOUT_MIN"] = (str(int(dispatch_minutes)) if dispatch_minutes.is_integer()
                                                  else str(dispatch_minutes))
    timeout = gate.verify_timeout_secs(env)
    # Ephemeral ci-verify ref: skip the repo pre-push guardrails (fix-the-fix breaker walks every
    # pushed fix: commit and exceeded the 60s git timeout on multi-branch windows, 2026-10-16).
    push = gate.git(repo, "push", "--no-verify", "origin", f"{cand}:refs/heads/{ref}", check=False)
    pushed = push.returncode == 0
    try:
        if not pushed:
            return dict(state="undetermined", nodes=[], url="", doc={}, why="could not push the ci-verify ref: " + push.stderr[:200])
        proc = gate.subprocess.Popen(["bash", script], cwd=str(repo), env=env, text=True, start_new_session=True,
                                     stdout=gate.subprocess.PIPE, stderr=gate.subprocess.STDOUT)
        try:
            train = gate._VERIFY_TRAIN.get()
            if train is None or train["window"] != window:
                out, _ = proc.communicate(timeout=timeout)
            else:
                deadline = gate.time.monotonic() + timeout
                while True:
                    reason = gate.predecessor_state(**{k: v for k, v in train.items() if k != "window"})
                    if reason:
                        raise gate.TrainCancelled(reason)
                    left = deadline - gate.time.monotonic()
                    if left <= 0:
                        raise gate.subprocess.TimeoutExpired(proc.args, timeout)
                    try:
                        out, _ = proc.communicate(timeout=min(gate.train_poll_secs(), left))
                        break
                    except gate.subprocess.TimeoutExpired:
                        continue
        except gate.TrainCancelled:
            gate.stop_verification(proc)
            raise
        except gate.subprocess.TimeoutExpired:
            gate.stop_verification(proc)
            return dict(state="undetermined", nodes=[], url="", doc={}, why=f"verification timed out after {timeout:.0f}s")
        doc, nodes, url = gate.parse_verdict(out)
        checks = doc.get("checks")
        # predeploy_gate.sh (L-spec-0764) reports each check as {"state": "ran", "run_id", "url"};
        # older fixtures use the bare string "ran". Accept both: a shape mismatch here turned
        # every green CI verdict into a red window with nodes=[] (windows 0101 and 0201, 2026-10-10).
        def _ran(v):
            return v == "ran" or (isinstance(v, dict) and v.get("state") == "ran")
        all_ran = (isinstance(checks, dict) and all(_ran(checks.get(c)) for c in required))
        pg_failed, pg_failures = False, {}
        if all_ran and gate.PG_CHECK in required:
            try:
                pg_failed, pg_failures = gate.pg_result(repo, doc, rehearsal=rehearsal)
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
            gate.git(repo, "push", "--no-verify", "origin", f":refs/heads/{ref}", check=False)
