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
up.INSTALLED = TMP / "claude-agents" / "planner.md"
_, env = up.main(print_only=True)
ok(up.INSTALLED.is_symlink() and up.INSTALLED.resolve() == (dispatch.AGENTS / "planner.md").resolve(),
   "up links the contract into ~/.claude/agents — `--agent planner` resolves nowhere else")
ok(env["DOIT_LEDGER_FILE"] == "L-planner-0001.jsonl", f"the pane writes as itself (D90): {env['DOIT_LEDGER_FILE']}")
ok(env["PATH"].split(":")[0] == str(up.HERE.parent), "`doit` resolves inside the pane")
_, env2 = up.main(print_only=True)
ok(env2["DOIT_LEDGER_FILE"] == "L-planner-0002.jsonl", "a second pane never shares the first's actor file")
ok(up.INSTALLED.is_symlink(), "linking is idempotent — a second up does not fail on its own link")
up.INSTALLED.unlink(); up.INSTALLED.write_text("someone else's planner")
try:
    up.main(print_only=True)
    ok(False, "a foreign file at the link name must stop the pane")
except SystemExit as e:
    ok("ln -sfn" in str(e), f"and it names the one line that resolves it: {e}")
up.INSTALLED.unlink()
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
ok("Agent" not in tools, "no Agent tool: `doit dispatch` is the only spawn path")
ok("StructuredOutput" not in tools, "a pane has no schema; its Output is the files and events it writes")
body = (real_agents / "planner.md").read_text()
for step in ("cut-written", "plan-written", "doit dispatch plan-auditor", "doit alloc spec",
             "doit dispatch spec-writer", "Never read a spec you commissioned"):
    ok(step in body, f"the cycle names {step!r} — a step the body omits is a step nothing performs")

print(f"up: {n} checks pass")
