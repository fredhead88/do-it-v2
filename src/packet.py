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
import fold  # noqa: E402

ROOT = pathlib.Path(os.environ.get("DOIT_ROOT", pathlib.Path.home() / ".do-it"))
CONTENT, PACKETS = ROOT / "content", ROOT / "packets"

# The done-condition every build-facing packet carries. Bytecode is not residue:
# the first real chain rejected a correct build because running the verify script
# left `__pycache__/` in the worktree, and "porcelain empty" cannot be the
# condition for a repo with no .gitignore for it (Active Problem 12).
DONE = ("the verify command exits 0; no tracked file is modified and no untracked file "
        "outside `__pycache__/` remains; exactly one commit above base_sha")

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
        if not self.evs and not (getattr(a, "slot", None) and getattr(a, "role", None) == "spec-writer"):
            # The one legitimate exception is spec-writer round one: the spec id was
            # just allocated and nothing has been written about it yet, so the slot
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
        return next((e.get("charter") for e in reversed(self.evs) if e.get("charter")), None)


def verify_script(c):
    """The spec's Verification block becomes `$R/content/verify-<spec>.sh`; the packet
    hands `bash <that>`. A multi-step block cannot ride in the card's verify.command,
    which §5.10 caps at 300 characters."""
    spec = c.spec_file()
    lines = section(spec.read_text(), "Verification")
    blk = re.search(r"```[a-z]*\n(.*?)```", "\n".join(lines), re.S)
    if not blk or not blk.group(1).strip():
        return None
    p = CONTENT / f"verify-{c.a.subject}.sh"
    p.parent.mkdir(parents=True, exist_ok=True)
    # -e, because a multi-command block whose last line passes would otherwise exit 0
    # over an earlier failure, and the done-condition is that exit code.
    p.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + blk.group(1))
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


# ─────────────────────────────── the six Input lists ───────────────────────────────

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
    ledger. Rework is round one's packet plus the audit's list."""
    prev = sorted(PACKETS.glob(f"{c.a.subject}-spec-writer-*.md"))
    if prev:
        base = prev[-1].read_text().splitlines()
    elif c.a.slot:
        base = pathlib.Path(c.a.slot).read_text().splitlines()
    else:
        die("no prior spec-writer packet and no --slot: round one carries the plan slot, "
            "which only the Planner holds")
    findings = [e for e in c.all_of("audit-finding") if e.get("list") != "rejected"]
    if not findings:
        # Round one from a slot the Executor wrote is legitimate — D7's brief-authored
        # spec has no plan slot and no audit yet. Round one from a PRIOR PACKET is a
        # rework with nothing to rework, which is the mistake this refuses.
        if prev or not c.a.slot:
            die("no audit findings on this subject — a rework packet with no fix list is round one again")
        return base
    return base + ["", "## Fix list — apply each, same spec id, same path", ""] + [
        f"{i}. [{f.get('field', '?')} · {f.get('category', '?')}] {f.get('finding', '')}"
        f"{'  → ' + f['suggested_fix'] if f.get('suggested_fix') else ''}"
        for i, f in enumerate(findings, 1)]


def p_builder(c):
    spec, v = c.spec_file(), verify_script(c)
    repo = c.a.repo or str(ROOT / "repos" / c.project())
    base_sha = c.a.base_sha or subprocess.run(
        ["git", "-C", c.worktree(), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip() or "UNKNOWN — read it back from the worktree"
    ch = c.charter_file()
    extract = section(ch.read_text(), "Constraints") if ch else []
    rej, fix = c.standing()
    adrs = [e["adr"] for e in c.all_of("adr-filed") if e.get("adr")]
    L = [f"1. The spec: `{spec}`. Read it from disk once. You are not given its text here.",
         "2. The charter extract — binding constraints and product decisions, verbatim:",
         *(extract or ["   none"]),
         f"3. `base_sha` = {base_sha}. Read it back from the worktree and confirm it before the first edit.",
         f"4. Verify command: `bash {v}`" if v else
         "4. Verify command: NONE — the spec's Verification block is empty. Declare `spec-ambiguity` and stop.",
         f"   Done-condition: {DONE}.",
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
    vline = next((l for l in card.read_text().splitlines() if l.startswith("verify ")), "verify: not reported")
    ver = subprocess.run(["shasum", "-a", "256", str(v)], capture_output=True, text=True).stdout.split()[0][:16] \
        if v else "no script"
    return [
        "1. The acceptance criteria, verbatim from the spec, typed, each with its evidence obligation:",
        *crit,
        "2. The output card's per-criterion rows. These are claims to test, not facts:",
        *([f"   {r}" for r in rows] or ["   none"]),
        "3. Evidence-type validator: not installed; no row is pre-failed on its account.",
        f"4. The verify command the spec authored, with the reported exit code and result: {vline}",
        f"5. Checker: `verify-{c.a.subject}` · version {ver} · coverage note "
        f"\"the spec's Verification block\" · re-run it with cwd `{c.worktree()}`.",
        f"6. The done-condition: {DONE}.",
    ]


def p_reviewer(c):
    spec = c.spec_file()
    body = spec.read_text()
    crit = criteria(body)
    bd = c.last("build-done") or {}
    where = c.a.url or f"the worktree at ready_sha {bd.get('ready_sha', '?')}: `{c.worktree()}`"
    return [
        f"1. The deployed thing, on its execution host: {where}. Never staging.",
        f"2. The done-condition: {DONE}.",
        "3. Every criterion the spec enumerates, verbatim, with its `review_path` "
        "(log in as / go to / do / worked if / failed if):",
        *crit,
        f"4. Structural pre-pass, ground truth: the spec's verify command reported exit "
        f"{bd.get('verify_exit', '?')}.",
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
        bd and cards.append(f"   {s['id']}: {bd['card']}")
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
         "grader": p_grader, "reviewer": p_reviewer, "charter-reviewer": p_charter_reviewer}


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
        out += [("the builder's branch", bd.get("branch")), ("base_sha", bd.get("base_sha")),
                ("ready_sha", bd.get("ready_sha")), ("a build timestamp", bd.get("ts"))]
        out += [("the builder's spawn id", e.get("spawn")) for e in c.all_of("spawn-done")
                if e.get("actor") == "builder"]
        card = pathlib.Path(bd.get("card") or "/")
        if card.exists():
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
    return []


def main(argv=None):
    ap = argparse.ArgumentParser(prog="doit packet", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("role", choices=sorted(BUILD))
    ap.add_argument("subject")
    ap.add_argument("--charter", help="path to the charter file; else the charter-filed event")
    ap.add_argument("--repo"), ap.add_argument("--worktree"), ap.add_argument("--project")
    ap.add_argument("--slot", help="spec-writer round one: the file holding the plan slot")
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


if __name__ == "__main__":
    print(main())
