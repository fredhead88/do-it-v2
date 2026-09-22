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
import audit, fold  # noqa: E402

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


def section(body, name):
    """The lines under the first heading whose text contains `name`, to the next heading."""
    m = re.search(rf"^#+\s*\d*\.?\s*[^\n]*{name}[^\n]*$", body, re.M | re.I)
    if not m:
        return []
    rest = body[m.end():]
    end = re.search(r"^#+ ", rest, re.M)
    return (rest[:end.start()] if end else rest).strip().splitlines()


def criteria(body):
    """The spec's acceptance criteria, verbatim and with their review paths.

    One extractor for the grader and the reviewer, because they had two and the
    reviewer's — an `AC\\d+ \\[` line anchor — missed a spec whose criteria are
    bold (`**AC1 [ui] — ...**`). It reported "none found in the spec" and the
    reviewer returned no blocking finding on a review of nothing
    (`L-reviewer-0002`, 2026-09-08). A criterion the packet cannot find is
    undetermined, and undetermined is never clean: refuse."""
    return (section(body, "Acceptance")
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
        if not self.evs and not spec_writer_round_one and not charter_set:
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


def _newline_violations(block):
    """Rule (b)'s other half: a physical line that is not the block's last and
    does not end in `&&` is a statement the `&&` chain does not gate — `set -e`
    covers it today, but the next edit that wraps one line in a conditional
    would not, and a linted block is not meant to depend on that."""
    lines = [l for l in block.splitlines() if l.strip() and not l.strip().startswith("#")]
    return [l.strip() for l in lines[:-1] if not l.split("#", 1)[0].rstrip().endswith("&&")]


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
    the block itself would do on every run. Precedence: `--base-sha`, a recorded
    `base_sha` (build-done is the most authoritative — it is the actual build's —
    then build-started, then spec-written), else `git merge-base` against the
    worktree, once, at packet time; `git rev-parse HEAD` is the last resort for a
    worktree with nothing to diverge from yet.

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
    for t in ("build-done", "build-started", "spec-written"):
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


def verify_script(c):
    """The spec's Verification block becomes `$R/content/verify-<spec>.sh`; the packet
    hands `bash <that>`. A multi-step block cannot ride in the card's verify.command,
    which §5.10 caps at 300 characters.

    Linted before it is ever written (S23/S31a): `bash -n` on the assembled script;
    the block must be one `&&`-gated chain (no bare `;`/`||`, no newline-separated
    statement outside it); an interpreter named by a bare word instead of an
    absolute path; and `BASE=<sha>` pinned once, with any `$(git merge-base …)` in
    the block rewritten to `$BASE`. Any violation refuses the packet — `die()`,
    non-zero exit, on stderr — rather than writing a script that lies about what it
    checks."""
    spec = c.spec_file()
    lines = section(spec.read_text(), "Verification")
    blk = re.search(r"```[a-z]*\n(.*?)```", "\n".join(lines), re.S)
    if not blk or not blk.group(1).strip():
        return None
    block = blk.group(1)
    segs, bad = _quote_aware_scan(block)
    bad += [f"a newline-separated statement — `{l}` does not end in `&&`"
            for l in _newline_violations(block)]
    if bad:
        die(f"{c.a.subject}'s Verification block is not one gated `&&` chain: " + "; ".join(bad[:3]))
    bad_interp = _interp_violations(segs)
    if bad_interp:
        die(f"{c.a.subject}'s Verification block names an interpreter by bare word, not an "
            f"absolute path: " + "; ".join(bad_interp[:3]))
    base = verify_base_sha(c)
    block = MERGE_BASE_CALL.sub("$BASE", block)
    # -e, because a multi-command block whose last line passes would otherwise exit 0
    # over an earlier failure, and the done-condition is that exit code.
    text = f"#!/usr/bin/env bash\nset -euo pipefail\nBASE={base}\n" + block
    check = subprocess.run(["bash", "-n", "/dev/stdin"], input=text, capture_output=True, text=True)
    if check.returncode != 0:
        die(f"{c.a.subject}'s assembled verify script fails `bash -n`: {check.stderr.strip()}")
    p = CONTENT / f"verify-{c.a.subject}.sh"
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
    """`section()` returns a list of lines; the a-14 packets embed prose, so join it
    back into text once, here, rather than at every call site."""
    lines = section(text, name)
    return "\n".join(lines) if lines else default


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
    reqs = [l for l in section(ch_text, "Requirements") if any(l.strip().startswith(f"- {r}") for r in delivers)]
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
    spec, v = c.spec_file(), verify_script(c)
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
    if not findings:
        # Round one from a slot the Executor wrote is legitimate — D7's brief-authored
        # spec has no plan slot and no audit yet. Round one from a PRIOR PACKET is a
        # rework with nothing to rework, which is the mistake this refuses.
        if prev or not slot:
            die("no audit findings on this subject — a rework packet with no fix list is round one again")
        return base
    return base + ["", "## Fix list — apply each, same spec id, same path", ""] + [
        f"{i}. [{f.get('field', '?')} · {f.get('category', '?')}] {f.get('finding', '')}"
        f"{'  → ' + f['suggested_fix'] if f.get('suggested_fix') else ''}"
        for i, f in enumerate(findings, 1)]


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
    spec, v = c.spec_file(), verify_script(c)
    repo = c.a.repo or str(ROOT / "repos" / c.project())
    base_sha = c.a.base_sha or subprocess.run(
        ["git", "-C", c.worktree(), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip() or "UNKNOWN — read it back from the worktree"
    ch = c.charter_file()
    extract = section(ch.read_text(), "Constraints") if ch else []
    rej, fix = c.standing()
    adrs = [e["adr"] for e in c.all_of("adr-filed") if e.get("adr")]
    done = done_condition(c, c.charter_id())
    L = [f"1. The spec: `{spec}`. Read it from disk once. You are not given its text here.",
         "2. The charter extract — binding constraints and product decisions, verbatim:",
         *(extract or ["   none"]),
         f"3. `base_sha` = {base_sha}. Read it back from the worktree and confirm it before the first edit.",
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
    L += ["", f"Your cwd is your worktree, on branch `{c.a.subject.lower()}`. "
              f"The repository is `{repo}`. One spec, one commit, on that branch."]
    return L


def p_grader(c):
    spec, card, v = c.spec_file(), c.card_file(), verify_script(c)
    body = spec.read_text()
    crit = criteria(body)
    rows = [l for l in card.read_text().splitlines() if AC_ROW.match(l)]
    # Every row, from the card object the wrapper writes beside the rendered card —
    # the fifteen-line render drops rows past its room, and a grader that cannot see
    # a claim cannot test it. Falls back to the render for a pre-sidecar card.
    side = card.with_suffix(".json")
    if side.is_file():
        import json
        rows = [f"{a['id']} [{a['criterion_type']}] {a['disposition']} · {a['evidence_type']} · "
                f"check: {a['check']} · evidence: {a['evidence']}" for a in json.loads(side.read_text())["acs"]]
    vline = next((l for l in card.read_text().splitlines() if l.startswith("verify ")), "verify: not reported")
    ver = subprocess.run(["shasum", "-a", "256", str(v)], capture_output=True, text=True).stdout.split()[0][:16] \
        if v else "no script"
    done = done_condition(c, c.charter_id())
    return [
        "1. The acceptance criteria, verbatim from the spec, typed, each with its evidence obligation:",
        *crit,
        "2. The output card's per-criterion rows. These are claims to test, not facts:",
        *([f"   {r}" for r in rows] or ["   none"]),
        "3. Evidence-type validator: not installed; no row is pre-failed on its account.",
        f"4. The verify command the spec authored, with the reported exit code and result: {vline}",
        f"5. Checker: `verify-{c.a.subject}` · version {ver} · coverage note "
        f"\"the spec's Verification block\" · re-run it with cwd `{c.worktree()}`.",
        f"6. The done-condition: {done}.",
    ]


def p_reviewer(c):
    spec = c.spec_file()
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


BUILD = {"spec-auditor": p_spec_auditor, "spec-writer": p_spec_writer, "builder": p_builder,
         "grader": p_grader, "reviewer": p_reviewer, "charter-reviewer": p_charter_reviewer,
         "plan-auditor": p_plan_auditor}


# ──────────────────────────── Blindness, as a check ────────────────────────────

def sibling_bodies(c):
    """Every other spec's substantial lines. A packet that pasted one is carrying a
    sibling slot's internals."""
    out = []
    for sid, s in c.specs.items():
        if sid == c.a.subject:
            continue
        # A `spec-written` need not carry a path: the three from the pre-wrapper era
        # do not, and one of them crashed the first real rework dispatch. A sibling
        # with no file on disk has no body to leak — it is skipped, never guessed at.
        e = next((x for x in reversed(s["evs"])
                  if x["type"] == "spec-written" and pathlib.Path(x.get("path") or "/").is_file()), None)
        if e:
            out += [(f"a sibling spec's body ({sid})", l) for l in long_lines(pathlib.Path(e["path"]))[:20]]
    return out


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
        out += [("base_sha", bd.get("base_sha")),
                ("ready_sha", bd.get("ready_sha")), ("a build timestamp", bd.get("ts"))]
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
               [("the spec's Assumptions reasoning", l) for l in section(c.spec_file().read_text(), "Assumptions")
                if len(l.strip()) > 50] + \
               [("a prior audit finding", e.get("finding")) for e in c.all_of("audit-finding")]
    if role == "spec-writer":
        return sibling_bodies(c)
    if role == "builder":
        return sibling_bodies(c) + other_cards(c)
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
                    for l in section(pathlib.Path(f).read_text(), name) if len(l.strip()) > 50]
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
                out += [(label, l) for l in sec if len(l.strip()) > 50] if sec else []
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
    diff-shaped hint is refused, not rendered. Returns the packet path."""
    return main(["builder", spec, "--hint", hint])


if __name__ == "__main__":
    print(main())
