#!/usr/bin/env python3
"""Checks on `doit up`, `doit alloc`, and the Planner contract. Run: python3 test_up.py

Every check here is a silent failure made loud: a pane that writes as the operator,
a cron line that disagrees with the staleness alarm watching it, a helpful later
edit that installs the crontab, two units handed the same spec id, a contract whose
frontmatter drifts from the pane the launcher builds.
"""
import inspect, os, pathlib, subprocess, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"], os.environ["DOIT_NO_POKE"] = str(TMP), "1"
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import dispatch, fold, tick, up  # noqa: E402

n = 0


def ok(cond, why):
    global n
    assert cond, why
    n += 1


# ── the cron line is printed, never installed ────────────────────────────────
src = inspect.getsource(up)
ok("crontab" not in src, "up must never touch a crontab: cron is the operator's (§4.9, D117)")
os.environ["DOIT_TICK_MIN"] = "7"
line = up.cron_line()
ok(line.startswith("*/7 * * * * "), f"the cron line honours DOIT_TICK_MIN — the same variable "
                                    f"the fold's tick-stale alarm reads: {line}")
ok(f"DOIT_ROOT={TMP} " in line and line.split("DOIT_ROOT=")[1].split()[1].endswith("/doit"),
   f"the line carries this root and this doit, not whatever is on the operator's PATH: {line}")
ok(line.rstrip().endswith("tick.log 2>&1") and " tick " in line, f"the line runs a tick and keeps its output: {line}")

# ── the pane: right agent, RETIRE denied by name, and interactive ────────────
cmd = up.pane_cmd()
ok(cmd[:3] == ["claude", "--agent", "planner"], f"the pane is the planner contract: {cmd}")
deny = cmd[cmd.index("--disallowedTools") + 1]
ok(all(f"Skill({s})" in deny for s in tick.RETIRE), "D119: every RETIRE skill denied by name, or the "
                                                    "plugin reintroduces the loop D24 deleted")
ok(not ({"-p", "--json-schema", "--output-format"} & set(cmd)), f"the pane is interactive, not a -p spawn: {cmd}")

# ── a missing contract is loud, not a claude that fails five minutes later ───
dispatch.AGENTS, real_agents = TMP / "agents", dispatch.AGENTS
dispatch.AGENTS.mkdir()
try:
    up.main(print_only=True)
    ok(False, "no planner.md must exit non-zero")
except SystemExit as e:
    ok("no planner contract" in str(e), f"a missing contract names itself: {e}")

# ── the contract is linked where `--agent` looks, or the pane dies after the ──
# ── cron line has printed, which reads like success (measured 2026-09-08) ─────
(dispatch.AGENTS / "planner.md").write_text((real_agents / "planner.md").read_text())
up.AGENTS_HOME = TMP / "claude-agents"
LINK = up.AGENTS_HOME / "planner.md"
_, env = up.main(print_only=True)
ok(LINK.is_symlink() and LINK.resolve() == (dispatch.AGENTS / "planner.md").resolve(),
   "up links the contract into ~/.claude/agents — `--agent planner` resolves nowhere else")
ok(env["DOIT_LEDGER_FILE"] == "L-planner-0001.jsonl", f"the pane writes as itself (D90): {env['DOIT_LEDGER_FILE']}")
ok(env["PATH"].split(":")[0] == str(up.HERE.parent), "`doit` resolves inside the pane")
_, env2 = up.main(print_only=True)
ok(env2["DOIT_LEDGER_FILE"] == "L-planner-0002.jsonl", "a second pane never shares the first's actor file")
ok(LINK.is_symlink(), "linking is idempotent — a second up does not fail on its own link")
LINK.unlink(); LINK.write_text("someone else's planner")
try:
    up.main(print_only=True)
    ok(False, "a foreign file at the link name must stop the pane")
except SystemExit as e:
    ok("ln -sfn" in str(e), f"and it names the one line that resolves it: {e}")
LINK.unlink()
ok((fold.ROOT / "logs").is_dir(), "the cron line's log directory exists before the line is handed over")

# ── `doit alloc` — the id the Planner must not invent ────────────────────────
DOIT = str(up.HERE.parent / "doit")
run = lambda *a: subprocess.run([DOIT, *a], capture_output=True, text=True, env={**os.environ})
p1, p2 = run("alloc", "spec").stdout.strip(), run("alloc", "spec").stdout.strip()
ok(pathlib.Path(p1).name == "L-spec-0001.md" and pathlib.Path(p2).name == "L-spec-0002.md",
   f"alloc is max+1 of that kind (§2.8): {p1} {p2}")
ok(pathlib.Path(p1).is_file() and pathlib.Path(p1).stat().st_size == 0,
   "the file is claimed empty — dispatch's --path check still fails if the spec-writer writes nothing")
ok(pathlib.Path(run("alloc", "research").stdout.strip()).name == "L-research-0001.md",
   "each kind numbers from its own max, never a shared counter")

# ── the two Planner events are authorized to the Planner and nobody else ─────
ok(fold.EMITS["plan-written"] == {"planner"} and fold.EMITS["cut-written"] == {"planner"},
   "§3.2: the cut and the Plan have one author")
ev = [{"v": 1, "ts": "2026-09-08T10:00:00+00:00", "type": t, "subject": "L-charter-0001",
       "actor": a, "_src": "x:1", "path": "/tmp/p.md"}
      for t, a in (("plan-written", "planner"), ("plan-written", "builder"), ("cut-written", "executor"))]
_, _, ignored, by = fold.fold(ev)
ok(len(ignored) == 2 and [e["actor"] for e in ignored] == ["builder", "executor"],
   f"a Plan pointer from a non-Planner seat is recorded and ignored: {[e['actor'] for e in ignored]}")
ok(len(by["L-charter-0001"]) == 1, "the Planner's own lands")

# ── the contract the launcher spawns is the contract on disk ────────────────
fm = dispatch.frontmatter("planner")
ok(fm["name"] == "planner" and fm["model"] == "claude-opus-5", f"frontmatter: {fm}")
tools = [t.strip() for t in fm["tools"].split(",")]
ok("Skill" in tools, "the pane keeps §10.5's KEEP skills — the deny list is what removes the rest")
ok("Agent" in tools, "the Agent tool serves the pane's own `doit dispatch` seat packets (b-26); dispatch stays the only spawn path")
ok("StructuredOutput" not in tools, "a pane has no schema; its Output is the files and events it writes")
body = (real_agents / "planner.md").read_text()
for step in ("cut-written", "plan-written", "doit dispatch plan-auditor", "doit alloc spec",
             "doit dispatch spec-writer", "Never read a spec you commissioned"):
    ok(step in body, f"the cycle names {step!r} — a step the body omits is a step nothing performs")

# ── the Executor's own supervising loop ──────────────────────────────────────
# Nothing here starts a real pane: `claude` is a stub on PATH that exits at once,
# and the sleep is a counter. The defects these hold are the ones a passing suite
# would otherwise hide — a loop that needs a keystroke, a pane that can never
# reach a quiet point, a hand on a master checkout nobody recorded.
import json, shutil, textwrap

ok("\n".join(l for l in src.splitlines() if l.startswith("def pane_name")) == "",
   "AC1: pane_name is a sibling's Produces — imported, never redefined here (adr-friction)")
prompt = up.executor_prompt("L-executor-0099.jsonl", "BOARD TEXT")
ok("L-executor-0099" in prompt and "BOARD TEXT" in prompt,
   f"AC1: the pane is seeded with its own ledger stem and the whole board: {prompt[:80]!r}")
ok("L-executor-0099.jsonl" not in prompt.split("BOARD TEXT")[0].split("doit append")[0],
   "AC1: the seed names the stem, which is what -n and the actor file agree on (D90)")

# ── AC8: the deny list, verbatim, and only in the shape measured to fire ──────
deny_l = up.executor_deny_list()
for want in ("Bash(pytest:*)", "Bash(python3 -m pytest:*)", "Bash(python -m pytest:*)",
             "Bash(npm test:*)", "Bash(npm run test:*)", "Bash(doit test:*)",
             "Bash(./doit test:*)", "Bash(node:*)", "Bash(git commit:*)"):
    ok(want in deny_l, f"AC8/R15a: the Executor's deny list withholds {want} — 30 hand edits and "
                       f"43 pytest runs in one measured session is what it is for")
ok(all(f"Skill({s})" in deny_l for s in tick.RETIRE), "AC8: §10.5's RETIRE denied by name here too (D119)")
import re as _re
ok(all(_re.match(r"^Bash\([^*]+:\*\)$", e) for e in deny_l if e.startswith("Bash(")),
   f"AC8: every Bash entry is the PREFIX form dispatch.BUILDER_DENY ships — no mid-path glob "
   f"has ever been measured firing: {[e for e in deny_l if e.startswith('Bash(')]}")

# ── the executor contract has to be where --agent looks, same hole as planner ─
shutil.copyfile(real_agents / "executor.md", dispatch.AGENTS / "executor.md")

# ── AC6: a root whose map says claude-p is the TICK's, and gets no second driver ─
CP = TMP / "claude-p-root"
CP.mkdir()
(CP / "models.toml").write_text('[defaults]\nbackend = "claude-p"\n'
                                '[contracts.executor]\nbackend = "claude-p"\nmodel = "claude-opus-5"\n')
try:
    up.executor_loop(CP, 5, max_cycles=1)
    ok(False, "AC6: a claude-p root must refuse, not start a second duplicate driver")
except SystemExit as e:
    ok(e.code == 2, f"AC6: it refuses with a non-zero code, mirroring tick.main's own guard: {e.code}")
ok(not list(CP.glob("events/L-executor-*.jsonl")),
   "AC6: the refusal spawns nothing and allocates no ledger file")

# ── the scratch root IS this suite's DOIT_ROOT, so fold.read_events sees the ──
# ── pane's own appends — the whole point of R2's durable-state restart. ───────
(TMP / "models.toml").write_text('[defaults]\nbackend = "pane"\n'
                                 '[contracts.executor]\nbackend = "pane"\nmodel = "claude-opus-5"\n')
BIN = TMP / "bin"
BIN.mkdir()
STUB = BIN / "claude"
STUB.write_text(textwrap.dedent("""\
    #!/bin/sh
    # the pane: record the argv it was launched with, hand over on its own ledger
    # file (L-adr-0043's message-sent), and end — exactly what R2 supervises.
    printf '%s\\n' "$1 $2 $3 $4 $5 $6 $7" >> "$DOIT_ROOT/panes.log"
    printf '%s\\n' "${8:-NO-SEED}" | head -1 >> "$DOIT_ROOT/seed.log"
    printf '{"v":1,"ts":"%s","type":"message-sent","subject":"%s"}\\n' \\
      "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" "${DOIT_LEDGER_FILE%.jsonl}" \\
      >> "$DOIT_ROOT/events/$DOIT_LEDGER_FILE"
    exit 0
    """))
STUB.chmod(0o755)
os.environ["PATH"] = f"{BIN}:{os.environ['PATH']}"
up.PANE_NAME = lambda ledger_file: ledger_file.rsplit(".", 1)[0]     # AC5: wave 1's seam, stubbed
slept = []
up._SLEEP = lambda s: slept.append(s)

before_files = {p.name for p in (TMP / "events").glob("L-executor-*.jsonl")}
up.executor_loop(TMP, 5, max_cycles=2)
panes = sorted({p.name for p in (TMP / "events").glob("L-executor-*.jsonl")} - before_files)
ok(len(panes) == 2, f"AC2: two cycles allocate two distinct ledger files, unprompted: {panes}")
ok(not slept, "AC2: nothing sleeps between one pane's exit and the next one's start (R2)")
argvs = (TMP / "panes.log").read_text().splitlines()
ok(len(argvs) == 2, f"AC2: each cycle launched exactly one pane, with no second invocation: {argvs}")
seeds = (TMP / "seed.log").read_text().splitlines()
ok(len(seeds) == 2 and all(s.startswith(f[:-len(".jsonl")]) for f, s in zip(panes, seeds)),
   f"R1: the pane is SELF-SEEDED — the prompt is argv's last positional, not a blinking "
   f"cursor waiting for an operator to type the lane in: {seeds}")
for f, argv in zip(panes, argvs):
    evs = [json.loads(l) for l in (TMP / "events" / f).read_text().splitlines() if l.strip()]
    types = [e["type"] for e in evs]
    ok(types[0] == "spawn-started" and "spawn-done" in types,
       f"AC2: {f} carries its own start and end record: {types}")
    ok(all(e.get("spawn") == f[:-len('.jsonl')] for e in evs if e["type"].startswith("spawn-")),
       f"AC2: the records name the pane they describe: {f}")
    # AC5: -n immediately followed by the ledger stem pane_name returned
    toks = argv.split()
    ok(toks[0] == "-n" and toks[1] == f[:-len(".jsonl")],
       f"AC5/R12: the pane is named after its own ledger file, so the name and the actor "
       f"cannot drift (D90): {toks[:2]} vs {f}")
    ok("--agent" in toks and toks[toks.index("--agent") + 1] == "executor" and "-p" not in toks,
       f"D121/spec 572: the Executor pane is the interactive seat, never a metered -p: {toks[:6]}")
    ok("--disallowedTools" in toks, "the deny list is passed to the pane, not merely computed")

# ── AC7: the pane's own handover flips its own quiet point, nobody else's ─────
ev_all = fold.read_events()
ok(up.quiet_point(ev_all, panes[0][:-len(".jsonl")]),
   "AC7: after its own message-sent landed on its own file, this pane may end")
ok(not up.quiet_point(ev_all, "L-executor-9999"),
   "AC7: another pane's handover never flips this one's quiet point")

# ── AC3: the two quiet_point fixtures, built by hand ─────────────────────────
NOWS = fold.NOW.isoformat(timespec="seconds")
hand = {"v": 1, "ts": NOWS, "type": "message-sent", "subject": "L-executor-0031",
        "actor": "executor", "_src": "L-executor-0031.jsonl:1"}
ok(up.quiet_point([hand], "L-executor-0031"), "AC3: handover on its own file, nothing in flight → quiet")
busy = {"v": 1, "ts": NOWS, "type": "build-started", "subject": "L-spec-0077",
        "actor": "executor", "_src": "L-executor-0031.jsonl:2", "spawn": "L-builder-0077"}
ok(not up.quiet_point([hand, busy], "L-executor-0031"),
   "AC3: a builder still in flight is not a quiet point, handover or no handover (A7)")
ok(not up.quiet_point([busy], "L-executor-0031"), "AC3: no handover, no quiet point")
ok(not up.quiet_point([dict(hand, _src="L-executor-0003.jsonl:1")], "L-executor-0031"),
   "a prefix match is not an identity: 0003's handover is not 0031's")
mine = {"v": 1, "ts": NOWS, "type": "spawn-started", "subject": "L-executor-0031",
        "actor": "executor", "_src": "L-executor-0031.jsonl:0", "spawn": "L-executor-0031"}
ok(up.quiet_point([mine, hand], "L-executor-0031"),
   "a pane is never in flight against ITSELF — else the loop's own start record wedges "
   "every quiet point and the restart loop never turns")

# ── AC9: a hand on a repo the Executor was told not to touch is recorded ─────
def _repo(p):
    p.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    return p

MASTER = _repo(TMP / "repos" / "do-it-v2")
FREE = _repo(TMP / "worktrees" / "proj" / "l-spec-0088")       # no open build-started
BUILDING = _repo(TMP / "worktrees" / "proj" / "l-spec-0089")   # a builder is writing here by grant
PLAIN = TMP / "worktrees" / "proj"                             # depth-1: a plain project directory
watch = [MASTER, FREE, BUILDING, PLAIN]
snap_a = up._snapshot(TMP, watch)
ok(snap_a[str(PLAIN)] == dispatch.NOT_A_REPO,
   f"a plain project directory is a DEFINITE not-a-repo, never an undetermined diff: {snap_a[str(PLAIN)]!r}")
for d in (MASTER, FREE, BUILDING):
    (d / "touched.py").write_text("# a hand, not a dispatch\n")
snap_b = up._snapshot(TMP, watch)
LED = fold.EVENTS / "L-executor-0900.jsonl"
LED.write_text("")
open_b = [{"v": 1, "ts": NOWS, "type": "build-started", "subject": "L-spec-0089",
           "actor": "executor", "_src": "x:1", "spawn": "L-builder-0089"}]
hit = up._repo_edits(snap_a, snap_b, LED, open_b)
ok(hit == [str(MASTER), str(FREE)], f"AC9: the master checkout and the idle worktree are recorded; "
                                    f"the one with an open build-started is not: {hit}")
rows = [json.loads(l) for l in LED.read_text().splitlines() if l.strip()]
ok(len(rows) == 2 and {r["type"] for r in rows} == {"repo-edit"},
   f"AC9: exactly one repo-edit per changed repo, on the supervised pane's OWN file: {rows}")
ok({r["path"] for r in rows} == {str(MASTER), str(FREE)} and
   all(r["ledger"] == LED.name and any("touched.py" in c for c in r["changed"]) for r in rows),
   f"AC9: each names the path that changed, and what changed in it: {rows}")
ok(not any(r["path"] == str(PLAIN) for r in rows), "AC9: NOT_A_REPO can never produce a repo-edit")
shutil.rmtree(FREE)
LED2 = fold.EVENTS / "L-executor-0901.jsonl"
LED2.write_text("")
ok(up._repo_edits(snap_a, up._snapshot(TMP, watch), LED2, open_b) == [str(MASTER)]
   and len(LED2.read_text().splitlines()) == 1,
   "AC9: present-then-absent is a reap or a worktree add, not an edit — no event for it")
ok({str(p) for p in up._repo_dirs(TMP)} == {str(MASTER), str(BUILDING)},
   f"R15b: exactly two globs are watched — repos/* and worktrees/*/*: {up._repo_dirs(TMP)}")

# ── the seams wave 1 owes, and what this loop does while they are missing ────
up.PANE_NAME = None
ok(up._seam("pane_name", "PANE_NAME") is None and up._seam("decide_overdue", "DECIDE_OVERDUE") is None,
   "measured 2026-09-17: neither wave-1 seam is on this root yet — the loop binds them late "
   "and says so, rather than crashing on a sibling that has not landed")
ok(up._stem("L-executor-0031.jsonl") == "L-executor-0031" and up._stem("L-executor-0031") == "L-executor-0031",
   "the pane_name fallback is the same ledger.stem dispatch.alloc already returned")

print(f"up: {n} checks pass")
