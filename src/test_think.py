#!/usr/bin/env python3
"""Checks on `doit think` and the Thinker contract. Run: python3 test_think.py

Landing is the only place a charter is ever checked (§3.4), so every check here
is a charter that would otherwise reach the Planner unprovable, uncited, or
carrying the Plan's decisions — and every one is written with its negative, so a
refusal that fires on everything counts as nothing.
"""
import os, pathlib, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"], os.environ["DOIT_NO_POKE"] = str(TMP), "1"
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import dispatch, fold, think, tick, up  # noqa: E402

n = 0
CONTENT = TMP / "content"
CONTENT.mkdir(parents=True), (TMP / "events").mkdir(parents=True)


def ok(cond, why):
    global n
    assert cond, why
    n += 1


def refuses(path, phrase, why):
    try:
        think.check(path)
    except SystemExit as e:
        return ok(phrase in str(e), f"{why} — got: {e}")
    ok(False, f"{why} — nothing was refused")


GOOD = """# The board shows spend per project

## 1. Intent
Because nobody can answer what a charter cost while it is still running.

## 2. Requirements
- R1: every spawn's cost is attributed to a project
- R2: the board totals it per project

## 3. Constraints and product decisions
Cost is the CLI's list-price estimate, not money.

## 4. Done for the whole
Can the operator answer "what has this charter cost so far" without asking anyone?

review_path:
  log in as   the operator's own shell (capabilities: read)
  go to       `doit`
  do          read the HEALTH block after a charter has spawned twice
  worked if   a per-project total appears and matches `doit events`
  failed if   the block is absent, or the total disagrees with the ledger

## 5. Covers
Covers: G2, G7
"""


def charter(name, text=GOOD, **sub):
    for k, v in sub.items():
        text = text.replace(k.replace("_", " "), v)
    p = CONTENT / name
    p.write_text(text)
    return str(p)


# ── the honest charter lands, and the event carries what the diff needs ──────
c = think.check(charter("L-charter-0001.md"))
ok(c["id"] == "L-charter-0001" and c["covers"] == ["G2", "G7"], f"the five sections pass: {c}")
ok(c["title"] == "The board shows spend per project", f"the title is the H1: {c}")

# ── each of §3.4's five sections is a refusal, not a note ────────────────────
for s in think.SECTIONS:
    body = GOOD.replace(f" {s}", " Something else", 1)
    refuses(charter("L-charter-0002.md", body), s, f"a charter with no '{s}' section is refused")

# ── the rules inside the sections ────────────────────────────────────────────
refuses(charter("L-charter-0003.md", GOOD.replace("- R1:", "- the cost is attributed")
                                         .replace("- R2:", "- the board totals it")),
        "stable id", "a requirement with no stable id is not citable by the coverage diff")
refuses(charter("L-charter-0004.md", GOOD.replace("  failed if   the block is absent, or the "
                                                  "total disagrees with the ledger\n", "")),
        "review_path", "a done-for-the-whole with only half a review_path is refused (D99)")
refuses(charter("L-charter-0005.md", GOOD.replace("review_path:", "proof:")),
        "review_path", "and one with none at all")
refuses(charter("L-charter-0006.md", GOOD + "\n## 6. Seams\nA hands B the total.\n"),
        "execution shape", "seams belong to the Plan (§3.4)")
refuses(charter("L-charter-0007.md", GOOD + "\n## Waves\nWave 1 is the fold.\n"),
        "execution shape", "so do waves")
ok(think.check(charter("L-charter-0008.md", GOOD.replace(
    "Cost is the CLI's", "The seams between the fold and the board are the Plan's problem. Cost is the CLI's")))
   ["id"] == "L-charter-0008",
   "the word 'seams' in a sentence is prose, not a section — the refusal is on headings only")
refuses(charter("L-charter-0009.md", GOOD.replace("Covers: G2, G7", "Covers:")),
        "Covers", "an empty Covers: is refused — the governor is checkable or it is not")
ok(think.check(charter("L-charter-0010.md", GOOD.replace("Covers: G2, G7", "Covers: none (no goal yet)")))
   ["covers"] == [], "`Covers: none` is the adopted-project case and is explicit (§12.2)")
refuses(str(CONTENT / "L-charter-0404.md"), "nothing on disk", "a missing file is refused before the event")
(TMP / "elsewhere.md").write_text(GOOD)
refuses(str(TMP / "elsewhere.md"), "doit alloc charter", "a charter outside content/ is refused")

# ── landing: content first, event second, and the actor is the session ───────
os.environ["DOIT_LEDGER_FILE"] = "L-thinker-0001.jsonl"
think.land([charter("L-charter-0011.md", GOOD.replace("Covers: G2, G7", "Covers: none"))], print_only=True)
ev = fold.read_events()
ok([e["type"] for e in ev] == ["charter-filed"] and ev[0]["subject"] == "L-charter-0011",
   f"landing appends exactly one charter-filed: {ev}")
ok(ev[0]["actor"] == "thinker" and "actor" not in (TMP / "events" / "L-thinker-0001.jsonl").read_text(),
   "the actor is the filename, never a field (D90)")
ok(ev[0]["path"].endswith("L-charter-0011.md") and ev[0]["covers"] == "none",
   f"the event carries the file and what it covers: {ev[0]}")

# ── a half-landed set is worse than none: every charter is checked first ─────
bad = charter("L-charter-0012.md", GOOD.replace("## 4. Done for the whole", "## 4. Done"))
try:
    think.land([charter("L-charter-0013.md"), bad], print_only=True)
    ok(False, "a set with one bad charter must not land the good ones")
except SystemExit:
    ok(not any(e["subject"] == "L-charter-0013" for e in fold.read_events()),
       "no event was appended before the whole set had passed")

# ── the charter-set audit: the diff runs BOTH directions (D98) ──────────────
GOAL = """# Spend visibility

## Requirements
- G2: cost is attributed to a project
- G7: the operator can see it without asking
- G9: a per-client invoice line

## Out of scope
- G4: real-money metering — the seat is not metered in dollars

## Done for the whole
Can the operator price a charter from the board alone?
"""
goal = CONTENT / "L-goal-0001.md"
goal.write_text(GOAL)
diff = think.coverage(GOAL, [{"covers": ["G2", "G7"]}, {"covers": ["G4", "G12"]}])
ok(diff["undelivered"] == ["G9"], f"a goal requirement no charter cites is under-delivery: {diff}")
ok(diff["creep"] == ["G12"], f"a charter citing what the goal never asked for is scope creep: {diff}")
ok(diff["out_of_scope"] == ["G4"], f"and an excluded id is named separately (§7.9): {diff}")

cmd = think.land([charter("L-charter-0014.md")], goal=str(goal), print_only=True)
ok(cmd[:4] == [str(think.DOIT), "dispatch", "plan-auditor", "L-goal-0001"],
   f"a charter set citing a goal fires the charter-set audit, subject the goal: {cmd}")
ok("--detach" in cmd, "detached: the landing command returns and the window closes")
packet = (CONTENT / "charter-set-L-goal-0001.md").read_text()
ok("stage: charter-set" in packet, "§4.6·6's third stage is named in the packet")
ok("G9" in packet and "Can the operator price a charter" in packet,
   "the packet carries the diff and the goal's done-condition")
ok("The board shows spend per project" in packet, "and the charter set itself")

ok(think.land([charter("L-charter-0015.md", GOOD.replace("Covers: G2, G7", "Covers: none"))],
              print_only=True) is None,
   "no goal, nothing to diff, no audit — and that is a legitimate state (§12.2)")
try:
    think.land([charter("L-charter-0016.md")], print_only=True)
    ok(False, "a charter citing goal ids with no goal file must not pass silently")
except SystemExit as e:
    ok("Undetermined is never clean" in str(e), f"it escalates instead: {e}")

# ── the fold authorizes the charter, because landing is what checked it ──────
ok(fold.EMITS["charter-filed"] == {"thinker", "operator"}, "§2.1: the charter has one author")
raw = [{"v": 1, "ts": "2026-09-08T10:00:00+00:00", "type": "charter-filed",
        "subject": "L-charter-0100", "actor": a, "_src": "x:1"}
       for a in ("thinker", "operator", "planner", "builder")]
_, _, ignored, by = fold.fold(raw)
ok([e["actor"] for e in ignored] == ["planner", "builder"],
   f"a charter filed by a seat that never ran the landing checks is ignored: {ignored}")
ok(len(by["L-charter-0100"]) == 2, "the Thinker's and the operator's land")

# ── the session: named, thinker, RETIRE denied, and linked where --agent looks ─
cmd = think.pane_cmd("spend")
ok(cmd[:5] == ["claude", "-n", "think-spend", "--agent", "thinker"],
   f"§3.3: named, so `claude agents` and §8.9's creative counter can find it: {cmd}")
ok(not ({"-p", "--output-format", "--json-schema"} & set(cmd)), f"conversational, not a spawn: {cmd}")
deny = cmd[cmd.index("--disallowedTools") + 1]
ok(all(f"Skill({s})" in deny for s in tick.RETIRE), "D119: every RETIRE skill denied by name")

dispatch.AGENTS, real_agents = TMP / "agents", dispatch.AGENTS
dispatch.AGENTS.mkdir()
try:
    think.open_session("spend", print_only=True)
    ok(False, "a missing contract must exit non-zero")
except SystemExit as e:
    ok("no thinker contract" in str(e), f"and name itself: {e}")
(dispatch.AGENTS / "thinker.md").write_text((real_agents / "thinker.md").read_text())
up.AGENTS_HOME = TMP / "claude-agents"
_, env = think.open_session("spend", print_only=True)
ok((up.AGENTS_HOME / "thinker.md").is_symlink(),
   "the contract is linked into ~/.claude/agents — `--agent thinker` resolves nowhere else (AP15)")
ok(env["DOIT_LEDGER_FILE"] == "L-thinker-0002.jsonl",
   f"the session writes as itself, and never over the last one's file (D90): {env['DOIT_LEDGER_FILE']}")

# ── discard costs what landing costs, and leaves the state distinguishable ───
os.environ["DOIT_LEDGER_FILE"] = "L-thinker-0002.jsonl"
think.main(["spend", "--discard"])
ok(any(e["type"] == "think-discarded" and e["subject"] == "think-spend" for e in fold.read_events()),
   "§3.3: a discarded session is on the record, so 'ended with nothing' and 'still open' differ")
ok(not any(e["type"] == "charter-filed" and e["actor"] == "thinker"
           and e["subject"].startswith("think-") for e in fold.read_events()),
   "and it keeps no content")

# ── the contract the launcher opens is the contract on disk ─────────────────
fm = dispatch.frontmatter("thinker")
ok(fm["name"] == "thinker" and fm["model"] == "claude-opus-5", f"frontmatter: {fm}")
tools = [t.strip() for t in fm["tools"].split(",")]
ok("Skill" in tools and "Agent" not in tools and "StructuredOutput" not in tools,
   f"a session, not a contract: KEEP skills, no spawn path, no schema: {tools}")
body = (real_agents / "thinker.md").read_text()
for line in ("doit alloc charter", "doit think --land", "doit think --discard", "review_path",
             "Covers:", "You spawn nothing", "NEVER deletes", "blocked_me"):
    ok(line in body, f"the contract names {line!r} — a step the body omits is a step nothing performs")
for s in think.SECTIONS:
    ok(s in body, f"the template names the section the landing check refuses without: {s}")

print(f"think: {n} checks pass")
