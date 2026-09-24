#!/usr/bin/env python3
"""Checks on `src/launch.py` and its wiring into `up.py`'s three standing
loops (L-charter-0036 R2, L-spec-0320). Run: python3 test_launch.py

Every AC below is the exact marker `./doit test`'s reviewer greps for under
`PASS test_launch.py`. AC15 is `[observed-data, owed]` — not buildable from a
worktree alone (Declarations) — and has no block here.
"""
import json, os, pathlib, shutil, subprocess, sys, tempfile, textwrap, time, types

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"] = str(TMP)
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import dispatch, fold, launch, models, up  # noqa: E402

n = 0


def ok(cond, why):
    global n
    assert cond, why
    n += 1


DOIT = str(pathlib.Path(up.HERE.parent) / "doit")

# ══════════════════════════════════════════════════════════════════════════
# AC1 — child_env strips exactly the configured names, per role, and never
# mutates or aliases its `base`.
# ══════════════════════════════════════════════════════════════════════════
FIVE = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GH_TOKEN", "GITHUB_TOKEN")
FIXTURE_BASE = {**{k: f"secret-{k}" for k in FIVE}, "PATH": "/usr/bin:/bin", "HOME": "/fake/home"}
relay_env = launch.child_env("relay", base=FIXTURE_BASE)
ok(relay_env is not FIXTURE_BASE, f"AC1: child_env returns a NEW dict: {relay_env is FIXTURE_BASE}")
ok(all(k not in relay_env for k in FIVE), f"AC1: relay strips all five names: {relay_env}")
ok(relay_env["PATH"] == "/usr/bin:/bin" and relay_env["HOME"] == "/fake/home",
   f"AC1: PATH/HOME pass through untouched: {relay_env}")
ok(relay_env["GH_CONFIG_DIR"] == "/fake/home/.config/gh-none",
   f"AC1: GH_CONFIG_DIR expands $HOME against base, never real os.environ: {relay_env}")
ok(FIXTURE_BASE["CLAUDE_CODE_OAUTH_TOKEN"] == "secret-CLAUDE_CODE_OAUTH_TOKEN",
   "AC1: base is never mutated")
planner_env = launch.child_env("planner", base=FIXTURE_BASE)
ok(all(k not in planner_env for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")),
   f"AC1: planner strips its own three: {planner_env}")
ok("GH_TOKEN" in planner_env and "GITHUB_TOKEN" in planner_env,
   f"AC1: planner does NOT strip relay's extra two: {planner_env}")
print("AC1 ok")

# ══════════════════════════════════════════════════════════════════════════
# AC2 — no base given, defaults to (a copy of) os.environ, and still strips.
# ══════════════════════════════════════════════════════════════════════════
os.environ["ANTHROPIC_API_KEY"] = "leaked-into-ambient-shell"
env2 = launch.child_env("planner")
ok("ANTHROPIC_API_KEY" not in env2, f"AC2: default base still strips a monkeypatched os.environ: {env2.get('ANTHROPIC_API_KEY')}")
del os.environ["ANTHROPIC_API_KEY"]
print("AC2 ok")

# ══════════════════════════════════════════════════════════════════════════
# AC3 — an unknown role raises ValueError, naming it.
# ══════════════════════════════════════════════════════════════════════════
try:
    launch.child_env("no-such-role")
    ok(False, "AC3: an unknown role must raise")
except ValueError as e:
    ok("no-such-role" in str(e), f"AC3: names the unknown role: {e}")
print("AC3 ok")

# ══════════════════════════════════════════════════════════════════════════
# AC4/AC5/AC6 — record()/ended() write role-launched/role-ended, the eleven
# base fields, caller-gated model_configured, and fold's derived actor.
# ══════════════════════════════════════════════════════════════════════════
BASE_FIELDS = {"role", "pane", "pid", "host", "proc_start", "model",
               "account", "stripped", "rules", "rules_sha256", "tmux_pane"}
child4 = subprocess.Popen(["sh", "-c", "exit 0"])
pid4 = child4.pid
child4.wait()
rules4 = TMP / "fixture-rules.md"
rules4.write_text("rules body\n")
events4 = TMP / "ac4-events"
entry4 = launch.record("planner", "L-planner-0099", pid4, "claude-sonnet-5", str(rules4), events_dir=events4)
ok(BASE_FIELDS <= set(entry4) and "model_configured" not in entry4,
   f"AC4: record's return carries all eleven base fields and no model_configured: {entry4}")
lines4 = (events4 / "L-launch-local.jsonl").read_text().splitlines()
ok(len(lines4) == 1, f"AC4: exactly one line appended: {lines4}")
row4 = json.loads(lines4[0])
ok(row4["type"] == "role-launched" and BASE_FIELDS <= set(row4) and "model_configured" not in row4,
   f"AC4: the appended LINE carries the eleven fields, no model_configured: {row4}")
saved_root, saved_events = fold.ROOT, fold.EVENTS
fold.ROOT, fold.EVENTS = events4.parent, events4
derived = fold.read_events()
fold.ROOT, fold.EVENTS = saved_root, saved_events
ok(any(e["type"] == "role-launched" and e["actor"] == "launch" for e in derived),
   f"AC4: fold.read_events() derives actor 'launch' from the filename alone (D90): {derived}")
print("AC4 ok")

os.environ["ANTHROPIC_API_KEY"], os.environ["SOME_OTHER_TOKEN"] = "x", "y"
entry5 = launch.record("planner", "L-planner-0100", pid4, "m", str(rules4), events_dir=events4)
ok(entry5["stripped"] == ["ANTHROPIC_API_KEY"],
   f"AC5: only the configured AND present name lists, never the unconfigured one: {entry5['stripped']}")
del os.environ["ANTHROPIC_API_KEY"], os.environ["SOME_OTHER_TOKEN"]
print("AC5 ok")

ended6 = launch.ended("L-planner-0099", 0, events_dir=events4)
ok(ended6 == {"pane": "L-planner-0099", "rc": 0}, f"AC6: ended() returns exactly pane/rc: {ended6}")
last6 = json.loads((events4 / "L-launch-local.jsonl").read_text().splitlines()[-1])
ok(last6["type"] == "role-ended" and last6["pane"] == "L-planner-0099" and last6["rc"] == 0,
   f"AC6: the appended line is role-ended, same file: {last6}")
print("AC6 ok")

# ══════════════════════════════════════════════════════════════════════════
# AC7 — account()'s four undetermined fixtures for planner/relay/executor.
# ══════════════════════════════════════════════════════════════════════════
real_cfg = launch.CLAUDE_CONFIG
CFG = TMP / "claude-config"
CFG.mkdir()
launch.CLAUDE_CONFIG = CFG / "missing.json"
ok(launch.account("planner") == "undetermined", "AC7: absent CLAUDE_CONFIG -> undetermined")
unreadable = CFG / "unreadable.json"
unreadable.mkdir()
launch.CLAUDE_CONFIG = unreadable
ok(launch.account("relay") == "undetermined", "AC7: unreadable (a directory) -> undetermined")
malformed = CFG / "malformed.json"
malformed.write_text("{not json")
launch.CLAUDE_CONFIG = malformed
ok(launch.account("executor") == "undetermined", "AC7: malformed JSON -> undetermined")
missing_key = CFG / "missing-key.json"
missing_key.write_text(json.dumps({"oauthAccount": {}}))
launch.CLAUDE_CONFIG = missing_key
ok(launch.account("planner") == "undetermined", "AC7: missing emailAddress key -> undetermined")
good = CFG / "good.json"
good.write_text(json.dumps({"oauthAccount": {"emailAddress": "a@b.example"}}))
launch.CLAUDE_CONFIG = good
ok(launch.account("relay") == "a@b.example", "AC7 (sanity): a clean read returns the email, never raises")
launch.CLAUDE_CONFIG = real_cfg
print("AC7 ok")

# ══════════════════════════════════════════════════════════════════════════
# AC8 — account("codex"), run_codex_status stubbed six ways.
# ══════════════════════════════════════════════════════════════════════════
real_status = launch.run_codex_status
launch.run_codex_status = lambda: (0, "", "someone@example.com\n")
ok(launch.account("codex") == "someone@example.com", "AC8: stdout empty, the account on stderr")
launch.run_codex_status = lambda: (0, "first@stdout.example\n", "second@stderr.example\n")
ok(launch.account("codex") == "first@stdout.example", "AC8: stdout-first ordering")
launch.run_codex_status = lambda: (1, "someone@example.com\n", "")
ok(launch.account("codex") == "undetermined", "AC8: non-zero exit code -> undetermined")
launch.run_codex_status = lambda: (0, "  \n", " \n")
ok(launch.account("codex") == "undetermined", "AC8: an all-blank combined stream -> undetermined")


def _raise_fnf():
    raise FileNotFoundError("codex not on PATH")


launch.run_codex_status = _raise_fnf
ok(launch.account("codex") == "undetermined", "AC8: FileNotFoundError -> undetermined, never raises")
launch.run_codex_status = real_status
print("AC8 ok")

# ══════════════════════════════════════════════════════════════════════════
# AC9 — a real subprocess: a missing rules file refuses before any subprocess
# or ledger write, and names the missing path.
# ══════════════════════════════════════════════════════════════════════════
AC9_DIR = TMP / "ac9"
AC9_DIR.mkdir()
missing_rules = AC9_DIR / "nope.md"
(AC9_DIR / "launch.toml").write_text(f'[roles.codex]\nstrip = []\nrules = "{missing_rules}"\n')
AC9_ROOT = AC9_DIR / "root"
(AC9_ROOT / "events").mkdir(parents=True)
env9 = {**os.environ, "DOIT_LAUNCH_TOML": str(AC9_DIR / "launch.toml"), "DOIT_ROOT": str(AC9_ROOT)}
p9 = subprocess.run([DOIT, "launch", "codex"], capture_output=True, text=True, env=env9)
ok(p9.returncode != 0, f"AC9: refuses before starting anything: rc={p9.returncode} {p9.stderr}")
ok(str(missing_rules) in (p9.stdout + p9.stderr), f"AC9: names the missing path: {p9.stdout}{p9.stderr}")
ok(not (AC9_ROOT / "events" / "L-launch-local.jsonl").exists(),
   "AC9: zero new lines on L-launch-local.jsonl — no ledger write before the refusal")
print("AC9 ok")

# ══════════════════════════════════════════════════════════════════════════
# AC10 — a real subprocess: Popen (never exec), role-launched before the
# fixture child exits, role-ended with its rc, and doit's own exit code.
# ══════════════════════════════════════════════════════════════════════════
AC10_DIR = TMP / "ac10"
(AC10_DIR / "bin").mkdir(parents=True)
rules10 = AC10_DIR / "rules.md"
rules10.write_text("codex rules\n")
cwd10 = AC10_DIR / "cwd"
cwd10.mkdir()
(AC10_DIR / "launch.toml").write_text(
    f'[roles.codex]\nstrip = []\nrules = "{rules10}"\ncwd = "{cwd10}"\n')
fixture_codex = AC10_DIR / "bin" / "codex"
# `record()`'s own `account("codex")` shells out to `codex login status` too —
# on this fixture PATH that is the SAME binary. Answer that invocation fast
# (its own value is irrelevant here) so it never competes with the timing
# this AC is actually about: the MAIN launch's own sleep-then-exit-3.
fixture_codex.write_text('#!/bin/sh\ncase "$1 $2" in\n  "login status") exit 1 ;;\nesac\n'
                          'sleep 0.3\nexit 3\n')
fixture_codex.chmod(0o755)
AC10_ROOT = AC10_DIR / "root"
(AC10_ROOT / "events").mkdir(parents=True)
env10 = {**os.environ, "DOIT_LAUNCH_TOML": str(AC10_DIR / "launch.toml"),
         "DOIT_ROOT": str(AC10_ROOT), "PATH": f"{AC10_DIR / 'bin'}:{os.environ.get('PATH', '')}"}
proc10 = subprocess.Popen([DOIT, "launch", "codex"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=env10, text=True)
own_pid10 = proc10.pid
ledger10 = AC10_ROOT / "events" / "L-launch-local.jsonl"
seen_before_exit, deadline = False, time.time() + 2
while proc10.poll() is None and time.time() < deadline:
    if ledger10.exists() and ledger10.read_text().strip():
        seen_before_exit = True
        break
    time.sleep(0.02)
proc10.communicate()
ok(seen_before_exit, "AC10: role-launched lands before the fixture script exits")
ok(proc10.returncode == 3, f"AC10: doit launch codex's own exit code is the child's rc: {proc10.returncode}")
rows10 = [json.loads(l) for l in ledger10.read_text().splitlines()]
ok([r["type"] for r in rows10] == ["role-launched", "role-ended"],
   f"AC10: role-launched then role-ended, in order: {rows10}")
ok(rows10[0]["pid"] != own_pid10,
   f"AC10: the recorded pid is the REAL forked child, never doit-launch-codex's own (Popen, not exec): "
   f"{rows10[0]['pid']} vs {own_pid10}")
ok(rows10[1]["rc"] == 3, f"AC10: role-ended carries the child's own exit code: {rows10[1]}")
print("AC10 ok")

# ══════════════════════════════════════════════════════════════════════════
# AC11 — up._start / up.relay_main / up.executor_loop, hermetic: the child
# never inherits the stripped credential; role-launched/-ended land right
# around the real Popen call, alongside unchanged pane-bookkeeping; and
# executor_loop's own `root` — distinct from fold.EVENTS — is where they land.
# ══════════════════════════════════════════════════════════════════════════
AGENTS11 = TMP / "agents11"
AGENTS11.mkdir()
real_agents_dir = pathlib.Path(up.HERE.parent) / "agents"
for role in ("planner", "relay", "executor"):
    shutil.copyfile(real_agents_dir / f"{role}.md", AGENTS11 / f"{role}.md")
dispatch.AGENTS = AGENTS11
up.AGENTS_HOME = TMP / "claude-agents11"
BIN11 = TMP / "bin11"
BIN11.mkdir()
STUB11 = BIN11 / "claude"
STUB11.write_text(textwrap.dedent("""\
    #!/bin/sh
    printf '%s\\n' "$*" >> "$DOIT_ROOT/argv.log"
    if env | grep -q '^ANTHROPIC_API_KEY='; then echo LEAKED > "$DOIT_ROOT/env-check.log"
    else echo CLEAN > "$DOIT_ROOT/env-check.log"; fi
    printf '{"v":1,"ts":"2026-09-24T00:00:00+00:00","type":"message-sent","subject":"%s"}\\n' \\
      "${DOIT_LEDGER_FILE%.jsonl}" >> "$DOIT_ROOT/events/$DOIT_LEDGER_FILE"
    sleep 0.1
    exit 0
    """))
STUB11.chmod(0o755)
os.environ["PATH"] = f"{BIN11}:{os.environ['PATH']}"
os.environ["ANTHROPIC_API_KEY"] = "should-never-reach-the-child"


def ac_root(name):
    r = TMP / name
    (r / "events").mkdir(parents=True, exist_ok=True)
    (r / "logs").mkdir(parents=True, exist_ok=True)
    fold.ROOT, fold.EVENTS, dispatch.EVENTS = r, r / "events", r / "events"
    os.environ["DOIT_ROOT"] = str(r)
    return r


r11a = ac_root("ac11-start")
up._start({"up": None}, subject="L-charter-9999", mode="charter", attempt=1, prompt="L-charter-9999")
ok((r11a / "env-check.log").read_text().strip() == "CLEAN",
   "AC11: _start's own child never inherits ANTHROPIC_API_KEY")
rows11a = [json.loads(l) for l in (r11a / "events" / "L-launch-local.jsonl").read_text().splitlines()]
ok([r["type"] for r in rows11a] == ["role-launched", "role-ended"] and rows11a[0]["role"] == "planner"
   and isinstance(rows11a[0]["pid"], int),
   f"AC11: _start appends role-launched (real pid) then role-ended: {rows11a}")
plannerfile = next((r11a / "events").glob("L-planner-*.jsonl"))
ok([json.loads(l)["type"] for l in plannerfile.read_text().splitlines()]
   == ["planner-started", "message-sent", "planner-ended"],
   "AC11: the pane's own ledger file — planner-bookkeeping — is unchanged in shape/count")

r11b = ac_root("ac11-relay")
_relay_mod = types.ModuleType("relay")
_relay_mod.pending_packets = lambda *a, **k: [{"spawn": "L-builder-0009", "age_min": 1}]
sys.modules["relay"] = _relay_mod
up.relay_main(print_only=False, max_cycles=1)
ok((r11b / "env-check.log").read_text().strip() == "CLEAN",
   "AC11: relay_main's own child never inherits ANTHROPIC_API_KEY")
rows11b = [json.loads(l) for l in (r11b / "events" / "L-launch-local.jsonl").read_text().splitlines()]
ok([r["type"] for r in rows11b] == ["role-launched", "role-ended"] and rows11b[0]["role"] == "relay",
   f"AC11: relay_main appends role-launched/-ended for the relay role: {rows11b}")
relayfile = next((r11b / "events").glob("L-relay-*.jsonl"))
ok([json.loads(l)["type"] for l in relayfile.read_text().splitlines()]
   == ["spawn-started", "message-sent", "spawn-done"],
   "AC11: relay's own pane-bookkeeping is unchanged in shape/count")

EXEC11_FOLD = ac_root("ac11-executor-foldroot")     # fold.EVENTS, elsewhere on purpose
EXEC11_ROOT = TMP / "ac11-executor-root"
(EXEC11_ROOT / "events").mkdir(parents=True)
(EXEC11_ROOT / "logs").mkdir(parents=True)
up.executor_loop(EXEC11_ROOT, 5, max_cycles=1)
ok((EXEC11_ROOT / "env-check.log").read_text().strip() == "CLEAN",
   "AC11: executor_loop's own child never inherits ANTHROPIC_API_KEY")
launch_file11 = EXEC11_ROOT / "events" / "L-launch-local.jsonl"
ok(launch_file11.exists(), f"AC11 (finding 5): role-launched/-ended land under root/events: {EXEC11_ROOT}")
ok(not (EXEC11_FOLD / "events" / "L-launch-local.jsonl").exists(),
   "AC11 (finding 5): never under fold.EVENTS's own default when it differs from root")
rows11c = [json.loads(l) for l in launch_file11.read_text().splitlines()]
ok([r["type"] for r in rows11c] == ["role-launched", "role-ended"] and rows11c[0]["role"] == "executor",
   f"AC11: executor_loop appends role-launched/-ended for the executor role: {rows11c}")
execfile = next((EXEC11_ROOT / "events").glob("L-executor-*.jsonl"))
exectypes = [json.loads(l)["type"] for l in execfile.read_text().splitlines()]
ok(exectypes == ["spawn-started", "message-sent", "spawn-done"],
   f"AC11: the executor pane's own bookkeeping (SAME dir as role-launched) is unchanged: {exectypes}")
del os.environ["ANTHROPIC_API_KEY"]
print("AC11 ok")

# ══════════════════════════════════════════════════════════════════════════
# AC12 — pane_cmd/relay_pane_cmd/_pane_argv: model=None unchanged, model=<x>
# inserted right after --agent <role>.
# ══════════════════════════════════════════════════════════════════════════
ok("--model" not in up.pane_cmd(), f"AC12: pane_cmd() default carries no --model: {up.pane_cmd()}")
wm = up.pane_cmd(model="claude-sonnet-5")
i = wm.index("--agent")
ok(wm[i:i + 4] == ["--agent", "planner", "--model", "claude-sonnet-5"], f"AC12: planner insertion point: {wm}")
ok("--model" not in up.relay_pane_cmd(), f"AC12: relay_pane_cmd() default: {up.relay_pane_cmd()}")
wmr = up.relay_pane_cmd(model="claude-opus-5")
ir = wmr.index("--agent")
ok(wmr[ir:ir + 4] == ["--agent", "relay", "--model", "claude-opus-5"], f"AC12: relay insertion point: {wmr}")
bare_e = up._pane_argv("L-executor-0001.jsonl", "BOARD")
ok("--model" not in bare_e, f"AC12: _pane_argv default carries no --model: {bare_e[:8]}")
wme = up._pane_argv("L-executor-0001.jsonl", "BOARD", model="claude-fable-1")
ie = wme.index("--agent")
ok(wme[ie:ie + 4] == ["--agent", "executor", "--model", "claude-fable-1"], f"AC12: executor insertion point: {wme[:8]}")
print("AC12 ok")

# ══════════════════════════════════════════════════════════════════════════
# AC13 — model_for never falls back to models.toml, ever.
# ══════════════════════════════════════════════════════════════════════════
AC13_DIR = TMP / "ac13"
AC13_DIR.mkdir()
(AC13_DIR / "launch.toml").write_text('[roles.executor]\nstrip = []\nrules = "x.md"\n')
os.environ["DOIT_LAUNCH_TOML"] = str(AC13_DIR / "launch.toml")
ok(launch.model_for("executor") is None,
   "AC13: no model key in launch.toml -> None, regardless of any co-present models.toml")
(AC13_DIR / "launch.toml").write_text('[roles.executor]\nstrip = []\nrules = "x.md"\nmodel = "claude-opus-5"\n')
ok(launch.model_for("executor") == "claude-opus-5", "AC13: with a model key set, model_for returns it")
del os.environ["DOIT_LAUNCH_TOML"]
print("AC13 ok")

# ══════════════════════════════════════════════════════════════════════════
# AC14 — executor_loop, unpinned launch.toml + a models.toml that pins one:
# argv carries NO --model; role-launched reads model=="unpinned" AND
# model_configured==the models.toml pin, both present together.
# ══════════════════════════════════════════════════════════════════════════
AC14_ROOT = TMP / "ac14-root"
(AC14_ROOT / "events").mkdir(parents=True)
(AC14_ROOT / "logs").mkdir(parents=True)
(AC14_ROOT / "models.toml").write_text(
    '[defaults]\nbackend = "pane"\n[contracts.executor]\nbackend = "pane"\nmodel = "claude-sonnet-5"\n')
ac_root("ac14-foldroot")            # fold.EVENTS elsewhere; unrelated to AC14_ROOT
up.executor_loop(AC14_ROOT, 5, max_cycles=1)
argv14 = (AC14_ROOT / "argv.log").read_text()
ok("--model" not in argv14, f"AC14: today's shipped launch.toml (no model for executor) inserts no --model: {argv14!r}")
rows14 = [json.loads(l) for l in (AC14_ROOT / "events" / "L-launch-local.jsonl").read_text().splitlines()]
launched14 = rows14[0]
ok(launched14["model"] == "unpinned" and launched14.get("model_configured") == "claude-sonnet-5",
   f"AC14: unpinned wins, but the models.toml pin is visible alongside it, not acted on: {launched14}")
print("AC14 ok")

print(f"launch: {n} checks pass")
