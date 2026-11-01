"""Parallel, bounded diagnostic CI for a red window; never merge evidence."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import combinations
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

WORKFLOW = ".github/workflows/targeted-attribution.yml"
STATES = {"green", "red", "absent", "undetermined"}
TIMEOUT = 14 * 60


def _gh(args, seconds):
    """Shared quota admission with a bounded subprocess and no mutation retry."""
    import ghlimit
    if not ghlimit.gate(wait=False)["ok"]:
        raise ValueError("diagnostic quota unavailable")
    ghlimit._bump_spent()
    p = subprocess.run([ghlimit.real_gh(), *args], capture_output=True, text=True, timeout=seconds)
    if p.returncode:
        raise ValueError("diagnostic GitHub operation unavailable")
    return p.stdout


def classify(specs, ids, base, singles, pairs=None):
    """Classify independent observations; only positive reproduction blames a spec.

    An interaction charges the newer member of each failing pair. For windows
    larger than four, entirely determined clean singles permit the requested
    conservative newest-half fallback. Missing evidence never permits it.
    """
    plan = dict(drop={}, proven={}, environment=[], unresolved=[], need_pairs=[], mode="singles")
    if len(base) != 3:
        return dict(plan, unresolved=list(ids))
    def drop(spec, node, *, proven=False):
        nodes = plan["drop"].setdefault(spec, [])
        if node not in nodes:
            nodes.append(node)
        if proven and node not in plan["proven"].setdefault(spec, []):
            plan["proven"][spec].append(node)
    for node in ids:
        observed = [row.get(node, "undetermined") for row in base]
        if "red" in observed:
            plan["environment"].append(node)
            continue
        if any(s not in ("green", "absent") for s in observed):
            plan["unresolved"].append(node)
            continue
        states = {s: singles.get(s, {}).get(node, "undetermined") for s in specs}
        bad = [s for s in specs if states[s] == "red"]
        if bad:
            for spec in bad:
                drop(spec, node, proven=True)
            continue
        if any(v not in ("green", "absent") for v in states.values()):
            plan["unresolved"].append(node)
        elif len(specs) > 4:
            plan["mode"] = "newest-half"
            for spec in specs[len(specs) // 2:]:
                drop(spec, node)
        elif len(specs) < 2:
            plan["unresolved"].append(node)
        elif pairs is None:
            plan["need_pairs"].append(node)
            plan["unresolved"].append(node)
        else:
            plan["mode"] = "pairs"
            implicated = False
            for left, right in combinations(specs, 2):
                if pairs.get(left + "+" + right, {}).get(node) == "red":
                    drop(left if left in plan["drop"] else right, node, proven=True)
                    implicated = True
            if not implicated:
                plan["unresolved"].append(node)
    return plan


def validate(payload, sha, probe_id, ids):
    if (not isinstance(payload, dict) or payload.get("version") != 1 or payload.get("sha") != sha
            or payload.get("probe_id") != probe_id or payload.get("ids") != ids):
        raise ValueError("diagnostic identity mismatch")
    states = payload.get("states")
    if (not isinstance(states, dict) or set(states) != set(ids)
            or any(s not in STATES for s in states.values())):
        raise ValueError("incomplete diagnostic result")
    return states


def supported(repo, sha, ids):
    """Fail before dispatch: an old tree must never silently run full CI."""
    import window_gate as wg
    try:
        if (not ids or len(json.dumps(ids)) > 60000 or any(not isinstance(n, str) or "::" not in n
                or any(ord(c) < 32 for c in n) for n in ids)):
            return False
        for node in ids:
            path = node.split("::", 1)[0]
            if (not wg.safe_path(path) or path.startswith("-") or
                    not re.search(r"(?:\.py|\.(?:test|spec)\.[cm]?[jt]sx?)$", path)):
                return False
        workflow = wg.git(repo, "show", f"{sha}:{WORKFLOW}", check=False)
        return workflow.returncode == 0 and all(x in workflow.stdout for x in ("tree_sha:", "node_ids:", "probe_id:"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def probe(repo, sha, ids, *, deadline=None):
    """One unique dispatch/ref/observation, including timeout cancellation/cleanup."""
    import ghlimit
    import window_gate as wg
    unknown = dict.fromkeys(ids, "undetermined")
    if not supported(repo, sha, ids):
        return dict(states=unknown, why="targeted workflow/selectors unavailable")
    nonce = uuid.uuid4().hex
    ref = "ci-verify/attribution-" + nonce
    until = min(time.monotonic() + TIMEOUT, deadline if deadline is not None else float("inf"))
    pushed, run_id, complete = False, None, False
    owner = ""
    def gh(args):
        if time.monotonic() >= until:
            raise TimeoutError("targeted attribution deadline")
        return _gh(args, max(1, min(60, until-time.monotonic())))
    try:
        import base_red
        owner = base_red._owner(repo)
        pushed = wg.git(repo, "push", "--no-verify", "origin", f"{sha}:refs/heads/{ref}", check=False).returncode == 0
        if not pushed:
            raise ValueError("diagnostic ref unavailable")
        gh(["workflow", "run", WORKFLOW, "--repo", owner, "--ref", ref,
            "-f", "tree_sha=" + sha, "-f", "node_ids=" + json.dumps(ids), "-f", "probe_id=" + nonce])
        while True:
            if run_id is None:
                page = json.loads(gh(["api", f"repos/{owner}/actions/runs?head_sha={sha}&event=workflow_dispatch&per_page=100"]))
                runs = [r for r in page["workflow_runs"] if r.get("head_sha") == sha
                        and r.get("head_branch") == ref and r.get("path", "").split("@")[0] == WORKFLOW
                        and r.get("display_title") == "targeted-attribution " + nonce]
                if len(runs) > 1:
                    raise ValueError("ambiguous diagnostic dispatch")
                if runs:
                    run_id = str(runs[0]["id"])
            if run_id is not None:
                run = json.loads(gh(["api", f"repos/{owner}/actions/runs/{run_id}"]))
                if run.get("head_sha") != sha or run.get("head_branch") != ref:
                    raise ValueError("diagnostic run identity mismatch")
                if run.get("status") == "completed":
                    complete = True
                    if run.get("conclusion") not in ("success", "failure"):
                        raise ValueError("diagnostic cancelled or unavailable")
                    break
            time.sleep(min(10, max(.01, until - time.monotonic())))
        with tempfile.TemporaryDirectory(prefix="window-attribution-") as temp:
            gh(["run", "download", run_id, "--repo", owner, "-n", "targeted-attribution-" + nonce, "-D", temp])
            payload = json.loads((Path(temp) / "targeted-result.json").read_text())
        return dict(states=validate(payload, sha, nonce, ids), run_id=run_id,
                    url=f"https://github.com/{owner}/actions/runs/{run_id}", sha=sha)
    except Exception as exc:
        return dict(states=unknown, why=type(exc).__name__, run_id=run_id, sha=sha)
    finally:
        if run_id and not complete:
            try:
                _gh(["run", "cancel", run_id, "--repo", owner], 30)
            except Exception:
                pass
        if pushed:
            try:
                wg.git(repo, "push", "--no-verify", "origin", f":refs/heads/{ref}", check=False)
            except Exception:
                pass


def attribute(repo, base, entries, ids, *, runner=None, prepare=None):
    """Run three master controls and all singles concurrently, then only needed pairs.

    `prepare` is the gate's ordinary merge/rechain/policy builder. Diagnostic
    assembly must not drop specs or mutate the main candidate ref.
    """
    import window_gate as wg
    ids = list(dict.fromkeys(ids))
    specs = [s for s, _, _ in entries]
    run = runner or probe
    until = time.monotonic() + TIMEOUT
    evidence = {}
    batches = {("base", i): base for i in range(3)}
    def candidate(members):
        try:
            if prepare is not None:
                return prepare(members)
            tip = base
            for spec, _, sha in entries:
                if spec in members:
                    tip, conflicts = wg.merge_into(repo, tip, sha, "diagnostic attribution " + spec)
                    if conflicts:
                        return None
            return tip
        except Exception:
            return None
    for spec in specs:
        batches[("single", spec)] = candidate([spec])
    def matrix(trees, selected):
        answers = {}
        with ThreadPoolExecutor(max_workers=min(16, len(trees))) as pool:
            futures = {pool.submit(run, repo, sha, selected, deadline=until): label
                       for label, sha in trees.items() if sha is not None}
            for future in as_completed(futures):
                label = futures[future]
                try:
                    answer = future.result()
                    states = answer["states"]
                    if set(states) != set(selected) or any(v not in STATES for v in states.values()):
                        raise ValueError("invalid diagnostic states")
                except Exception:
                    answer = dict(states=dict.fromkeys(selected, "undetermined"), why="probe unavailable")
                key = label[0] + ":" + "+".join(map(str, label[1:]))
                evidence[key] = dict(answer, specs=list(label[1:]) if label[0] != "base" else [])
                answers[label] = answer["states"]
        return answers
    first = matrix(batches, ids)
    base_rows = [first.get(("base", i), {}) for i in range(3)]
    singles = {s: first.get(("single", s), {}) for s in specs}
    plan = classify(specs, ids, base_rows, singles)
    if plan["need_pairs"]:
        until = time.monotonic() + TIMEOUT
        trees = {("pair", a, b): candidate([a, b]) for a, b in combinations(specs, 2)}
        pair_answers = matrix(trees, plan["need_pairs"])
        plan = classify(specs, ids, base_rows, singles,
                        {a + "+" + b: states for (_, a, b), states in pair_answers.items()})
    return dict(plan, evidence=evidence, base_sha=base)
