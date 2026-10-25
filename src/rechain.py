"""Fail-closed Alembic re-chaining and isolated final-tree fingerprinting.

Only Git objects are edited; fingerprinting uses a disposable detached worktree.
The PG override accepts <directory> loopback to start and <directory> stop to stop.
"""
import ast
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import tempfile

import fold
import grading_env
import prod_runner
import resources

VERSIONS = "api/alembic_supabase/versions/"
MANIFEST = "api/alembic_supabase/branch_isolation.json"
TEST = "api/tests/test_alembic_branch_isolation.py"
AUTHORITY = "scratch_listing_authority_cleanup.py"
FINGERPRINT = "api/alembic_supabase/genesis_schema_fingerprint.txt"
SHARED = {MANIFEST, TEST, AUTHORITY, FINGERPRINT}
IDENT = ("-c", "user.name=doit-window", "-c", "user.email=doit-window@localhost")


def git(repo, *args, inp=None, check=True):
    p = subprocess.run(["git", "-C", str(repo), *args], input=inp,
                       capture_output=True, timeout=60)
    if check and p.returncode:
        raise ValueError("git object operation failed")
    return p


def out(repo, *args):
    return git(repo, *args).stdout.decode().strip()


def blob(repo, rev, path):
    return git(repo, "show", f"{rev}:{path}").stdout


def text(repo, rev, path):
    return blob(repo, rev, path).decode()


def added_versions(repo, base, branch):
    return [p for p in out(repo, "diff", "--diff-filter=A", "--name-only",
                          f"{base}...{branch}").splitlines()
            if p.startswith(VERSIONS) and "/_archive/" not in p and p.endswith(".py")]


def revision(source):
    values = {}
    for node in ast.parse(source).body:
        names = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for name in names:
            if isinstance(name, ast.Name) and name.id in ("revision", "down_revision"):
                values[name.id] = ast.literal_eval(node.value)
    if "revision" not in values or "down_revision" not in values:
        raise ValueError("migration lacks literal revision metadata")
    return values["revision"], values["down_revision"]


def policy(source):
    doc = json.loads(source)
    heads = [h for h in doc["heads"] if h.get("deployment") == "routed"]
    if len(heads) != 1:
        raise ValueError("manifest does not have exactly one routed head")
    head = heads[0]
    allowed = head["allowedCurrentRevisions"]
    if len(set(allowed)) != len(allowed) or not allowed or allowed[-1] != head["revision"]:
        raise ValueError("allowed revisions must be unique with head last")
    return doc, head


def routed_head(repo, rev):
    _, head = policy(text(repo, rev, MANIFEST))
    graph = {}
    for path in out(repo, "ls-tree", "-r", "--name-only", rev, VERSIONS).splitlines():
        if not path.endswith(".py") or "/_archive/" in path or path.endswith("/__init__.py"):
            continue
        r, p = revision(text(repo, rev, path))
        if r in graph:
            raise ValueError("duplicate migration revision")
        graph[r] = () if p is None else (p,) if isinstance(p, str) else tuple(p)
    children = {p for parents in graph.values() for p in parents}
    if set(graph) - children != {head["revision"]}:
        raise ValueError("manifest head disagrees with migration graph")
    # Every active revision must reach that head; cycles fail closed.
    seen, todo = set(), [head["revision"]]
    while todo:
        r = todo.pop()
        if r not in seen:
            seen.add(r)
            todo.extend(graph.get(r, ()))
    if not set(graph) <= seen:
        raise ValueError("disconnected migration graph")
    return head["revision"]


def manifest_edit(source, old, new, addition):
    doc, head = policy(source)
    if head["revision"] != old or new in head["allowedCurrentRevisions"]:
        raise ValueError("manifest head or successor is inconsistent")
    # Replace just this routed object's three values, retaining whitespace.
    old_head = dict(head)
    head["revision"] = new
    head["allowedCurrentRevisions"] = [*head["allowedCurrentRevisions"], new]
    head["reason"] += addition
    for key in ("revision", "allowedCurrentRevisions", "reason"):
        pattern = r'("' + key + r'"\s*:\s*)' + (r'\[[^\]]*\]' if key == "allowedCurrentRevisions" else r'"(?:[^"\\]|\\.)*"')
        hits = [m for m in re.finditer(pattern, source)
                if json.loads(m.group(0)[len(m.group(1)):]) == old_head[key]]
        if len(hits) != 1:
            raise ValueError("ambiguous manifest value position")
        m = hits[0]
        value = json.dumps(head[key], ensure_ascii=False)
        if key == "allowedCurrentRevisions":
            old_value = source[m.start() + len(m.group(1)):m.end()]
            if "\n" in old_value:
                indent = re.search(r'\n([ \t]*)"', old_value).group(1)
                end = old_value[old_value.rfind("\n"):]
                value = old_value[:old_value.rfind("\n")].rstrip() + ",\n" + indent + json.dumps(new) + end
            else:
                value = old_value[:-1].rstrip() + ", " + json.dumps(new) + "]"
        source = source[:m.start()] + m.group(1) + value + source[m.end():]
    return source


def test_edit(source, old, new):
    """Only revision literals in the real routed/scratch/CLI checks are changed."""
    lines = source.splitlines(keepends=True)
    tree = ast.parse(source)
    edits = []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef) or not re.search(r"isolated_heads|scratch|cli|route", fn.name):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.lineno != node.end_lineno:
                continue
            value = node.value
            if value not in (old, old + "\n", old + " (head)\n"):
                continue
            ln = lines[node.lineno - 1]
            literal = ln[node.col_offset:node.end_col_offset]
            replacement = literal.replace(old, new, 1)
            if value == old + " (head)\n" and ln.strip().endswith(","):
                replacement = literal.replace(" (head)", "") + ",\n" + ln[:node.col_offset] + replacement
            edits.append((node.lineno - 1, node.col_offset, node.end_col_offset, replacement))
        for i in range(fn.lineno - 1, fn.end_lineno):
            if re.search(r"#.*(?:current head|one current head)", lines[i]) and old in lines[i]:
                lines[i] = lines[i].replace(old, new)
    for i, start, end, value in sorted(edits, reverse=True):
        lines[i] = lines[i][:start] + value + lines[i][end:]
    return "".join(lines)


def reason_addition(base, branch):
    if branch.count(base) != 1:
        raise ValueError("reason addition is not unambiguous")
    return branch.replace(base, "", 1)


def authority_edit(current, base, branch, new):
    if current is None or base is None or branch is None:
        if base == branch:
            return current
        raise ValueError("authority file is missing")
    # Only one literal insertion into AUTHORITY_REVISIONS is allowed.
    if base == branch:
        return current
    bl, nl = base.splitlines(keepends=True), branch.splitlines(keepends=True)
    extra = [i for i, line in enumerate(nl) if re.fullmatch(r'\s*["\']' + re.escape(new) + r'["\'],\s*', line)]
    if len(extra) != 1 or nl[:extra[0]] + nl[extra[0] + 1:] != bl:
        raise ValueError("unrecognised authority change")
    i = extra[0]
    prefix = "".join(nl[:i])
    if "AUTHORITY_REVISIONS" not in prefix or i >= len(nl) - 1:
        raise ValueError("authority insertion outside revision set")
    anchor = nl[i + 1]
    if current.count(anchor) != 1:
        raise ValueError("ambiguous authority insertion point")
    return current.replace(anchor, nl[i] + anchor, 1)


def rewrite_tree(repo, tree, changes):
    """Write nested trees directly, preserving all modes; no temporary index."""
    entries = {}
    for raw in git(repo, "ls-tree", "-z", tree).stdout.split(b"\0"):
        if raw:
            meta, name = raw.split(b"\t", 1)
            entries[name.decode()] = meta.decode().split()
    grouped = {}
    for path, content in changes.items():
        name, sep, rest = path.partition("/")
        if sep:
            grouped.setdefault(name, {})[rest] = content
        else:
            mode = entries.get(name, ["100644"])[0]
            entries[name] = [mode, "blob", out_blob(repo, content)]
    for name, children in grouped.items():
        if name not in entries or entries[name][1] != "tree":
            raise ValueError("missing tree directory")
        entries[name][2] = rewrite_tree(repo, entries[name][2], children)
    data = b"".join((" ".join(meta) + "\t" + name).encode() + b"\0" for name, meta in entries.items())
    return git(repo, "mktree", "-z", inp=data).stdout.decode().strip()


def out_blob(repo, content):
    if isinstance(content, str):
        content = content.encode()
    return git(repo, "hash-object", "-w", "--stdin", inp=content).stdout.decode().strip()


def mechanical(repo, cur, branch):
    base = out(repo, "merge-base", cur, branch)
    paths = added_versions(repo, base, branch)
    if len(paths) != 1:
        raise ValueError("branch must add exactly one active migration")
    path = paths[0]
    source = text(repo, branch, path)
    r, p = revision(source)
    if not isinstance(r, str) or not isinstance(p, str):
        raise ValueError("migration requires a single literal parent")
    h = routed_head(repo, cur)
    _, bh = policy(text(repo, base, MANIFEST))
    _, nh = policy(text(repo, branch, MANIFEST))
    if bh["revision"] != p or nh["revision"] != r:
        raise ValueError("branch manifest and migration disagree")
    allowed = SHARED | {path}
    changed = out(repo, "diff", "--name-only", f"{base}..{branch}").splitlines()
    for other in changed:
        if other not in allowed and p.encode() in git(repo, "show", f"{branch}:{other}", check=False).stdout:
            raise ValueError("old parent literal in another changed file")
    expected = test_edit(text(repo, base, TEST), p, r)
    if text(repo, branch, TEST) != expected:
        raise ValueError("unrecognised isolation-test branch change")
    addition = reason_addition(bh["reason"], nh["reason"]).replace("from " + p, "from " + h, 1)
    migration, count = re.subn(r'(?m)^(down_revision\s*(?::[^=\n]+)?=\s*)["\'][^"\']+["\']',
                             lambda m: m.group(1) + repr(h), source)
    if count != 1:
        raise ValueError("unrecognised down_revision assignment")
    # Preserve quote style when the parent already is correct (replay).
    if p == h:
        migration = source
    changes = {path: migration, MANIFEST: manifest_edit(text(repo, cur, MANIFEST), h, r, addition),
               TEST: test_edit(text(repo, cur, TEST), h, r), FINGERPRINT: blob(repo, cur, FINGERPRINT)}
    def optional(rev):
        q = git(repo, "show", f"{rev}:{AUTHORITY}", check=False)
        return q.stdout.decode() if q.returncode == 0 else None
    authority = authority_edit(optional(cur), optional(base), optional(branch), r)
    if authority is not None:
        changes[AUTHORITY] = authority
    return changes, r, p, h, allowed


def resolve(repo, cur, entry, window, *, ev):
    spec, branch, sha = entry
    try:
        merged = git(repo, "merge-tree", "--write-tree", "--name-only", cur, sha, check=False)
        lines = merged.stdout.decode().splitlines()
        if merged.returncode == 0:
            commit = out(repo, *IDENT, "commit-tree", lines[0], "-p", cur, "-p", sha,
                         "-m", f"merge({spec}): window {window}")
            return commit, None
        if merged.returncode != 1:
            raise ValueError("merge-tree failed")
        conflicts = lines[1:lines.index("", 1)]
        changes, r, p, h, allowed = mechanical(repo, cur, sha)
        if not set(conflicts) <= allowed:
            raise ValueError("conflict outside re-chain allowlist")
        tree = rewrite_tree(repo, lines[0], changes)
        if routed_head(repo, tree) != r:
            raise ValueError("resolved tree does not have the new single head")
        commit = out(repo, *IDENT, "commit-tree", tree, "-p", cur, "-p", sha,
                     "-m", f"merge({spec}): window {window} (mechanical re-chain)")
        ev("rechained", spec, window=window, branch=branch, branch_sha=sha, merge_sha=commit,
           old_parent=p, new_parent=h, revision=r, files=sorted(changes), mechanical=True)
        return commit, None
    except (ValueError, KeyError, TypeError, SyntaxError, UnicodeError, subprocess.SubprocessError):
        why = "re-chain rules could not be applied safely"
        ev("rechain-declined", spec, window=window, branch=branch, why=why)
        return None, why


def is_pure(repo, merge_sha):
    try:
        parents = out(repo, "show", "-s", "--format=%P", merge_sha).split()
        if len(parents) != 2:
            return False
        base = out(repo, "merge-base", *parents)
        paths = added_versions(repo, base, parents[1])
        if len(paths) != 1:
            return False
        p = git(repo, "merge-tree", "--write-tree", *parents, check=False)
        if p.returncode not in (0, 1):
            return False
        plain = p.stdout.decode().splitlines()[0]
        diff = set(out(repo, "diff", "--name-only", plain, merge_sha).splitlines())
        return diff <= SHARED | set(paths)
    except (ValueError, subprocess.SubprocessError):
        return False


def child_env():
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("PG") or "DSN" in k or "DATABASE" in k
                    or "SUPABASE_DB" in k or k in ("AS_LOCAL_PG_ADMIN", "SCRATCH_PG_URL"))}


def stderr_tail(raw, limit=5):
    """Keep only bounded, redacted diagnostics from an untrusted child."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    lines = [line for line in (raw or "").splitlines() if line.strip()]
    return [prod_runner.redact(line)[:300] for line in lines[-limit:]] if limit > 0 else []


class FingerprintFailed(ValueError):
    def __init__(self, message, stage, raw=None):
        super().__init__(message)
        self.stage = stage
        self.stderr_tail = stderr_tail(raw)


class FailedFingerprints:
    """Pending specs excluded for tool failure, separate from merge conflicts."""
    def __init__(self):
        self.specs = set()

    def decline(self, specs, exc, window, ev):
        fields = dict(stage=exc.stage, stderr_tail=exc.stderr_tail) if isinstance(exc, FingerprintFailed) else {}
        for spec in specs:
            ev("rechain-declined", spec, window=window,
               why="final-tree fingerprint or private PG failed", **fields)
        self.specs.update(specs)
        print("gate-window: re-chain failed for " + ", ".join(specs) + f" ({len(specs)} migration(s) excluded)")

    def why(self, fallback):
        return "re-chain failed for " + ", ".join(sorted(self.specs)) + "; specs remain pending" if self.specs else fallback


def fingerprint(repo, candidate, window, *, ev, project=None):
    """Regenerate once with project Python; always stop and remove resources."""
    try:
        return _fingerprint(repo, candidate, window, ev=ev, project=project)
    except FingerprintFailed:
        raise
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise FingerprintFailed(prod_runner.redact(str(exc)), "worktree", getattr(exc, "stderr", None)) from None


def _fingerprint(repo, candidate, window, *, ev, project):
    env = child_env()
    try:
        if project is not None:
            if not grading_env._toml_path().is_file():
                raise ValueError("project grading configuration is missing")
            venv = grading_env.project_venv(project)
            if venv:
                bindir = pathlib.Path(venv) / "bin"
                if not bindir.is_dir():
                    raise ValueError("project venv bin is missing")
                env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", os.defpath)
    except (ValueError, OSError, TypeError):
        raise FingerprintFailed("fingerprint regeneration failed: project environment unavailable", "worktree") from None
    override = os.environ.get("DOIT_RECHAIN_PG_CMD")
    pgcmd = shlex.split(override) if override else ["bash", str(pathlib.Path(__file__).resolve().parent.parent / "scripts/private-pg.sh")]
    parent = fold.ROOT / "worktrees"
    parent.mkdir(parents=True, exist_ok=True)
    wt = pathlib.Path(tempfile.mkdtemp(prefix="doit-rechain-", dir=parent))
    wt_row = resources.record("worktree", str(wt), lane="rechain", subject=window)
    pg_row = None
    try:
        pg = pathlib.Path(tempfile.mkdtemp(prefix="doit-rechain-pg-"))
        pg_row = resources.record("scratch-db", str(pg), lane="rechain", subject=window)
        stage = "worktree"
        try:
            git(repo, "worktree", "add", "--detach", str(wt), candidate)
            stage = "pg-start"
            started = subprocess.run([*pgcmd, str(pg), "loopback"], env=env, cwd=wt,
                                     capture_output=True, timeout=120, text=True)
            if started.returncode:
                raise FingerprintFailed("private PG start failed", stage, started.stderr)
            m = re.search(r"^ADMIN_DSN='(postgresql://[^'\n]+@127\.0\.0\.1:\d+/postgres)'$", started.stdout, re.M)
            if not m:
                raise FingerprintFailed("private PG did not return a loopback admin endpoint", stage, started.stderr)
            env["AS_LOCAL_PG_ADMIN"] = m.group(1)
            stage = "fingerprint"
            cmd = shlex.split(os.environ.get("DOIT_RECHAIN_FINGERPRINT_CMD", "bash scripts/refresh_genesis_fingerprint.sh"))
            target = wt / FINGERPRINT
            target.unlink(missing_ok=True)
            result = subprocess.run([*cmd, "--out", str(target)], cwd=wt, env=env,
                                    capture_output=True, timeout=600)
            if result.returncode or not target.is_file():
                raise FingerprintFailed("fingerprint regeneration failed", stage, result.stderr)
            content = target.read_bytes()
            if content == blob(repo, candidate, FINGERPRINT):
                return candidate
            tree = rewrite_tree(repo, out(repo, "rev-parse", f"{candidate}^{{tree}}"), {FINGERPRINT: content})
            sha = out(repo, *IDENT, "commit-tree", tree, "-p", candidate, "-m", "window: regenerate genesis fingerprint")
            ev("rechain-fingerprint", window, sha=sha, fingerprint_sha256=hashlib.sha256(content).hexdigest())
            return sha
        except FingerprintFailed:
            raise
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise FingerprintFailed(prod_runner.redact(str(exc)), stage, getattr(exc, "stderr", None)) from None
        finally:
            stop = [*pgcmd, str(pg), "stop"] if override else ["/usr/lib/postgresql/17/bin/pg_ctl", "-w", "-D", str(pg / "data"), "-m", "fast", "stop"]
            stopped = subprocess.run(stop, env=child_env(), capture_output=True, timeout=30)
            if stopped.returncode and (pg / "data/postmaster.pid").exists():
                raise ValueError("private PG stop failed; directory retained")
            resources.release(pg_row, reason="creator-finally")
    finally:
        resources.release(wt_row, reason="creator-finally")
