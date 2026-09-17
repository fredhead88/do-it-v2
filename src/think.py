#!/usr/bin/env python3
"""think — open a Thinker session, and land what it produced (§3.3).

  think.py <topic>                 open `claude -n L-thinker-NNNN --agent thinker`
                                   (the pane is named after its own ledger file,
                                   so a message to that actor has a pane to land on)
  think.py --land FILE [FILE …]    check each charter, append charter-filed,
                                   fire the charter-set audit (D98)
  think.py --discard <topic>       end with nothing kept, on the record

A Thinker is conversational, read-only on code, and **spawns nothing** (§3.3).
The charter-set check belongs to the *driver* — D73's pattern verbatim, the
privileged act sits where the checks are — so it is here and not in the pane.

**Landing is the only place a charter is ever checked.** Nothing downstream
re-reads it for shape: the Planner cuts it, two fable audits read the cut, and
the charter-reviewer reads the done-condition at close. So every §3.4 rule that
is a rule rather than a taste is a refusal here — five sections, a requirement
with a stable id, a `review_path` carrying both halves, a non-empty `Covers:`,
and no execution-shape heading. A rule that is not one of these did not ship
(§7.3).
"""
import argparse, json, os, pathlib, re, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import audit, dispatch, fold, panes, tick, up  # noqa: E402
# The sequencing seam, bound ONCE and at module level so a verifier can substitute a
# stand-in by assigning to `think.sequencing`. `relay.sequencing` is the producing
# unit's declared name, not one picked here — a guessed module would leave this
# permanently None after that unit merges and the feature would never activate.
try:
    from relay import sequencing            # noqa: E402
except ImportError:                          # not merged yet: degrade, and say so
    sequencing = None
# The requirement-id machinery is `audit`'s, and it is one implementation used at
# two levels (§3.6): units against their charter there, the charter set against the
# goal here. `section` reads a heading's body; `LISTED`/`ID` are the stable-id forms.
from audit import ID, LISTED, section          # noqa: E402
from audit import goal_coverage as coverage    # noqa: E402

DOIT = HERE.parent / "doit"

# §3.4's five, in order. The names are the contract's headings verbatim.
SECTIONS = ("Intent", "Requirements", "Constraints and product decisions",
            "Done for the whole", "Covers")
# What is deliberately NOT in a charter (§3.4) — as a HEADING, never as a word in
# prose. v1's charter carried these and that is how a conversational session made
# binding architectural decisions with no audit between them and the builders.
PLANS = re.compile(r"^#+\s*[0-9.]*\s*(seams?|waves?|interfaces?|data shapes?|"
                   r"error[- ]handling|schema|branch)\b", re.I | re.M)
# A charter-level ORDER decision, not execution shape: it names charters, never units,
# files, seams or waves — which is why it is a line in the body and not a sixth §3.4
# heading (SECTIONS is a closed set the check already enforces).
SEQ_KINDS = ("after", "alongside", "conflicts")
SEQ_LINE = re.compile(rf"^({'|'.join(SEQ_KINDS)}):(.*)$", re.M)


def die(msg):
    sys.exit(f"think: {msg}")


def pane_cmd(name):
    """Named, so the session is findable in `claude agents`, in /resume and in the
    terminal title (§3.3) — and §8.9 infers the `creative` category from exactly
    that name, which is the only reason that counter is derivable at all.

    `name` is the pane's name, not the topic: it is the ledger stem this session
    writes as (`panes.pane_name(ledger.name)`), so the address a message is sent
    to and the actor the fold derives from the filename (D90) are one string. An
    operator-typed `think-<topic>` was addressable by nobody."""
    return ["claude", "-n", name, "--agent", "thinker",
            "--disallowedTools", ",".join(f"Skill({s})" for s in tick.RETIRE),
            "--dangerously-skip-permissions"]


def covers(body):
    """The goal requirement ids this charter delivers (D98). `none` is the
    adopted-project case (§12.2: goal: null) and is explicit, never an omission —
    an empty Covers: and a deliberate free-standing charter must not look alike."""
    line = next((l for l in body.splitlines() if "covers" in l.lower()), body)
    if re.search(r"\bnone\b|\bnull\b", line, re.I):
        return []
    return sorted(set(ID.findall(line)))


def sequencing_block(t, name):
    """The `after:`/`alongside:`/`conflicts:` lines a charter declares, as the mapping
    the `sequencing` seam takes: `{kind: "<id> <id> …"}`, the same space-joined form
    `covers=` already uses. A key appears iff at least one line of that kind appears —
    a mapping carrying all three keys would read as a *declared empty* block, and "no
    block at all" and "an empty block" must not look alike. An indented or bulleted
    `- after: …` is prose; only column 0 is an entry, which is what keeps this parse
    off charter prose."""
    block = {}
    for m in SEQ_LINE.finditer(t):
        kind, rest = m.group(1), m.group(2).split(None, 1)
        if len(rest) < 2 or not rest[1].strip():
            die(f"{name}: '{m.group(0).strip()}' — a sequencing line is "
                f"`{kind}: <charter-id> <one-line reason>`, and this one has "
                f"{'no charter id' if not rest else 'no reason'} after the colon. "
                f"A malformed entry is refused, never silently dropped")
        block.setdefault(kind, []).append(rest[0])
    return {k: " ".join(v) for k, v in block.items()}


def check(path, events=None):
    """Refuse a charter the Planner could not plan from, before any event points
    at it. Returns the facts the `charter-filed` event carries.

    `events` is the ledger the sequencing check reads (which charters have a
    `charter-filed` event); it defaults to `fold.read_events()` so every existing
    single-argument call site keeps working unchanged."""
    p = pathlib.Path(path).resolve()
    content = (fold.ROOT / "content").resolve()
    if content not in p.parents:
        die(f"{p}: a charter is content — write it under {content} (`doit alloc charter`)")
    if not p.is_file() or not p.read_text().strip():
        die(f"{p}: nothing on disk. Content before the event, always (§9.2 rule 3)")
    t = p.read_text()
    body = {s: section(t, s) for s in SECTIONS}
    missing = [s for s in SECTIONS if body[s] is None]
    if missing:
        die(f"{p.name}: no '{missing[0]}' section — a missing section is a missing "
            f"decision, not a short document (§3.4). Missing: {', '.join(missing)}")
    if not LISTED.search(body["Requirements"]):
        die(f"{p.name}: no requirement carries a stable id (`- R1: …`). The coverage "
            f"diff, the sweep's citation test (§3.12) and the charter review all key off them")
    done = body["Done for the whole"]
    if "review_path" not in done or "worked if" not in done or "failed if" not in done:
        die(f"{p.name}: done-for-the-whole has no `review_path` with both halves "
            f"(`worked if` / `failed if`). An unprovable done-for-the-whole is caught "
            f"here or after the specs are built (D99, §5.1)")
    bad = PLANS.search(t)
    if bad:
        die(f"{p.name}: '{bad.group(0).strip()}' is execution shape and belongs to the "
            f"Plan, not the charter (§3.4). Requirements only")
    c = covers(body["Covers"])
    if not c and not re.search(r"\bnone\b|\bnull\b", body["Covers"], re.I):
        die(f"{p.name}: Covers: is empty. Name the goal requirement ids this charter "
            f"delivers, or `Covers: none` where there is no goal document (§12.2)")
    block = sequencing_block(t, p.name)
    seq = None
    if sequencing is None:
        # The producer is not in the tree. L-adr-0033's rule for a failing producer,
        # applied to a missing one: degrade to yesterday's behaviour and SAY SO, so a
        # block that is silently not being enforced cannot pass for one that is.
        if block:
            kinds = " ".join(sorted(block))
            print(f"# sequencing seam unavailable (relay.sequencing not importable) — "
                  f"{p.name}'s {kinds} line(s) are recorded nowhere and checked "
                  f"by nothing; landing continues as before")
    else:
        # The mapping goes in whole, empty or not: deciding that no keys means "no
        # block declared" is the seam's, not this caller's.
        seq = sequencing(block, fold.read_events() if events is None else events)
        if seq and seq.get("unknown"):
            die(f"{p.name}: sequencing names {', '.join(seq['unknown'])} — no charter "
                f"has been filed under that id (a charter naming ITSELF lands here too: "
                f"it has no `charter-filed` event either, yet). Land the charter it "
                f"depends on first; nothing in this set was appended")
    title = next((l.lstrip("# ").strip() for l in t.splitlines() if l.startswith("# ")), p.stem)
    return {"id": p.stem, "path": str(p), "title": title, "covers": c, "seq": seq}


def goal_path(explicit):
    """`--goal FILE`, else the newest `goal-filed` event's path. No goal is a
    legitimate state (§12.2) — the charter-set check then has nothing to diff."""
    if explicit:
        return pathlib.Path(explicit)
    e = next((e for e in reversed(fold.read_events())
              if e.get("type") == "goal-filed" and e.get("path")), None)
    return pathlib.Path(e["path"]) if e else None


def charter_set_packet(goal, charters, diff):
    """§4.6 · 6's `stage: charter-set` Input, exactly: the charter set, the goal's
    done-condition, and the both-directions diff. No rationale — the plan-auditor
    is blind to the author's reasons and a sentence of them voids the run."""
    g = goal.read_text()
    parts = [f"stage: charter-set\ngoal: {goal.stem}\n",
             "## The goal's done-condition\n" + (section(g, "Done for the whole") or "(none)"),
             "\n" + audit.render("charter-set", audit.coverage_rows(diff))]
    for c in charters:
        parts.append(f"\n## {c['id']} — {c['title']}\n" + pathlib.Path(c["path"]).read_text())
    p = fold.ROOT / "content" / f"charter-set-{goal.stem}.md"
    p.write_text("\n".join(parts))
    return p


def land(paths, goal=None, print_only=False):
    """Check every charter BEFORE appending any event: a set that lands half-way
    leaves the coverage diff reading a set that does not exist."""
    events = fold.read_events() if sequencing else None
    charters = [check(p, events) for p in paths]
    for c in charters:
        fields = ["charter-filed", c["id"], f"path={c['path']}", f"title={c['title']}",
                  "covers=" + (" ".join(c["covers"]) or "none")]
        if c["seq"] is not None:
            # All three, together, or none of them — a subset would make "declared
            # nothing of this kind" and "the field was never written" the same read.
            # `fold.append` parses a value opening with `[` as JSON, so these land as
            # real arrays rather than as the string "['L-charter-0001']".
            fields += [f"{k}={json.dumps(list(c['seq'].get(k) or []))}" for k in SEQ_KINDS]
        fold.append(fields)
        seq = f" · {' '.join(fields[5:])}" if c["seq"] is not None else ""
        print(f"# filed {c['id']} · covers {' '.join(c['covers']) or 'none'} · "
              f"{c['path']}{seq}")
    g = goal_path(goal)
    if not any(c["covers"] for c in charters):
        print("# no charter cites a goal requirement — no charter-set audit (§12.2: goal: null)")
        return None
    if not g or not g.is_file():
        die("a charter cites goal requirements but there is no goal file "
            "(`--goal FILE`, or a `goal-filed` event). Undetermined is never clean")
    diff = coverage(g.read_text(), charters)
    packet = charter_set_packet(g, charters, diff)
    cmd = [str(DOIT), "dispatch", "plan-auditor", g.stem, "--packet", str(packet), "--detach"]
    repo = fold.ROOT / "repos" / (fold.PROJECT or "")
    if repo.is_dir():
        cmd += ["--cwd", str(repo)]
    print(f"# charter-set audit: {' '.join(cmd)}")
    if not print_only:
        subprocess.run(cmd, check=False)
    return cmd


def open_session(topic, print_only=False):
    contract = dispatch.AGENTS / "thinker.md"
    if not contract.exists():
        die(f"no thinker contract at {contract} — the session is the contract (D116)")
    up.install(contract)               # `--agent thinker` resolves nowhere else (AP15)
    (fold.ROOT / "events").mkdir(parents=True, exist_ok=True)
    (fold.ROOT / "content").mkdir(parents=True, exist_ok=True)
    ledger = dispatch.alloc(fold.EVENTS, "L-thinker-", ".jsonl")
    env = {**os.environ, "DOIT_LEDGER_FILE": ledger.name,
           "PATH": f"{HERE.parent}:{os.environ.get('PATH', '')}"}
    # The pane is named after the ledger allocated two lines up, and after
    # nothing else: `-n` and DOIT_LEDGER_FILE are the same id in the same call,
    # so a message addressed to the actor the fold derives lands on this pane.
    cmd = pane_cmd(panes.pane_name(ledger.name))
    print(f"# thinker: {ledger.stem} · {topic} · {' '.join(cmd)}")
    if print_only:
        return cmd, env
    os.execvpe(cmd[0], cmd, env)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="doit think", description=__doc__.split("\n\n")[0])
    ap.add_argument("topic", nargs="?", help="what this session is about; names the session")
    ap.add_argument("--land", nargs="+", metavar="FILE", help="charter file(s) to file and audit")
    ap.add_argument("--discard", action="store_true", help="end with nothing kept, on the record")
    ap.add_argument("--goal", help="the goal file for the charter-set diff; else the goal-filed event")
    ap.add_argument("--print-only", action="store_true", help="build the command, run nothing")
    a = ap.parse_args(argv)
    if a.land:
        return land(a.land, a.goal, a.print_only)
    if a.discard:
        # A discard leaves one line so that "ended with nothing" and "still open"
        # are different states. §3.3: discarding must cost what landing costs.
        if not a.topic:
            die("--discard names the topic it is discarding")
        # The record names the LEDGER it is ending, not only the topic: after the
        # rename, `think-<topic>` is a name no pane was ever launched under, so a
        # discard recorded under it points at nothing that ever existed. The
        # topic still rides along — it is what the session was about.
        ledger = os.environ.get("DOIT_LEDGER_FILE")
        name = panes.pane_name(ledger) if ledger else f"think-{a.topic}"
        fold.append(["think-discarded", name, f"topic=think-{a.topic}"])
        print(f"# discarded {name} · think-{a.topic} — nothing kept")
        return None
    if not a.topic:
        ap.error("a topic, --land FILE …, or --discard <topic>")
    return open_session(a.topic, a.print_only)


if __name__ == "__main__":
    main()
