#!/usr/bin/env python3
"""merge-gate — perform the merge in memory and diff the result. Never model it.

  merge_gate.py <branch> [main] [--spec ID] [--writes GLOB ...]

THE SCAR (§4.11, D108): a path main gained *after* the branch's `base_sha`, and
that the branch then deletes, is absent at both ends of `base_sha..branch` and
produces no numstat row at all. Measured: 7 of the last 200 merges on main
removed a path and landed.

★ HOW (D113). `git merge-tree --write-tree` performs the real three-way merge in
memory; the gate diffs current main against the tree it returns. Two earlier
mechanisms both reasoned ABOUT a merge instead of performing one, and both failed
where the model and the merge diverge: `base_sha..branch` was blind to the scar,
and merge-base confirmation was blind on criss-cross history.

★ AMBIENT STATE IS NOT TRUSTED (D114). Every git call is pinned — repo toplevel,
`diff.relative=false`, `diff.ignoreSubmodules=none`, `core.quotepath=false`, and
`LC_ALL=C` — because a *security decision* read from the caller's cwd, config or
locale is a decision the branch author can influence. A shallow clone or a
replace-ref is a HARD STOP: a truncated history cannot answer a question about
history, and answering it anyway is how a silent clean gets produced.

Three caller states, never conflated (§5.3): nothing-to-report -> proceed (0) ·
removals/reverts outside the grant -> rework (1) · could-not-determine ->
rework (1). AN UNRESOLVABLE OR AMBIGUOUS REF, A CONFLICT, A TIMEOUT, A SHALLOW
REPO AND A MISSING OR UNPARSEABLE `writes:` GRANT ALL READ AS REWORK.

Runs at the Executor's merge step (§3.9, D17), BEFORE `--no-ff`.
"""
import collections, json, os, re, subprocess, sys, time

import fold

# A removal under a `migrations/` path SEGMENT is always named — grant or not,
# rename or not. Segment, not prefix: real migrations live at `supabase/migrations/`.
MIGRATIONS = re.compile(os.environ.get("DOIT_MIGRATIONS", r"(^|/)migrations/"), re.I)
GIT_TIMEOUT = float(os.environ.get("DOIT_GIT_TIMEOUT", "20"))
DEADLINE = float(os.environ.get("DOIT_GATE_DEADLINE", "60"))
REVERT_DEPTH = int(os.environ.get("DOIT_REVERT_DEPTH", "50"))
REVERT_MAX_PATHS = int(os.environ.get("DOIT_REVERT_MAX_PATHS", "200"))
# Grants so broad they are not grants. `*` matches every top-level file; `**`
# and `**/*` match the entire tree, which switches the gate off.
NOT_A_GRANT = {"*", "**", "**/*", "**/", "./**", "."}
PINNED = ("-c", "diff.relative=false", "-c", "diff.ignoreSubmodules=none",
          "-c", "core.quotepath=false", "-c", "diff.renames=true")
ENV = {**os.environ, "LC_ALL": "C", "LANG": "C", "GIT_OPTIONAL_LOCKS": "0"}
_TOP, _START = None, time.monotonic()


Result = collections.namedtuple("Result", "removed reverted main_sha branch_sha tree")


class Undetermined(Exception):
    """Anything the gate could not establish. Never reads as clean."""


def run(args, stdin=None):
    if time.monotonic() - _START > DEADLINE:
        raise Undetermined(f"deadline of {DEADLINE:.0f}s exceeded — a partial scan is not a clean one")
    base = ("git",) + (("-C", _TOP) if _TOP else ()) + PINNED
    try:
        return subprocess.run(base + tuple(args), capture_output=True,
                              timeout=GIT_TIMEOUT, env=ENV, input=stdin)
    except (OSError, subprocess.SubprocessError) as e:
        raise Undetermined(f"git {' '.join(args)}: {e}")


def git(*args, stdin=None):
    p = run(args, stdin)
    if p.returncode:
        raise Undetermined(" ".join(args) + ": " +
                           (p.stderr.decode(errors="replace").strip() or "failed"))
    return p.stdout


def resolve(ref):
    """A ref to one sha. AMBIGUITY IS COULD-NOT-DETERMINE: with a tag and a branch
    both named `main`, git resolves the tag, warns on stderr, and exits 0 — so a
    gate that reads only stdout silently checks the wrong history."""
    p = run(("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"))
    err = p.stderr.decode(errors="replace")
    if p.returncode:
        raise Undetermined(f"{ref}: {err.strip() or 'unresolvable'}")
    if "ambiguous" in err.lower():
        raise Undetermined(f"{ref} is ambiguous ({err.strip()}) — refusing to guess which one")
    return p.stdout.decode().strip()


def preflight():
    """A truncated history cannot answer a question about history."""
    global _TOP
    _TOP = None                 # resolve the toplevel in the CALLER's cwd, not the last run's
    p = run(("rev-parse", "--show-toplevel"))
    if p.returncode:
        raise Undetermined("not inside a git repository")
    _TOP = p.stdout.decode().strip()
    if git("rev-parse", "--is-shallow-repository").decode().strip() == "true":
        raise Undetermined("shallow repository — history is truncated, so `clean` would be unearned "
                           "(CI checkouts are shallow by default: fetch full depth)")
    if git("replace", "-l").strip():
        raise Undetermined("replace refs present — the visible history is not the real one")


def blobs(refspecs):
    """Blob id per `<sha>:<path>`, None where absent. ONE subprocess for all of
    them — the previous shape spawned one `rev-parse` per revision per path."""
    if not refspecs:
        return {}
    out = git("cat-file", "--batch-check=%(objectname) %(objecttype)",
              stdin=("\n".join(refspecs) + "\n").encode("utf-8", "surrogateescape"))
    res = {}
    for spec, line in zip(refspecs, out.decode("utf-8", "surrogateescape").splitlines()):
        parts = line.split()
        res[spec] = parts[0] if len(parts) == 2 and parts[1] == "blob" else None
    return res


def glob_rx(g):
    """`*` never crosses a path separator; `**` does. fnmatch's `*` crosses, which
    is how a grant of `*.py` came to cover `secrets/prod_keys.py`."""
    out, i = "", 0
    while i < len(g):
        if g.startswith("**", i):
            out, i = out + ".*", i + 2
        elif g[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif g[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(g[i]), i + 1
    return re.compile(out)


def granted(path, grant):
    """A grant covers a path, a directory's contents, or a glob — never a SIBLING.
    `src` must not grant `src_backup/keys.py`, so the boundary is a separator."""
    for g in (x for x in grant if x):
        if g.endswith("/"):
            if path.startswith(g):
                return True
        elif glob_rx(g).fullmatch(path):
            return True
        elif not any(c in g for c in "*?") and path.startswith(g.rstrip("/") + "/"):
            return True
    return False


def diff_names(a, b):
    """`git diff -z --name-status`, parsed. -z gives RAW paths — no C-quoting."""
    toks = git("diff", "-z", "--name-status", a, b).decode("utf-8", "surrogateescape").split("\0")
    out, i = [], 0
    while i + 1 < len(toks) and toks[i]:
        st = toks[i]
        if st[0] in "RC" and i + 2 < len(toks):
            out.append((st, toks[i + 1], toks[i + 2])); i += 3
        else:
            out.append((st, toks[i + 1], None)); i += 2
    return out


def revert_window(paths, main):
    """path -> the recent revisions of that path on main, for revert detection.

    ponytail: bounded at REVERT_DEPTH revisions per path. A revert to a version
    more than 50 revisions back is not a thing that happens, and the UNBOUNDED
    version had to declare a long file history could-not-determine, which turned
    a 204-commit CHANGELOG into permanent rework — the misapplication that gets a
    guard switched off. Raise DOIT_REVERT_DEPTH if a real case ever needs it.
    Truncated history is a different problem and is a hard stop in preflight().
    """
    if len(paths) > REVERT_MAX_PATHS:
        raise Undetermined(f"{len(paths)} files modified outside the writes: grant "
                           f"(cap {REVERT_MAX_PATHS}) — widen the grant or split the branch")
    return {p: git("rev-list", f"-{REVERT_DEPTH}", main, "--", p).decode().split() for p in paths}


def gate(branch, main, grant):
    """What the merge would actually do to main, filtered to paths outside the grant."""
    global _START
    _START = time.monotonic()          # the budget is per RUN, not per process
    preflight()
    main_sha, branch_sha = resolve(main), resolve(branch)
    p = run(("merge-tree", "--write-tree", main_sha, branch_sha))
    if p.returncode:
        raise Undetermined("the merge does not apply cleanly — a conflicted merge is not a gated one")
    tree = p.stdout.decode().splitlines()[0].strip()

    removed, modified = [], []
    for st, path, dest in diff_names(main_sha, tree):
        if st[0] in "DRT":
            # ★ NO RENAME EXEMPTION. A rename REMOVES the old path, and `R` is a
            # similarity heuristic, not a declaration of intent — git pairs an
            # unrelated 89%-similar file as a rename. Moving a migration out of
            # migrations/ under cover of a grant is the excision case verbatim,
            # and both judging seats ranked exactly that first (D114).
            if MIGRATIONS.search(path) or not granted(path, grant):
                where = f" -> {dest}" if dest else ""
                removed.append(f"{path}@{(blobs([f'{main_sha}:{path}']).get(f'{main_sha}:{path}') or '?')[:7]}{where}")
        elif st[0] == "M" and not granted(path, grant):
            modified.append(path)

    reverts = []
    if modified:
        window = revert_window(modified, main_sha)
        # every lookup in ONE subprocess — the old shape spawned one rev-parse per
        # revision per path, measured at 1.9s for a single 151-revision file.
        seen = blobs([f"{tree}:{p}" for p in modified]
                     + [f"{c}:{p}" for p, cs in window.items() for c in cs])
        reverts = [p for p in modified
                   if seen.get(f"{tree}:{p}") is not None
                   and any(seen.get(f"{c}:{p}") == seen[f"{tree}:{p}"] for c in window[p])]
    return Result(removed, reverts, main_sha, branch_sha, tree)


def demark(t):
    """Strip markdown bold, never a glob. Only `**x**`/`__x__` with BOTH ends
    marked is emphasis — `src/**` and `**/*.py` are globs and must survive
    untouched. (Stripping `*` from either end turned `src/*` into `src/`.)"""
    for m in ("**", "__"):
        if len(t) > 2 * len(m) and t.startswith(m) and t.endswith(m):
            return t[len(m):-len(m)]
    return t


def writes_grant(spec):
    """The spec's `writes:` grant. Deny loudly rather than guess: a named spec with
    no usable grant is could-not-determine, because an empty grant makes every
    legitimate removal look like a violation."""
    p = fold.ROOT / "content" / f"{spec}.md"
    if not p.exists():
        raise Undetermined(f"no content file for {spec} — cannot read its writes: grant")
    for line in p.read_text().splitlines():
        # `writes` must be followed by `:` or a table `|`. A SPACE is prose:
        # "Writes are limited to the merge gate script." previously parsed to
        # ['are','limited','to',...], and "writes ** see below **" yielded `**`,
        # which compiles to `.*` and switches the gate off entirely.
        m = re.match(r"\s*[|*_\-\s]*writes[*_\s]*[:|]\s*(.*)", line, re.I)
        if not m:
            continue
        body = m.group(1).strip().strip("|").replace("`", "")
        toks = [t for t in (demark(x) for x in re.split(r"[,\s|]+", body)) if t]
        if not toks:
            raise Undetermined(f"{spec} has a writes: line with no paths on it")
        bad = [t for t in toks if t in NOT_A_GRANT]
        if bad:
            raise Undetermined(f"{spec} grants {bad[0]!r}, which matches everything — "
                               f"that is not a grant, it is an off switch")
        return toks
    raise Undetermined(f"{spec} states no writes: grant — nothing to filter against")


def parse(rest):
    main, spec, grant, flag = os.environ.get("DOIT_MAIN", "main"), None, [], None
    if rest and not rest[0].startswith("-"):
        main, rest = rest[0], rest[1:]
    for a in rest:
        if a.startswith("-"):
            flag = a.lstrip("-")
            if flag not in ("spec", "writes"):
                sys.exit(f"merge-gate: unknown flag {a}")       # a typo is not a verdict
        elif flag == "spec":
            spec, flag = a, None
        elif flag == "writes":
            grant.append(a)
        else:
            sys.exit(f"merge-gate: stray argument {a!r} — did you mean --writes {a!r}?")
    if flag:
        sys.exit(f"merge-gate: --{flag} needs a value")
    return main, spec, grant


def main_(argv):
    branch = argv[0]
    main, spec, grant = parse(list(argv[1:]))
    os.environ.setdefault("DOIT_LEDGER_FILE", "L-executor-0001.jsonl")
    subject = spec or branch
    try:
        if spec:
            grant += writes_grant(spec)
        r = gate(branch, main, grant)
        removed, reverts, main_sha, branch_sha, tree = r
        status = "rework" if (removed or reverts) else "clean"
        # The shas are the verdict's subject. Without them nothing ties a `clean`
        # to the --no-ff that follows it, and no audit can say what was gated.
        fold.append([f"merge-gate-{status}", subject, f"branch={branch}",
                     f"main_sha={main_sha[:12]}", f"branch_sha={branch_sha[:12]}",
                     f"merged_tree={tree[:12]}"]
                    + ([f"removed={json.dumps(removed)}", f"reverted={json.dumps(reverts)}"]
                       if status == "rework" else []))
        print(f"merge-gate: {status}  ({main} {main_sha[:7]} + {branch} {branch_sha[:7]})"
              + (f"\n  removed:  {', '.join(removed)}" if removed else "")
              + (f"\n  reverted: {', '.join(reverts)}" if reverts else ""))
        return 0 if status == "clean" else 1
    except Undetermined as e:
        fold.append(["merge-gate-rework", subject, f"branch={branch}", f"undetermined={e}"])
        print(f"merge-gate: rework — could not determine: {e}")
        return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(main_(sys.argv[1:]))
