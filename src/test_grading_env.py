#!/usr/bin/env python3
"""One runnable check on grading_env.py's AC1-AC9 (L-spec-0481). Run:
python3 test_grading_env.py

Before importing `grading_env`, this sets DOIT_ROOT to a fresh temp
directory and DOIT_GRADING_TOML to a fixture `grading.toml` — never the
live `$R`, never the real repo-root `grading.toml`. A fixture project
checkout (`node_modules`, `.venv`) and a fixture `look.toml` (for the
`deploy` capability) live under `DOIT_ROOT/repos/testproj`.

R8.2 (this spec): HOME and DOIT_SCRATCH are ALSO fixture-isolated, before
`import grading_env` — AC10's real `grader_view.bwrap_argv(...)` call (even
with `grading_env._run` stubbed) still runs `grader_view.config_dir()` for
its argv, which writes real files under `$HOME/doit-scratch/grader-claude`
by default; a direct `python3 test_grading_env.py`, run exactly as the
verify chain runs it, must never write there for real.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
DOIT_SRC = HERE.parent

FIXTURE_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="grading-env-root-"))
os.environ["DOIT_ROOT"] = str(FIXTURE_ROOT)
FIXTURE_HOME = pathlib.Path(tempfile.mkdtemp(prefix="grading-env-home-"))
os.environ["HOME"] = str(FIXTURE_HOME)
FIXTURE_SCRATCH = pathlib.Path(tempfile.mkdtemp(prefix="grading-env-scratch-"))
os.environ["DOIT_SCRATCH"] = str(FIXTURE_SCRATCH)
FIXTURE_TOML = FIXTURE_ROOT / "grading.toml"
os.environ["DOIT_GRADING_TOML"] = str(FIXTURE_TOML)

CONTENT = FIXTURE_ROOT / "content"
CONTENT.mkdir(parents=True, exist_ok=True)
CHECKOUT = FIXTURE_ROOT / "repos" / "testproj"
(CHECKOUT / "dashboard" / "node_modules" / ".bin").mkdir(parents=True, exist_ok=True)
(CHECKOUT / "dashboard" / "node_modules" / ".bin" / "marker").write_text("x")
(CHECKOUT / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
PY3 = CHECKOUT / ".venv" / "bin" / "python3"
PY3.write_text("#!/bin/sh\necho 'Python 3.11.0 (fixture)'\n")
PY3.chmod(0o755)

FIXTURE_TOML.write_text('''
[project.testproj]
db_env = ["SUPABASE_DB_URL", "DATABASE_URL", "PG_PARITY_DB_URL"]
node_dirs = ["dashboard"]
venv = ".venv"

[project.testproj.db_setup]
argv = ["/bin/bash", "-c", "psql \\"$SUPABASE_DB_URL\\" -c 'create table grading_probe(x int);'"]
cwd = "."
''')

REPO = FIXTURE_ROOT / "repo-with-look-toml"
REPO.mkdir(parents=True, exist_ok=True)
(REPO / "look.toml").write_text('''
[[prod]]
project = "testproj"
ssh_target = "example-host"
version_path = "/version"
''')

sys.path.insert(0, str(HERE))
import grading_env  # noqa: E402

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


def write_spec_verify(subject, role, spec_text="", verify_text=""):
    (CONTENT / f"{subject}.md").write_text(spec_text)
    (CONTENT / f"verify-{subject}-{role}.sh").write_text(verify_text)


# ═══════════════════════════ AC1 — required() is pure, ordered, never spawns ═══
def _raising(*a, **kw):
    raise AssertionError("required() must never call a subprocess/system entry point")


_real_run, _real_system = subprocess.run, os.system
subprocess.run, os.system, grading_env._run = _raising, _raising, _raising
try:
    # L-spec-8034/R12.f: `required()` is now per-criterion (`criterion_caps`),
    # not spec-level — a capability found only in spec_text prose OUTSIDE an
    # `AC<n> [` block contributes nothing (Assumption 3). Rows whose signal
    # lives in verify_text alone (an unattributed `&&`-segment always becomes
    # the pseudo-criterion `"verify"`, counted by default) are unaffected; a
    # row whose old signal lived in un-anchored spec_text prose now carries an
    # `AC1 [...]`/`review_path:` anchor so the same intent still counts.
    TABLE = [
        ("grader", "spec", "export .env something", "testproj", ["env-file"]),
        ("grader", "spec", "/opt/albert-scott/x /usr/bin/python3", "testproj", ["view-paths", "tools"]),
        ("grader", "spec", "run python3 script.py", "testproj", ["tools"]),
        ("grader", "spec", "npm run test", "testproj", ["node_modules"]),
        ("grader", "spec", "git log -1", "testproj", ["git"]),
        ("grader", "spec has a live_db criterion", "checks DB_URL too", "testproj", ["db", "db-schema"]),
        ("grader", "AC1 [ui]: browser check.\nreview_path: log in as operator, go to /",
         "verify", "testproj", ["browser", "review-account"]),
        # R12.j: "check deployed state against origin" names neither `/version`
        # nor `deployed at` nor a project base_url — no longer `deploy` (AC12).
        ("grader", "spec", "check deployed state against origin", "testproj", []),
        ("grader", "nothing special here", "nothing special here either", "testproj", []),
        ("reviewer", "anything at all", "anything at all", "testproj", []),
        ("grader", "AC1 [backend]: x\nreview_path: git log -1\nrollback_path: git revert HEAD",
         "/usr/bin/python3 t.py", "testproj", ["view-paths", "tools", "git"]),
    ]
    for role, spec_text, verify_text, project, expect in TABLE:
        got = grading_env.required(role, spec_text, verify_text, project)
        check(got == expect, f"required({role!r}, ...) == {expect}, got {got}")
        # subsequence-of-CAPABILITIES check
        idx = [grading_env.CAPABILITIES.index(c) for c in got]
        check(idx == sorted(idx), f"required() result not in CAPABILITIES order: {got}")
finally:
    subprocess.run, os.system, grading_env._run = _real_run, _real_system, _real_run
print("AC1 ok — required() table:")
for row in TABLE:
    print("  ", row[:2], "->", row[-1])


# ═══════════════════════════ AC2 — CAPABILITIES exact; SPEC_LOCAL; schema enum ═════
EXPECT_CAPS = ("env-file", "view-paths", "tools", "node_modules", "git",
               "db", "db-schema", "browser", "review-account", "deploy")
check(grading_env.CAPABILITIES == EXPECT_CAPS, grading_env.CAPABILITIES)
check(grading_env.SPEC_LOCAL == set(grading_env.CAPABILITIES), grading_env.SPEC_LOCAL)
schema = json.loads((DOIT_SRC / "agents" / "grader.schema.json").read_text())
mc = schema.get("properties", {}).get("missing_capability", {})
if "enum" in mc:
    check(set(mc["enum"]) == set(grading_env.CAPABILITIES) | {"unknown"}, mc["enum"])
    print("AC2 ok")
else:
    print("AC2: grader.schema.json lacks missing_capability (pre-rebase, Assumption 1) "
          "— enum equality not asserted; never passed silently")


# ═══════════════════════════ AC3 — reviewer needs are conditional (SD-R12-2a) ══════
check(grading_env.required("reviewer", "x", "x", "do-it-v2") == [], "AC3: gates-only, no-prod project -> []")
check(grading_env.required("reviewer", "x", "x", "do-it-v2", mcp_config=True) == ["browser"],
      "AC3: mcp_config=True -> browser only")
check(grading_env.required("reviewer", "x", "x", "do-it-v2", mcp_config=True, review_account=True)
      == ["browser", "review-account"], "AC3: + review_account")
check(grading_env.required("reviewer", "x", "x", "albert-scott") == ["deploy"],
      "AC3: albert-scott ([[prod]] row) adds deploy even gates-only")
check(grading_env.required("reviewer", "x", "x", "albert-scott", mcp_config=True, review_account=True)
      == ["browser", "review-account", "deploy"], "AC3: full-depth albert-scott, CAPABILITIES order")
_calls = []
_real_run3 = grading_env._run
grading_env._run = lambda *a, **kw: _calls.append(a) or (_ for _ in ()).throw(AssertionError("deploy proof invoked"))
try:
    view3r = FIXTURE_ROOT / "grade" / "ac3-reviewer"
    (view3r / "tree").mkdir(parents=True, exist_ok=True)
    write_spec_verify("L-spec-9602", "reviewer", "x", "x")
    grading_env.preflight("reviewer", "L-spec-9602", view3r, "do-it-v2", repo=str(REPO))
finally:
    grading_env._run = _real_run3
check(not _calls, "AC3: a do-it-v2 reviewer preflight never invokes the deploy proof")
print("AC3 ok")


# ═══════════════════════════ AC4 — db: real cluster, real round trip, teardown ═════
SENTINELS = {v: f"sentinel-{v}" for v in grading_env.PROD_DSN_VARS}
saved = {k: os.environ.get(k) for k in SENTINELS}
os.environ.update(SENTINELS)
view3 = FIXTURE_ROOT / "grade" / "ac4"
(view3 / "tree").mkdir(parents=True, exist_ok=True)
try:
    ok, reason, dsn = grading_env._prove_db(view3)
finally:
    for k, v in saved.items():
        os.environ[k] = v if v is not None else ""
        if v is None:
            os.environ.pop(k, None)
check(ok, f"AC4: _prove_db failed: {reason}")
check(dsn and "sentinel" not in dsn, f"AC4: dsn carries no sentinel: {dsn}")
env3 = grading_env.pane_env(view3)  # no state written yet by _prove_db alone; write it now for the round trip
grading_env._write_state(view3, venv="", db_env_names=["SUPABASE_DB_URL", "DATABASE_URL", "PG_PARITY_DB_URL"], dsn=dsn)
env3 = grading_env.pane_env(view3)
for name in ("SUPABASE_DB_URL", "DATABASE_URL", "PG_PARITY_DB_URL"):
    check(name in env3, f"AC4: pane_env carries {name}")
    check(env3[name] == dsn, f"AC4: pane_env[{name}] == dsn")
    check("sentinel" not in env3[name], f"AC4: pane_env[{name}] carries no sentinel")
subprocess.run(["psql", dsn, "-c", "create table probe3(x int); insert into probe3 values (42);"],
               check=True, capture_output=True)
r = subprocess.run(["psql", dsn, "-tAc", "select x from probe3;"], check=True, capture_output=True, text=True)
check(r.stdout.strip() == "42", f"AC4: fresh-connection read-back: {r.stdout!r}")
grading_env.teardown(view3)
r2 = subprocess.run([str(grading_env._PG_BIN / "pg_ctl"), "-D", str(view3 / "pg" / "data"), "status"],
                    capture_output=True, text=True)
check(r2.returncode != 0, f"AC4: cluster still running after teardown: {r2.stdout}")
grading_env.teardown(view3)  # idempotent, never raises
check(grading_env.teardown(FIXTURE_ROOT / "grade" / "no-pg-here") is None, "AC4: teardown with no pg/ returns None")
print("AC4 ok")


# ═══════════════════════════ AC5 — env-file ════════════════════════════════════
view4 = FIXTURE_ROOT / "grade" / "ac5"
(view4 / "tree").mkdir(parents=True, exist_ok=True)
row = grading_env._project_row("testproj")
grading_env._write_env_file(view4 / "tree", row, dsn)
envtxt = (view4 / "tree" / ".env").read_text()
lines = [l for l in envtxt.splitlines() if l]
check(len(lines) == 3, f"AC5: exactly one line per db_env name: {lines}")
for line in lines:
    check("sentinel" not in line, f"AC5: no sentinel in .env: {line}")
mode = (view4 / "tree" / ".env").stat().st_mode & 0o777
check(mode == 0o600, f"AC5: mode 0600, got {oct(mode)}")
# empty when db not required
view4b = FIXTURE_ROOT / "grade" / "ac5b"
(view4b / "tree").mkdir(parents=True, exist_ok=True)
grading_env._write_env_file(view4b / "tree", row, None)
check((view4b / "tree" / ".env").read_text() == "", "AC5: empty .env when db not proven")
# no grading.toml row -> empty .env
view4c = FIXTURE_ROOT / "grade" / "ac5c"
(view4c / "tree").mkdir(parents=True, exist_ok=True)
grading_env._write_env_file(view4c / "tree", grading_env._project_row("no-such-project"), dsn)
check((view4c / "tree" / ".env").read_text() == "", "AC5: empty .env for a project with no row")
print("AC5 ok")


# ═══════════════════════════ AC6 — db-schema: success, failure, real argv ══════
view5 = FIXTURE_ROOT / "grade" / "ac6"
(view5 / "tree").mkdir(parents=True, exist_ok=True)
ok5db, reason5db, dsn5 = grading_env._prove_db(view5)
check(ok5db, f"AC6: fresh cluster for the db-schema fixture: {reason5db}")
ok5, reason5 = grading_env._prove_db_schema(view5, row, "testproj", dsn5)
check(ok5, f"AC6: db_setup should succeed: {reason5}")
r5 = subprocess.run(["psql", dsn5, "-tAc", "select count(*) from grading_probe;"],
                    check=True, capture_output=True, text=True)
check(r5.stdout.strip() == "0", f"AC6: table visible from a fresh connection: {r5.stdout!r}")
bad_row = {**row, "db_setup": {"argv": ["/bin/bash", "-c", "exit 7"], "cwd": "."}}
ok5b, reason5b = grading_env._prove_db_schema(view5, bad_row, "testproj", dsn5)
check(not ok5b and "7" in reason5b, f"AC6: non-zero exit reported: {reason5b}")
grading_env.teardown(view5)
argv, cwd, venv = grading_env._db_setup_argv(
    {"db_setup": {"argv": ["{venv}/bin/alembic", "-c", "alembic_supabase.ini", "upgrade", "head"], "cwd": "api"},
     "venv": ".venv"}, "testproj")
check(argv == [str(CHECKOUT / ".venv" / "bin" / "alembic"), "-c", "alembic_supabase.ini", "upgrade", "head"], argv)
check(cwd == "api", cwd)
print("AC6 ok")


# ═══ AC7 (support) — _prove_view_paths generic pass/fail; full rewrite fixtures
# (symlinked repo, foreign/missing path) live in test_grader_view.py ══════════
view6 = FIXTURE_ROOT / "grade" / "ac7"
(view6 / "tree").mkdir(parents=True, exist_ok=True)
(view6 / "verify.sh").write_text(f"#!/bin/bash\n{view6}/tree/x /usr/bin/python3\n")
ok6, _ = grading_env._prove_view_paths(view6)
check(ok6, "AC7: a view-relative + interpreter path both pass")
(view6 / "verify.sh").write_text(f"#!/bin/bash\n/some/foreign/absolute/path\n")
ok6b, reason6b = grading_env._prove_view_paths(view6)
check(not ok6b and "foreign" in reason6b, f"AC7: a foreign absolute path fails: {reason6b}")
print("AC7 (support) ok")


# ═══════════════════════════ AC8 — git: derived, never provisioned (SD-R12-3a) ═════
# L-spec-8034/Assumption 4: "git status" names no history verb (merge-base/log/
# show/rev-list/diff/blame/cat-file/--is-ancestor) and no $BASE — it is a
# `tools` need, not `git`. Use a real history verb so this still exercises the
# git-preflight-always-fails path (AC8(i)).
write_spec_verify("L-spec-9601", "grader", "spec", "git log -1")
failures6 = grading_env.preflight("grader", "L-spec-9601", view6, "testproj", repo=str(REPO))
gitfail = dict(failures6).get("git")
check(gitfail is not None and "never provisioned" in gitfail, f"AC8(i): git always fails preflight: {failures6}")
check("git" in grading_env.required("grader", "spec", "cd $BASE && git diff --stat && /usr/bin/python3 t.py",
                                     "testproj"), "AC8(i): a git argv word in a &&-segment after $BASE subst")
check("git" not in grading_env.required(
    "grader", "rollback_path: git revert HEAD is safe", "nothing relevant here", "testproj"),
    "AC8(ii): rollback_path prose ('git revert') never requires git")
check("git" in grading_env.required("grader", "AC1 [backend]: x\nreview_path: run `git log -1`",
                                     "nothing relevant here",
                                     "testproj"), "AC8(iii): a review_path: line running git")
check("git" not in grading_env.required(
    "grader", "prose says the git history matters here, outside review_path", "nothing relevant here", "testproj"),
    "AC8(iv): 'the git history' prose outside review_path: never requires git")
print("AC8 ok")


# ═══════════════════════════ AC9 — node_modules/tools/deploy/review-account ════
view7 = FIXTURE_ROOT / "grade" / "ac9"
(view7 / "tree").mkdir(parents=True, exist_ok=True)
ok7, _ = grading_env._prove_node_modules(view7, row, "testproj")
check(ok7, "AC9: node_modules symlink provisioned")
link = view7 / "tree" / "dashboard" / "node_modules"
check(link.is_symlink(), "AC9: a real symlink, not a copy")
check((link / ".bin" / "marker").is_file(), "AC9: the linked tree resolves")
binds7 = grading_env.sandbox_binds(view7, "testproj")
resolved_nm = str((CHECKOUT / "dashboard" / "node_modules").resolve())
check((resolved_nm, resolved_nm) in binds7, f"AC9: sandbox_binds names the resolved node_modules: {binds7}")
venv_path7 = str(CHECKOUT / ".venv")
check((venv_path7, venv_path7) in binds7, f"AC9: sandbox_binds names the venv: {binds7}")

if shutil.which("bwrap"):
    ok7t, reason7t = grading_env._prove_tools(view7, "testproj", str(REPO), "python3", venv_path7)
    check(ok7t, f"AC9: tools proof under a real sandbox: {reason7t}")
else:
    print("SKIP: AC9 sandboxed tools proof (bwrap unavailable in this environment)")

# deploy: injected runner (monkeypatch _run)
calls = []
def fake_deploy_run(argv, **kw):
    calls.append(argv)
    class R:
        returncode = 0
    return R()
_saved_run = grading_env._run
grading_env._run = fake_deploy_run
try:
    ok7d, reason7d = grading_env._prove_deploy("testproj", str(REPO))
finally:
    grading_env._run = _saved_run
check(ok7d, f"AC9: deploy proof with an injected runner answering: {reason7d}")
check(calls and calls[0][0] == "ssh" and calls[0][1] == "example-host", calls)

ok7d2, reason7d2 = grading_env._prove_deploy("no-such-project", str(REPO))
check(not ok7d2, "AC9: deploy fails with no matching [[prod]] row")

# review-account: absent by default in this fixture root
ok7r, reason7r = grading_env._prove_review_account("testproj")
check(not ok7r, "AC9: review-account absent by default")
(FIXTURE_ROOT / "review-account-testproj").write_text("x")
ok7r2, _ = grading_env._prove_review_account("testproj")
check(ok7r2, "AC9: review-account proven once the file exists")
print("AC9 ok")


# ═══════════════════════════ AC10 — one environment for proof and grade ════════
view8 = FIXTURE_ROOT / "grade" / "ac10"
(view8 / "tree").mkdir(parents=True, exist_ok=True)
recorded = []
_saved_run2 = grading_env._run
def recording_run(argv, **kw):
    recorded.append((list(argv), kw.get("env")))
    class R:
        returncode = 0
        stdout, stderr = "1.0.0", ""
    return R()
grading_env._run = recording_run
try:
    grading_env._sandboxed_run(view8, "testproj", str(REPO), ["/bin/true"])
finally:
    grading_env._run = _saved_run2
if grading_env._bwrap_available():
    import grader_view
    expect_argv = grader_view.bwrap_argv(view8, doit_src=grading_env.DOIT_SRC,
                                          binds=grading_env.sandbox_binds(view8, "testproj")) + ["--", "/bin/true"]
    check(recorded and recorded[-1][0] == expect_argv, "AC10: proof argv equals grader_view.bwrap_argv(...)")
    check(grading_env.sandbox_mode() == "bwrap", "AC10: sandbox_mode() reports bwrap when it is used")
else:
    check(recorded and recorded[-1][0] == ["/bin/true"], "AC10: host fallback runs the bare argv")
    check(recorded[-1][1] == grading_env.pane_env(view8), "AC10: host fallback env equals pane_env(view)")
    check(grading_env.sandbox_mode() == "host", "AC10: sandbox_mode() reports host — the spawn-started sandbox= field")
# force the host lane regardless of this box's own bwrap, and confirm the field value directly
_real_bwrap_avail = grading_env._bwrap_available
grading_env._bwrap_available = lambda: False
try:
    check(grading_env.sandbox_mode() == "host", "AC10: sandbox_mode() == 'host' when bwrap is unavailable")
finally:
    grading_env._bwrap_available = _real_bwrap_avail
print("AC10 ok")


# ═══════════════════════════ AC11 — view/grading.env == pane_env, round trip ═══
view9 = FIXTURE_ROOT / "grade" / "ac11"
(view9 / "tree").mkdir(parents=True, exist_ok=True)
grading_env._write_state(view9, venv=str(CHECKOUT / ".venv"),
                          db_env_names=["SUPABASE_DB_URL"], dsn="postgresql://a b'c@/x?host=/y&port=1")
env9 = grading_env.pane_env(view9)
lines9 = "\n".join(f"export {k}={_q}" for k, _q in
                    [(k, "'" + v.replace("'", "'\\''") + "'") for k, v in env9.items()])
(view9 / "grading.env").write_text(lines9 + "\n")
r9 = subprocess.run(["/bin/bash", "-c", f'set -a; source "{view9}/grading.env"; '
                     + "; ".join(f'echo "{k}=$${k}"'.replace("$$", "$") for k in env9)],
                    capture_output=True, text=True, check=True)
for line in r9.stdout.splitlines():
    k, _, v = line.partition("=")
    check(env9.get(k) == v, f"AC11: sourcing round-trips {k}: {env9.get(k)!r} vs {v!r}")
print("AC11 ok")


# ═══════════════ L-spec-8027 AC8 — pane_env(view) is UNCHANGED: HOME == <view>, ═
# ═══════════════ PATH and DB keys as before, and no DOIT_ROOT key ══════════════
view8027 = FIXTURE_ROOT / "grade" / "l-spec-8027-ac8"
(view8027 / "tree").mkdir(parents=True, exist_ok=True)
grading_env._write_state(view8027, venv=str(CHECKOUT / ".venv"),
                          db_env_names=["SUPABASE_DB_URL"],
                          dsn="postgresql://u:p@dbhost:5433/mydb?host=/y&port=5433")
env8027 = grading_env.pane_env(view8027)
check(env8027["HOME"] == str(view8027),
      f"L-spec-8027 AC8: HOME == <view>, exactly as before this spec: {env8027}")
check(env8027["PATH"] == f"{CHECKOUT / '.venv'}/bin:/usr/bin:/bin",
      f"L-spec-8027 AC8: PATH is unchanged (venv-prefixed when a venv is recorded): {env8027}")
check(env8027.get("SUPABASE_DB_URL") == "postgresql://u:p@dbhost:5433/mydb?host=/y&port=5433"
      and env8027.get("PGDATABASE") == "mydb",
      f"L-spec-8027 AC8: the DB keys are unchanged: {env8027}")
check("DOIT_ROOT" not in env8027,
      f"L-spec-8027 AC8: pane_env gains no DOIT_ROOT key: {env8027}")

# a view carrying `.grading_state.json` with a dsn but no venv, still no DOIT_ROOT
view8027b = FIXTURE_ROOT / "grade" / "l-spec-8027-ac8b"
(view8027b / "tree").mkdir(parents=True, exist_ok=True)
grading_env._write_state(view8027b, venv="", db_env_names=["DATABASE_URL"],
                          dsn="postgresql://x:y@z:5432/w")
env8027b = grading_env.pane_env(view8027b)
check(env8027b["HOME"] == str(view8027b) and env8027b["PATH"] == "/usr/bin:/bin"
      and "DOIT_ROOT" not in env8027b,
      f"L-spec-8027 AC8: no venv -> bare PATH, HOME still <view>, still no DOIT_ROOT: {env8027b}")
print("L-spec-8027 AC8 ok")


# ═══════════════════════════════════════════════════════════════════════════
# L-spec-8034 (capability-routing) — AC1-AC5, AC12 below. AC13's size/purity
# assertions live in test_routing.py (needs no git); AC6-AC11/AC14 (dispatch,
# fold, packet, agents/*.md, the live post-deploy observation) live in
# test_dispatch.py/test_fold.py/test_packet.py.
# ═══════════════════════════════════════════════════════════════════════════

REPO_NOPROD = FIXTURE_ROOT / "repo-no-look-toml"
REPO_NOPROD.mkdir(parents=True, exist_ok=True)  # no look.toml at all -> no [[prod]] row

SPEC8034 = """## Acceptance criteria

AC1 [backend]: plain python, no GRADER_NEVER need.
review_path: run `/usr/bin/python3 check.py`

AC2 [backend]: needs git history via a review_path.
review_path: run `git merge-base HEAD $BASE`

AC3 [backend]: a temp-repo test — the git PROGRAM only, never history.
review_path: run a suite that does `git init` in a tmp dir

AC4 [ui]: a browser path.
review_path: log in as operator, go to /dashboard

AC5 [backend]: a deploy path.
review_path: curl /version

AC6 [observed-data]: a live-db path.
review_path: checks DB_URL

## Constraints
No money, no credentials. Mentions git log, origin, deployed, and .env here —
none of it inside a criterion block.

## rollback_path
git revert HEAD is safe; nothing here is a criterion either.
"""


# ═══════════════════════════ AC1 — criterion_caps: pure, per-criterion, git split
caps8034 = grading_env.criterion_caps(SPEC8034, "", "testproj")
print("AC1 (8034) criterion_caps table:")
for cid in sorted(caps8034):
    print("  ", cid, "->", caps8034[cid])
check(caps8034.get("AC1") == ["view-paths", "tools"], f"AC1(8034): plain python: {caps8034.get('AC1')}")
check(caps8034.get("AC2") == ["git"], f"AC1(8034): git-history review_path: {caps8034.get('AC2')}")
check(caps8034.get("AC3") == ["tools"], f"AC1(8034): temp-repo test carries no git: {caps8034.get('AC3')}")
check("git" not in (caps8034.get("AC3") or []), "AC1(8034): AC3 must not carry git")
check(set(caps8034.get("AC4") or []) >= {"browser", "review-account"}, caps8034.get("AC4"))
check("deploy" in (caps8034.get("AC5") or []), caps8034.get("AC5"))
check(set(caps8034.get("AC6") or []) == {"db", "db-schema"}, caps8034.get("AC6"))
check("AC-CONSTRAINTS-PROSE" not in json.dumps(caps8034), "sanity: no stray key")
for cid, cs in caps8034.items():
    idx = [grading_env.CAPABILITIES.index(c) for c in cs]
    check(idx == sorted(idx), f"AC1(8034): {cid} caps not in CAPABILITIES order: {cs}")
# prose outside any criterion block (Constraints/rollback_path) yields nothing:
# confirmed by the fact that no extra key beyond AC1-AC6 exists and none of
# AC1-AC6's own lists were inflated by "git log"/"origin"/".env"/"deployed"
# sitting in those two sections (git only appears on AC2/AC3's own lines;
# env-file/deploy tokens from Constraints never surface on any id above).
check("env-file" not in sum(caps8034.values(), []), "AC1(8034): Constraints' '.env' contributes nothing")
print("AC1 (8034) ok")


# ═══════════════════════════ AC2 — required(): union minus owed/routed ══════
req_all = grading_env.required("grader", SPEC8034, "", "testproj")
check(set(req_all) >= {"git", "browser", "deploy"}, f"AC2(8034): empty owed/routed includes all three: {req_all}")
req_routed = grading_env.required("grader", SPEC8034, "", "testproj", routed={"AC2", "AC4", "AC5"})
check("git" not in req_routed and "browser" not in req_routed and "deploy" not in req_routed,
      f"AC2(8034): routing AC2/AC4/AC5 drops git/browser/deploy: {req_routed}")
req_owed_git = grading_env.required("grader", SPEC8034, "", "testproj", owed={"AC2"})
check("git" not in req_owed_git, f"AC2(8034): owed={{'AC2'}} drops git alone: {req_owed_git}")
check("browser" in req_owed_git and "deploy" in req_owed_git, req_owed_git)
# spec-level derivation is gone: a fixture whose only `git` sits in a
# rollback_path (outside any AC block) returns no git at all.
ROLLBACK_ONLY = "## Acceptance criteria\n\nAC1 [backend]: plain.\nreview_path: run `/usr/bin/python3 x.py`\n\n" \
                "## rollback_path\ngit log -1 is safe to revert\n"
check("git" not in grading_env.required("grader", ROLLBACK_ONLY, "", "testproj"),
      "AC2(8034): rollback_path git contributes nothing to required()")
# reviewer branch unchanged (an L-spec-0481 reviewer row, replayed)
check(grading_env.required("reviewer", "x", "x", "albert-scott", mcp_config=True, review_account=True)
      == ["browser", "review-account", "deploy"], "AC2(8034): reviewer branch unchanged")
_pf_calls = []
_real_required = grading_env.required


def _recording_required(*a, **kw):
    _pf_calls.append((a, kw))
    return _real_required(*a, **kw)


grading_env.required = _recording_required
try:
    write_spec_verify("L-spec-9700", "grader", SPEC8034, "")
    view_pf = FIXTURE_ROOT / "grade" / "ac2-8034"
    (view_pf / "tree").mkdir(parents=True, exist_ok=True)
    grading_env.preflight("grader", "L-spec-9700", view_pf, "testproj", repo=str(REPO_NOPROD),
                          owed=frozenset({"AC2"}), routed=frozenset({"AC4"}))
finally:
    grading_env.required = _real_required
check(_pf_calls and _pf_calls[-1][1].get("owed") == frozenset({"AC2"})
      and _pf_calls[-1][1].get("routed") == frozenset({"AC4"}),
      f"AC2(8034): preflight passes owed/routed straight through: {_pf_calls[-1] if _pf_calls else None}")
print("AC2 (8034) ok")


# ═══════════════════════════ AC3 — verify-script git is never silently dropped ══
VERIFY_0469 = 'run_checks.sh && BASE_LINES=$(git show $BASE:deploy.sh | wc -l)'
caps_0469 = grading_env.criterion_caps("", VERIFY_0469, "testproj")
check(caps_0469.get("verify") == ["git"], f"AC3(8034): unattributed verify git -> pseudo-criterion 'verify': {caps_0469}")
route_0469 = grading_env.route("", VERIFY_0469, "testproj")
check(route_0469.get("verify") == ("git", "owed"), f"AC3(8034): route()['verify']: {route_0469}")
check("git" in grading_env.required("grader", "", VERIFY_0469, "testproj"),
      "AC3(8034): required() with routed empty still includes the verify-only git")
check("git" not in grading_env.required("grader", "", VERIFY_0469, "testproj", routed={"verify"}),
      "AC3(8034): routed={'verify'} drops it")
# a segment naming AC3 attributes git to AC3, not to the pseudo-criterion
# (Assumption 5: the SAME segment both needs git and names the AC<n>)
VERIFY_NAMED = 'echo running AC3 && cd . && git show $BASE:x.py | wc -l # for AC3'
caps_named = grading_env.criterion_caps("", VERIFY_NAMED, "testproj")
check(caps_named.get("AC3") == ["git"] and "verify" not in caps_named, f"AC3(8034): named attribution: {caps_named}")
# a bare `git init` (temp repo) verify segment yields tools, never git
VERIFY_TMPREPO = 'cd "$(mktemp -d)" && git init -q && python3 -m pytest'
caps_tmp = grading_env.criterion_caps("", VERIFY_TMPREPO, "testproj")
check(caps_tmp.get("verify") and "git" not in caps_tmp["verify"] and "tools" in caps_tmp["verify"],
      f"AC3(8034): git-init-only verify segment yields tools, never git: {caps_tmp}")
print("AC3 (8034) ok")


# ═══════════════════════════ AC4 — over-holding is gone in real-shaped fixtures ═
# (a) 0469-shaped: one browser criterion, one deploy criterion, five plain
# ones, plus the AC3-style unattributed verify git line -- routing all three
# GRADER_NEVER needs leaves only the plain criteria's capabilities.
SPEC_0469SHAPE = """## Acceptance criteria

AC1 [backend]: plain.
review_path: run `/usr/bin/python3 a.py`

AC2 [ui]: browser.
review_path: log in as operator, go to /x

AC3 [backend]: deploy.
review_path: curl /version
"""
routes_0469 = grading_env.route(SPEC_0469SHAPE, VERIFY_0469, "testproj")
check(set(routes_0469) >= {"AC2", "AC3", "verify"}, routes_0469)
req_0469_routed = grading_env.required("grader", SPEC_0469SHAPE, VERIFY_0469, "testproj",
                                       routed=set(routes_0469))
check(not ({"git", "browser", "deploy"} & set(req_0469_routed)), req_0469_routed)
check("tools" in req_0469_routed, req_0469_routed)  # AC1's plain python need survives

# (b) 0484-shaped: three [backend] criteria whose review_path is a temp-repo
# suite -- route() is empty, required() includes tools and no git.
SPEC_0484SHAPE = """## Acceptance criteria

AC4 [backend]: temp-repo suite one.
review_path: run a suite that does `git init` in a tmp dir

AC8 [backend]: temp-repo suite two.
review_path: run a suite that does `git commit` in a tmp dir

AC11 [backend]: temp-repo suite three.
review_path: run a suite that does `git init` in a tmp dir
"""
check(grading_env.route(SPEC_0484SHAPE, "", "testproj") == {}, "AC4(8034)(b): no GRADER_NEVER need at all")
req_0484 = grading_env.required("grader", SPEC_0484SHAPE, "", "testproj")
check("tools" in req_0484 and "git" not in req_0484, f"AC4(8034)(b): {req_0484}")

# (c) 0486-shaped: temp-repo criteria + a DB_URL criterion -- db/db-schema
# survive unless AC11 itself is owed.
SPEC_0486SHAPE = """## Acceptance criteria

AC1 [backend]: temp-repo one.
review_path: run a suite that does `git init` in a tmp dir

AC3 [backend]: temp-repo two.
review_path: run a suite that does `git init` in a tmp dir

AC7 [backend]: temp-repo three.
review_path: run a suite that does `git init` in a tmp dir

AC11 [observed-data]: db criterion.
review_path: checks DB_URL
"""
req_0486 = grading_env.required("grader", SPEC_0486SHAPE, "", "testproj")
check({"db", "db-schema"} <= set(req_0486), f"AC4(8034)(c): db survives when AC11 is not owed: {req_0486}")
req_0486_owed = grading_env.required("grader", SPEC_0486SHAPE, "", "testproj", owed={"AC11"})
check(not ({"db", "db-schema"} & set(req_0486_owed)), f"AC4(8034)(c): owed={{'AC11'}} drops both: {req_0486_owed}")
print("AC4 (8034)(a)(b)(c) ok — (d) (fold.held_specs) lives in test_fold.py")


# ═══════════════════════════ AC5 — route()/GRADER_NEVER exact table ═════════
check(grading_env.GRADER_NEVER == ("browser", "deploy", "git"), grading_env.GRADER_NEVER)

# `_look_toml(DOIT_SRC)` reads the REAL repo-root look.toml, not the fixture
# REPO above (route()/required() always resolve `[[prod]]` against
# `DOIT_SRC`/`look.toml` — Assumptions/Constraints: "the project's own
# `[[prod]] base_url`"). Monkeypatch `_look_toml` for a clean, hermetic table.
_real_look_toml = grading_env._look_toml
def _fake_look_toml(repo):
    if str(repo) == str(grading_env.DOIT_SRC):
        return {"prod": [{"project": "hasprod", "base_url": "https://x.example"}]}
    return _real_look_toml(repo)


grading_env._look_toml = _fake_look_toml
try:
    r_hasprod = grading_env.route(SPEC8034, "", "hasprod")
    r_gone = grading_env.route(SPEC8034, "", "noprod")
    check(r_hasprod.get("AC4") == ("browser", "reviewer"), r_hasprod)
    check(r_hasprod.get("AC5") == ("deploy", "reviewer"), r_hasprod)
    check(r_hasprod.get("AC2") == ("git", "owed"), r_hasprod)  # git always owed
    check(r_gone.get("AC4") == ("browser", "owed"), r_gone)
    check(r_gone.get("AC5") == ("deploy", "owed"), r_gone)
    check(r_gone.get("AC2") == ("git", "owed"), r_gone)
    # several GRADER_NEVER needs -> first in GRADER_NEVER order (browser, deploy, git)
    multi = grading_env.criterion_caps(
        "## Acceptance criteria\n\nAC9 [ui]: multi.\nreview_path: git log -1, log in as x, go to /, curl /version\n",
        "", "hasprod")
    check(set(multi.get("AC9") or []) >= {"browser", "deploy", "git"}, multi)
    r_multi = grading_env.route(
        "## Acceptance criteria\n\nAC9 [ui]: multi.\nreview_path: git log -1, log in as x, go to /, curl /version\n",
        "", "hasprod")
    check(r_multi.get("AC9") == ("browser", "reviewer"), f"AC5(8034): first-in-GRADER_NEVER-order: {r_multi}")
    print("AC5 (8034) full table ok")
finally:
    grading_env._look_toml = _real_look_toml


# ═══════════════════════════ AC12 — deploy tokens no longer match prose ═════
check(grading_env._deploy_match("the branch origin", "testproj") is False, "AC12: bare 'origin' never matches")
check(grading_env._deploy_match("already deployed", "testproj") is False, "AC12: bare 'deployed' never matches")
check(grading_env._deploy_match("deployed by hand", "testproj") is False, "AC12: 'deployed by hand' never matches")
check(grading_env._deploy_match("curl /version", "testproj") is True, "AC12: '/version' matches")
check(grading_env._deploy_match("deployed at 03:00 UTC", "testproj") is True, "AC12: 'deployed at' matches")
grading_env._look_toml = _fake_look_toml
try:
    check(grading_env._deploy_match("hit https://x.example/health", "hasprod") is True,
          "AC12: the project's own [[prod]] base_url matches")
finally:
    grading_env._look_toml = _real_look_toml
check("origin" not in grading_env._DEPLOY_TOKENS and "deployed" not in grading_env._DEPLOY_TOKENS,
      grading_env._DEPLOY_TOKENS)
print("AC12 (8034) ok")


shutil.rmtree(FIXTURE_ROOT, ignore_errors=True)
print("ALL OK")
