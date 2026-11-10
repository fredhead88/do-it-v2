#!/usr/bin/env python3
"""Bounded master guard scheduling and collection; never changes lane state."""
import argparse
import ast
from datetime import datetime, timedelta, timezone
import fcntl
import io
import json
import os
import pathlib
import re
import subprocess
import tempfile
import zipfile
import zlib

import yaml

import base_red as br

SHA = re.compile(r"[a-f0-9]{40}")
COVERED = ["pg-parity", "pytest"]
MAX_BYTES = 8 * 1024 * 1024
EXPIRY_AGE = timedelta(hours=24)
SHARD_NAME = re.compile(r"pytest — shard (\d+) of (\d+)")
# Failures that say nothing about the run itself: never a reason to expire an intent.
TRANSIENT = frozenset({
    "GitHub API failed", "missing GitHub API pages", "invalid GitHub API page",
    "invalid GitHub API rows", "GitHub log quota unavailable",
    "selection artifact quota unavailable", "selection artifact unavailable or oversized"})


class Undetermined(ValueError):
    """The answer could not be established; the intent stays pending."""


class Ambiguous(ValueError):
    """More than one run satisfies the intent."""


def _now():
    return datetime.now(timezone.utc)


def _clean(value):
    # One inert line, including C1 controls and Unicode line separators.
    return " ".join("".join(c if c.isprintable() else " " for c in str(value)).split())


def _save(path, state):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
            temporary = pathlib.Path(out.name)
            json.dump(state, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _transient(error):
    if isinstance(error, (OSError, subprocess.SubprocessError, Undetermined)):
        return True
    return isinstance(error, ValueError) and str(error) in TRANSIENT


def _bound(text, size=300):
    return _clean(text)[:size]


def _stamp(value):
    return br._time(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(owner, rid):
    """One run by id: the document, None for a proven 404, ValueError otherwise."""
    rc, out, err = br.ghlimit.run(["gh", "api", f"repos/{owner}/actions/runs/{rid}"], wait=False)
    if rc:
        if rc != 75 and "HTTP 404" in (err or ""):
            return None
        raise Undetermined("run lookup failed: " + _bound(err or f"exit {rc}", 100))
    try:
        doc = json.loads(out)
    except ValueError:
        raise Undetermined("run lookup unparsable") from None
    if not isinstance(doc, dict) or str(doc.get("id")) != str(rid):
        raise Undetermined("run lookup returned a different run")
    return doc


def accept(project, repo, owner, pending):
    """The run id this intent dispatched, None when provably absent, else ValueError."""
    full = pending["kind"] == "full"
    wanted = "full" if full else "selected"
    try:
        sent = br._time(pending["sent_at"])
        path = (f"repos/{owner}/actions/workflows/python-tests.yml/runs?event=workflow_dispatch"
                f"&branch=master&created=>={_stamp(sent)}&per_page=100")
        listing = br._api(path, "workflow_runs")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        pending["last_reason"] = _bound("run listing unreadable: " + str(error))
        raise Undetermined(pending["last_reason"]) from None
    accepted, dropped, unread = [], [], []
    for run in listing:
        rid = str(run.get("id", ""))
        try:
            fresh = (rid.isdigit() and rid not in pending["before_ids"]
                     and run.get("event") == "workflow_dispatch" and run.get("head_branch") == "master"
                     and br._time(run["created_at"]) >= sent)
        except (ValueError, KeyError, TypeError):
            fresh = False
        if not fresh:
            continue
        try:
            selection = _artifact(owner, rid)
        except (OSError, subprocess.SubprocessError) as error:
            unread.append(f"{rid}: artifact {type(error).__name__}")
            continue
        except ValueError as error:
            if _transient(error):
                unread.append(f"{rid}: {error}")
            else:
                dropped.append(f"{rid}: artifact {error}")
            continue
        except (KeyError, TypeError, zipfile.BadZipFile, zlib.error, UnicodeError) as error:
            dropped.append(f"{rid}: artifact {type(error).__name__}")
            continue
        mode = selection.get("mode") if isinstance(selection, dict) else None
        reason = selection.get("reason") if isinstance(selection, dict) else None
        if mode == wanted and (not full or reason == "suite-full"):
            accepted.append(run)
        else:
            dropped.append(f"{rid}: mode={mode} reason={reason}")
    notes = unread + dropped
    if notes:
        pending["last_reason"] = _bound("dropped " + "; ".join(notes), 1000)
    elif "last_reason" in pending:
        pending["last_reason"] = ""
    if unread:
        raise Undetermined(pending["last_reason"])
    exact = [r for r in accepted if r.get("head_sha") == pending["sha"]]
    chosen = exact or accepted
    if len(chosen) > 1:
        pending["last_reason"] = _bound("ambiguous runs " + ", ".join(str(r["id"]) for r in chosen))
        raise Ambiguous("dispatch acceptance ambiguous")
    return str(chosen[0]["id"]) if chosen else None


def _descendant(repo, base, head):
    if not (isinstance(base, str) and SHA.fullmatch(base)):
        return False
    try:
        return base in br.window_gate.git(repo, "rev-list", "--first-parent", head).stdout.split()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return False


def _shards(jobs):
    """Pytest shard jobs, minus a skipped shard numbered past the count in its own name."""
    result = []
    for job in jobs:
        name = job.get("name", "")
        if not name.startswith("pytest — shard "):
            continue
        parsed = SHARD_NAME.fullmatch(name)
        if parsed and job.get("conclusion") == "skipped" and int(parsed[1]) > int(parsed[2]):
            continue
        result.append(job)
    return result


def _shard_text(shards):
    return _bound("; ".join(f"{j.get('name')}: {j.get('conclusion')}" for j in shards), 300)


def _expression(source, context):
    """Evaluate only a small, side-effect-free Actions expression vocabulary.

    Unknown syntax fails closed. This is deliberately not Python eval, nor an
    assumption that merely mentioning suite/ref/full_run isolates a group.
    """
    source = source.replace("||", " or ").replace("&&", " and ")
    tree = ast.parse(source.strip(), mode="eval")

    def read(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bool, int)):
            return node.value
        if isinstance(node, ast.Name) and node.id in ("true", "false"):
            return node.id == "true"
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return context[node.value.id][node.attr]
        if isinstance(node, ast.BoolOp):
            value = read(node.values[0])
            for child in node.values[1:]:
                if isinstance(node.op, ast.Or) and value:
                    return value
                if isinstance(node.op, ast.And) and not value:
                    return value
                value = read(child)
            return value
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = read(node.left), read(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq): return left == right
            if isinstance(node.ops[0], ast.NotEq): return left != right
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "format" and not node.keywords):
            args = [read(a) for a in node.args]
            if args and isinstance(args[0], str):
                return args[0].format(*[_text(a) for a in args[1:]])
        raise ValueError("unsupported concurrency expression")
    return read(tree.body)


def _text(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def _group(template, inputs, ref="refs/heads/master"):
    context = dict(inputs={"tree": "", "runner": "", "pg_runner": "", **inputs},
                   github=dict(workflow="Python Tests", ref=ref))
    rendered = re.sub(r"\$\{\{(.*?)\}\}",
                      lambda m: _text(_expression(m[1], context)), template)
    if "${{" in rendered or not rendered.strip():
        raise ValueError("unreadable concurrency group")
    return rendered


def _workflow(repo, sha):
    source = br.window_gate.git(repo, "show", f"{sha}:.github/workflows/python-tests.yml").stdout
    document = yaml.safe_load(source)
    trigger = document.get("on", document.get(True, {}))
    inputs = trigger["workflow_dispatch"]["inputs"]
    if not {"window", "nightly", "full"} <= set(inputs["suite"]["options"]):
        raise ValueError("master workflow does not accept suite=full/nightly/window")
    if not {"full_run", "select_base", "rerun_ids", "checks"} <= inputs.keys():
        raise ValueError("master workflow is missing dispatch inputs")
    group = document["concurrency"]["group"]
    if not isinstance(group, str):
        raise ValueError("master workflow concurrency is unreadable")
    common = dict(checks="", rerun_ids="", select_base="", full_run=True)
    combinations = [("full", True, "refs/heads/master"),
                    ("nightly", True, "refs/heads/master"),
                    ("window", True, "refs/heads/master"),
                    ("window", False, "refs/heads/master"),
                    ("window", True, "refs/heads/ci-verify-a"),
                    ("window", True, "refs/heads/ci-verify-b")]
    groups = [_group(group, dict(common, suite=s, full_run=f), ref) for s, f, ref in combinations]
    if len(set(groups)) != len(groups):
        raise ValueError("workflow concurrency does not isolate guard, nightly and lane runs")
    declarations = []
    for job in document["jobs"].values():
        for step in job.get("steps", []):
            if "ci_selection.py request" in step.get("run", ""):
                declarations.append(step.get("env", {}).get("SELECT_BASE"))
    if not declarations or any(x != "${{ inputs.select_base }}" for x in declarations):
        raise ValueError("full_run must preserve a nonempty selected base")
    return group


def _tip(repo):
    br.window_gate.git(repo, "fetch", "--quiet", "origin", "master")
    sha = br.window_gate.git(repo, "rev-parse", "FETCH_HEAD^{commit}").stdout.strip()
    if not SHA.fullmatch(sha):
        raise ValueError("master did not resolve to a full commit")
    return sha


def _canary_base(repo, sha):
    ancestors = br.window_gate.git(repo, "rev-list", "--first-parent", "--max-count=65", sha).stdout.splitlines()
    for base in ancestors[1:]:
        if not SHA.fullmatch(base):
            raise ValueError("invalid canary ancestor")
        changed = br.window_gate.git(repo, "diff", "--name-only", base, sha, "--").stdout.splitlines()
        if any(re.fullmatch(r"(?:api/)?tests/(?:[\w.-]+/)*test_[\w.-]+\.py", p) for p in changed):
            return base
    raise ValueError("no ancestral base with a nonempty Python test change")


def _artifact(owner, rid):
    artifacts = br._api(f"repos/{owner}/actions/runs/{rid}/artifacts?per_page=100", "artifacts")
    found = [a for a in artifacts if a.get("name") == "ci-selection-request" and not a.get("expired")]
    if len(found) != 1 or not str(found[0].get("id", "")).isdigit():
        raise ValueError("selection artifact missing or ambiguous")
    if not br.ghlimit.gate(wait=False)["ok"]:
        raise ValueError("selection artifact quota unavailable")
    aid = str(found[0]["id"])
    proc = subprocess.run([br.ghlimit.real_gh(), "api", f"repos/{owner}/actions/artifacts/{aid}/zip"],
                          capture_output=True, timeout=30)
    if proc.returncode or len(proc.stdout) > MAX_BYTES:
        raise ValueError("selection artifact unavailable or oversized")
    with zipfile.ZipFile(io.BytesIO(proc.stdout)) as archive:
        members = archive.infolist()
        if (len(members) != 1 or members[0].filename != "ci-selection-request.json"
                or members[0].file_size > MAX_BYTES):
            raise ValueError("selection artifact has invalid members")
        return json.loads(archive.read(members[0]))


def _attribution(project, repo, sha):
    greens = [r for r in br._rows(project) if r.get("type") == "master-full-run"
              and r.get("complete") is True and r.get("green") is True
              and r.get("suite") == "full" and r.get("covered") == COVERED]
    if not greens:
        return dict(merges_since_green=[], attribution_undetermined=True, baseline=None)
    baseline = max(greens, key=lambda r: br._time(r["ts"]))["sha"]
    first_parent = br.window_gate.git(repo, "rev-list", "--first-parent", sha).stdout.splitlines()
    if baseline not in first_parent:
        return dict(merges_since_green=[], attribution_undetermined=True, baseline=baseline)
    rows = br.window_gate.git(repo, "log", "--first-parent", "--merges", "--reverse",
                             "--format=%H%x00%s", baseline + ".." + sha).stdout.splitlines()
    merges = []
    for row in rows:
        commit, subject = row.split("\0", 1)
        if not SHA.fullmatch(commit):
            raise ValueError("invalid attribution commit")
        merges.append(dict(sha=commit, subject=_clean(subject)))
    return dict(merges_since_green=merges, attribution_undetermined=False, baseline=baseline)


def _question(project, row):
    if row.get("complete") is not True or row.get("green") is not False:
        return
    rid = row["run_ids"][0]
    subject = f"master-full:{project}:{rid}"
    if any(r.get("type") == "question" and r.get("subject") == subject for r in br._nightly_rows(project)):
        return
    baseline = "undetermined" if row["attribution_undetermined"] else row["baseline"]
    merges = "; ".join(m["sha"] + " " + _clean(m["subject"]) for m in row["merges_since_green"])
    asks = ("Acknowledge red master full run " + row["url"] + "; failed identifiers: "
            + "; ".join(_clean(i) for i in row["ids"]) + "; baseline: " + str(baseline)
            + "; merges since green: " + (merges or "none"))
    br._append("question", project, row["sha"], complete=False, ids=row["ids"],
               subject=subject, kind="master-full-red", run_id=rid, asks=asks,
               master_full_run=rid)


def _collect(project, repo, owner, pending, run):
    rid = str(run["id"])
    if run.get("status") != "completed":
        return None
    actual = run.get("head_sha")
    if not isinstance(actual, str) or not SHA.fullmatch(actual):
        raise ValueError("run head is invalid")
    event = "master-full-run" if pending["kind"] == "full" else "selected-canary"
    rows = br._nightly_rows(project)
    previous = next((r for r in rows if r.get("type") == event and
                     (rid in r.get("run_ids", []) or r.get("run_id") == rid)), None)
    if previous is not None:
        if event == "master-full-run": _question(project, previous)
        return previous
    fields = dict(run_ids=[rid], suite=pending["inputs"]["suite"], covered=[],
                  green=False, undetermined=[], url=f"https://github.com/{owner}/actions/runs/{rid}")
    ids, complete = [], False
    if actual != pending["sha"] and not (pending["kind"] == "full"
                                         and _descendant(repo, pending["sha"], actual)):
        fields["undetermined"].append("master advanced before dispatch acceptance")
    elif run.get("conclusion") not in {"success", "failure"}:
        fields["undetermined"].append("run terminal state is " + _clean(run.get("conclusion")))
    else:
        _workflow(repo, actual)
        selection = _artifact(owner, rid)
        members = br._logs(owner, rid)
        mode = br._selection(repo, actual, run, members)
        wanted = "full" if pending["kind"] == "full" else "selected"
        if not isinstance(selection, dict) or selection.get("mode") != wanted or mode != wanted:
            raise ValueError("selection artifact and execution do not prove " + wanted)
        fields["selection"] = selection
        if pending["kind"] == "full":
            snapshot, _ = br._snapshot(repo, actual, runs=[run])
            fields["covered"] = snapshot["kinds_covered"]
            fields["undetermined"] = snapshot["undetermined"]
            jobs = br._api(f"repos/{owner}/actions/runs/{rid}/jobs?per_page=100", "jobs")
            partial = [j for j in _shards(jobs) if j.get("conclusion") not in {"success", "failure"}]
            if partial:
                fields["undetermined"].append(f"{len(partial)} pytest shards did not execute completely: "
                                               + _shard_text(partial))
            ids = sorted(set(i for k in br.KINDS for i in snapshot["ids"][k]))
            unexpected = [i for i in ids if br.id_kind(i) not in COVERED]
            if unexpected:
                fields["undetermined"].append(f"{len(unexpected)} failures outside Python/PG coverage: "
                                               + "; ".join(_clean(i) for i in unexpected))
            complete = (fields["covered"] == COVERED and not fields["undetermined"]
                        and (bool(ids) == (run["conclusion"] == "failure")))
            fields["green"] = complete and not ids
            if not complete: fields["undetermined"].append("Python/PG verdict is incomplete")
        else:
            files = selection.get("pytest_files")
            if (not isinstance(files, list) or not files or any(not isinstance(p, str)
                    or not br.window_gate.safe_path(p) or not p.endswith(".py") for p in files)):
                raise ValueError("canary did not select a nonempty Python test set")
            declarations = [re.search(r"CI-SELECTION mode=selected reason=[\w-]+ files=(\d+)", text)
                            for _, text in members]
            if not any(m and int(m[1]) > 0 for m in declarations):
                raise ValueError("canary selected request has no selected execution")
            parsed = [br.parse_log(text) for _, text in members]
            ids = sorted(set(i for p in parsed for k in br.KINDS for i in p[k]))
            fields["undetermined"] = [why for p in parsed for why in p["undetermined"]]
            jobs = br._api(f"repos/{owner}/actions/runs/{rid}/jobs?per_page=100", "jobs")
            executed = {"success", "failure"}
            shards = _shards(jobs)
            partial = [j for j in shards if j.get("conclusion") not in executed]
            if partial:
                fields["undetermined"].append("pytest shards did not execute completely: " + _shard_text(partial))
            if (not any(j.get("name") == "pytest" and j.get("conclusion") in executed for j in jobs)
                    or not shards or any(j.get("conclusion") not in executed for j in shards)
                    or not any("SUMMARY pytest_baseline_gate.py check:" in text for _, text in members)):
                fields["undetermined"].append("canary selected execution has incomplete pytest coverage")
            unexpected = [i for i in ids if br.id_kind(i) not in COVERED]
            if unexpected:
                fields["undetermined"].append(f"{len(unexpected)} failures outside Python/PG coverage: "
                                               + "; ".join(_clean(i) for i in unexpected))
            classification = br.classify_selected(project, actual, ids)
            fields.update(run_id=rid, classification=classification,
                          select_base=pending["inputs"]["select_base"])
            complete = not fields["undetermined"] and (run["conclusion"] == "success" or bool(ids))
            fields["green"] = (complete and classification["undetermined"] is False
                               and not classification["rerun_on_base"]
                               and set(classification["known_red"]) == set(ids))
            if not fields["green"]: fields["undetermined"].append("selected failures not fully explained")
            fields["covered"] = ["pytest"] if complete else []
    if pending["kind"] == "full":
        fields.update(_attribution(project, repo, actual))
    else:
        fields["run_id"] = rid
    br._append(event, project, actual, complete=complete, ids=ids, **fields)
    row = dict(type=event, project=project, sha=actual, complete=complete, ids=ids, **fields)
    if pending["kind"] == "full": _question(project, row)
    return row


def _old(pending, now):
    return now - br._time(pending["sent_at"]) > EXPIRY_AGE


def _note(pending, text, path, state):
    text = _bound(text, 1000)
    if pending.get("last_reason") != text:
        pending["last_reason"] = text
        _save(path, state)


def _expire(project, pending, slug, now, path, state):
    event = "master-full-run" if pending["kind"] == "full" else "selected-canary"
    if not any(r.get("type") == event and r.get("expired_intent") == pending["sent_at"]
               for r in br._nightly_rows(project)):
        br._append(event, project, pending["sha"], complete=False, ids=[], green=False, covered=[],
                   undetermined=[f"intent expired: {slug}"], run_ids=[],
                   suite=pending["inputs"]["suite"], expired_intent=pending["sent_at"],
                   last_reason=_bound(pending.get("last_reason") or ""))
    pending["expired"] = {"at": now.astimezone(timezone.utc).isoformat(), "reason": slug}
    _save(path, state)


def _advance(project, repo, owner, pending, state, path, now, result):
    """Move one unresolved intent to accepted, collected or expired; doubt leaves it pending."""
    try:
        if not pending.get("run_id"):
            try:
                rid = accept(project, repo, owner, pending)
            except Ambiguous as error:
                _save(path, state)
                if _old(pending, now):
                    return _expire(project, pending, "ambiguous-runs", now, path, state)
                raise error
            except ValueError:
                _save(path, state)
                raise
            if rid is None:
                _save(path, state)
                if _old(pending, now):
                    return _expire(project, pending, "no-run-found", now, path, state)
                raise ValueError("dispatch acceptance pending: " + (pending.get("last_reason") or "no run yet"))
            pending["run_id"] = rid
            pending["last_reason"] = ""
            _save(path, state)
        try:
            run = _run(owner, pending["run_id"])
        except ValueError as error:
            _note(pending, str(error), path, state)
            raise
        if run is None:
            _note(pending, "run " + pending["run_id"] + " not found", path, state)
            return _expire(project, pending, "run-missing", now, path, state)
        if run.get("status") != "completed":
            _note(pending, "run status is " + _clean(run.get("status")), path, state)
            if _old(pending, now):
                return _expire(project, pending, "run-not-completed", now, path, state)
            return
        try:
            row = _collect(project, repo, owner, pending, run)
        except ValueError as error:
            _note(pending, str(error), path, state)
            if not _transient(error) and _old(pending, now):
                return _expire(project, pending, "collection-unprovable", now, path, state)
            raise
        if row is None:
            return
        state["pending"].remove(pending)
        result["collected"] = True
        result["undetermined"].extend(row["undetermined"])
        _save(path, state)
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, RuntimeError, zipfile.BadZipFile,
            zlib.error, UnicodeError) as error:
        _note(pending, f"{type(error).__name__}: {error}", path, state)
        raise


def tick(project):
    """Serialize one bounded pass; intents are durable before a paid call."""
    result = dict(sha=None, dispatched=False, collected=False, undetermined=[])
    try:
        if project != "albert-scott":
            raise ValueError("master full dispatch is only configured for albert-scott")
        repo = br._repo(project)
        owner = br._owner(repo)
        now = _now()
        if now.tzinfo is None:
            raise ValueError("scheduler requires a timezone-aware clock")
        directory = br.fold.ROOT / "master-full" / project
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "tick.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result["undetermined"].append("another tick owns the project scheduler")
                return result
            path = directory / "state.json"
            state = json.loads(path.read_text()) if path.exists() else dict(slots={}, pending=[], last_full=None)
            sha = _tip(repo)
            result["sha"] = sha
            # Collection is independent of current workflow eligibility and SHA.
            runs = br._api(f"repos/{owner}/actions/runs?per_page=100", "workflow_runs")
            for pending in list(state["pending"]):
                if pending.get("expired"):
                    continue
                try:
                    _advance(project, repo, owner, pending, state, path, now, result)
                except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError,
                        zipfile.BadZipFile, zlib.error, UnicodeError) as error:
                    result["undetermined"].append(_clean(error))
            group = _workflow(repo, sha)
            for kind, hours in (("full", 3), ("canary", 12)):
                slot = int(now.timestamp()) // (hours * 3600)
                if state["slots"].get(kind) == slot:
                    continue
                if kind == "full" and state["last_full"] == sha:
                    continue
                try:
                    inputs = dict(suite="full" if kind == "full" else "window", full_run=True,
                                  select_base="" if kind == "full" else _canary_base(repo, sha),
                                  rerun_ids="", checks="")
                    target_group = _group(group, inputs)
                    for run in runs:
                        if run.get("status") == "completed": continue
                        if run.get("name") != "Python Tests": continue
                        if run.get("head_branch") != "master": continue
                        tracked = any(p.get("run_id") == str(run.get("id")) for p in state["pending"])
                        # Do not risk cancelling an unrelated active master run.
                        if not tracked:
                            raise ValueError("unrelated active master run has unproven concurrency group")
                    intent = dict(kind=kind, sha=sha, slot=slot, inputs=inputs, group=target_group,
                                  sent_at=now.isoformat(), before_ids=[str(r["id"]) for r in runs])
                    state["pending"].append(intent)
                    state["slots"][kind] = slot
                    if kind == "full": state["last_full"] = sha
                    _save(path, state)
                    argv = ["gh", "workflow", "run", "python-tests.yml", "--repo", owner, "--ref", "master"]
                    for key, value in inputs.items(): argv.extend(["-f", key + "=" + _text(value)])
                    rc, _, _ = br.ghlimit.run(argv, wait=False)
                    if rc:
                        result["undetermined"].append("dispatch acceptance unknown; durable intent retained")
                    else:
                        result["dispatched"] = True
                except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
                    result["undetermined"].append(_clean(error))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, SyntaxError,
            yaml.YAMLError, subprocess.SubprocessError) as error:
        result["undetermined"].append(_clean(error))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    result = tick(parser.parse_args(argv).project)
    print(json.dumps(result, sort_keys=True))
    return 1 if result["undetermined"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
