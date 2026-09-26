#!/usr/bin/env python3
"""Checks on the sweeper's two production wrappers: AC1-AC22 of L-spec-0386.
Run: python3 test_sweep_wrappers.py

Every `.env`/`look.toml` this exercises is either a scratch file under
`harness.py`'s sandbox or THIS repo's own real, credential-free `look.toml`
(AC15-17, Current — its one `[[prod]]` row names no real secret). Every
`ro-sql`/`droplet-read` subprocess call here runs against a stub `psql`/`ssh`
placed first on `PATH` — no network, no real credential, exactly the Plan
SD6 promise ("Tests stub `ssh` and `psql` on `PATH` and never touch the
network").
"""
import contextlib
import os
import pathlib
import stat
import subprocess
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import harness  # noqa: E402
import sweep_wrappers  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RO_SQL = REPO_ROOT / "scripts" / "sweep" / "ro-sql"
DROPLET_READ = REPO_ROOT / "scripts" / "sweep" / "droplet-read"

n = 0


def ok(cond, why):
    global n
    assert cond, why
    n += 1


def write_stub(path, body):
    path.write_text(textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@contextlib.contextmanager
def path_prepended(dirpath):
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{dirpath}{os.pathsep}{old}" if old else str(dirpath)
    try:
        yield
    finally:
        if old:
            os.environ["PATH"] = old
        else:
            os.environ.pop("PATH", None)


def env_checkout(name, env_lines):
    """A `harness.test_root`-rooted checkout with the given `.env` lines."""
    root = harness.test_root(name)
    (root / ".env").write_text("\n".join(env_lines) + "\n" if env_lines else "")
    return root


# ── AC1-AC3: resolve_ro_dsn ──────────────────────────────────────────────
no_env_root = harness.test_root("ac1-no-file")
dsn, refusal = sweep_wrappers.resolve_ro_dsn(no_env_root / "nonexistent-checkout-does-not-exist")
ok(dsn is None and refusal, f"AC1a: missing .env dir must refuse: {(dsn, refusal)}")

no_key_root = env_checkout("ac1-no-key", ["OTHER_KEY=x"])
dsn, refusal = sweep_wrappers.resolve_ro_dsn(no_key_root)
ok(dsn is None and refusal, f"AC1b: .env with no SUPABASE_DB_URL_RO must refuse: {(dsn, refusal)}")

collide_root = env_checkout("ac2-collide", ["SUPABASE_DB_URL_RO=X", "SUPABASE_DB_URL=X"])
dsn, refusal = sweep_wrappers.resolve_ro_dsn(collide_root)
ok(dsn == "X" and refusal, f"AC2: byte-identical collision must still return dsn, with a refusal: {(dsn, refusal)}")

distinct_root = env_checkout("ac3-distinct", ["SUPABASE_DB_URL_RO=RODSN", "SUPABASE_DB_URL=WRITEDSN"])
dsn, refusal = sweep_wrappers.resolve_ro_dsn(distinct_root)
ok(refusal is None and dsn == "RODSN", f"AC3: distinct RO dsn must admit clean: {(dsn, refusal)}")

# ── AC4-AC5: sql_statement_refusal ───────────────────────────────────────
admitted = ["SELECT 1", "  -- a comment\nSELECT 1", "WITH x AS (SELECT 1) SELECT * FROM x",
            "SHOW search_path", "EXPLAIN SELECT 1"]
refused = ["INSERT INTO t VALUES (1)", "UPDATE t SET x=1", "DELETE FROM t", "DROP TABLE t",
           "TRUNCATE t", "EXPLAIN ANALYZE SELECT 1"]
for sql in admitted:
    ok(sweep_wrappers.sql_statement_refusal(sql) is None, f"AC4: must admit {sql!r}")
for sql in refused:
    ok(sweep_wrappers.sql_statement_refusal(sql) is not None, f"AC4: must refuse {sql!r}")

ok(sweep_wrappers.sql_statement_refusal("SELECT 1; DROP TABLE t") is not None,
   "AC5: a second statement after one semicolon must refuse")
ok(sweep_wrappers.sql_statement_refusal("SELECT 1;;") is not None,
   "AC5: a doubled trailing semicolon must refuse")
ok(sweep_wrappers.sql_statement_refusal("SELECT 1;") is None,
   "AC5: exactly one trailing semicolon must admit")

# ── AC6-AC9: droplet_command_refusal ─────────────────────────────────────
for cmd, args in [("cat", ["/etc/hostname"]), ("systemctl", ["status", "bluedot-webhook"]),
                   ("systemctl", ["list-timers"]), ("crontab", ["-l"]),
                   ("curl", ["-s", "http://127.0.0.1:8000/health"])]:
    ok(sweep_wrappers.droplet_command_refusal(cmd, args) is None, f"AC6: must admit {(cmd, args)}")
for cmd, args in [("rm", ["-rf", "/"]), ("deploy.sh", [])]:
    ok(sweep_wrappers.droplet_command_refusal(cmd, args) is not None, f"AC6: must refuse {(cmd, args)}")

ok(sweep_wrappers.droplet_command_refusal("find", ["/var/log", "-name", "*.log"]) is None,
   "AC7: a plain find must admit")
for cmd, args in [
    ("find", ["/var/log", "-delete"]),
    ("find", ["/tmp", "-exec", "rm", "{}", ";"]),
    ("find", ["/tmp", "-execdir", "rm", "{}", ";"]),
    ("find", ["/tmp", "-ok", "rm", "{}", ";"]),
    ("find", ["/tmp", "-okdir", "rm", "{}", ";"]),
    ("find", ["/tmp", "-fprintf", "/tmp/out", "%p\n"]),
    ("find", ["/tmp", "-fls", "/tmp/out"]),
    ("systemctl", ["restart", "bluedot-webhook"]),
    ("crontab", ["-e"]),
]:
    ok(sweep_wrappers.droplet_command_refusal(cmd, args) is not None, f"AC7: must refuse {(cmd, args)}")

for args in [
    ["http://127.0.0.1:8000/health"],
    ["-s", "-X", "POST", "http://127.0.0.1:8000/health"],
    ["-s", "http://10.0.0.5:8000/health"],
    ["-s", "-o", "/tmp/x", "http://127.0.0.1:8000/health"],
    ["-s", "--json", "{}", "http://127.0.0.1:8000/health"],
    ["-s", "-XPOST", "http://127.0.0.1:8000/health"],
    ["-s", "10.0.0.5:9000/x"],
    ["-s", "http://127.0.0.1:8000/h", "http://10.0.0.5/x"],
]:
    ok(sweep_wrappers.droplet_command_refusal("curl", args) is not None, f"AC8: must refuse curl {args}")

for bad in ["a;b", "a&b", "a|b", "a`b", "a$b", "a<b", "a>b", "a\nb"]:
    ok(sweep_wrappers.droplet_command_refusal("cat", [bad]) is not None,
       f"AC9: must refuse metacharacter-bearing argument {bad!r}")

# ── AC10: quote_remote_command ───────────────────────────────────────────
import shlex  # noqa: E402
ok(shlex.split(sweep_wrappers.quote_remote_command("cat", ["/var/log/syslog"]))
   == ["cat", "/var/log/syslog"], "AC10a: plain argv must round-trip")
ok(shlex.split(sweep_wrappers.quote_remote_command("grep", ["a b", "/tmp/f"]))
   == ["grep", "a b", "/tmp/f"], "AC10b: a space-bearing argument must round-trip")

# ── AC18: ALLOWED_DROPLET_COMMANDS ───────────────────────────────────────
ok(sweep_wrappers.ALLOWED_DROPLET_COMMANDS == ("cat", "head", "tail", "ls", "stat", "grep", "find",
                                                "journalctl", "systemctl", "crontab", "curl"),
   f"AC18: allowlist tuple must match exactly: {sweep_wrappers.ALLOWED_DROPLET_COMMANDS}")

# ── AC19: journalctl/tail follow + destructive flags ─────────────────────
for cmd, args in [
    ("journalctl", ["--vacuum-time=1d"]), ("journalctl", ["--rotate"]), ("journalctl", ["--flush"]),
    ("journalctl", ["--relinquish-var"]), ("journalctl", ["--smart-relinquish-var"]),
    ("journalctl", ["--setup-keys"]), ("journalctl", ["-f"]), ("journalctl", ["--follow"]),
    ("tail", ["-f", "/var/log/syslog"]),
]:
    ok(sweep_wrappers.droplet_command_refusal(cmd, args) is not None, f"AC19: must refuse {(cmd, args)}")
ok(sweep_wrappers.droplet_command_refusal("journalctl", ["-u", "bluedot-webhook", "--since", "1 hour ago"]) is None,
   "AC19: a plain journalctl call must admit")

# ── AC20: secret-path denylist ────────────────────────────────────────────
for cmd, args in [
    ("cat", ["/opt/albert-scott/.env"]), ("head", ["/root/.ssh/id_rsa"]),
    ("tail", ["/etc/shadow"]), ("grep", ["-r", "credentials", "/opt"]),
    ("cat", ["/etc/ssl/private/server.pem"]), ("tail", ["/opt/albert-scott/secret.key"]),
]:
    ok(sweep_wrappers.droplet_command_refusal(cmd, args) is not None, f"AC20: must refuse {(cmd, args)}")
ok(sweep_wrappers.droplet_command_refusal("grep", ["ERROR", "/var/log/syslog"]) is None,
   "AC20: a plain grep must admit")


# ── AC11-AC14, AC21: ro-sql subprocess behaviour ──────────────────────────
def run_ro_sql(project, sql, extra_env=None, doit_root=None):
    env = dict(os.environ)
    if doit_root is not None:
        env["DOIT_ROOT"] = str(doit_root)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, str(RO_SQL), project, sql],
                           capture_output=True, text=True, env=env)


ac11_root = harness.test_root("ac11-root")
project_checkout = ac11_root / "repos" / "demo-project"
project_checkout.mkdir(parents=True)
(project_checkout / ".env").write_text("SUPABASE_DB_URL_RO=postgresql://ro-fixture-dsn\n")
bin_dir = ac11_root / "bin"
bin_dir.mkdir()
capture_file = ac11_root / "psql-capture.txt"

write_stub(bin_dir / "psql", """\
    #!/bin/sh
    for a in "$@"; do printf '%s\\n' "$a"; done > "$CAPTURE_FILE"
    printf 'PGOPTIONS=%s\\n' "$PGOPTIONS" >> "$CAPTURE_FILE"
    printf '1\\n'
    exit 0
    """)

with path_prepended(bin_dir):
    r = run_ro_sql("demo-project", "SELECT '$HOME''x'",
                    extra_env={"CAPTURE_FILE": str(capture_file)}, doit_root=ac11_root)
    ok(r.stdout == "1\n", f"AC11: stdout must pass through verbatim: {r.stdout!r}")
    ok(r.returncode == 0, f"AC11: exit code must be 0: {r.returncode} stderr={r.stderr!r}")
    cap_lines = capture_file.read_text().splitlines()
    ok(cap_lines[-1] == "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=30000",
       f"AC11: PGOPTIONS must be forced read-only + statement_timeout: {cap_lines[-1]!r}")
    argv = cap_lines[:-1]
    ok(argv[0] == "postgresql://ro-fixture-dsn", f"AC11: argv[1] (argv[0] here) must be the DSN: {argv}")
    ok("-X" in argv and "ON_ERROR_STOP=1" in argv, f"AC11: -X and ON_ERROR_STOP=1 must both appear: {argv}")
    c_idx = argv.index("-c")
    ok(argv[c_idx + 1] == "SELECT '$HOME''x'",
       f"AC11: the element after -c must equal SQL byte-for-byte, no shell interpolation: {argv[c_idx + 1]!r}")

# AC12: refused SQL never invokes psql
marker12 = ac11_root / "marker12.txt"
write_stub(bin_dir / "psql", f"""\
    #!/bin/sh
    touch "{marker12}"
    exit 0
    """)
capture_file.unlink()
with path_prepended(bin_dir):
    r = run_ro_sql("demo-project", "DROP TABLE t", doit_root=ac11_root)
    ok(r.returncode == 2, f"AC12: refused SQL must exit 2: {r.returncode}")
    ok(r.stderr.strip() != "", "AC12: stderr must be non-empty")
    ok(r.stdout == "", f"AC12: stdout must be empty: {r.stdout!r}")
    ok(not marker12.exists(), "AC12: psql must never be invoked when SQL is refused")

# AC13: no usable DSN never invokes psql
ac13_root = harness.test_root("ac13-root")
no_dsn_checkout = ac13_root / "repos" / "demo-project"
no_dsn_checkout.mkdir(parents=True)
(no_dsn_checkout / ".env").write_text("OTHER=1\n")
marker13 = ac13_root / "marker13.txt"
write_stub(bin_dir / "psql", f"""\
    #!/bin/sh
    touch "{marker13}"
    exit 0
    """)
with path_prepended(bin_dir):
    r = run_ro_sql("demo-project", "SELECT 1", doit_root=ac13_root)
    ok(r.returncode == 2, f"AC13: no usable DSN must exit 2: {r.returncode}")
    ok(r.stderr.strip() != "", "AC13: stderr must be non-empty")
    ok(not marker13.exists(), "AC13: psql must never be invoked with no usable DSN")

# AC14: psql error passthrough -> exit 1
write_stub(bin_dir / "psql", """\
    #!/bin/sh
    printf 'ERROR: relation does not exist\\n' >&2
    exit 3
    """)
with path_prepended(bin_dir):
    r = run_ro_sql("demo-project", "SELECT 1", doit_root=ac11_root)
    ok(r.returncode == 1, f"AC14: psql error must map to exit 1: {r.returncode}")
    ok("ERROR: relation does not exist" in r.stderr, f"AC14: stderr must contain psql's own error: {r.stderr!r}")

# AC21: local timeout -> exit 3
write_stub(bin_dir / "psql", """\
    #!/bin/sh
    sleep 5
    printf 'should never be seen\\n'
    """)
with path_prepended(bin_dir):
    r = run_ro_sql("demo-project", "SELECT 1", extra_env={"SWEEP_SUBPROCESS_TIMEOUT_SECONDS": "1"},
                    doit_root=ac11_root)
    ok(r.returncode == 3, f"AC21: a hung psql must exit 3: {r.returncode}")
    ok("timed out" in r.stderr, f"AC21: stderr must say timed out: {r.stderr!r}")
    ok(r.stdout == "", f"AC21: stdout must be empty: {r.stdout!r}")


# ── AC15-AC17, AC22: droplet-read subprocess behaviour ────────────────────
def run_droplet_read(args, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, str(DROPLET_READ), *args],
                           capture_output=True, text=True, env=env)


ssh_root = harness.test_root("ssh-bin")
ssh_bin_dir = ssh_root / "bin"
ssh_bin_dir.mkdir()
ssh_capture = ssh_root / "ssh-capture.txt"

write_stub(ssh_bin_dir / "ssh", """\
    #!/bin/sh
    for a in "$@"; do printf '%s\\n' "$a"; done > "$CAPTURE_FILE"
    exit 0
    """)

with path_prepended(ssh_bin_dir):
    r = run_droplet_read(["albert-scott", "cat", "/etc/hostname"], extra_env={"CAPTURE_FILE": str(ssh_capture)})
    ok(r.returncode == 0, f"AC15: exit code must be 0: {r.returncode} stderr={r.stderr!r}")
    argv = ["ssh"] + ssh_capture.read_text().splitlines()
    ok(argv == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "root@167.71.46.51", "cat /etc/hostname"],
       f"AC15: captured argv must match exactly: {argv}")

# AC16: refused command never invokes ssh
ssh_marker16 = ssh_root / "marker16.txt"
write_stub(ssh_bin_dir / "ssh", f"""\
    #!/bin/sh
    touch "{ssh_marker16}"
    exit 0
    """)
with path_prepended(ssh_bin_dir):
    r = run_droplet_read(["albert-scott", "rm", "-rf", "/"])
    ok(r.returncode == 2, f"AC16: refused command must exit 2: {r.returncode}")
    ok(r.stderr.strip() != "", "AC16: stderr must be non-empty")
    ok(not ssh_marker16.exists(), "AC16: ssh must never be invoked when the command is refused")

# AC17: unknown project never invokes ssh
ssh_marker17 = ssh_root / "marker17.txt"
write_stub(ssh_bin_dir / "ssh", f"""\
    #!/bin/sh
    touch "{ssh_marker17}"
    exit 0
    """)
with path_prepended(ssh_bin_dir):
    r = run_droplet_read(["no-such-project-xyz", "cat", "/etc/hostname"])
    ok(r.returncode == 2, f"AC17: unknown project must exit 2: {r.returncode}")
    ok(r.stderr.strip() != "", "AC17: stderr must be non-empty")
    ok(not ssh_marker17.exists(), "AC17: ssh must never be invoked for an unknown project")

# AC22: local timeout -> exit 3
write_stub(ssh_bin_dir / "ssh", """\
    #!/bin/sh
    sleep 5
    printf 'should never be seen\\n'
    """)
with path_prepended(ssh_bin_dir):
    r = run_droplet_read(["albert-scott", "cat", "/etc/hostname"],
                          extra_env={"SWEEP_SUBPROCESS_TIMEOUT_SECONDS": "1"})
    ok(r.returncode == 3, f"AC22: a hung ssh must exit 3: {r.returncode}")
    ok("timed out" in r.stderr, f"AC22: stderr must say timed out: {r.stderr!r}")
    ok(r.stdout == "", f"AC22: stdout must be empty: {r.stdout!r}")

for root in (no_env_root, no_key_root, collide_root, distinct_root, ac11_root, ac13_root, ssh_root):
    harness.cleanup(root)

print(f"sweep_wrappers: {n} checks pass")
