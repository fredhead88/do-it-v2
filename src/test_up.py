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
import panes as panes_mod  # noqa: E402 — aliased: `panes` is reused below as a local variable name

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

# ── AC1: pane_cmd(name=...) — R12's Planner half of the naming rule ──────────
ok(up.pane_cmd() == cmd and up.pane_cmd(name=None) == cmd,
   f"AC1: name=None, the default (or passed explicitly), is byte-identical to today's argv: {cmd}")
named = up.pane_cmd(name="L-planner-0042")
ok(named[:4] == ["claude", "-n", "L-planner-0042", "--agent"],
   f"AC1: given a name, the first four tokens are claude, -n, the name, --agent: {named[:4]}")
ok(named[3:] == cmd[1:], f"AC1: everything from --agent on is byte-identical whether or not a "
                         f"name is given: {named[3:]} vs {cmd[1:]}")
named_p, unnamed_p = up.pane_cmd("L-charter-0009", name="L-planner-0099"), up.pane_cmd("L-charter-0009")
ok(named_p[:3] == ["claude", "-n", "L-planner-0099"], f"AC1: -n placement holds with a prompt too: {named_p[:3]}")
ok(named_p[3:] == unnamed_p[1:], f"AC1: the deny list, skip-permissions flag and trailing prompt "
                                 f"positional are identical whether or not a name is given: {named_p} vs {unnamed_p}")

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
ok(fm["name"] == "planner" and fm["model"] == "claude-opus-5-5", f"frontmatter: {fm}")
tools = [t.strip() for t in fm["tools"].split(",")]
ok("Skill" in tools, "the pane keeps §10.5's KEEP skills — the deny list is what removes the rest")
ok("Agent" in tools, "the Agent tool serves the pane's own `doit dispatch` seat packets (b-26); dispatch stays the only spawn path")
ok("StructuredOutput" not in tools, "a pane has no schema; its Output is the files and events it writes")
body = (real_agents / "planner.md").read_text()
# "cut-written" is stale here: 014b204 collapsed the old two-document cut-then-Plan
# flow into one planning document and removed the separate cut-written event with
# it — the body no longer names a step that does not exist.
for step in ("plan-written", "doit dispatch plan-auditor", "doit alloc spec",
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
    # 0187-AC1: what this pane's child env actually carried for DOIT_SUPERVISED.
    printf '%s\\n' "$DOIT_SUPERVISED" >> "$DOIT_ROOT/supervised.log"
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

# 0187-AC1: DOIT_SUPERVISED is truthy on every pane executor_loop starts — the
# gap `_child_env(ledger, supervised=True)` closes (a hand-built env never set it).
sup_log = (TMP / "supervised.log").read_text().splitlines()
ok(len(sup_log) == 2 and all(l.strip() for l in sup_log),
   f"0187-AC1: DOIT_SUPERVISED is truthy on every pane executor_loop starts, both cycles: {sup_log}")

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
# ★ measured 2026-09-23: `src/panes.py` (pane_name) landed on main well before
# this unit's base_sha (`dd11840`, ancestor of `42883cc`) — `_seam` finds it by
# late-binding through SEAM_MODULES exactly as designed, so it now resolves to
# a real callable rather than None. `decide_overdue` has still not landed
# anywhere under src/, so that half of the original assertion still holds.
up.PANE_NAME = None
ok(up._seam("pane_name", "PANE_NAME") is panes_mod.pane_name
   and up._seam("decide_overdue", "DECIDE_OVERDUE") is None,
   "pane_name now resolves to the shipped panes.pane_name; decide_overdue is still unbound")
ok(up._stem("L-executor-0031.jsonl") == "L-executor-0031" and up._stem("L-executor-0031") == "L-executor-0031",
   "the pane_name fallback is the same ledger.stem dispatch.alloc already returned")

# ── L-spec-0031 · the-supervisor fixtures ─────
# No live process and no live pane: `subprocess.run` is recorded, `relay` is a
# stand-in (relay-queries lands after this unit), and every fixture gets a root of
# its own, because the supervisor's whole input is a ledger.
import contextlib, io, json as js, time as clock, types  # noqa: E402


def sup_root(name):
    r = TMP / name
    (r / "events").mkdir(parents=True, exist_ok=True)
    (r / "logs").mkdir(parents=True, exist_ok=True)
    fold.ROOT, fold.EVENTS, dispatch.EVENTS = r, r / "events", r / "events"
    return r


def sup_relay(**fns):
    m = types.ModuleType("relay")
    base = {"plannable": lambda *a, **k: ([], []), "waiting_lines": lambda *a, **k: [],
            "ledger_changed": lambda *a, **k: (False, None), "pending_packets": lambda *a, **k: [],
            "planner_attempts": lambda *a, **k: {"attempts": 0, "last_reason": "", "next": "start"}}
    for k, v in {**base, **fns}.items():
        setattr(m, k, v)
    sys.modules["relay"] = m
    return m


def sup_runs(rc=0, write=None):
    """Replaces subprocess.run — records the launch, never makes one. `started` is
    read at launch time, so AC5's ordering is observed and not inferred."""
    calls = []

    def run(cmd, env=None, **kw):
        led = fold.EVENTS / env["DOIT_LEDGER_FILE"]
        calls.append({"cmd": cmd, "env": env, "ledger": led,
                      "started": [l for l in led.read_text().splitlines() if "planner-started" in l]})
        write and write(led)
        return types.SimpleNamespace(returncode=rc)
    up.subprocess = types.SimpleNamespace(run=run)
    return calls


def sup_rows(f=None):
    files = [f] if f else sorted(fold.EVENTS.glob("*.jsonl"))
    return [js.loads(l) for p in files for l in p.read_text().splitlines() if l.strip()]


def named_ok(calls):
    """AC2: every pane `_start` actually launches carries `-n <its own ledger
    stem>` right after `claude`, matching the `DOIT_LEDGER_FILE` its own
    environment carries — the pane's launch name and the file it appends events
    under (D90) cannot drift apart."""
    return all(c["cmd"][1:3] == ["-n", c["env"]["DOIT_LEDGER_FILE"][:-len(".jsonl")]] for c in calls)


def sup_boom(*a, **k):
    raise RuntimeError("relay is out")


os.environ["DOIT_SUPERVISOR_INTERVAL_SEC"] = "0"

# ── AC1 · one main(), one Planner per plannable charter, and never an exec ────
sup_root("ac1")
sup_relay(plannable=lambda *a, **k: (["L-charter-0001", "L-charter-0002"], []))
calls = sup_runs()
up.main(max_cycles=1)
ok([c["cmd"][-1] for c in calls] == ["L-charter-0001", "L-charter-0002"],
   f"the loop starts one Planner per ready charter, each named its own charter: {[c['cmd'][-1] for c in calls]}")
ok(all(c["cmd"][-2] == "--dangerously-skip-permissions" for c in calls),
   f"the opening prompt is a trailing positional, AFTER the last option (ba6a264): {calls[0]['cmd']}")
ok(all(c["env"].get("DOIT_SUPERVISED") == "1" for c in calls),
   "every supervised child carries the marker the pane reads to end itself")
stems = [c["env"]["DOIT_LEDGER_FILE"] for c in calls]
ok(len(set(stems)) == 2 and all(s.startswith("L-planner-") for s in stems),
   f"each Planner writes as itself and never over the last one's file (D90): {stems}")
ok("execvpe" not in inspect.getsource(up), "the launcher never replaces itself with the pane — a "
                                           "replaced process cannot notice the pane exit (L-adr-0026)")
ok(named_ok(calls), f"AC2/R12: every dispatched Planner pane is named its own ledger stem, right "
                    f"after claude, agreeing with its own DOIT_LEDGER_FILE: {[c['cmd'][:3] for c in calls]}")

# ── AC12 · print-only is what it was: no relay, no child, no event, no L-up ───
sup_root("ac12")
seen = []
sup_relay(**{f: (lambda *a, _f=f, **k: seen.append(_f)) for f in up.RELAY_FNS})
calls = sup_runs()
cmd1, env1 = up.main(print_only=True)
cmd2, env2 = up.main(print_only=True)
ok(env1["DOIT_LEDGER_FILE"] == "L-planner-0001.jsonl" and env2["DOIT_LEDGER_FILE"] == "L-planner-0002.jsonl",
   f"print-only still allocates one fresh pane file per call: {env1['DOIT_LEDGER_FILE']} {env2['DOIT_LEDGER_FILE']}")
ok(cmd1 == up.pane_cmd(name="L-planner-0001") and cmd2 == up.pane_cmd(name="L-planner-0002"),
   f"AC2: the print-only branch now names the pane it would print, from its own ledger stem: {cmd1}")
ok(cmd1[1:3] == ["-n", "L-planner-0001"] and cmd2[1:3] == ["-n", "L-planner-0002"],
   f"AC2: -n immediately after claude, naming each printed pane its own stem: {cmd1[:3]} {cmd2[:3]}")
ok(env1["PATH"].split(":")[0] == str(up.HERE.parent), "`doit` still resolves inside the pane")
ok(not seen and not calls, f"print-only calls no relay function and starts nothing: {seen}")
ok(not list(fold.EVENTS.glob("L-up-*.jsonl")), "print-only allocates no launcher file")
ok(all(p.stat().st_size == 0 for p in fold.EVENTS.glob("*.jsonl")), "and appends no event anywhere")
sys.modules["relay"] = None                  # unimportable: see AC7's note below
cmd3, env3 = up.main(print_only=True)
ok(cmd3 == up.pane_cmd(name="L-planner-0003") and env3["DOIT_LEDGER_FILE"] == "L-planner-0003.jsonl",
   "with relay unimportable — the state this unit was written against — print-only is unchanged, "
   "and the relay-unimportable case still names the pane (AC2)")
ok(cmd3[1:3] == ["-n", "L-planner-0003"], "AC2: naming does not depend on relay being importable")

# ── AC3/AC13 · restart once, then escalate ON THE CHARTER, in the launcher's file
# ★ measured 2026-09-23 (0187 Assumption 2, re-measured post-rebase onto main
# tip 3b19b3f): `dispatch.emit` gates every append on `fold.required_reason`
# (R3/L-spec-0192), but `up._charter_pass`'s escalation-blocking call is NOT
# the malformed write Assumption 2 flagged as "flagged, not fixed, here" —
# L-spec-0188 (merged ahead of this unit on main) already closed that exact
# gap, adding `irreversible=` to this same call site (its own comment: "fixed
# here because it otherwise blocks this file from ever reaching exit 0").
# `escalation_ok` reads `irreversible=` as sufficient (no default=/deadline=/
# revert= required for an irreversible act), so the write is well-formed and
# lands, not refused. This fixture is updated to assert the CURRENT,
# well-formed-and-landed behavior rather than the pre-fix refusal it was
# written against.
sup_root("ac3")


def sup_attempts(events, charter_id=None, **k):
    return ({"attempts": 2, "last_reason": "exit-1", "next": "escalate"} if charter_id == "L-charter-0001"
            else {"attempts": 0, "last_reason": "", "next": "start"})


sup_relay(plannable=lambda *a, **k: (["L-charter-0001", "L-charter-0002"], []), planner_attempts=sup_attempts)
calls = sup_runs()
up.main(max_cycles=1)
esc = [e for e in sup_rows() if e["type"] == "escalation-blocking"]
ok(len(esc) == 1 and esc[0]["subject"] == "L-charter-0001" and "irreversible" in esc[0]
   and not all(k in esc[0] for k in ("default", "deadline", "revert")),
   f"_charter_pass's escalation-blocking now carries irreversible= (L-spec-0188's fix), so "
   f"fold.required_reason accepts it and it lands, well-formed, on the launcher's own file: {esc}")
ok([c["cmd"][-1] for c in calls] == ["L-charter-0002"],
   f"the escalated charter starts nothing this cycle and the next ready charter still starts: {calls}")
ups = list(fold.EVENTS.glob("L-up-[0-9][0-9][0-9][0-9].jsonl"))
ok(len(ups) == 1, f"one L-up-NNNN.jsonl per run, allocated lazily and reused: {ups}")
evs = fold.read_events()
ok(len([e for e in evs if e["type"] == "escalation-blocking"]) == 1,
   "the one escalation-blocking is on the whole ledger too — the same append, not a second one")

# ── AC4 · the end row only where the child wrote none ─────────────────────────
sup_root("ac4-crash")
sup_relay(plannable=lambda *a, **k: (["L-charter-0001"], []))
calls = sup_runs(rc=1)
up.main(max_cycles=1)
ends = [e for e in sup_rows(calls[0]["ledger"]) if e["type"] == "planner-ended"]
ok(len(ends) == 1 and ends[0]["reason"] == "exit-1" and ends[0]["mode"] == "charter",
   f"a pane that dies writing nothing still leaves the failed-attempt record: {ends}")
sup_root("ac4-clean")
sup_relay(plannable=lambda *a, **k: (["L-charter-0002"], []))
calls = sup_runs(write=lambda led: dispatch.emit(led, {"spawn": led.stem}, "planner-ended",
                                                 subject="L-charter-0002", planner=led.stem,
                                                 mode="charter", reason="l1-complete"))
up.main(max_cycles=1)
ends = [e for e in sup_rows(calls[0]["ledger"]) if e["type"] == "planner-ended"]
ok(len(ends) == 1 and ends[0]["reason"] == "l1-complete",
   f"and the pane's own clean end row is never doubled by the launcher's: {ends}")

# ── AC5 · planner-started lands in the child's own file BEFORE the child runs ─
sup_root("ac5")
sup_relay(plannable=lambda *a, **k: (["L-charter-0007"], []),
          planner_attempts=lambda *a, **k: {"attempts": 1, "last_reason": "exit-2", "next": "retry"})
calls = sup_runs()
up.main(max_cycles=1)
led = calls[0]["ledger"]
st = [e for e in sup_rows(led) if e["type"] == "planner-started"]
ok(len(st) == 1 and st[0]["subject"] == "L-charter-0007" and st[0]["planner"] == led.stem
   and st[0]["attempt"] == 2 and st[0]["mode"] == "charter",
   f"the start row names charter, planner, attempt and mode, in the child's own file: {st}")
ok(calls[0]["started"], "and it is on disk before the child starts — a pane that dies in its "
                        "first second has still been recorded as started (L-adr-0027)")

# ── AC6 · a dry queue holds the loop open and says what it is waiting on ──────
sup_root("ac6")
WAIT = ["PLANNER WAITING ON", "  L-charter-0003 · after: L-charter-0002"]
sup_relay(waiting_lines=lambda *a, **k: WAIT)
calls = sup_runs()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    cycles = up.main(max_cycles=3)
out = buf.getvalue().splitlines()
ok(cycles == 3 and not calls, "an empty queue holds the loop open and starts nothing; it never ends it (R6)")
ok([l for l in out if l in WAIT] == WAIT,
   f"the board's waiting lines are the pane's, verbatim and in order (L-adr-0032): {[l for l in out if l in WAIT]}")
ok(out.count(WAIT[0]) == 1, "and unchanged waiting is not re-printed — a dry night does not scroll the pane")
os.environ["DOIT_SUPERVISOR_INTERVAL_SEC"] = "600"
sup_relay(waiting_lines=lambda *a, **k: WAIT, ledger_changed=lambda *a, **k: (True, 7))
t0 = clock.monotonic()
with contextlib.redirect_stdout(io.StringIO()):
    up.main(max_cycles=2)
ok(clock.monotonic() - t0 < 5, "a ledger write re-derives immediately instead of waiting out the interval")
os.environ["DOIT_SUPERVISOR_INTERVAL_SEC"] = "0"

# ── AC7 · a relay failure degrades to one named line and starts nothing ───────
# relay-queries LANDED on main while this unit was in flight — `src/relay.py` exists
# as of this rebase, where A10 measured it absent. So absence is simulated at the
# import layer rather than read off the disk: `None` in sys.modules is exactly the
# ImportError `import relay` raises when the file is not there, and R5 names the
# EXCEPTION, never the missing file. The guard has to hold either way, or the first
# root that ships without a sibling wedges the launcher.
sup_root("ac7-absent")
sys.modules["relay"] = None
calls = sup_runs()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    up.main(max_cycles=2)
ok(("ImportError" in buf.getvalue() or "ModuleNotFoundError" in buf.getvalue()) and not calls
   and not any(p.stat().st_size for p in fold.EVENTS.glob("*.jsonl")),
   f"with src/relay.py absent the loop names the exception and starts nothing: {buf.getvalue()!r}")
for fn in up.RELAY_FNS:
    sup_root("ac7-" + fn)
    conf = {fn: sup_boom}
    if fn == "planner_attempts":
        conf["plannable"] = lambda *a, **k: (["L-charter-0001"], [])
    sup_relay(**conf)
    calls = sup_runs()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        up.main(max_cycles=1)
    ok("RuntimeError" in buf.getvalue() and not calls
       and not any(p.stat().st_size for p in fold.EVENTS.glob("*.jsonl")),
       f"relay.{fn} raising is caught, named, and starts no pane of either mode: {buf.getvalue()!r}")

# ── AC8 · the serving pass, and charter work's priority over it ───────────────
sup_root("ac8")
PEND = [{"spawn": "L-builder-0009", "age": 3}, {"spawn": "L-grader-0002", "age": 1}]
IDS = "L-builder-0009,L-grader-0002"
sup_relay(pending_packets=lambda *a, **k: PEND)
calls = sup_runs()
up.main(max_cycles=1)
ok(len(calls) == 1 and calls[0]["cmd"][-1] == IDS,
   f"a pending packet is served on the FIRST cycle with nothing plannable: {calls}")
rows = sup_rows(calls[0]["ledger"])
ok([e["mode"] for e in rows] == ["serving", "serving"] and rows[0]["subject"] == "serving:" + IDS
   and rows[0]["spawn_ids"] == IDS and rows[0]["attempt"] == 1,
   f"both rows carry mode=serving, the serving subject and the sorted ids: {rows}")
ok(rows[-1]["type"] == "planner-ended" and rows[-1]["spawn_ids"] == IDS and rows[-1]["reason"] == "exit-0",
   f"and the end row carries the ids the next cycle's already-attempted check reads: {rows[-1]}")
ok(named_ok(calls), f"AC2: the serving pass names its pane too — it reuses _start's own naming: "
                    f"{calls[0]['cmd'][:3]}")
sup_root("ac8-priority")
sup_relay(plannable=lambda *a, **k: (["L-charter-0005"], []), pending_packets=lambda *a, **k: PEND)
calls = sup_runs()
up.main(max_cycles=1)
ok(len(calls) == 1 and calls[0]["cmd"][-1] == "L-charter-0005"
   and all(e["mode"] == "charter" for e in sup_rows(calls[0]["ledger"])),
   f"a ready charter pre-empts the serving pass entirely that cycle (L-adr-0029): {calls}")

# ── AC9 · at most one serving pass per spawn id, and the skip is said out loud ─
sup_root("ac9")
seed = fold.EVENTS / "L-planner-0001.jsonl"
dispatch.emit(seed, {"spawn": seed.stem}, "planner-ended", subject="serving:L-builder-0009",
              planner=seed.stem, mode="serving", spawn_ids="L-builder-0009", reason="exit-0")
sup_relay(pending_packets=lambda *a, **k: [{"spawn": "L-builder-0009"}])
calls = sup_runs()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    up.main(max_cycles=1)
ok(not calls, f"a spawn a serving pass already took is never taken a second time: {calls}")
ok("L-builder-0009" in buf.getvalue() and "skipped" in buf.getvalue(),
   f"and the skip is printed, never a silent drop: {buf.getvalue()!r}")

# ── AC14 · the other half of this file belongs to L-spec-0045 (A2) ────────────
# A2 called the merge order and it held the other way round: L-spec-0045 landed on
# main first, so its additive functions are HERE already and this unit rebased onto
# them. The boundary is the same boundary — this spec wrote none of them, calls none
# of them, and left every one intact and callable above.
ok(all(callable(getattr(up, f, None)) for f in ("executor_loop", "executor_prompt",
                                                "executor_deny_list", "quiet_point")),
   "L-spec-0045's additive functions survive this spec's rebase intact, none rewritten here")
ok(not hasattr(up, "pane_name"),
   "and `pane_name` is still a sibling's Produces — imported, never redefined in up.py (AC1 of 0045)")
ok("Bash(git commit:*)" in up.executor_deny_list()
   and "L-executor-0099" in up.executor_prompt("L-executor-0099.jsonl", "B"),
   "the Executor launcher's own shape is untouched by the supervisor loop that now sits above it")
ok("-n" not in up.pane_cmd() and "-n" not in up.pane_cmd("L-charter-0001"),
   f"and no pane name is added when `name` is omitted from either call — AC1's name=None default "
   f"is byte-identical to today's argv; R12 itself is what the two named call sites above rely "
   f"on: {up.pane_cmd('x')}")
ok(callable(up.cron_line) and callable(up.install) and up.AGENTS_HOME.name == "claude-agents",
   "cron_line, install and AGENTS_HOME are untouched by this spec")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0187 · executor-runs-unattended (L-charter-0028) — R3
# ══════════════════════════════════════════════════════════════════════════════

# 0187-AC13: the wake-and-end procedure is in the pane's own seed, greppably.
prompt13 = up.executor_prompt("L-executor-0187.jsonl", "BOARD")
ok("doit wait --max 300" in prompt13,
   f"0187-AC13: the seed names the background wake command: {prompt13[:200]!r}")
ok("doit pane-end --executor" in prompt13,
   f"0187-AC13: the seed names the real end command, never a bare stop: {prompt13[:200]!r}")

# 0187: `guard.registered` is late-bound exactly like `pane_name` — missing or
# False degrades to one stderr line and the loop still runs to completion.
up.GUARD_REGISTERED = lambda: False
buf_g = io.StringIO()
with contextlib.redirect_stderr(buf_g):
    up._check_guard()
ok("not registered" in buf_g.getvalue() or "unguarded" in buf_g.getvalue(),
   f"0187: a False registration degrades to one stderr line, never a block: {buf_g.getvalue()!r}")
up.GUARD_REGISTERED = lambda: True
buf_g2 = io.StringIO()
with contextlib.redirect_stderr(buf_g2):
    up._check_guard()
ok(buf_g2.getvalue() == "", f"0187: a True registration prints nothing: {buf_g2.getvalue()!r}")
up.GUARD_REGISTERED = None
real_guard_module = sys.modules.get("guard")
sys.modules["guard"] = None       # simulates the seam absent (wave 1 not merged), like AC7's `relay`
buf_g3 = io.StringIO()
with contextlib.redirect_stderr(buf_g3):
    up._check_guard()
ok("guard.registered" in buf_g3.getvalue() and "unguarded" in buf_g3.getvalue(),
   f"0187: a missing seam (wave 1 not merged) degrades the same way, never sys.exit: {buf_g3.getvalue()!r}")
if real_guard_module is not None:
    sys.modules["guard"] = real_guard_module
else:
    del sys.modules["guard"]
# ── end L-spec-0031 fixtures ─────
print(f"up: {n} checks pass")
