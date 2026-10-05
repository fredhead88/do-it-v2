#!/usr/bin/env python3
"""doit packet <role> <subject> — build one role's packet from the ledger.

A packet is the role's **Input** list, in order, and nothing its **Blindness**
strips. Both halves are code here, not prose in the Executor's head:

- the Input list is the per-role builder below;
- the strip list is `strip()`, which pulls the *real* strings this subject's
  packet must not contain — the builder's spawn id and branch, the grader's
  reasons, a sibling spec's body, the Plan's rationale — out of the ledger and
  the content dir, and **refuses the packet before it is written** if one is
  present. A contaminated packet costs a whole spawn (D120); this is cheaper.

Writes `$R/packets/<subject>-<role>-<n>.md` and prints the path.
"""
import argparse, hashlib, os, pathlib, re, subprocess, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import audit, fold, merge_gate, models, routing  # noqa: E402

ROOT = pathlib.Path(os.environ.get("DOIT_ROOT", pathlib.Path.home() / ".do-it"))
CONTENT, PACKETS = ROOT / "content", ROOT / "packets"

# The done-condition every build-facing packet carries, BY DEFAULT. Bytecode is
# not residue: the first real chain rejected a correct build because running the
# verify script left `__pycache__/` in the worktree, and "porcelain empty" cannot
# be the condition for a repo with no .gitignore for it (Active Problem 12).
DONE = ("the verify command exits 0; no tracked file is modified and no untracked file "
        "outside `__pycache__/` remains; exactly one commit above base_sha")


def done_condition(c, charter_id):
    """L-spec-0129: the done-condition text THIS packet carries. A charter may
    rule its own baseline convention (e.g. L-charter-0021's
    FAIL-SET-IDENTICAL-OR-SMALLER — hold the fail set identical to or smaller
    than a named baseline, rather than force exit 0) and record it as a
    `done-condition-override` event on the charter's own subject — planner or
    operator only. `fold.EMITS` is the sole authorization gate (R2): an override
    authored by any other actor never reaches `by_subject`, so it is invisible
    here — this function does not re-check the actor, because there is nothing
    left to check by the time an event survives the fold.

    No charter, or a charter with no override on file, returns the hardcoded
    `DONE` string UNCHANGED — R1's regression floor (AC1): byte-identical to
    current behavior. The LATEST override wins, same convention as every other
    `last()`-style read in this file."""
    if charter_id:
        ev = next((e for e in reversed(c.by.get(charter_id, []))
                   if e["type"] == "done-condition-override" and e.get("clause")), None)
        if ev:
            return ev["clause"]
    return DONE


AC_ROW = re.compile(r"^\S+ \[(ui|backend|observed-data|financial)\] ")
PLACEHOLDER = re.compile(r"TODO|TBD|\[NEEDS CLARIFICATION|as needed|etc\.")


def die(msg):
    sys.exit(f"packet: {msg}")


def resolve(p):
    """A content path in the ledger may be absolute, root-relative, or a bare name."""
    q = pathlib.Path(p)
    for cand in (q, ROOT / q, CONTENT / q.name):
        if cand.exists():
            return cand
    die(f"nothing on disk at {p}")


def long_lines(path, n=50):
    """A file's substantial lines — what a paste of it would drag along. The strip
    check looks for these verbatim in the packet."""
    try:
        return [l.strip() for l in pathlib.Path(path).read_text().splitlines() if len(l.strip()) > n]
    except OSError:
        return []


FENCE = re.compile(r"^```")


def _heading_positions(text):
    """Line indices that are real markdown headings (`^#+ `) — SKIPPING any such
    -looking line that sits inside a fenced (``` ``` ```) code block. Fence state
    toggles on any line starting with ``` , the same convention every fenced
    block in this repo already uses."""
    out, in_fence = [], False
    for i, line in enumerate(text.splitlines()):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and re.match(r"^#+ ", line):
            out.append(i)
    return out


def section(text, name):
    """The joined body of the first heading matching `name`, up to the next
    heading of any level — skipping a `#`-prefixed line that sits inside a
    fenced (``` ``` ```) code block, for BOTH the start match and the end
    match (R3c). `str | None`: `None` when no matching heading exists outside
    a fence; an existing heading with nothing beneath it before the next
    heading returns `""` (found, just empty) — never a bare list, never `[]`
    for absent."""
    lines = text.splitlines()
    headings = _heading_positions(text)
    pat = re.compile(rf"^#+\s*\d*\.?\s*[^\n]*{name}[^\n]*$", re.I)
    start = next((i for i in headings if pat.match(lines[i])), None)
    if start is None:
        return None
    end = next((i for i in headings if i > start), None)
    body_lines = lines[start + 1:end] if end is not None else lines[start + 1:]
    return "\n".join(body_lines).strip()


def criteria(body):
    """The spec's acceptance criteria, verbatim and with their review paths.

    One extractor for the grader and the reviewer, because they had two and the
    reviewer's — an `AC\\d+ \\[` line anchor — missed a spec whose criteria are
    bold (`**AC1 [ui] — ...**`). It reported "none found in the spec" and the
    reviewer returned no blocking finding on a review of nothing
    (`L-reviewer-0002`, 2026-09-08). A criterion the packet cannot find is
    undetermined, and undetermined is never clean: refuse."""
    sec = section(body, "Acceptance")
    return ((sec.splitlines() if sec else None)
            or [l for l in body.splitlines() if re.match(r"\s*\**AC\d+ \[", l)]
            or die("no acceptance criteria in the spec — a grade or review of "
                   "nothing is not a pass"))


class Ctx:
    def __init__(self, a):
        self.a = a
        self.specs, self.charters, _, self.by = fold.fold(fold.read_events())
        self.evs = self.by.get(a.subject, [])
        spec_writer_round_one = (getattr(a, "role", None) == "spec-writer"
                                 and (getattr(a, "slot", None) or getattr(a, "unit", None)))
        # a-14: `stage: charter-set` names no single charter at all — its subject is
        # the goal, and the charter SET comes from every `charter-filed` event on the
        # ledger, not from this one subject's own stream.
        charter_set = (getattr(a, "role", None) == "plan-auditor" and getattr(a, "stage", None) == "charter-set")
        # L-charter-0038 R1 / L-spec-0387: an owed-sweeper subject is a sweep id,
        # never a spec — it legitimately carries no prior events of its own.
        # `content/<subject>.md` (the manifest) is the packet's real input.
        owed_sweeper = getattr(a, "role", None) == "owed-sweeper"
        if not self.evs and not spec_writer_round_one and not charter_set and not owed_sweeper:
            # The one other legitimate exception is spec-writer round one: the spec id
            # was just allocated and nothing has been written about it yet, so the slot
            # IS the input (D7 — the Executor authoring a brief's spec has no other).
            die(f"{a.subject} has no events — nothing to build a packet from")

    def last(self, t, subject=None):
        return next((e for e in reversed(self.by.get(subject, self.evs) if subject else self.evs)
                     if e["type"] == t), None)

    def all_of(self, t):
        return [e for e in self.evs if e["type"] == t]

    def spec_file(self):
        e = self.last("spec-written") or die(f"{self.a.subject} has no spec-written event")
        return resolve(e.get("path") or die(f"{self.a.subject}'s spec-written carries no path"))

    def card_file(self):
        e = self.last("build-done") or die(f"{self.a.subject} has no build-done event")
        return resolve(e.get("card") or die(f"{self.a.subject}'s build-done carries no card"))

    def footprint(self):
        return (self.last("spec-written") or {}).get("footprint") or []

    def project(self):
        return self.a.project or next((e["project"] for e in reversed(self.evs) if e.get("project")), "unknown")

    def worktree(self):
        return self.a.worktree or str(ROOT / "worktrees" / self.project() / self.a.subject.lower())

    def standing(self):
        """(rejected criteria with their why, must-fix reverify lines) — the rework packet's centre."""
        open_ = fold.standing_rejects(self.evs)
        rej = [f"{e['criterion']}: {e.get('why', '')}" for e in self.all_of("rejected-criterion")
               if e.get("criterion") in open_]
        fix = [f"{e['criterion']}: reverify — {e.get('reverify', '')}" for e in self.all_of("must-fix")
               if e.get("criterion") in open_]
        return rej, fix

    def charter_file(self):
        p = self.a.charter or (self.last("charter-filed", self.charter_id()) or {}).get("path")
        return resolve(p) if p else None

    def charter_id(self):
        # Normalised, because half the ledger's `charter` fields are a path: an
        # unnormalised one matches no `charter-filed` subject and the file is lost.
        return fold.charter_id(next((e.get("charter") for e in reversed(self.evs) if e.get("charter")), None))

    def cut_file(self):
        """The Planner's cut for a charter (a-14): a `cut-written` event's path, else
        the `content/cut-<charter>.md` convention `mkpacket.py` used by hand."""
        e = self.last("cut-written")
        p = (e or {}).get("path") or str(CONTENT / f"cut-{self.a.subject}.md")
        return resolve(p)

    def plan_file(self):
        """The Planner's Plan, same shape as `cut_file()`. `None`, not a die — a
        `stage: cut` packet has no Plan yet, and that is a stage fact, not an error."""
        e = self.last("plan-written")
        p = (e or {}).get("path")
        if p:
            return resolve(p)
        cand = CONTENT / f"plan-{self.a.subject}.md"
        return cand if cand.is_file() else None


INTERP_NAMES = {"python", "python3", "pytest", "ruff", "node", "npm"}
MERGE_BASE_CALL = re.compile(r"\$\(\s*git\s+merge-base\b[^)]*\)")


def _quote_aware_scan(block):
    """One pass over a bash block, aware of `'...'`/`"..."` strings and `#`
    comments (not a shell parser — good enough for a verify block, which is one
    gated command chain, not general bash): the top-level segments split on
    `&&`, and any bare `;` (not `;;`) or bare `||` found outside a quote."""
    segs, cur, bad, i, n, q = [], "", [], 0, len(block), None
    while i < n:
        ch = block[i]
        if q:
            cur += ch
            if ch == "\\" and q == '"' and i + 1 < n:
                cur += block[i + 1]
                i += 2
                continue
            if ch == q:
                q = None
            i += 1
            continue
        if ch in "'\"":
            q, cur = ch, cur + ch
            i += 1
            continue
        if ch == "#":
            j = block.find("\n", i)
            j = n if j == -1 else j
            cur += block[i:j]
            i = j
            continue
        if block[i:i + 2] == "&&":
            segs.append(cur)
            cur = ""
            i += 2
            continue
        if block[i:i + 2] == "||":
            bad.append("a bare `||` near " + repr(block[max(0, i - 20):i + 22].strip()))
            cur += "||"
            i += 2
            continue
        if ch == ";" and block[i:i + 2] != ";;":
            bad.append("a bare `;` near " + repr(block[max(0, i - 20):i + 1].strip()))
            cur += ch
            i += 1
            continue
        cur += ch
        i += 1
    segs.append(cur)
    return [s.strip() for s in segs if s.strip()], bad


def _join_continuations(block):
    """R3a: a physical line ending in a backslash continuation (`... && \\`) joins
    with the line beneath it into ONE logical line, once, before either lint pass
    below runs — so a real multi-line `&&`-chain reads as the single command it
    is, not as several newline-separated statements. Quote-aware, the same way
    `_quote_aware_scan` is: a trailing backslash inside an open quote is a
    literal character of the string, not a continuation, so it is copied
    through untouched rather than joined away."""
    out, i, n, q = "", 0, len(block), None
    while i < n:
        ch = block[i]
        if q:
            out += ch
            if ch == "\\" and q == '"' and i + 1 < n:
                out += block[i + 1]
                i += 2
                continue
            if ch == q:
                q = None
            i += 1
            continue
        if ch in "'\"":
            q, out = ch, out + ch
            i += 1
            continue
        if ch == "\\" and i + 1 < n and block[i + 1] == "\n":
            out = out.rstrip(" \t") + " "
            i += 2
            continue
        out += ch
        i += 1
    return out


def _strip_comment(line):
    """R3b: a quote-aware `#`-strip for ONE physical line — a `#` inside a
    `'...'`/`"..."` string is never read as a comment start. `_newline_violations`
    below used to strip naively (`l.split("#", 1)[0]`), which truncated a line
    like `echo "value # not comment" &&` before its real trailing `&&`."""
    out, i, n, q = "", 0, len(line), None
    while i < n:
        ch = line[i]
        if q:
            out += ch
            if ch == "\\" and q == '"' and i + 1 < n:
                out += line[i + 1]
                i += 2
                continue
            if ch == q:
                q = None
            i += 1
            continue
        if ch in "'\"":
            q, out = ch, out + ch
            i += 1
            continue
        if ch == "#":
            break
        out += ch
        i += 1
    return out


def _newline_violations(block):
    """Rule (b)'s other half: a physical line that is not the block's last and
    does not end in `&&` is a statement the `&&` chain does not gate — `set -e`
    covers it today, but the next edit that wraps one line in a conditional
    would not, and a linted block is not meant to depend on that. `block` is
    expected already joined by `_join_continuations` (a real continuation is
    never a violation); the `#`-strip is quote-aware (R3b)."""
    lines = [l for l in block.splitlines() if l.strip() and not l.strip().startswith("#")]
    return [l.strip() for l in lines[:-1] if not _strip_comment(l).rstrip().endswith("&&")]


def _interp_violations(segs):
    """Rule (c): a segment invoking python/python3/pytest/ruff/node/npm — directly,
    or via `env` — by bare name resolves through the builder's PATH, which a worktree
    with no activated venv does not carry the same way twice."""
    out = []
    for seg in segs:
        toks = seg.split()
        while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
            toks.pop(0)
        if toks and pathlib.PurePosixPath(toks[0]).name == "env":
            toks = toks[1:]
        if not toks:
            continue
        cmd = toks[0]
        if pathlib.PurePosixPath(cmd).name in INTERP_NAMES and not cmd.startswith("/"):
            out.append(f"`{cmd}` in `{seg.strip()}`")
    return out


def verify_base_sha(c):
    """The base a verify script is measured against, pinned ONCE here — never
    re-derived by the running script, which is what a `$(git merge-base …)` inside
    the block itself would do on every run. Precedence, all six steps (R3d, R7
    Target 2 — this docstring is REVISED, not untouched: an earlier round of this
    same spec claimed this function was byte-for-byte unchanged; it is not,
    because Target 2 is a real second `build-done` step, additively before the
    first one):
    `--base-sha`, else the LATEST `build-done` carrying `base_sha_explicit: true`
    for this subject (if any), else the EARLIEST recorded `build-done.base_sha`
    for this subject, else `build-started`, else `spec-written`, else
    `git merge-base` against the worktree, once, at packet time; `git rev-parse
    HEAD` is the last resort for a worktree with nothing to diverge from yet.

    R7 Target 2 — the explicit step, additive: a landed merge-conflict/rebase
    round (`build-done.base_sha_explicit == true`, stamped by `dispatch.main`
    only when ITS OWN packet carried `PINNED_BASE_SHA_EXPLICIT: true`) pins the
    main tip it rebased onto — a later, plain rework must keep riding THAT pin,
    never revert past it to an earlier-recorded base. Byte-for-byte unchanged
    whenever no `build-done` on this subject's stream ever carries
    `base_sha_explicit` — every subject built before this spec ships.

    R3d — EARLIEST (the SECOND step, unchanged from before this revision), never
    the live worktree HEAD and never a later round's own base_sha: on a rework
    round (same worktree, same branch, an earlier round's commit already on it)
    the worktree's live HEAD is that earlier round's own `ready_sha`, not the
    true pre-round-1 base — third live occurrence, named at
    `events/L-executor-0001.jsonl:255`, caught once already at
    `events/L-builder-0207.jsonl:6`. `c.all_of("build-done")` is already scoped to
    THIS subject's own event stream (`Ctx.evs`), so a subject re-cut under a new
    spec id never inherits a killed predecessor's anchor. `build-started`/
    `spec-written` are unchanged — the MOST RECENT of each, same as before — only
    the `build-done` step moved from most-recent to earliest (and now carries the
    explicit sub-step ahead of it).

    One further, documented last-resort step, and only then the `die()`: **when the
    worktree path is not a directory**, take `git -C <repo> rev-parse HEAD`, the same
    repo path `_round_one_slot` and `p_builder` already derive (`--repo`, else
    `$R/repos/<project>`). A spec's audit is dispatched BEFORE any build, so at that
    moment no `base_sha` is recorded (not one `spec-written` event on the ledger
    carries one), and the worktree the builder will cut does not exist yet — without
    this step a bare `packet_spec_auditor(spec)` dies, and the base has to be handed
    in out of band by whoever dispatches. The existing precedence above is untouched;
    the `die()` remains for the case where the repo itself yields nothing."""
    if c.a.base_sha:
        return c.a.base_sha
    bds = c.all_of("build-done")
    explicit = [e for e in bds if e.get("base_sha") and e.get("base_sha_explicit")]
    if explicit:
        return explicit[-1]["base_sha"]
    bd = next((e for e in bds if e.get("base_sha")), None)
    if bd:
        return bd["base_sha"]
    for t in ("build-started", "spec-written"):
        e = c.last(t)
        if e and e.get("base_sha"):
            return e["base_sha"]
    wt = c.worktree()
    for ref in ("main", "master"):
        r = subprocess.run(["git", "-C", wt, "merge-base", "HEAD", ref], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    r = subprocess.run(["git", "-C", wt, "rev-parse", "HEAD"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    if not pathlib.Path(wt).is_dir():
        repo = getattr(c.a, "repo", None) or str(ROOT / "repos" / c.project())
        r = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    die(f"{c.a.subject}: no base_sha recorded, and neither `git merge-base` nor "
        f"`git rev-parse HEAD` in {wt} produced one — pin it with --base-sha")


class VerifyLint(Exception):
    """A Verification block violates one of `verify_script`'s lint rules — the
    only exception `verify_script` raises."""


def verify_script(spec_text):
    """AC1: pure — no `Ctx`, no `base_sha` argument, no filesystem write, never
    calls `sys.exit`. `None` when the spec's Verification fenced block is empty
    or absent (each call site's own "Verify command: NONE" line is unaffected).
    On a lint violation raises `VerifyLint`, the only exception this function
    raises. Otherwise returns the assembled `#!/usr/bin/env bash\\nset -euo
    pipefail\\n` + linted block, any `$(git merge-base …)` call rewritten to the
    LITERAL `$BASE` — this function never resolves one; `write_verify_script`
    below pins the real value.

    Linted (S23/S31a): `bash -n` on the assembled script; the block must be one
    `&&`-gated chain (no bare `;`/`||`, no newline-separated statement outside
    it, once backslash-continuation lines are joined into their one logical
    line — R3a/R3b); an interpreter named by a bare word instead of an
    absolute path."""
    sec = section(spec_text, "Verification")
    blk = re.search(r"```[a-z]*\n(.*?)```", sec or "", re.S)
    if not blk or not blk.group(1).strip():
        return None
    block = blk.group(1)
    joined = _join_continuations(block)
    segs, bad = _quote_aware_scan(joined)
    bad += [f"a newline-separated statement — `{l}` does not end in `&&`"
            for l in _newline_violations(joined)]
    if bad:
        raise VerifyLint("Verification block is not one gated `&&` chain: " + "; ".join(bad[:3]))
    bad_interp = _interp_violations(segs)
    if bad_interp:
        raise VerifyLint("Verification block names an interpreter by bare word, not an "
                          "absolute path: " + "; ".join(bad_interp[:3]))
    block = MERGE_BASE_CALL.sub("$BASE", block)
    # -e, because a multi-command block whose last line passes would otherwise exit 0
    # over an earlier failure, and the done-condition is that exit code.
    text = f"#!/usr/bin/env bash\nset -euo pipefail\n" + block
    check = subprocess.run(["bash", "-n", "/dev/stdin"], input=text, capture_output=True, text=True)
    if check.returncode != 0:
        raise VerifyLint(f"assembled verify script fails `bash -n`: {check.stderr.strip()}")
    return text


def standing_waivers(c):
    """L-spec-0195/AC7-AC9: every `verify-waiver{step, reason}` on this subject —
    `(step, reason)` pairs, step ascending, 1-indexed against the same `&&`
    segmentation `_quote_aware_scan` produces. A step re-waived carries the LATEST
    reason (a later operator/executor event may re-state one); nothing here
    re-numbers or invalidates a waiver when a later rework changes the segment
    count — that drift is the Executor's, out of band (Out of scope)."""
    out = {}
    for e in c.all_of("verify-waiver"):
        step = e.get("step")
        if step:
            out[int(step)] = e.get("reason", "")
    return sorted(out.items())


def apply_waivers(block, waivers):
    """L-spec-0195/AC7: `block` with every waived 1-indexed `&&`-segment replaced by
    a no-op (`true`) — `waivers` is `standing_waivers(c)`'s `(step, reason)` list.
    `[]` returns `block` unchanged."""
    if not waivers:
        return block
    steps = {s for s, _ in waivers}
    segs, _ = _quote_aware_scan(block)
    segs = ["true" if (i + 1) in steps else s for i, s in enumerate(segs)]
    return " && ".join(segs) + "\n"


def waiver_line(c):
    """L-spec-0195/AC8: one line naming every standing waived step and its reason,
    for the grader's and reviewer's own text — never the card (Out of scope,
    finding 5)."""
    w = standing_waivers(c)
    if not w:
        return "no standing verify-waiver applies to this subject"
    return "; ".join(f"step {s} waived — {r}" for s, r in w)


def out_of_grant_line(c):
    """L-spec-0195/AC9: `merge_gate.out_of_grant(branch, "main", spec_id)`'s result,
    for the reviewer packet ONLY (ADR-0028-6) — never the grader's, never the
    builder's. `merge_gate`'s own convention (every existing caller, including its
    own test suite) is the process cwd; this chdir's there and back rather than
    changing that seam's signature, which is owned by a sibling unit and consumed
    here strictly by its Seams-table signature. Never raises: a missing branch, a
    repo directory that does not exist, or `merge_gate.Undetermined` all render as
    prose, and packet building always completes (no `die()`, per AC9)."""
    bd = c.last("build-done") or {}
    branch = bd.get("branch")
    if not branch:
        return "no build-done branch on record for this subject"
    repo = c.a.repo or str(ROOT / "repos" / c.project())
    if not pathlib.Path(repo).is_dir():
        return "could not determine — no repository checkout on disk to gate against"
    prev = os.getcwd()
    try:
        os.chdir(repo)
        paths = merge_gate.out_of_grant(branch, "main", c.a.subject)
    except merge_gate.Undetermined:
        return "could not determine"
    finally:
        os.chdir(prev)
    return "no path outside the Writes grant" if not paths else "out-of-grant path(s): " + ", ".join(paths)


def write_verify_script(c):
    """The orchestration `verify_script` used to do itself: resolve the spec,
    resolve the real `base_sha` (`verify_base_sha`), inject `BASE=<sha>` right
    after `set -euo pipefail`, and write the executable `$R/content/verify-
    <spec>.sh` — the packet hands `bash <that>`, because a multi-step block
    cannot ride in the card's verify.command, which §5.10 caps at 300
    characters. A `VerifyLint` from the pure function still refuses the packet
    here — `die()`, non-zero exit, on stderr — rather than writing a script
    that lies about what it checks; `None` (no Verification block) passes
    through unchanged for every caller's own empty-verify line.

    L-spec-0194: right after the injected `BASE=<sha>` line, and before the
    spec's own block, one further conditional line is inserted when the
    subject's OWN most recent `build-started` carries `dsn_role == "readonly"`
    (`dispatch.provision_worktree_env`'s verdict) — meaning `<worktree>/.env`
    now holds the RO DSN under the name every `live_db` test reads. Exporting
    it at the OS-process level, before any interpreter in the chain starts, is
    what stops a `live_db` criterion from self-skipping — a bare `.env` file
    alone does not, since neither conftest in albert-scott calls
    `load_dotenv`. No `build-started` event yet, or `dsn_role` reading
    `absent`/`refused`/missing entirely, adds no line — byte-for-byte what this
    function produced before this addition.

    L-spec-0195/finding 3: writes a PER-ROLE file, `verify-<subject>-<c.a.role>.sh`
    — never one file two roles share, because a grader/reviewer waiver and the
    builder's own real chain must never collide on one path. For `c.a.role` in
    `("grader", "reviewer")` only, every standing `verify-waiver{step}` on the
    subject (`standing_waivers`) substitutes a no-op (`true`) for that 1-indexed
    `&&`-segment of the block, BEFORE the BASE/dsn assembly above — the builder's
    own script is never touched by a waiver, whatever role dispatched it."""
    spec = c.spec_file()
    try:
        text = verify_script(spec.read_text())
    except VerifyLint as e:
        die(f"{c.a.subject}'s {e}")
    if text is None:
        return None
    base = verify_base_sha(c)
    shebang, set_e, block = text.split("\n", 2)
    if c.a.role in ("grader", "reviewer"):
        block = apply_waivers(block, standing_waivers(c))
    bs = c.last("build-started")
    dsn_line = "set -a; . ./.env; set +a\n" if bs and bs.get("dsn_role") == "readonly" else ""
    # A blind role (grader/reviewer) gets the BASE line only when its block uses it:
    # a short block is inlined into the packet, and an unused BASE=<base_sha> there
    # tripped the grader's own Blindness check (L-spec-0484, 2026-10-04).
    uses_base = re.search(r"\$\{?BASE\b", block) is not None
    base_line = f"BASE={base}\n" if (uses_base or c.a.role not in ("grader", "reviewer")) else ""
    text = f"{shebang}\n{set_e}\n" + base_line + dsn_line + block
    p = CONTENT / f"verify-{c.a.subject}-{c.a.role}.sh"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    p.chmod(0o755)
    return p


def conflicts(c):
    """One line per other open spec whose footprint intersects — id, what it changes,
    state. Never its content (§4.6·3 item 7)."""
    mine, out = set(c.footprint()), []
    for sid, s in sorted(c.specs.items()):
        if sid == c.a.subject or s["state"] not in ("written", "building", "graded", "reviewing"):
            continue
        fp = set((next((e for e in reversed(s["evs"]) if e["type"] == "spec-written"), {}) or {})
                 .get("footprint") or [])
        if fp & mine:
            out.append(f"{sid} · {', '.join(sorted(fp & mine))} · {s['state']}")
    return out or ["none"]


def review_account(c):
    p = ROOT / f"review-account-{c.project()}"
    return p.read_text().strip() if p.exists() else "none — drive only what an anonymous visitor can reach"


RATIONALE_HEADING = re.compile(r"^#+\s*(Rationale|Why)\b", re.I)


def _sec(text, name, default="(none)"):
    """`section()` returns the section's text (`str | None`) directly now; this
    wrapper only supplies the default for "no such heading"/"empty body"."""
    sec = section(text, name)
    return sec if sec else default


def strip_rationale(text):
    """No Planner rationale ever reaches the plan-auditor, or a round-one spec-writer
    packet built straight from the cut/Plan (a-14, §4.6·6: "You are not given the
    Planner's rationale"). Strips any `## Rationale`/`## Why` section, to the next
    heading of any depth, and any line starting `Rationale:` — proactively, before
    the text is ever assembled into a packet (the Blindness `strip()` check below is
    the second, narrower line of defence, scoped to just those sections)."""
    out, skip = [], False
    for line in text.splitlines():
        if RATIONALE_HEADING.match(line):
            skip = True
            continue
        if skip and re.match(r"^#+\s", line):
            skip = False
        if skip:
            continue
        if re.match(r"^\s*Rationale:", line, re.I):
            continue
        out.append(line)
    return "\n".join(out)


def cut_audit_findings(c):
    """The LATEST cut-audit's findings only, mkpacket.py's `files[-1:]`: a re-cut is
    audited fresh and a superseded round's findings must not ride along into the
    plan-audit."""
    hits = [e for e in c.all_of("audit-finding") if e.get("stage") == "cut"]
    if not hits:
        return []
    latest = max(e["_src"].split(":")[0] for e in hits)
    return [e for e in hits if e["_src"].split(":")[0] == latest]


def _doc_for_charter(c, charter_id, event_type, prefix):
    """Where a charter-scoped document (cut, Plan) lives: the event that names it —
    looked up by CHARTER id, not the Ctx's own subject, since a spec-writer's round
    one is scoped to a spec that does not exist in the ledger yet — else the
    `content/<prefix>-<charter>.md` convention `mkpacket.py`/`mkslot.py` used by hand."""
    e = c.last(event_type, charter_id)
    return (e or {}).get("path") or str(CONTENT / f"{prefix}-{charter_id}.md")


FIELD_LABEL = re.compile(r"^(?:Goal|Delivers|Footprint|Consumes|Produces|Wave):")


def _unit_field(block, name):
    """A SINGLE-LINE unit field: `Goal:`, `Delivers:`, `Wave:` — and ONLY those three.
    The multi-line fields have their own reader below; this one is deliberately not it.

    Measured, not assumed: `\\s*` is not newline-safe, so against a BARE label this
    returns the next non-blank line's text — a bare `Produces:` above `Wave: 2` yields
    `"Wave: 2"`, not `""`. That bleed is why the multi-line fields moved off this
    reader; the three above always carry their value on the label line, so it does not
    reach them, and their extraction is unchanged on purpose."""
    m = re.search(rf"^{name}:\s*(.*)$", block, re.M)
    return m.group(1).strip() if m else ""


def _unit_field_lines(block, name):
    """Every entry of a MULTI-LINE unit field (`Footprint:`, `Consumes:`, `Produces:`),
    in the cut's own order — never re-ordered, never deduplicated.

    The defect this closes, as MEASURED against the real cut and the real packet built
    from it (`packets/L-spec-0049-spec-writer-1.md`), not as reasoned about: a real cut
    writes its seam entries on the lines BENEATH a bare label, and `_unit_field` reads
    the label LINE. It did not render `nothing` there — it rendered the FIRST entry and
    silently dropped the rest, for the unit's own Seams line and for every sibling's
    Produces alike, and truncated a multi-line `Footprint:` to its first path,
    narrowing the merge grant below the audited cut. Where a label has no entry lines
    at all it was worse still: `\\s*` crosses the newline, so a bare `Produces:` above
    `Wave: 2` rendered `produces: Wave: 2`. `content/cut-L-charter-0021.md`'s own
    header tells authors to work around this by keeping footprints on one line; the
    reader is the thing that was wrong, so the reader is what is fixed here.

    The rule: the remainder of the label line if it carries one, then every non-empty
    line under it, up to the next field label or the end of the unit block. A label
    with no entry line beneath it and nothing after the colon yields `[]` — which the
    callers still render as `nothing`.

    Deliberately NOT `audit.fields()`'s rule, which continues a label only on
    `-`/`*` bullet lines and so sees no entries at all in a real cut whose seam
    entries are bare lines. The divergence is declared, not hidden: one cut must not
    be read two ways, and reconciling it belongs to whoever owns `src/audit.py`."""
    lines = block.splitlines()
    for i, line in enumerate(lines):
        m = re.match(rf"^{name}:\s*(.*)$", line)
        if not m:
            continue
        out = [m.group(1).strip()] if m.group(1).strip() else []
        for nxt in lines[i + 1:]:
            if FIELD_LABEL.match(nxt) or nxt.startswith("## "):
                break
            if nxt.strip():
                out.append(nxt.strip())
        return out
    return []


def p_plan_auditor(c):
    """The Planner's packet for the plan-auditor (a-14, S6/S21): done-condition,
    requirements, the cut — plus, at stage `plan`, the Plan and the latest cut-audit's
    findings — plus the `doit audit` script pre-pass. No rationale (`strip_rationale`).
    `stage: charter-set` is the one stage with no single charter: it comes from every
    `charter-filed` event on the ledger, diffed against a goal."""
    stage = c.a.stage or die("plan-auditor needs --stage doc|cut|plan|charter-set")
    if stage == "charter-set":
        return _charter_set_packet(c)
    if stage == "doc":
        return _doc_packet(c)
    ch = c.charter_file()
    ch_text = ch.read_text() if ch else None
    cut = resolve(_doc_for_charter(c, c.a.subject, "cut-written", "cut"))
    L = [f"stage: {stage}", f"charter: {c.a.subject}", "",
         "## The charter's done-condition",
         (strip_rationale(_sec(ch_text, "Done for the whole"))
          if ch_text else "(no charter on file — `--charter PATH` or a charter-filed event)"),
         "", "## The charter's requirements",
         (strip_rationale(_sec(ch_text, "Requirements"))
          if ch_text else "(no charter on file)"),
         "", "## The cut", strip_rationale(cut.read_text().rstrip())]
    plan_text = None
    if stage == "plan":
        plan_path = _doc_for_charter(c, c.a.subject, "plan-written", "plan")
        plan = pathlib.Path(plan_path)
        if not plan.is_file():
            die(f"stage plan needs a Plan — a `plan-written` event on {c.a.subject}, "
                f"or {plan_path}")
        plan_text = plan.read_text()
        findings = cut_audit_findings(c)
        L += ["", "## The Plan", strip_rationale(plan_text.rstrip()),
              "", "## The cut-audit's findings (prior round, by design)"]
        L += ([f"- [{f.get('category')}] {f.get('finding')}"
               + (f" — confirms_with: {f['confirms_with']}" if f.get("confirms_with") else "")
               for f in findings] or ["   none"])
    L += ["", audit.prepass_two_file(stage, cut.read_text(), ch_text, plan_text, c.a.repo)]
    return L


def _doc_packet(c):
    """`stage: doc` — the cut and the Plan are ONE document and one audit, not two
    files and two rounds. The document keeps the Plan's name, `plan-<charter>.md`,
    so `Ctx.plan_file()` (a `plan-written` event's path, else the convention) finds
    it unchanged; there is no cut file at this stage and none is looked for.

    The whole document goes to the fable audit, after `strip_rationale`, through the
    four-positional seam `audit.prepass(doc_text, charter_text, repo, events)` —
    `events=None`, matching how the `cut`/`plan` calls already omit it and leave the
    sibling to resolve it via `fold.read_events()`.

    `--stage cut`/`plan`/`charter-set` are untouched: the charters cut under the
    two-document convention are not retrofitted."""
    ch = c.charter_file()
    ch_text = ch.read_text() if ch else None
    doc = c.plan_file() or die(
        f"stage doc needs the one document — a `plan-written` event on {c.a.subject}, "
        f"or {CONTENT / f'plan-{c.a.subject}.md'}")
    doc_text = strip_rationale(doc.read_text().rstrip())
    return ["stage: doc", f"charter: {c.a.subject}", "",
            "## The charter's done-condition",
            (strip_rationale(_sec(ch_text, "Done for the whole"))
             if ch_text else "(no charter on file — `--charter PATH` or a charter-filed event)"),
            "", "## The charter's requirements",
            (strip_rationale(_sec(ch_text, "Requirements")) if ch_text else "(no charter on file)"),
            "", "## The document — the cut and the Plan, collapsed", doc_text,
            "", audit.prepass(doc_text, ch_text, c.a.repo, None)]


def _charter_set_packet(c):
    """§4.6·6's `stage: charter-set` Input, exactly (think.py's `charter_set_packet`,
    reused rather than re-implemented — §3.6: the coverage diff is one implementation):
    the charter set, the goal's done-condition, and the both-directions diff."""
    import think
    goal = think.goal_path(c.a.goal)
    if not goal or not goal.is_file():
        die("stage charter-set needs a goal — `--goal PATH` or a goal-filed event; "
            "§12.2: charter-set has nothing to diff without one")
    charters = []
    for cid, ch in c.charters.items():
        ev = next((e for e in ch["evs"] if e["type"] == "charter-filed"), None)
        if not ev:
            continue
        cov = ev.get("covers") or ""
        charters.append({"id": cid, "path": ev.get("path"), "title": ev.get("title", cid),
                          "covers": [] if cov in ("", "none") else cov.split()})
    diff = think.coverage(goal.read_text(), charters)
    g = goal.read_text()
    L = [f"stage: charter-set", f"goal: {goal.stem}", "",
         "## The goal's done-condition", strip_rationale(_sec(g, "Done for the whole")),
         "", audit.render("charter-set", audit.coverage_rows(diff))]
    for ch in sorted(charters, key=lambda x: x["id"]):
        p = pathlib.Path(ch["path"]) if ch["path"] else None
        body = strip_rationale(p.read_text()) if p and p.is_file() else "(no file on disk)"
        L += [f"## {ch['id']} — {ch['title']}", body]
    return L


def _round_one_slot(c):
    """Round one, built straight from the cut/Plan/charter (a-14, closes S6/S21) —
    the shape `mkslot.py` built by hand for the pilot, and spec-writer.md's Input
    items 1-9: the charter extract, this unit's block, the Plan's Seams + Shared
    decisions verbatim, the envelope, siblings' `Produces:`, ADRs, the write path,
    the 400-line cap. No Rationale (`strip_rationale`)."""
    unit = c.a.unit or die("spec-writer round one needs --unit: the cut's unit heading this spec covers")
    ch = c.charter_file() or die("spec-writer round one needs --charter: the requirement ids and "
                                 "constraints have no other source before the spec exists")
    charter_id = fold.charter_id(c.a.charter)
    cut_path = pathlib.Path(_doc_for_charter(c, charter_id, "cut-written", "cut"))
    if not cut_path.is_file():
        die(f"no cut on file for {charter_id} — a `cut-written` event, or content/cut-{charter_id}.md")
    cut_text = cut_path.read_text()
    blocks = {m.group(1): m.group(0) for m in re.finditer(r"^## (\S+)\n(?:(?!^## ).*\n?)*", cut_text, re.M)}
    ub = blocks.get(unit)
    if ub is None:
        die(f"no unit `{unit}` in the cut for {charter_id} — units on file: "
            f"{', '.join(sorted(blocks)) or 'none'}")
    delivers = [d.strip() for d in _unit_field(ub, "Delivers").split(",") if d.strip()]
    goal, wave = _unit_field(ub, "Goal"), _unit_field(ub, "Wave")
    # Multi-line fields (`_unit_field_lines`): the footprint joins with a space, because
    # `Writes:` is a bare space-separated path list; the seams join with `; `, because
    # an entry is a signature that may itself carry commas.
    footprint = " ".join(_unit_field_lines(ub, "Footprint"))
    consumes = "; ".join(_unit_field_lines(ub, "Consumes")) or "nothing"
    produces = "; ".join(_unit_field_lines(ub, "Produces")) or "nothing"
    ch_text = ch.read_text()
    reqs = [l for l in (section(ch_text, "Requirements") or "").splitlines()
            if any(l.strip().startswith(f"- {r}") for r in delivers)]
    siblings = [f"- `{n}` produces: {'; '.join(_unit_field_lines(b, 'Produces')) or 'nothing'}"
                for n, b in sorted(blocks.items()) if n != unit]
    plan_path = pathlib.Path(_doc_for_charter(c, charter_id, "plan-written", "plan"))
    plan_text = plan_path.read_text() if plan_path.is_file() else None
    adrs = [e["adr"] for e in c.by.get(charter_id, []) if e.get("type") == "adr-filed" and e.get("adr")]
    L = [f"## Plan slot · unit `{unit}` · charter {charter_id} · wave {wave or '?'}", "",
         "1. Charter extract — the requirement ids this slot covers, and the constraints and "
         "product decisions that bind it, verbatim:", "",
         f"Requirements delivered by this unit: {', '.join(delivers) or 'none'}.", ""]
    L += reqs or ["(none matched by id in the charter's Requirements section)"]
    L += ["", "Constraints and product decisions, verbatim:",
          strip_rationale(_sec(ch_text, "Constraints", "(none on file)")), "",
          "2. The plan slot:",
          f"- Unit: `{unit}`", f"- Goal: {goal}",
          f"- Footprint (= the merge grant, `Writes:`): {footprint}",
          f"- Wave: {wave or '?'}. Seams: Consumes {consumes}; Produces {produces}.",
          "- The Plan's Seams and Shared decisions this spec must honour, verbatim:", ""]
    if plan_text:
        L += ["### Seams", strip_rationale(_sec(plan_text, "Seams")), "",
              "### Shared decisions", strip_rationale(_sec(plan_text, "Shared decisions"))]
    else:
        L += ["(no Plan on file yet — Seams/Shared decisions unavailable)"]
    L += ["", f"3. Read access to the repository: your cwd is "
              f"`{c.a.repo or ROOT / 'repos' / c.project()}`. Read-only.",
          "", f"4. Builder capability envelope: {c.a.envelope or 'none on file'}.",
          "", f"5. Cost-path inventory: {c.a.cost_path or 'none on file'}.",
          "", "6. Sibling units' `Produces:`:"] + (siblings or ["   none"])
    L += ["", f"7. Acquisition ADRs for this footprint: {', '.join(adrs) or 'none'}.",
          "", "8. Probe residue: none on file.",
          "", f"9. Write the spec to: `{CONTENT / (c.a.subject + '.md')}`. "
              "That path is the ONLY file you may write.",
          "", "10. The template: the eleven slots of your contract (spec-writer.md). Every "
              "acceptance criterion is `AC<n> [type]:` with type from `ui` · `backend` · "
              "`observed-data` · `financial`, and carries a `review_path` (log in as / go to / "
              f"do / worked if / failed if). Name the charter in the spec header as "
              f"`charter: {charter_id}` and cite `Writes:` exactly as the footprint above — a "
              "bare space-separated list of paths on one line, no prose in the list. "
              "**The spec is at most 400 lines** — say less, cite more.", ""]
    return L


# ─────────────────────────────── the seven Input lists ──────────────────────────────

def p_spec_auditor(c):
    spec, v = c.spec_file(), write_verify_script(c)
    body = spec.read_text()
    hits = [f"  {n}: {l.strip()}" for n, l in enumerate(body.splitlines(), 1) if PLACEHOLDER.search(l)]
    cited = set((c.last("spec-written") or {}).get("requirement_ids") or [])
    ch = c.charter_file()
    ids = set(re.findall(r"^\s*[-*|]?\s*\**([A-Z]{1,4}-?\d+)\b", ch.read_text(), re.M)) if ch else None
    return [
        f"1. The spec under audit: `{spec}`. Read it from disk.",
        "2. Your cwd is the repository the spec targets; you have read access to it.",
        "3. The mechanical pre-pass. This is ground truth; do not re-derive it.",
        "```",
        f"placeholder / scope-reduction greps: {len(hits)}",
        *(hits or ["  none"]),
        f"[NEEDS CLARIFICATION] count: {body.count('[NEEDS CLARIFICATION')}",
        f"Verification section holds a runnable command: {'yes' if v else 'NO — empty verify'}",
        f"requirement ids the spec cites: {', '.join(sorted(cited)) or 'none'}",
        (f"charter ids the spec does not cite: {', '.join(sorted(ids - cited)) or 'none'}"
         if ids is not None else "charter id list: unavailable — no charter on file"),
        (f"ids cited that the charter does not list: {', '.join(sorted(cited - ids)) or 'none'}"
         if ids is not None else ""),
        "```",
        "4. Calibration examples: none on file.",
    ]


def p_spec_writer(c):
    """Rework. Round one is the Planner's: it holds the plan slot, which is not in the
    ledger. Rework is round one's packet plus the audit's list.

    Round one itself has two sources now (a-14): `--unit NAME` (bare `--slot`, or
    `--slot` with `--unit`) builds it straight from the cut/Plan/charter
    (`_round_one_slot`, reproducing `mkslot.py`); `--slot FILE`, or the
    `content/slot-<spec>.md` convention, still reads a hand-built one."""
    if c.a.unit or c.a.slot is True:
        return _round_one_slot(c)
    prev = sorted(PACKETS.glob(f"{c.a.subject}-spec-writer-*.md"))
    # Round one's packet is the Planner's slot file, which by convention lives at
    # content/slot-<spec>.md (planner.md ⑥, executor.md's brief row) — not under
    # packets/. The first rework of every pilot spec needed `--slot` typed by hand
    # for that reason (S6); the convention is read here so it does not.
    slot = (c.a.slot if isinstance(c.a.slot, str) else None) or (
        str(CONTENT / f"slot-{c.a.subject}.md") if (CONTENT / f"slot-{c.a.subject}.md").is_file() else None)
    if prev:
        base = prev[-1].read_text().splitlines()
    elif slot:
        base = pathlib.Path(slot).read_text().splitlines()
    else:
        die("no prior spec-writer packet, no --slot, and no content/slot-<spec>.md: round one "
            "carries the plan slot, which only the Planner holds")
    findings = [e for e in c.all_of("audit-finding") if e.get("list") != "rejected"]
    # L-spec-0195/finding 2: a `spec-shape-failed` finding list is a SECOND, equally
    # sufficient source for the Fix list — either source alone is enough to build a
    # rework packet, and neither empties the other's contribution when both are
    # present.
    shape_findings = [f for e in c.all_of("spec-shape-failed") for f in (e.get("findings") or [])]
    if not findings and not shape_findings:
        # Round one from a slot the Executor wrote is legitimate — D7's brief-authored
        # spec has no plan slot and no audit yet. Round one from a PRIOR PACKET is a
        # rework with nothing to rework, which is the mistake this refuses.
        if prev or not slot:
            die("no audit findings on this subject — a rework packet with no fix list is round one again")
        return base
    lines = [f"{i}. [{f.get('field', '?')} · {f.get('category', '?')}] {f.get('finding', '')}"
             f"{'  → ' + f['suggested_fix'] if f.get('suggested_fix') else ''}"
             for i, f in enumerate(findings, 1)]
    lines += [f"{len(findings) + i}. [spec-shape] {f}" for i, f in enumerate(shape_findings, 1)]
    return base + ["", "## Fix list — apply each, same spec id, same path", ""] + lines


HINT_DIFF_MARKER = re.compile(r"^(\+\+\+|---|@@)")


def hint_block(c):
    """L-adr-0039's builder re-dispatch: a one-line correction, or a merge collision,
    is a re-dispatch carrying a SENTENCE and the paths it concerns — never a diff, and
    never an Executor's hand on the code. One shape for both.

    A hint carrying a fenced code block, or any line starting `+++`/`---`/`@@`, is
    refused outright: a diff is an implementation plan, and the builder with the code
    in front of it is the one that writes that. The scan runs over the HINT ARGUMENT
    ALONE — a charter's Constraints extract elsewhere in the same packet may
    legitimately carry a `---` rule, and tripping on that would refuse honest builds."""
    hint = getattr(c.a, "hint", None)
    if not hint:
        return []
    if "```" in hint or any(HINT_DIFF_MARKER.match(l) for l in hint.splitlines()):
        die("--hint carries a diff, not a sentence (a fenced block, or a line starting "
            "`+++`/`---`/`@@`). Say what is wrong in one sentence; the builder writes the "
            "change (L-adr-0039).")
    return ["This is a re-dispatch on the same worktree and branch. The correction, verbatim:",
            hint,
            f"   The paths it concerns: {', '.join(c.footprint()) or 'none stated'}."]


def p_builder(c):
    hint = hint_block(c)          # refused before anything else is assembled
    spec, v = c.spec_file(), write_verify_script(c)
    repo = c.a.repo or str(ROOT / "repos" / c.project())
    # R7/Target 1: ONE call, `verify_base_sha(c)` — the exact same value
    # `write_verify_script` above already pinned into `$BASE` — never a second,
    # independently computed worktree-HEAD fallback. On a rework round the
    # worktree's live HEAD is the PRIOR round's own commit, not the true base;
    # that mismatch between this line and the verify script's own `$BASE` is the
    # goalpost bug this spec closes (AC2).
    base_sha = verify_base_sha(c)
    ch = c.charter_file()
    extract = (section(ch.read_text(), "Constraints") or "").splitlines() if ch else []
    rej, fix = c.standing()
    adrs = [e["adr"] for e in c.all_of("adr-filed") if e.get("adr")]
    done = done_condition(c, c.charter_id())
    L = [f"1. The spec: `{spec}`. Read it from disk once. You are not given its text here.",
         "2. The charter extract — binding constraints and product decisions, verbatim:",
         *(extract or ["   none"]),
         f"3. `base_sha` = {base_sha}. Read it back from the worktree and confirm it before the first edit.",
         # R7/Target 1, AC1/AC3: the machine line `dispatch.pinned_base` reads back
         # off this same file — unconditional, byte-equal to `base_sha` above.
         f"PINNED_BASE_SHA: {base_sha}",
         *(["PINNED_BASE_SHA_EXPLICIT: true"] if c.a.base_sha else []),
         f"4. Verify command: `bash {v}`" if v else
         "4. Verify command: NONE — the spec's Verification block is empty. Declare `spec-ambiguity` and stop.",
         f"   Done-condition: {done}.",
         f"5. Sibling units' `Produces:` — {c.a.produces or 'none'}.",
         "6. Parked findings on your footprint: none on file.",
         "7. Live cross-charter conflict list — id · what it changes · state:",
         *[f"   {x}" for x in conflicts(c)],
         f"8. ADRs binding your footprint: {', '.join(adrs) or 'none'}. "
         f"Conventions file: {c.a.conventions or 'none'}.",
         f"   Writes (the merge grant, = the spec's footprint): {', '.join(c.footprint()) or 'none stated'}."]
    if rej or fix:
        L += ["", "This is a rework on the same worktree and branch. Standing and blocking:",
              *[f"   rejected — {x}" for x in rej], *[f"   must-fix — {x}" for x in fix]]
    # Two independent sections: a standing grader rejection and a hint may both be on
    # one packet, and neither is dropped when the other is present.
    if hint:
        L += ["", *hint]
    for d in c.all_of("decision"):
        L.append(f"   decided, and binding: {d.get('why', '')}")
    # L-spec-0273: three known local-box footguns, informational only — the
    # fixes are L-charter-0028's, not this builder's to make.
    L += ["## STOP",
          "- DSN missing in a worktree can silently skip a live_db criterion instead of failing it (L-charter-0028).",
          "- the stale test baseline cloned per builder fills /tmp across many builds (L-charter-0028).",
          "- a gate-clean merge sometimes leaves master red (L-charter-0028)."]
    L += ["", f"Your cwd is your worktree, on branch `{c.a.subject.lower()}`. "
              f"The repository is `{repo}`. One spec, one commit, on that branch."]
    return L


def _drop_owed_blocks(lines, owed):
    """L-spec-0435 (R10(a)'s enforcement half); body moved to `routing.drop_blocks`
    (L-spec-8034, R12 size offset) and re-imported here under the old name."""
    return routing.drop_blocks(lines, owed)


def p_grader(c):
    spec, card, v = c.spec_file(), c.card_file(), write_verify_script(c)
    body = spec.read_text()
    # L-spec-0435/R10(a): a criterion typed `owed-ac` at spec-write time is
    # unobservable outside a real deploy — the grader can never spend on it.
    # L-spec-8034/R12.h: a routed criterion (reviewer OR owed) is not the
    # grader's either — dropped whole, the same as an owed one.
    owed = {e["criterion"] for e in c.all_of("owed-ac")} | set(fold.routed_criteria(c.evs, c.a.subject))
    crit = _drop_owed_blocks(criteria(body), owed)
    rows = [l for l in card.read_text().splitlines() if AC_ROW.match(l)]
    # Every row, from the card object the wrapper writes beside the rendered card —
    # the fifteen-line render drops rows past its room, and a grader that cannot see
    # a claim cannot test it. Falls back to the render for a pre-sidecar card.
    side = card.with_suffix(".json")
    if side.is_file():
        import json
        rows = [f"{a['id']} [{a['criterion_type']}] {a['disposition']} · {a['evidence_type']} · "
                f"check: {a['check']} · evidence: {a['evidence']}" for a in json.loads(side.read_text())["acs"]]
    rows = [r for r in rows if r.split(None, 1)[0] not in owed]
    vline = next((l for l in card.read_text().splitlines() if l.startswith("verify ")), "verify: not reported")
    ver = subprocess.run(["shasum", "-a", "256", str(v)], capture_output=True, text=True).stdout.split()[0][:16] \
        if v else "no script"
    done = done_condition(c, c.charter_id())
    # R8/L-spec-0437: item 5 alone is gated on whether a view will actually exist
    # for this grader dispatch — `models.resolve` is the same, backend-blind lookup
    # `dispatch.main` itself uses, since `doit packet grader` runs before a backend
    # is chosen and carries no argument naming one (Assumptions §4). Every other
    # resolved backend, or no `models.toml` at all, keeps base_sha's worktree line
    # (AC13) — only `backend == "seat"` names the view's own relative layout,
    # never a host-absolute path (AC10).
    seat_view = models.resolve("grader")["backend"] == "seat"
    item5 = (f"5. Checker: `verify.sh` · version {ver} · coverage note "
             f"\"the spec's Verification block\" · re-run it from this view's own root — "
             f"cwd `tree/`; no other path is reachable · environment `view/grading.env` "
             f"(sourced by verify.sh's own first line).") if seat_view else (
             f"5. Checker: `verify-{c.a.subject}` · version {ver} · coverage note "
             f"\"the spec's Verification block\" · re-run it with cwd `{c.worktree()}`.")
    out = [
        "1. The acceptance criteria, verbatim from the spec, typed, each with its evidence obligation:",
        *crit,
        "2. The output card's per-criterion rows. These are claims to test, not facts:",
        *([f"   {r}" for r in rows] or ["   none"]),
        "3. Evidence-type validator: not installed; no row is pre-failed on its account.",
        f"4. The verify command the spec authored, with the reported exit code and result: {vline}",
        item5,
        f"6. The done-condition: {done}.",
        f"7. Standing verify-waivers applied to this checker: {waiver_line(c)}.",
    ]
    # L-spec-0668, 2026-10-05: a builder's own evidence/check prose sometimes names
    # its own spawn id verbatim ("Re-run in worktree by L-builder-0973: ...") — a
    # row the output card legitimately carries (AC1/AC2 "claims to test"), but one
    # that trips builder_cues's "the builder's spawn id" Blindness line the moment
    # it is quoted into item 2 above. Redact the token rather than refuse the whole
    # packet before any spend — the same shape of fix as _minus_charter (strip a
    # false cue) applied on the producing side instead: the text stays, the id
    # does not.
    return [_redact_builder_spawn(l) for l in out]


_BUILDER_SPAWN_RE = re.compile(r"\bL-builder-\d+\b")


def _redact_builder_spawn(text):
    return _BUILDER_SPAWN_RE.sub("[this build]", text)


def p_reviewer(c):
    spec = c.spec_file()
    # L-spec-0195/finding 4: the reviewer's own call to write_verify_script — new
    # work, not an existing behaviour extended. Its checker file is `verify-
    # <subject>-reviewer.sh`, waived exactly like the grader's own; never cited by
    # path here (the grader's item 5 already names that convention).
    write_verify_script(c)
    body = spec.read_text()
    crit = criteria(body)
    bd = c.last("build-done") or {}
    where = c.a.url or f"the worktree at ready_sha {bd.get('ready_sha', '?')}: `{c.worktree()}`"
    done = done_condition(c, c.charter_id())
    # L-spec-0140: `build-done.verify_exit` is a ONE-TIME, never-refreshed fact — it
    # went stale the moment a grader re-ran the same checker and got a different
    # result (L-spec-0129's live case: verify_exit recorded 1, a later grader
    # verdict measured exit 0 twice). The ledger's most recent `verdict` event
    # (`Context.last`, same accessor and same reversed-iteration "most recent"
    # semantics already used for build-done) is re-derived every packet build, so
    # it cannot go stale the way a frozen field can. Draw ONLY its own typed
    # fields (confirmed/card_ok/matches_intent/cannot_assess) — never a
    # `rejected-criterion.why` or a `card-quality.line`, both grader reasoning the
    # reviewer must never see (R3). Neither branch below calls itself "ground
    # truth": a live verdict can itself be re-graded, and an unmeasured state is
    # not truth of any kind.
    verdict = c.last("verdict")
    if verdict is None:
        item4 = ("4. Structural pre-pass: no live verify result is available for "
                  "this packet — no grader verdict has been recorded yet.")
    else:
        item4 = (
            "4. Structural pre-pass, from the ledger's most recent live grader "
            f"verdict: confirmed={verdict.get('confirmed', 'unknown')} · "
            f"card_ok={verdict.get('card_ok', 'unknown')} · "
            f"matches_intent={verdict.get('matches_intent', 'unknown')} · "
            f"cannot_assess={verdict.get('cannot_assess', 'unknown')}."
        )
    # L-spec-8034/R12.i: criteria routed to=reviewer are yours ALONE — a
    # grader never has the capability, so its packet drops them whole
    # (p_grader/_drop_owed_blocks) and you report one row for each, by id and
    # text, and may `cleared` them in any round (agents/reviewer.md).
    to_reviewer = sorted(cid for cid, to in fold.routed_criteria(c.evs, c.a.subject).items() if to == "reviewer")
    groups = routing.group_blocks(crit)
    routed_lines = [f"   {cid}: " + " / ".join(groups.get(cid, [])) for cid in to_reviewer] or ["   none"]
    return [
        f"1. The deployed thing, on its execution host: {where}. Never staging.",
        f"2. The done-condition: {done}.",
        "3. Every criterion the spec enumerates, verbatim, with its `review_path` "
        "(log in as / go to / do / worked if / failed if):",
        *crit,
        item4,
        "5. Metrics you may cite: none supplied.",
        f"6. The review account for this app: {review_account(c)}. It may never delete and never move money.",
        f"7. depth: {c.a.depth} · round: {c.a.round}.",
        f"8. Standing verify-waivers applied to this checker: {waiver_line(c)}.",
        f"9. Paths this branch touches outside the spec's Writes grant (ADR-0028-6): {out_of_grant_line(c)}.",
        "10. Criteria routed to you alone — a grader can never assess these (yours to clear in any round):",
        *routed_lines,
    ]


def p_charter_reviewer(c):
    ch = c.charter_file() or die("no charter file — `--charter PATH` or a charter-filed event")
    mine = [s for s in c.specs.values() if s["charter"] == c.a.subject]
    cards, specs = [], []
    for s in sorted(mine, key=lambda s: s["id"]):
        bd = next((e for e in reversed(s["evs"]) if e["type"] == "build-done"), None)
        sw = next((e for e in reversed(s["evs"]) if e["type"] == "spec-written"), None)
        # A build-done with no card is a gap the charter-reviewer must see, not a
        # row it never gets (AP24/AP25: absent must not read as fine).
        bd and cards.append(
            f"   {s['id']}: {bd.get('card') or 'NO CARD RECORDED — build-done carries none'}")
        sw and specs.append(f"   {s['id']}: {sw['path']}")
    fp = c.last("sweep-fixpoint")
    return [
        f"1. The charter, all five sections: `{ch}`. Read it from disk.",
        "2. Every output card in the charter — read each, including its not-built half:",
        *(cards or ["   none"]),
        "3. The spec set:",
        *(specs or ["   none"]),
        f"4. The sweep's fixpoint result: reached {fp['ts'] if fp else 'NOT REACHED'}.",
        f"5. The review account for this app: {review_account(c)}. Read-only or write-scoped; "
        "never delete, never money.",
    ]


_AC_ANY = re.compile(r"^\s*\**AC(\d+)\s*\[")  # group added (L-spec-0435): same
# anchor shape `criterion_block` already matched — `_drop_owed_blocks` below
# reuses it to also RECOVER the id, rather than forking a near-identical regex.


def parse_sweep_manifest(path):
    """The batch manifest an `owed-sweeper` spawn reads (L-charter-0038 R1 /
    L-spec-0387 Assumptions §8 — pinned here, since the manifest's WRITER,
    `owed-sweep-driver`, is a sibling not yet specced): line 1 `# <sweep-id>`;
    blank; `project: <name>`; `cwd: <absolute path>`; blank; `## Rows`; a
    fenced ```json array of 1-8 row objects, each `{"spec", "criterion",
    "declared_src", "line", "spec_path"}`. Every row's `spec_path`, resolved,
    must equal `dispatch.spec_path(row["spec"])` exactly (fix 6) — a lazy,
    function-scoped import, matching this file's existing `import
    fold`/`import validate` convention. A row that fails that check dies
    naming the offending spec. Returns `{"project", "cwd", "rows"}`, rows
    exactly as written (never mutated)."""
    p = pathlib.Path(path)
    if not p.is_file():
        die(f"no sweep manifest at {path}")
    raw = p.read_text()
    m = re.search(r"^project:\s*(.+)$", raw, re.M)
    if not m:
        die(f"{path}: no 'project:' line")
    project = m.group(1).strip()
    m = re.search(r"^cwd:\s*(.+)$", raw, re.M)
    if not m:
        die(f"{path}: no 'cwd:' line")
    cwd = m.group(1).strip()
    sec = section(raw, "Rows")
    if not sec:
        die(f"{path}: no '## Rows' section")
    m = re.search(r"```json\s*\n(.*?)\n```", sec, re.S)
    if not m:
        die(f"{path}: no fenced ```json block under '## Rows'")
    import json
    try:
        rows = json.loads(m.group(1))
    except ValueError as e:
        die(f"{path}: malformed JSON in the Rows block ({e})")
    if not isinstance(rows, list) or not (1 <= len(rows) <= 8):
        die(f"{path}: the Rows block must be a JSON array of 1-8 objects")
    import dispatch
    for row in rows:
        want = dispatch.spec_path(row["spec"])
        got = pathlib.Path(row["spec_path"]).resolve()
        if got != want:
            die(f"{path}: row {row['spec']}'s spec_path {row['spec_path']!r} does not resolve "
                f"to dispatch.spec_path({row['spec']!r}) == {want}")
    return {"project": project, "cwd": cwd, "rows": rows}


def criterion_block(body, crit_id):
    """One acceptance criterion's FULL block, verbatim — from its `^\\s*{crit_id}
    [` line to (not including) the next `^\\s*AC\\d+ [` line, or the Acceptance
    section's end — so a continuation-line `review_path:` rides along (fix 6,
    the sweeper's own AC1 precedent). `None` when `crit_id` is absent."""
    sec = section(body, "Acceptance")
    lines = sec.splitlines() if sec else body.splitlines()
    # Specs write a criterion as `**AC5 [`, `- **AC5 [`, `  - **AC5 [`, `| AC5 [` (a table
    # row) or `- Acceptance: AC5 [`. The bare `**AC5 [` shape alone orphaned every due
    # check of 53 (spec, criterion) pairs from the owed sweep (2026-10-04).
    lead = r"^\s*(?:[-*|]\s*)*(?:Acceptance:\s*)?\**"
    start_pat = re.compile(lead + re.escape(crit_id) + r"\**\s*\[")
    any_pat = re.compile(lead + r"AC\d+[a-z]?\**\s*\[")
    start = next((i for i in range(len(lines)) if start_pat.match(lines[i])), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if any_pat.match(lines[i])), len(lines))
    return "\n".join(lines[start:end]).strip()


def p_owed_sweeper(c):
    """L-charter-0038 R1 / L-spec-0387 — the read-only sweep packet: blind to any
    card, grade or audit reasoning (SD5), so this carries only the batch's
    project/cwd and, per row, the criterion's OWN full block, verbatim, from
    that row's own spec body — never one line, so a continuation-line
    `review_path:` rides in. L-spec-9004 OC1: two more lines follow the due
    checks — this batch's own read-only DB credential path (or its absence)
    for a `live_db` observation, and the read-only portal review account."""
    manifest = parse_sweep_manifest(CONTENT / f"{c.a.subject}.md")
    L = [f"1. Batch `{c.a.subject}` — project `{manifest['project']}`, cwd `{manifest['cwd']}`.",
         "2. Every due check in this batch, each the criterion's own full block, verbatim, "
         "from its own spec — nothing else:"]
    for row in manifest["rows"]:
        sp = pathlib.Path(row["spec_path"])
        if not sp.is_file():
            die(f"no spec file at {row['spec_path']} for {row['spec']}")
        block = criterion_block(sp.read_text(), row["criterion"])
        if block is None:
            die(f"{row['spec']}'s {row['criterion']} is not in {row['spec_path']}")
        L += [f"   spec {row['spec']} — criterion {row['criterion']} "
              f"(declared: {row['declared_src']} @ {row['line']}):"]
        L += [f"     {ln}" for ln in block.splitlines()]
    env_path = pathlib.Path(manifest["cwd"]) / ".env"
    if env_path.is_file():
        L.append(f"3. This batch's own read-only DB credential, for a `live_db` observation: "
                 f"`{env_path}` holds `SUPABASE_DB_URL` — `set -a; . {env_path}; set +a` before "
                 "reading it.")
    else:
        L.append("3. This batch's own read-only DB credential, for a `live_db` observation: "
                  "not available for this batch (capability=ro-dsn).")
    L.append(f"4. The review account for this app: {review_account(c)}. It may never delete and "
             "never move money.")
    return L


BUILD = {"spec-auditor": p_spec_auditor, "spec-writer": p_spec_writer, "builder": p_builder,
         "grader": p_grader, "reviewer": p_reviewer, "charter-reviewer": p_charter_reviewer,
         "plan-auditor": p_plan_auditor, "owed-sweeper": p_owed_sweeper}


# ──────────────────────────── Blindness, as a check ────────────────────────────

def _minus_charter(c, pairs):
    """A sibling spec line that is also a line of this packet's own charter is the charter's text, which the
    packet legitimately carries; flagging it made every packet on a charter refuse once any spec quoted the
    charter verbatim (L-spec-0164 on L-charter-0024's Footprint bound line, 2026-09-27)."""
    ch = c.charter_file()
    if not ch or not pathlib.Path(ch).is_file():
        return pairs
    text = pathlib.Path(ch).read_text()
    return [(label, l) for label, l in pairs if l.strip() not in text]


def sibling_bodies(c):
    """Every other spec's substantial lines. A packet that pasted one is carrying a
    sibling slot's internals."""
    out = []
    for sid, s in c.specs.items():
        if sid == c.a.subject:
            continue
        # A killed sibling is not a slot anyone builds; a duplicate-carried twin killed in favour
        # of this spec must not refuse this spec's packet (L-spec-0657 vs killed 0658, 2026-10-05).
        if any(x["type"] == "spec-killed" for x in s["evs"]):
            continue
        # A `spec-written` need not carry a path: the three from the pre-wrapper era
        # do not, and one of them crashed the first real rework dispatch. A sibling
        # with no file on disk has no body to leak — it is skipped, never guessed at.
        e = next((x for x in reversed(s["evs"])
                  if x["type"] == "spec-written" and pathlib.Path(x.get("path") or "/").is_file()), None)
        if e:
            out += [(f"a sibling spec's body ({sid})", l) for l in long_lines(pathlib.Path(e["path"]))[:20]
                    if not _PATH_ONLY_RE.match(l)]
    return out


# A line that is only a repo path (optionally bulleted/backticked) names a file, not a sibling's
# internals; two specs touching the same file must both be able to list it (L-spec-0664 vs 0290, 2026-10-05).
_PATH_ONLY_RE = re.compile(r"^\s*[-*]?\s*`?[\w.-]*/[\w./-]+`?\s*$")


def other_cards(c):
    """Every other spec's card. Same shape as sibling_bodies: a `build-done` need not
    carry a `card` — the pre-wrapper era wrote four that do not, and one of them
    crashed `doit packet builder`, the one dispatch a written spec cannot proceed
    without. A card with no file on disk has nothing to leak."""
    out = []
    for sid, s in c.specs.items():
        if sid == c.a.subject:
            continue
        e = next((x for x in reversed(s["evs"])
                  if x["type"] == "build-done" and pathlib.Path(x.get("card") or "/").is_file()), None)
        if e:
            out += [(f"another builder's card ({sid})", l) for l in long_lines(pathlib.Path(e["card"]), 30)[:20]]
    return out


def builder_cues(c):
    """Who built it, on what branch, when, and what it argued — §4.6·4's strip list."""
    bd, out = c.last("build-done"), []
    if bd:
        # A branch named for the subject is not a cue. The grader's packet names the
        # subject by necessity, and the Executor's cut recipe makes the worktree
        # directory the branch name — so stripping it refused every spec built on
        # that recipe, the grader packet being the one that must carry the worktree
        # path (L-spec-0005, 2026-09-08). Strip only what the grader cannot already
        # derive from what it legitimately holds; anything short of provably
        # derivable stays stripped.
        branch = bd.get("branch")
        if branch and branch.lower() != c.a.subject.lower():
            out.append(("the builder's branch", branch))
        # A base_sha the SPEC ITSELF cites (frontmatter base_sha, `git diff <base_sha>` in its
        # Verification) is spec text the grader legitimately holds, not a cue about who built
        # it; stripping it refused every grader packet for such specs (L-spec-0420/0421,
        # 2026-09-27).
        try:
            _spec_text = c.spec_file().read_text()
        except Exception:
            _spec_text = ""
        _base = bd.get("base_sha")
        if not (_base and _base in _spec_text):
            out.append(("base_sha", _base))
        out += [("ready_sha", bd.get("ready_sha")), ("a build timestamp", bd.get("ts"))]
        out += [("the builder's spawn id", e.get("spawn")) for e in c.all_of("spawn-done")
                if e.get("actor") == "builder"]
        # is_file, not exists: the "/" fallback for a build-done with no card is a
        # directory and passes exists() (AP24 — a field the wrapper always writes,
        # absent on an event written before it did).
        card = pathlib.Path(bd.get("card") or "/")
        if card.is_file():
            head = card.read_text().splitlines()[0]
            out.append(("the card's header line", head))
            # who built it, straight off the header — the cue the grader must not see
            # even when no spawn-done for the builder is on this subject's ledger yet.
            out += [("the builder's spawn id", m) for m in re.findall(r"built by (\S+)", head)]
            out += [("the card's not-built prose", l) for l in card.read_text().splitlines()
                    if l.startswith("not built:")]
    out += [("a declared deviation", e.get("what")) for e in c.all_of("build-deviation")]
    return out


def grader_reasons(c):
    return ([("the grader's reason", e.get("why")) for e in c.all_of("rejected-criterion")]
            + [("a prior audit finding", e.get("finding")) for e in c.all_of("audit-finding")])


def strip(c, role):
    """(label, forbidden string) pairs — this subject's real Blindness list."""
    if role == "spec-auditor":
        ch = c.charter_file()
        return ([("the charter's text", l) for l in long_lines(ch)] if ch else []) + \
               [("the spec's Assumptions reasoning", l)
                for l in (section(c.spec_file().read_text(), "Assumptions") or "").splitlines()
                if len(l.strip()) > 50] + \
               [("a prior audit finding", e.get("finding")) for e in c.all_of("audit-finding")]
    if role == "spec-writer":
        return _minus_charter(c, sibling_bodies(c))
    if role == "builder":
        return _minus_charter(c, sibling_bodies(c)) + other_cards(c)
    if role == "grader":
        return builder_cues(c)
    if role == "reviewer":
        bd = c.last("build-done")
        # Not the shas: item 1 hands the reviewer the worktree AT ready_sha. What it
        # must not see is the card's prose and the grader's reasons (§4.6·5).
        card = [("the card's prose", l) for l in long_lines(pathlib.Path(bd.get("card") or "/"), 30)] if bd else []
        return card + grader_reasons(c) + [("a declared deviation", e.get("what"))
                                           for e in c.all_of("build-deviation")]
    if role == "charter-reviewer":
        pw = c.last("plan-written")
        return [("the Plan's rationale", l) for l in long_lines(pathlib.Path(pw.get("path") or "/"))] if pw else []
    if role == "plan-auditor":
        # The whole cut/Plan is legitimately IN the packet at their stages — unlike
        # charter-reviewer above, this is scoped to just the Rationale/Why sections
        # (`strip_rationale` already removes them from the text; this is the second,
        # narrower line of defence, not a re-check of the whole file).
        if c.a.stage == "charter-set":
            return []
        out = []
        # At stage `doc` there is no cut file and none is looked for: `cut_file()`
        # resolves or dies, so asking it here would refuse a one-document packet for
        # want of the very file the stage exists to do without.
        if c.a.stage == "doc":
            return [("the document's rationale", l)
                    for f in [c.plan_file()] if f and pathlib.Path(f).is_file()
                    for name in ("Rationale", "Why")
                    for l in (section(pathlib.Path(f).read_text(), name) or "").splitlines()
                    if len(l.strip()) > 50]
        docs = [(c.cut_file, "the cut's rationale")]
        if c.a.stage == "plan":
            docs.append((lambda: pathlib.Path(_doc_for_charter(c, c.a.subject, "plan-written", "plan")), "the Plan's rationale"))
        for getter, label in docs:
            f = getter()
            if not f or not pathlib.Path(f).is_file():
                continue
            body = pathlib.Path(f).read_text()
            for name in ("Rationale", "Why"):
                sec = section(body, name)
                out += [(label, l) for l in sec.splitlines() if len(l.strip()) > 50] if sec else []
        return out
    if role == "owed-sweeper":
        # SD5 ("blind to card/grade"), mechanically enforced (fix 4): every row's
        # OWN spec's card prose and grader/audit reasoning — read off that row's
        # OWN event stream (`s["evs"]`), never `c.evs` (empty per the Ctx exemption).
        out = []
        manifest = parse_sweep_manifest(CONTENT / f"{c.a.subject}.md")
        for row in manifest["rows"]:
            s = c.specs.get(row["spec"])
            if s is None:
                continue
            evs = s["evs"]
            bd = next((e for e in reversed(evs) if e["type"] == "build-done"), None)
            if bd:
                card = pathlib.Path(bd.get("card") or "/")
                if card.is_file():
                    out += [(f"a row spec's card ({row['spec']})", l) for l in long_lines(card, 30)]
            out += [(f"a row spec's grader reason ({row['spec']})", e.get("why"))
                    for e in evs if e["type"] == "rejected-criterion"]
            out += [(f"a row spec's audit finding ({row['spec']})", e.get("finding"))
                    for e in evs if e["type"] == "audit-finding"]
        return out
    return []


def main(argv=None):
    ap = argparse.ArgumentParser(prog="doit packet", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("role", choices=sorted(BUILD))
    ap.add_argument("subject")
    ap.add_argument("--charter", help="path to the charter file; else the charter-filed event")
    ap.add_argument("--repo"), ap.add_argument("--worktree"), ap.add_argument("--project")
    ap.add_argument("--slot", nargs="?", const=True,
                    help="spec-writer round one: a FILE holding a hand-built plan slot, or bare "
                         "(with --unit) to build it from the cut/Plan/charter (a-14)")
    ap.add_argument("--unit", help="spec-writer round one, built from scratch: the cut's unit heading")
    ap.add_argument("--envelope", help="spec-writer round one: the builder capability envelope")
    ap.add_argument("--cost-path", help="spec-writer round one: the cost-path inventory")
    ap.add_argument("--stage", choices=["cut", "plan", "charter-set", "doc"],
                    help="plan-auditor's stage; `doc` is the one-document stage")
    ap.add_argument("--hint", help="builder re-dispatch: one sentence saying what is wrong "
                                   "(a merge collision, a one-line correction) — never a diff")
    ap.add_argument("--goal", help="plan-auditor stage charter-set: the goal file; else the goal-filed event")
    ap.add_argument("--produces", help="sibling units' Produces: signatures")
    ap.add_argument("--conventions"), ap.add_argument("--base-sha"), ap.add_argument("--url")
    ap.add_argument("--depth", default="gates-only", choices=["gates-only", "full"])
    ap.add_argument("--round", default="1")
    a = ap.parse_args(argv)

    c = Ctx(a)
    text = "\n".join(x for x in BUILD[a.role](c) if x) + "\n"
    hit = [(w, s) for w, s in strip(c, a.role) if s and str(s) in text]
    if hit:
        die("REFUSED — the packet carries what {}'s Blindness strips: {}".format(
            a.role, "; ".join(f"{w} ({str(s)[:60]!r})" for w, s in hit[:3])))
    # L-spec-0273/R5: the append-only checklist, run against the assembled
    # packet text itself (`packet:<role>`) — `applies_to` never equals
    # "packet:builder" for any role but "builder", so a grader/spec-auditor/etc
    # packet is untouched either way, even though the checklist carries PL-009.
    import packet_lint  # noqa: E402 — local: packet_lint imports this module back
    block_texts, warn_texts = packet_lint.split(
        packet_lint.run(text, packet_lint.CHECKLIST, f"packet:{a.role}"))
    if block_texts:
        die("REFUSED — packet-lint blocked: " + "; ".join(block_texts))
    if warn_texts:
        text += "## Packet-lint notices (non-blocking)\n" + "\n".join(f"- {w}" for w in warn_texts) + "\n"
    PACKETS.mkdir(parents=True, exist_ok=True)
    n = 1 + len(list(PACKETS.glob(f"{a.subject}-{a.role}-*.md")))
    p = PACKETS / f"{a.subject}-{a.role}-{n}.md"
    p.write_text(text)
    assert p.read_text() == text, f"write to {p} did not land"
    return p


# ───────────────────── the producers a pane calls as plain Python ─────────────────────
# The Planner dispatches a spec's audit and its rewrite itself, and a correction or a
# merge collision is a builder re-dispatch — so those packets must be buildable from
# inside a Python pane, not only off a command line. Each is a thin wrapper over
# `main(argv)`: one code path, one Blindness check, one file-naming sequence for the CLI
# route and the import route alike, exactly as `if __name__ == "__main__"` already
# delegates. None of them reads who is calling: no actor, no role, no `DOIT_LEDGER_FILE`
# enters a packet body, so the same call from any pane yields the same bytes.

def packet_plan_auditor(charter):
    """The single-stage plan-auditor packet for a charter whose cut and Plan are one
    document. Returns the packet path."""
    return main(["plan-auditor", charter, "--stage", "doc"])


def packet_spec_auditor(spec, base_sha=None):
    """The packet for the audit the Planner dispatches. Returns the packet path.

    With no `base_sha` and no worktree on disk it BUILDS rather than dying —
    `verify_base_sha`'s last-resort step takes the project repo's HEAD."""
    return main(["spec-auditor", spec] + (["--base-sha", base_sha] if base_sha else []))


def packet_spec_rework(spec):
    """The packet carrying the standing audit findings for the rewrite the Planner
    dispatches. Returns the packet path."""
    return main(["spec-writer", spec])


def packet_builder_rework(spec, hint):
    """The builder re-dispatch packet (L-adr-0039) — the identical builder packet for
    that spec's own worktree, plus the hint verbatim and the paths it concerns. A
    diff-shaped hint is refused, not rendered.

    R7/Target 3: this is the Executor's merge-conflict/rebase re-dispatch — the sole
    non-test caller of this symbol (Assumptions §8) — so it pins the CURRENT main
    tip here, unconditionally, before calling `main()`: the subject's `project`,
    read off the ledger (`fold.subject_project` — the same reading `dispatch.main`'s
    own project fallback uses), then `git -C <ROOT/repos/project> rev-parse HEAD` —
    the tip of the repo the Executor merges into, i.e. the main tip the builder is
    about to rebase onto (SD7). An unresolvable project, or a non-zero/empty git
    result, adds no `--base-sha` at all: this never dies, it falls through to
    `p_builder`'s own existing `verify_base_sha` precedence unchanged. Keeps its
    exact two-argument signature — no Executor change, no new CLI flag needed
    (`--base-sha` already exists). Returns the packet path."""
    argv = ["builder", spec, "--hint", hint]
    project = fold.subject_project(spec)
    if project:
        r = subprocess.run(["git", "-C", str(ROOT / "repos" / project), "rev-parse", "HEAD"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            argv += ["--base-sha", r.stdout.strip()]
    return main(argv)


if __name__ == "__main__":
    print(main())
