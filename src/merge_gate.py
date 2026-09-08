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

★ AMBIENT STATE IS NOT TRUSTED (D114, D115). Every git call is pinned — repo
toplevel, `diff.relative=false`, `diff.ignoreSubmodules=none`,
`core.quotepath=false`, `LC_ALL=C` — and **every `GIT_*` environment variable is
dropped**, because `GIT_DIR` redirects the whole gate at another repository and
beats `-C`. A shallow clone, a replace-ref, a submodule cwd and an ambiguous ref
are all HARD STOPS: a truncated or misidentified history cannot answer a question
about history, and answering anyway is how a silent clean gets produced.

★ AND THE GRANT IS VALIDATED, NEVER INTERPRETED (D115). Three rounds of judging
seats each found a different way for English to become a grant. The lesson was
not another special case — the format was wrong. A token must look like a path,
a bare directory must carry its trailing slash, and breadth is tested by asking
whether the grant covers paths no grant may ever cover.

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
# Migration layouts that actually occur. A removal under any of them is named
# whatever the grant says: the excision case removes the evidence with the artifact.
DEFAULT_MIGRATIONS = r"(^|/)(migrations|migrate|versions)/"
DEFAULTS = {"DOIT_MIGRATIONS": DEFAULT_MIGRATIONS, "DOIT_GIT_TIMEOUT": "20",
            "DOIT_GATE_DEADLINE": "60", "DOIT_REVERT_DEPTH": "50",
            "DOIT_REVERT_MAX_PATHS": "200"}
PINNED = ("-c", "diff.relative=false", "-c", "diff.ignoreSubmodules=none",
          "-c", "core.quotepath=false", "-c", "diff.renames=true",
          # whether git WARNS about an ambiguous ref is itself a config key; the
          # ambiguity test below no longer depends on it, but pin it anyway.
          "-c", "core.warnAmbiguousRefs=true")
# ★ Every GIT_* variable is DROPPED, not inherited. GIT_DIR and GIT_WORK_TREE
# redirect the entire gate at another repository and beat `-C`; GIT_CONFIG_KEY_n
# reintroduces the very settings PINNED exists to fix. Git sets GIT_DIR itself
# inside hooks, aliases, `rebase --exec` and `bisect run`, so this is the normal
# case, not an attack. Anything the gate needs, it sets explicitly.
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
ENV.update(LC_ALL="C", LANG="C", GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
# Paths no grant may ever cover. A grant is tested against these rather than
# against a blocklist of literal spellings — `*/**`, `**.*` and `**/*.*` are all
# off switches and none of them appears in any list anyone would think to write.
SENTINELS = ("secrets/prod_keys.py", ".github/workflows/deploy.yml",
             "migrations/001_init.sql", "a/b/c/d/e.txt", "Makefile")
MIGRATIONS = re.compile(DEFAULT_MIGRATIONS, re.I)
GIT_TIMEOUT, DEADLINE, REVERT_DEPTH, REVERT_MAX_PATHS = 20.0, 60.0, 50, 200
_TOP, _START, _KNOBS = None, time.monotonic(), {}


def load_config():
    """Read the knobs, validate them, and remember which were overridden. A knob
    that silently weakens the gate and leaves no trace makes a doctored `clean`
    indistinguishable from an honest one, so every non-default is recorded on the
    event. A malformed one is could-not-determine, not a crash before any event
    is written."""
    global MIGRATIONS, GIT_TIMEOUT, DEADLINE, REVERT_DEPTH, REVERT_MAX_PATHS, _KNOBS
    _KNOBS = {k: os.environ[k] for k, d in DEFAULTS.items()
              if k in os.environ and os.environ[k] != d}
    try:
        MIGRATIONS = re.compile(os.environ.get("DOIT_MIGRATIONS", DEFAULT_MIGRATIONS), re.I)
        GIT_TIMEOUT = float(os.environ.get("DOIT_GIT_TIMEOUT", "20"))
        DEADLINE = float(os.environ.get("DOIT_GATE_DEADLINE", "60"))
        REVERT_DEPTH = int(os.environ.get("DOIT_REVERT_DEPTH", "50"))
        REVERT_MAX_PATHS = int(os.environ.get("DOIT_REVERT_MAX_PATHS", "200"))
    except (re.error, ValueError) as e:
        raise Undetermined(f"bad configuration: {e}")
    if MIGRATIONS.search("migrations/001.sql") is None:
        raise Undetermined("DOIT_MIGRATIONS no longer matches a migrations path — "
                           "that removes the always-name carve-out")
    for name, v in (("DOIT_GIT_TIMEOUT", GIT_TIMEOUT), ("DOIT_GATE_DEADLINE", DEADLINE),
                    ("DOIT_REVERT_DEPTH", REVERT_DEPTH), ("DOIT_REVERT_MAX_PATHS", REVERT_MAX_PATHS)):
        if v < 1:
            raise Undetermined(f"{name}={v} disables a check rather than tuning it")


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
    both named `main`, git resolves the TAG, warns on stderr and exits 0.

    ★ The ref store is asked directly. The previous version grepped stderr for
    "ambiguous", which is emitted only when `core.warnAmbiguousRefs` is true —
    a config key, i.e. exactly the ambient state this gate is not allowed to
    trust. Turning that key off produced a silent clean on a real removal."""
    if not ref.startswith("refs/"):
        hits = [ns + ref for ns in ("refs/heads/", "refs/tags/", "refs/remotes/")
                if run(("rev-parse", "--verify", "--quiet", ns + ref)).returncode == 0]
        if len(hits) > 1:
            raise Undetermined(f"{ref} is ambiguous — it names {' and '.join(hits)}; "
                               f"refusing to guess. Say refs/heads/{ref}")
    p = run(("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"))
    if p.returncode:
        raise Undetermined(f"{ref}: {p.stderr.decode(errors='replace').strip() or 'unresolvable'}")
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
    sup = git("rev-parse", "--show-superproject-working-tree").decode().strip()
    if sup:
        raise Undetermined(f"cwd is inside a submodule of {sup} — gating the submodule and "
                           f"reporting on it as if it were the project is how a clean gets "
                           f"earned about the wrong repository")


def blobs(refspecs):
    """Blob id per `<sha>:<path>`, None where absent. One subprocess for the batch.

    ★ `cat-file --batch-check` ECHOES its input on a miss and terminates every
    record with a NEWLINE — `-z` changes only the input side. So a path
    containing a line break splits one record into two, and zipping then
    attributes one file's content to a DIFFERENT file for every entry after it.
    A single `a\nb.txt` in the tree silently killed revert detection repo-wide.
    Such paths are rare and are looked up individually, which is exact."""
    res, batch = {}, [r for r in refspecs if "\n" not in r and "\r" not in r]
    for r in refspecs:
        if r not in batch:
            p = run(("rev-parse", r))
            res[r] = p.stdout.decode().strip() if p.returncode == 0 else None
    if batch:
        out = git("cat-file", "--batch-check=%(objectname) %(objecttype)",
                  stdin=("\n".join(batch) + "\n").encode("utf-8", "surrogateescape"))
        lines = out.decode("utf-8", "surrogateescape").splitlines()
        if len(lines) != len(batch):        # never guess at an alignment
            raise Undetermined(f"cat-file returned {len(lines)} records for {len(batch)} "
                               f"queries — refusing to align them by guesswork")
        for spec, line in zip(batch, lines):
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

    ★ ONE `git log` for the whole set. The previous shape ran one `rev-list` per
    path — measured at 25s for 200 paths on a busy repo, 42% of the deadline on a
    branch that turned out CLEAN. The batching that was already here covered the
    blob lookups and left the real cost driver alone.

    ponytail: bounded at REVERT_DEPTH commits of main's history. A revert to
    something older than that is not a thing that happens, and the unbounded
    version had to call a long history could-not-determine, which turned a
    204-commit CHANGELOG into permanent rework. Truncated history is a different
    problem and is a hard stop in preflight().
    """
    if len(paths) > REVERT_MAX_PATHS:
        raise Undetermined(f"{len(paths)} files modified outside the writes: grant "
                           f"(cap {REVERT_MAX_PATHS}) — widen the grant or split the branch")
    if not paths:
        return {}
    out = git("log", "-z", "--name-only", "--format=%x01%H", f"-{REVERT_DEPTH}",
              main, "--", *paths).decode("utf-8", "surrogateescape")
    window, commit = {p: [] for p in paths}, None
    for rec in out.split("\0"):
        if rec.startswith("\x01") or "\x01" in rec:
            head, _, tail = rec.rpartition("\x01")
            commit, rec = tail.strip().split("\n")[0], tail.partition("\n")[2]
        for name in rec.split("\n"):
            if name and commit and name in window:
                window[name].append(commit)
    return window


def gate(branch, main, grant):
    """What the merge would actually do to main, filtered to paths outside the grant."""
    global _START
    _START = time.monotonic()          # the budget is per RUN, not per process
    load_config()
    preflight()
    main_sha, branch_sha = resolve(main), resolve(branch)
    if main_sha == branch_sha:
        raise Undetermined(f"{branch} and {main} are the same commit — there is nothing to gate, "
                           f"and an empty diff is not a clean merge")
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


def check_grant(toks, whose):
    """★ A grant is VALIDATED, never interpreted.

    Three rounds of judging seats found three different ways for English to
    become a grant — `writes: the web dashboard only` granted `web/`, and
    `Writes: see the table below` granted `table/`. The lesson is not another
    special case: **the format was wrong.** A token must LOOK like a path, and a
    bare directory must say so with a trailing slash, which no English word does.

    Breadth is tested by BEHAVIOUR, not by a list of spellings. `*/**`, `**.*`
    and `**/*.*` are all off switches and none of them would occur to anyone
    writing a blocklist, so each candidate grant is simply asked whether it
    covers paths that no grant may ever cover."""
    if not toks:
        raise Undetermined(f"{whose} has a writes: line with no paths on it")
    for t in toks:
        if not re.fullmatch(r"[A-Za-z0-9_.@+*?\[\]/-]+", t) or not any(c in t for c in "/.*?"):
            raise Undetermined(f"{whose}: {t!r} is not a path or a glob. Prose is not a grant — "
                               f"write a bare directory as {t}/ if that is what you meant")
        hit = [s for s in SENTINELS if granted(s, [t])]
        if hit:
            raise Undetermined(f"{whose} grants {t!r}, which also covers {hit[0]!r} — "
                               f"that is not a grant, it is an off switch")
    return toks


def writes_grant(spec):
    """The spec's `writes:` grant. Deny loudly rather than guess: a named spec with
    no usable grant is could-not-determine, because an empty grant makes every
    legitimate removal look like a violation."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", spec):
        raise Undetermined(f"{spec!r} is not a spec id")
    p = fold.ROOT / "content" / f"{spec}.md"
    if not p.exists():
        raise Undetermined(f"no content file for {spec} — cannot read its writes: grant")
    for line in p.read_text().splitlines():
        # The key may wear markdown (`**Writes:**`, `| **Writes** |`); the VALUES
        # may not be touched, because `src/**` is a glob and `**bold**` is not.
        # So emphasis is stripped from the key region only, never per token.
        m = re.match(r"\s*[|>*_\-\s]*writes[*_\s]*[:|]\s*(.*)", line, re.I)
        if not m:
            continue
        body = m.group(1).strip()
        if m.group(0)[:m.start(1)].count("**") % 2 == 1 and body.startswith("**"):
            body = body[2:]                       # the closing half of `**Writes:**`
        body = body.strip().strip("|").replace("`", "").strip()
        return check_grant([t for t in re.split(r"[,\s|]+", body) if t], spec)
    raise Undetermined(f"{spec} states no writes: grant — nothing to filter against")


def parse(rest):
    main, spec, grant, flag = os.environ.get("DOIT_MAIN", "main"), None, [], None
    filled = set()          # which flags actually received a value
    if rest and not rest[0].startswith("-"):
        main, rest = rest[0], rest[1:]
    for a in rest:
        if a.startswith("-"):
            flag = a.lstrip("-")
            if flag not in ("spec", "writes"):
                sys.exit(f"merge-gate: unknown flag {a}")       # a typo is not a verdict
        elif flag == "spec":
            spec, flag = a, None; filled.add("spec")
        elif flag == "writes":
            grant.append(a); filled.add(flag)      # `--writes GLOB ...` takes several
        else:
            sys.exit(f"merge-gate: stray argument {a!r} — did you mean --writes {a!r}?")
    # the bug this replaces: `flag` was never cleared for --writes, so this test
    # fired every time and the documented flag exited unconditionally for two
    # rounds. Three arg checks all asserted failure cases, so the suite agreed.
    if flag and flag not in filled:
        sys.exit(f"merge-gate: --{flag} needs a value")
    return main, spec, grant


def main_(argv):
    branch = argv[0]
    if branch.startswith("-"):      # `doit gate --help` once appended a rework event with subject "--help"
        sys.exit(f"merge-gate: {branch!r} is not a branch\n{__doc__}")
    main, spec, grant = parse(list(argv[1:]))
    # NOT setdefault: an inherited DOIT_LEDGER_FILE routed the executor's stop
    # into another seat's file, and fold takes the actor from the filename — the
    # gate's own verdict would be recorded as the grader's.
    os.environ["DOIT_LEDGER_FILE"] = os.environ.get("DOIT_GATE_LEDGER_FILE", "L-executor-0001.jsonl")
    subject = spec or branch
    try:
        if grant:
            check_grant(grant, "--writes")     # the CLI was never checked at all
        if spec:
            grant += writes_grant(spec)
        r = gate(branch, main, grant)
        removed, reverts, main_sha, branch_sha, tree = r
        status = "rework" if (removed or reverts) else "clean"
        # The shas are the verdict's subject. Without them nothing ties a `clean`
        # to the --no-ff that follows it, and no audit can say what was gated.
        fold.append([f"merge-gate-{status}", subject, f"branch={branch}",
                     f"main_sha={main_sha[:12]}", f"branch_sha={branch_sha[:12]}",
                     f"merged_tree={tree[:12]}", f"repo={_TOP}", f"grant={json.dumps(grant)}"]
                    # a knob that weakens the gate and leaves no trace makes a
                    # doctored `clean` indistinguishable from an honest one
                    + ([f"knobs:={json.dumps(_KNOBS)}"] if _KNOBS else [])
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
