#!/usr/bin/env python3
"""One runnable check on grading_env.py's AC1-AC9 (L-spec-0481). Run:
python3 test_grading_env.py

Before importing `grading_env`, this sets DOIT_ROOT to a fresh temp
directory and DOIT_GRADING_TOML to a fixture `grading.toml` — never the
live `$R`, never the real repo-root `grading.toml`. A fixture project
checkout (`node_modules`, `.venv`) and a fixture `look.toml` (for the
`deploy` capability) live under `DOIT_ROOT/repos/testproj`.
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
    TABLE = [
        ("grader", "spec", "export .env something", "testproj", ["env-file"]),
        ("grader", "spec", "/opt/albert-scott/x /usr/bin/python3", "testproj", ["view-paths", "tools"]),
        ("grader", "spec", "run python3 script.py", "testproj", ["tools"]),
        ("grader", "spec", "npm run test", "testproj", ["node_modules"]),
        ("grader", "spec", "git log -1", "testproj", ["git"]),
        ("grader", "spec has a live_db criterion", "checks DB_URL too", "testproj", ["db", "db-schema"]),
        ("grader", "log in as operator, go to /", "verify", "testproj", ["browser", "review-account"]),
        ("grader", "spec", "check deployed state against origin", "testproj", ["deploy"]),
        ("grader", "nothing special here", "nothing special here either", "testproj", []),
        ("reviewer", "anything at all", "anything at all", "testproj", []),
        ("grader", "criterion:\nreview_path: git log -1\nrollback_path: git revert HEAD",
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
check(grading_env.SPEC_LOCAL == {"view-paths", "git"}, grading_env.SPEC_LOCAL)
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
write_spec_verify("L-spec-9601", "grader", "spec", "git status")
failures6 = grading_env.preflight("grader", "L-spec-9601", view6, "testproj", repo=str(REPO))
gitfail = dict(failures6).get("git")
check(gitfail is not None and "never provisioned" in gitfail, f"AC8(i): git always fails preflight: {failures6}")
check("git" in grading_env.required("grader", "spec", "cd $BASE && git diff --stat && /usr/bin/python3 t.py",
                                     "testproj"), "AC8(i): a git argv word in a &&-segment after $BASE subst")
check("git" not in grading_env.required(
    "grader", "rollback_path: git revert HEAD is safe", "nothing relevant here", "testproj"),
    "AC8(ii): rollback_path prose ('git revert') never requires git")
check("git" in grading_env.required("grader", "criterion:\nreview_path: run `git log -1`", "nothing relevant here",
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


shutil.rmtree(FIXTURE_ROOT, ignore_errors=True)
print("ALL OK")
