#!/usr/bin/env python3
"""Checks on the sweeper's two production wrappers: L-spec-0386 and L-spec-1007.
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
import json
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
                                                "journalctl", "systemctl", "crontab", "curl", "ps"),
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

# L-spec-1007: closed process reads and value-free environment state reads.
def checked(cond, why):
    ok(cond, why)
    print(why)


checked(sweep_wrappers._PS_FIELDS == ("pid", "ppid", "comm", "etime", "args"),
        "1007 AC1: _PS_FIELDS tuple equality")
for fields in (*sweep_wrappers._PS_FIELDS, "pid,etime,args", "pid,ppid,comm,etime", "pid,pid"):
    checked(sweep_wrappers.droplet_command_refusal("ps", ["-eo", fields]) is None,
            f"1007 AC1: admit ps {['-eo', fields]!r}")
for args in [[], ["aux"], ["-ef"], ["e"], ["-eo", "args", "-w"],
             *(["-eo", field] for field in ("environ", "env", "cmd", "command", "ucmd", "user", "uid")),
             ["-eo", "args;id"], ["-eo", "pid,"], ["-p", "1"], ["-eo", "args,env"],
             ["-eo", ""], ["-eo", ",pid"], ["-eo", "pid,,args"], ["-eo", "pid\nargs"]]:
    checked(bool(sweep_wrappers.droplet_command_refusal("ps", args)),
            f"1007 AC1: refuse ps {args!r}")

for key in ("ADS_BRIDGE_CATALOG_MAP_CLIENTS", "WEBHOOK_SECRET", "A", "A" * 64):
    checked(sweep_wrappers.named_read_refusal("env-has", [key]) is None,
            f"1007 AC2: admit env-has {[key]!r}")
for args in [[], ["a_b"], ["1A"], ["A=b"], ["A", "B"], ["A.*"], ["A;B"],
             ["A B"], ["A" * 65], [""], ["export A"], ["A\n"], ["A\r"],
             *(["A" + char + "B"] for char in sweep_wrappers._METACHARS)]:
    checked(bool(sweep_wrappers.named_read_refusal("env-has", args)),
            f"1007 AC2: refuse env-has {args!r}")
checked(bool(sweep_wrappers.named_read_refusal("no-such-read", ["A"])),
        "1007 AC2: refuse unknown named read")

remote_cmd, remote_args = sweep_wrappers.named_read_remote("env-has", ["WEBHOOK_SECRET"], "/opt/albert-scott")
checked((remote_cmd, remote_args) == ("awk", ["-v", "k=WEBHOOK_SECRET", sweep_wrappers._ENV_HAS_AWK,
                                           "/opt/albert-scott/.env"]),
        "1007 AC3: named_read_remote exact argv")
remote = sweep_wrappers.quote_remote_command(remote_cmd, remote_args)
checked(shlex.split(remote) == [remote_cmd, *remote_args], "1007 AC3: remote argv shlex round trip")
write_stub(ssh_bin_dir / "ssh", """\
    #!/usr/bin/env python3
    import json, os, sys
    with open(os.environ["CAPTURE_FILE"], "w") as capture:
        json.dump(["ssh", *sys.argv[1:]], capture)
    """)
with path_prepended(ssh_bin_dir):
    for call, expected_remote in [(["env-has", "WEBHOOK_SECRET"], remote),
                                  (["ps", "-eo", "pid,etime,args"], "ps -eo pid,etime,args")]:
        r = run_droplet_read(["albert-scott", *call], {"CAPTURE_FILE": str(ssh_capture)})
        captured = json.loads(ssh_capture.read_text())
        checked(r.returncode == 0 and captured == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                                   "root@167.71.46.51", expected_remote],
                f"1007 AC3: captured ssh argv {captured!r}")

checked(sweep_wrappers.ALLOWED_DROPLET_COMMANDS == ("cat", "head", "tail", "ls", "stat", "grep", "find",
                                                   "journalctl", "systemctl", "crontab", "curl", "ps"),
        "1007 AC4: ALLOWED_DROPLET_COMMANDS exact tuple equality (only ps added)")
checked(sweep_wrappers.NAMED_DROPLET_READS == ("env-has",), "1007 AC4: NAMED_DROPLET_READS exact tuple equality")
checked(sweep_wrappers.ENV_STATES == ("ABSENT", "PRESENT-EMPTY", "PRESENT-NONEMPTY"),
        "1007 AC9: ENV_STATES exact tuple equality")
for cmd in ("cat", "head", "tail", "grep"):
    for secret_path in ("/opt/albert-scott/.env", "/root/.ssh/id_rsa", "/etc/shadow"):
        checked(bool(sweep_wrappers.droplet_command_refusal(cmd, [secret_path])),
                f"1007 AC4: refuse {cmd} {secret_path}")
for cmd, args in [("curl", ["-s", "-X", "POST", "http://127.0.0.1:8000/health"]),
                  ("curl", ["-s", "-XPOST", "http://127.0.0.1:8000/health"]),
                  ("find", ["/tmp", "-delete"]), ("systemctl", ["restart", "x"])]:
    checked(bool(sweep_wrappers.droplet_command_refusal(cmd, args)), f"1007 AC4: refuse {cmd} {args!r}")
marker = ssh_root / "1007-refused-marker"
write_stub(ssh_bin_dir / "ssh", f"#!/bin/sh\ntouch '{marker}'\n")
with path_prepended(ssh_bin_dir):
    for call in (["ps", "aux"], ["env-has", "a_b"], ["rm", "-rf", "/"]):
        r = run_droplet_read(["albert-scott", *call])
        checked(r.returncode == 2 and bool(r.stderr.strip()) and r.stdout == "" and not marker.exists(),
                f"1007 AC4: refused {call!r}, exit 2, stderr nonempty, ssh marker absent")

for row, cmd, args, admit in [
    ("L-spec-0305/AC13", "journalctl", ["-u", "bluedot-webhook", "--since", "1 hour ago"], True),
    ("L-spec-0407/AC7", "curl", ["-s", "http://127.0.0.1:8000/health"], True),
    ("L-spec-0407/AC7 L-spec-0791/AC4", "journalctl", ["-u", "bluedot-webhook"], True),
    ("L-spec-0791/AC4", "env-has", ["WEBHOOK_SECRET"], True),
    ("L-spec-0792/AC4", "ps", ["-eo", "pid,etime,args"], True),
    ("L-spec-0895/AC6", "env-has", ["ADS_BRIDGE_CATALOG_MAP_CLIENTS"], True),
    ("L-spec-0393/AC1", "python3", ["-m", "latency_report", "--window-min", "60", "--json"], False),
    ("L-spec-0393/AC1", "bash", ["-c", "id"], False),
]:
    gate = sweep_wrappers.named_read_refusal if cmd in sweep_wrappers.NAMED_DROPLET_READS else sweep_wrappers.droplet_command_refusal
    checked((gate(cmd, args) is None) == admit,
            f"1007 AC5: {row}: {'admit' if admit else 'refuse'} {cmd} {args!r}")

with path_prepended(ssh_bin_dir):
    for key, token, status in [("MISSING_KEY", "ABSENT", 0), ("WEBHOOK_SECRET", "PRESENT-NONEMPTY", 0),
                               ("WEBHOOK_SECRET", "", 7)]:
        write_stub(ssh_bin_dir / "ssh", f"#!/bin/sh\nprintf '%s' '{token + chr(10) if token else ''}'\nexit {status}\n")
        r = run_droplet_read(["albert-scott", "env-has", key])
        expected = token + "\n" if token else ""
        checked(r.returncode == status and r.stdout == expected and r.stderr == "",
                f"1007 AC6: {key}: exit {status}, stdout {expected!r}, stderr empty")
    write_stub(ssh_bin_dir / "ssh", "#!/bin/sh\nexec sleep 5\n")
    r = run_droplet_read(["albert-scott", "env-has", "WEBHOOK_SECRET"],
                         {"SWEEP_SUBPROCESS_TIMEOUT_SECONDS": "1"})
    checked(r.returncode == 3 and "timed out" in r.stderr and r.stdout == "",
            "1007 AC6: env-has timeout: exit 3, stderr timed out, stdout empty")

source = DROPLET_READ.read_text()
for symbol in ("NAMED_DROPLET_READS", "named_read_refusal", "named_read_remote"):
    checked(symbol in source, f"1007 AC7: droplet-read source contains {symbol}")


def deploy_env_state(contents, key):
    """State-only transcription of deploy.sh's _assert_env_keys_gate."""
    assignments = [line for line in contents.split("\n")
                   if not line.lstrip(" \t").startswith("#") and line.startswith(key + "=")]
    if not assignments:
        return "ABSENT"
    value = assignments[-1].split("=", 1)[1]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return "PRESENT-NONEMPTY" if value else "PRESENT-EMPTY"


fixture_root = harness.test_root("1007-env-states")
fixture = fixture_root / "fixture.env"
for label, contents, expected in [
    ("unquoted nonempty", "WEBHOOK_SECRET=abc\n", "PRESENT-NONEMPTY"),
    ("unquoted empty", "WEBHOOK_SECRET=\n", "PRESENT-EMPTY"),
    ("double-quoted empty", 'WEBHOOK_SECRET=""\n', "PRESENT-EMPTY"),
    ("single-quoted empty", "WEBHOOK_SECRET=''\n", "PRESENT-EMPTY"),
    ("quoted nonempty", 'WEBHOOK_SECRET="x"\n', "PRESENT-NONEMPTY"),
    ("last empty wins", "WEBHOOK_SECRET=abc\nWEBHOOK_SECRET=\n", "PRESENT-EMPTY"),
    ("last nonempty wins", "WEBHOOK_SECRET=\nWEBHOOK_SECRET=abc\n", "PRESENT-NONEMPTY"),
    ("export ignored", "export WEBHOOK_SECRET=abc\n", "ABSENT"),
    ("indent ignored", "  WEBHOOK_SECRET=abc\n", "ABSENT"),
    ("comment ignored", "# WEBHOOK_SECRET=abc\n", "ABSENT"),
    ("indented comment ignored", " \t# WEBHOOK_SECRET=abc\n", "ABSENT"),
    ("exact key only", "WEBHOOK_SECRET_X=abc\n", "ABSENT"),
    ("absent key", "OTHER=abc\n", "ABSENT"),
    ("empty file", "", "ABSENT"),
    ("one quote layer only", "WEBHOOK_SECRET=\"''\"\n", "PRESENT-NONEMPTY"),
    ("lone quote retained", 'WEBHOOK_SECRET="\n', "PRESENT-NONEMPTY"),
    ("whitespace retained", "WEBHOOK_SECRET= \n", "PRESENT-NONEMPTY"),
    ("CRLF retained", "WEBHOOK_SECRET=\r\n", "PRESENT-NONEMPTY"),
    ("unterminated last line", "WEBHOOK_SECRET=abc", "PRESENT-NONEMPTY"),
]:
    fixture.write_bytes(contents.encode())
    cmd, args = sweep_wrappers.named_read_remote("env-has", ["WEBHOOK_SECRET"], "/unused")
    args[-1] = str(fixture)
    result = subprocess.run([cmd, *args], capture_output=True, text=True)
    checked(result.returncode == 0 and result.stderr == "" and result.stdout == expected + "\n"
            and expected == deploy_env_state(contents, "WEBHOOK_SECRET"),
            f"1007 AC9: {label}: {expected}, exact token-only stdout, matches deploy state")

for root in (no_env_root, no_key_root, collide_root, distinct_root, ac11_root, ac13_root, ssh_root, fixture_root):
    harness.cleanup(root)

print(f"sweep_wrappers: {n} checks pass")
