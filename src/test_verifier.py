#!/usr/bin/env python3
"""verifier — the full-environment owed-check lane (L-spec-0682 R1, R3: AC1, AC2, AC4, AC9, AC10).
Run: python3 test_verifier.py

Local stack: a private-pg scratch cluster (postgresql 17), started and removed under the scratch root
only, never reaching any production DSN (every SUPABASE_DB_URL* is unset before the script runs)."""
import contextlib, io, json, os, pathlib, shutil, subprocess, sys, tempfile, time
from datetime import datetime, timedelta, timezone

ORIG_HOME = pathlib.Path(os.path.expanduser("~"))
_BASE_PARENT = ORIG_HOME / "doit-scratch"
BASE = pathlib.Path(tempfile.mkdtemp(prefix="verifier-test-", dir=_BASE_PARENT if _BASE_PARENT.is_dir() else None))
SCRATCH_ROOT = BASE / "root"
SCRATCH_ROOT.mkdir()
os.environ["HOME"], os.environ["DOIT_ROOT"] = str(BASE / "home"), str(BASE / "droot")
os.environ["DOIT_SCRATCH"] = str(SCRATCH_ROOT)
for k in list(os.environ):
    if k.startswith(("SUPABASE_DB_URL", "DOIT_PROJECT", "DOIT_LEDGER_FILE")) or k in ("DATABASE_URL", "PGHOST", "PGPORT"):
        os.environ.pop(k)
ROOT = pathlib.Path(os.environ["DOIT_ROOT"])
EV = ROOT / "events"
EV.mkdir(parents=True, exist_ok=True)
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fold, owed, verifier  # noqa: E402

N = 0
NOW = datetime.now(timezone.utc).replace(microsecond=0)
PGBIN = pathlib.Path("/usr/lib/postgresql/17/bin")
RESET = HERE.parent / "scripts" / "scratch-pg-reset.sh"
PRIVATE_PG = HERE.parent / "scripts" / "private-pg.sh"
SCHEME = "post" + "gresql"      # a seeded FAKE DSN is assembled at run time; no credential literal lives in this file
ROLES = ("portal_reader", "anon", "authenticated", "service_role")


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


def iso(**kw):
    return (NOW + timedelta(**kw)).isoformat(timespec="seconds")


def write(actor, rows):
    with (EV / f"L-{actor}-0001.jsonl").open("a") as fh:
        for r in rows:
            fh.write(json.dumps({"v": 1, **r}) + "\n")


def spec(sid, crit="AC1", shipped_ago_d=2):
    write("spec-writer", [{"ts": iso(days=-shipped_ago_d - 1), "type": "owed-ac", "subject": sid, "criterion": crit,
                           "wake_at": iso(days=-shipped_ago_d), "line": f"{crit} OWED prod read"}])
    write("executor", [{"ts": iso(days=-shipped_ago_d), "type": "shipped", "subject": sid}])


def fresh_process(code):
    """Run `code` in a NEW python process against the same ledger; returns its parsed JSON stdout."""
    r = subprocess.run([sys.executable, "-c", "import sys, json; sys.path.insert(0, sys.argv[1])\n" + code, str(HERE)],
                       capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_pass_records_one_verdict_per_check_round_trip():
    for s in ("L-spec-6201", "L-spec-6202", "L-spec-6203"):
        spec(s)
    obs = {"L-spec-6201": {"verdict": "met", "evidence": "the portal tile renders 12 rows (GET 200)"},
           "L-spec-6202": {"verdict": "failed", "evidence": "tacos-tracker returned 504", "kind": "unmet"},
           "L-spec-6203": {"verdict": "blocked", "capability": "operator-action",
                           "why": "needs the production deposit to land first", "evidence": "no deposit yet"}}
    lines = verifier.run(observe=lambda r: obs[r["spec"]], now=NOW)
    check(len([l for l in lines if "->" in l]) == 3, f"three checks observed: {lines}")
    rows = fresh_process("""
import fold
from datetime import datetime, timezone
out = {}
for e in fold.read_events():
    if e.get('type') in ('owed-met', 'owed-failed', 'owed-unobservable') and e.get('actor') == 'verifier':
        out.setdefault(e['subject'], []).append([e['type'], e.get('criterion'), e.get('evidence') or ''])
print(json.dumps(out))
""")
    check(set(rows) == set(obs), f"a verdict for every check: {rows}")
    check({k: [v[0] for v in vs] for k, vs in rows.items()} ==
          {"L-spec-6201": ["owed-met"], "L-spec-6202": ["owed-failed"], "L-spec-6203": ["owed-unobservable"]},
          f"exactly one of the three verdict types per check, none duplicated: {rows}")
    check(all(v[0][2].strip() for v in rows.values()), f"every verdict carries non-empty evidence: {rows}")
    by = fold.fold(fold.read_events())[3]
    st = {s: owed.checks(by[s], NOW)[0] for s in obs}
    check(st["L-spec-6201"]["status"] == "met" and st["L-spec-6202"]["failures"] == 1
          and st["L-spec-6203"]["last_unobservable_capability"] == "operator-action",
          f"the production fold reads the same three verdicts: {[(s, r['status']) for s, r in st.items()]}")
    # an observer that returns nothing usable is recorded, not skipped
    spec("L-spec-6204")
    verifier.run(observe=lambda r: {"verdict": "met", "evidence": ""}, now=NOW, only=["L-spec-6204/AC1"])
    row = owed.checks(fold.fold(fold.read_events())[3]["L-spec-6204"], NOW)[0]
    check(row["status"] != "met" and row["last_unobservable_capability"] == "other",
          f"a verdict with no evidence is never a met: {row['status']}")


def test_failed_files_one_rework_brief_deduped():
    sid = "L-spec-6301"
    spec(sid, "AC7")
    failing = lambda r: {"verdict": "failed", "evidence": "cache-hit measured 212 ms against the 100 ms bound"}
    verifier.run(observe=failing, now=NOW)
    briefs = lambda: [e for e in fold.read_events() if e.get("type") == "brief" and e.get("subject") == sid]
    check(len(briefs()) == 1, f"one brief after the first pass: {briefs()}")
    b = briefs()[0]
    check(b["criterion"] == "AC7" and "212 ms" in b["evidence"] and "AC7" in b["why"], f"it names the criterion and the evidence: {b}")
    verifier.run(observe=failing, now=NOW)
    verifier.run(observe=failing, now=NOW, only=[f"{sid}/AC7"])
    check(len(briefs()) == 1, f"a second pass over the same failure files no second brief: {len(briefs())}")
    check(len([e for e in fold.read_events() if e.get("type") == "owed-failed" and e.get("subject") == sid]) in (1, 2),
          "(the forced --check re-observation may add a verdict, never a brief)")
    # a rework re-dates the check; the SAME failure observed again afterwards is a new failure, briefed once more
    time.sleep(2)                                  # the ledger stamps whole seconds; "after" is a strict >
    redate = datetime.now(timezone.utc).replace(microsecond=0).isoformat(timespec="seconds")
    write("spec-writer", [{"ts": redate, "type": "owed-ac", "subject": sid, "criterion": "AC7",
                           "wake_at": redate, "line": "AC7 OWED prod read"}])
    time.sleep(2)
    later = NOW + timedelta(hours=1)
    verifier.run(observe=failing, now=later)
    check(len(briefs()) == 2, f"a failure after a rework is a new brief: {len(briefs())}")


def test_preflight_lines_and_no_secret_leak():
    fake_dsn_pw, fake_tok = "FAKEDSNPASSWORD123", "FAKEVERIFIERSECRET456"
    env_file = ROOT / "repos" / "albert-scott" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(f"SUPABASE_DB_URL_RO={SCHEME}://ro_user:{fake_dsn_pw}@db.example.invalid:5432/postgres\n")
    os.environ["DOIT_VERIFIER_PORTAL_TOKEN"] = fake_tok
    try:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = verifier.main(["--preflight"])
        lines = out.getvalue().splitlines()
        check(len(lines) == 7 and [l.split(":")[0] for l in lines] == list(verifier.COMPONENTS), f"seven lines, in order: {lines}")
        check(all(l.endswith(": present") or ": absent: " in l for l in lines), f"each is present or absent: <why>: {lines}")
        check(lines[-1] == "read-only prod DSN: present", f"the seeded read-only DSN is seen: {lines[-1]}")
        check(rc == 1 and any(": absent: " in l for l in lines), "exit 1 when any component is absent")
        check(not any(s in out.getvalue() + err.getvalue() for s in (fake_dsn_pw, fake_tok)), "no secret on stdout or stderr")
        # a probe that blows up with a secret in its message is scrubbed
        leaky = {n: (lambda ctx: None) for n in verifier.COMPONENTS}
        leaky["git"] = lambda ctx: f"connection to {SCHEME}://u:{fake_dsn_pw}@h/db failed with {fake_tok}"
        ls, ok = verifier.preflight(probes=leaky)
        check(not ok and fake_dsn_pw not in "\n".join(ls) and fake_tok not in "\n".join(ls), f"a leaking probe is redacted: {ls[0]}")
        boom = dict(leaky, git=lambda ctx: (_ for _ in ()).throw(RuntimeError(f"token {fake_tok}")))
        ls, ok = verifier.preflight(probes=boom)
        check(fake_tok not in "\n".join(ls) and ls[0].startswith("git: absent: probe failed"), f"a raising probe is redacted: {ls[0]}")
        ls, ok = verifier.preflight(probes={n: (lambda ctx: None) for n in verifier.COMPONENTS})
        check(ok and all(l.endswith(": present") for l in ls), "all present -> ok")
        r = subprocess.run([str(HERE.parent / "doit"), "verify-owed", "--preflight"], capture_output=True, text=True, env=dict(os.environ))
        check(len(r.stdout.splitlines()) == 7 and r.returncode in (0, 1), f"the real command prints seven lines: {r.stdout!r}")
        check(not any(s in r.stdout + r.stderr for s in (fake_dsn_pw, fake_tok)), "no secret from the real command either")
    finally:
        os.environ.pop("DOIT_VERIFIER_PORTAL_TOKEN", None)
        env_file.unlink()


# ── the scratch reset tool (local stack: private-pg, postgresql 17) ──────────────────────────────

def _psql(d, port, db, sql, check_rc=True):
    r = subprocess.run([str(PGBIN / "psql"), "-X", "-q", "-At", "-h", str(d), "-p", str(port), "-U", "albert", "-d", db, "-c", sql],
                       capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    if check_rc:
        assert r.returncode == 0, r.stderr
    return r


def _cluster(d):
    d.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SUPABASE_")}
    r = subprocess.run(["bash", str(PRIVATE_PG), str(d)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    sock = next(p for p in d.glob(".s.PGSQL.*") if p.is_socket())
    return int(sock.name.rsplit(".", 1)[1])


def _stop(d):
    subprocess.run([str(PGBIN / "pg_ctl"), "-D", str(d / "data"), "-m", "immediate", "stop"], capture_output=True)


def _run_reset(d, **env_extra):
    env = {k: v for k, v in os.environ.items()}
    env.update(env_extra)
    return subprocess.run(["bash", str(RESET), str(d)], capture_output=True, text=True, env=env)


def _seed(d, port):
    for r in ROLES:
        _psql(d, port, "postgres", f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{r}') THEN CREATE ROLE {r} NOLOGIN; END IF; END $$")
    _psql(d, port, "postgres", "CREATE DATABASE as_l0447_parity")
    _psql(d, port, "as_l0447_parity", "CREATE TABLE t (i int); " + " ".join(f"GRANT SELECT ON t TO {r};" for r in ROLES))
    _psql(d, port, "postgres", "CREATE DATABASE unrelated")
    _psql(d, port, "unrelated", "CREATE TABLE keep (i int); INSERT INTO keep VALUES (1), (2), (3)")
    _psql(d, port, "scratch", "CREATE TABLE s (i int); GRANT SELECT ON s TO service_role")


def _dbs(d, port):
    return set(_psql(d, port, "postgres", "SELECT datname FROM pg_database").stdout.split())


def test_scratch_reset_clears_governed_role_ownership():
    d = SCRATCH_ROOT / "pg-reset"
    port = _cluster(d)
    try:
        _seed(d, port)
        before = _psql(d, port, "postgres", "DROP ROLE portal_reader", check_rc=False)
        check(before.returncode != 0 and "depend" in before.stderr,
              f"the governed role cannot be dropped while a leftover database holds its grants: {before.stderr.strip()[:120]}")
        dependency_count = int(_psql(d, port, "postgres", "SELECT count(DISTINCT d.datname) FROM pg_shdepend s JOIN pg_database d ON d.oid=s.dbid JOIN pg_roles r ON r.oid=s.refobjid WHERE r.rolname IN ('portal_reader','anon','authenticated','service_role') AND d.datname NOT IN ('postgres','template0','template1')").stdout.strip())
        check(dependency_count == 2, f"two fixture databases have governed-role dependencies: {dependency_count}")
        print(f"scratch reset pre-count: {dependency_count} affected fixture databases")
        r = _run_reset(d)
        check(r.returncode == 0, f"reset succeeds: {r.stderr}")
        dbs = _dbs(d, port)
        check("as_l0447_parity" not in dbs, f"the leftover database is gone: {dbs}")
        check({"unrelated", "scratch", "postgres"} <= dbs, f"the other databases are untouched: {dbs}")
        check(_psql(d, port, "unrelated", "SELECT count(*) FROM keep").stdout.strip() == "3", "the unrelated database's data is intact")
        check(_psql(d, port, "scratch", "SELECT count(*) FROM s").stdout.strip() == "0", "the primary scratch database itself survives")
        for role in ROLES:
            after = _psql(d, port, "postgres", f"DROP ROLE {role}", check_rc=False)
            check(after.returncode == 0, f"after the reset {role} drops cleanly: {after.stderr.strip()}")
        again = _run_reset(d)
        check(again.returncode == 0 and "0 leftover" in again.stdout, f"a second reset is a no-op: {again.stdout}")
    finally:
        _stop(d)
        shutil.rmtree(str(SCRATCH_ROOT / "pg-reset"), ignore_errors=True)


def test_scratch_reset_refuses_prod_and_non_scratch():
    d = SCRATCH_ROOT / "pg-refuse"
    other = BASE / "pg-outside-scratch-root"
    port, oport = _cluster(d), _cluster(other)
    try:
        _seed(d, port)
        _seed(other, oport)
        r = _run_reset(d, SUPABASE_DB_URL="postgresql://someone@prod.example.invalid/postgres")
        check(r.returncode != 0 and "refused" in r.stderr and "SUPABASE_DB_URL" in r.stderr, f"refuses with SUPABASE_DB_URL set: {r.stderr!r}")
        check("as_l0447_parity" in _dbs(d, port), "nothing executed: the leftover database is still there")
        r = _run_reset(other)
        check(r.returncode != 0 and "not under the scratch root" in r.stderr, f"refuses a cluster outside the scratch root: {r.stderr!r}")
        check("as_l0447_parity" in _dbs(other, oport), "nothing executed on the outside cluster")
        notcluster = SCRATCH_ROOT / "just-a-dir"
        notcluster.mkdir()
        r = _run_reset(notcluster)
        check(r.returncode != 0 and "no private-pg cluster" in r.stderr, f"refuses a directory that is no cluster: {r.stderr!r}")
        r = subprocess.run(["bash", str(RESET)], capture_output=True, text=True, env=dict(os.environ))
        check(r.returncode != 0 and "usage" in r.stderr, "refuses with no argument")
        check(r.stdout == "", "refusals print nothing to stdout")
    finally:
        _stop(d)
        _stop(other)
        shutil.rmtree(str(SCRATCH_ROOT / "pg-refuse"), ignore_errors=True)
        shutil.rmtree(str(other), ignore_errors=True)


def test_dispatch_path_builds_a_real_verifier_packet():
    """No observer handed in: the checks go to ONE detached `owed-verifier` dispatch. The packet build is real
    (`doit packet owed-verifier`); the spawn itself is stubbed — nothing here starts a model."""
    import dispatch, sweep_owed
    sid = "L-spec-6401"
    spec(sid, "AC1")
    (ROOT / "content").mkdir(parents=True, exist_ok=True)
    dispatch.spec_path(sid).write_text(f"# {sid}\n\n# Acceptance criteria\n\n## Acceptance\n\n"
                                       "AC1 [observed-data]: OWED (prod read). The tile shows rows.\n"
                                       "  review_path: read the tile; worked if rows show.\n")
    calls = []

    class Rec(sweep_owed.Runner):
        def run(self, argv):
            calls.append(argv[1:3])
            if argv[1] == "dispatch":
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, '{"detached": 1}', "")
            return super().run(argv)

    lines = verifier.run(runner=Rec(), now=NOW, only=[f"{sid}/AC1"])
    check(calls[0] == ["packet", "owed-verifier"] and calls[1] == ["dispatch", "owed-verifier"], f"packet then dispatch: {calls}")
    argv = calls[2]
    check("--detach" in argv and "--packet" in argv and "--project" in argv, f"a detached dispatch naming the packet and project: {argv}")
    packet_text = pathlib.Path(argv[argv.index("--packet") + 1]).read_text()
    check(sid in packet_text and "AC1" in packet_text and "review_path" in packet_text,
          "the packet carries the criterion's own full block")
    check(any("dispatched 1 check" in l for l in lines), f"the pass says what it dispatched: {lines}")
    check("owed-verifier" in dispatch.ROLES and dispatch.ROLES["owed-verifier"][1] == 90, "the role is registered with a 90-minute cap")
    started = {"ts": iso(minutes=-5), "type": "spawn-started", "subject": "L-sweep-0999", "role": "owed-verifier", "spawn": "L-owed-verifier-0001"}
    write("owed-verifier", [started])
    calls.clear()
    lines = verifier.run(runner=Rec(), now=NOW, only=[f"{sid}/AC1"])
    check(calls == [] and any("already open" in l for l in lines), f"one open batch at a time: {lines}")
    # Browser/repo waits now have status blocked on main, and still enter this lane.
    for key, cap in (("L-spec-6501", "browser"), ("L-spec-6502", "elapsed"), ("L-spec-6503", "operator-action")):
        spec(key)
        write("owed-sweeper", [{"ts": iso(hours=-7), "type": "owed-unobservable", "subject": key,
                                "criterion": "AC1", "capability": cap, "why": "waiting for the environment"}])
    by = fold.fold(fold.read_events())[3]
    selected = verifier.candidates({sid: by[sid] for sid in ("L-spec-6501", "L-spec-6502", "L-spec-6503")}, NOW)
    check([r["spec"] for r in selected] == ["L-spec-6501"], "blocked browser checks are retried; elapsed and operator waits remain explicit")
    # the contract and the schema exist and the schema validates the verdict shapes
    out = BASE / "verdicts.json"
    out.write_text(json.dumps({"batch": "L-sweep-0001", "contamination": False, "escalations": [], "declarations": [],
                               "results": [{"spec": sid, "criterion": "AC1", "verdict": "met", "evidence": "saw rows"},
                                           {"spec": sid, "criterion": "AC2", "verdict": "cannot-observe",
                                            "capability": "operator-action", "why": "waiting on the deposit"}]}))
    r = subprocess.run([str(HERE.parent / "doit"), "validate", "owed-verifier", str(out)], capture_output=True, text=True, env=dict(os.environ))
    check("VALID" in r.stdout and "INVALID" not in r.stdout, f"the verdict shape validates against the contract's schema: {r.stdout}{r.stderr}")


test_pass_records_one_verdict_per_check_round_trip()
test_failed_files_one_rework_brief_deduped()
test_preflight_lines_and_no_secret_leak()
test_scratch_reset_clears_governed_role_ownership()
test_scratch_reset_refuses_prod_and_non_scratch()
test_dispatch_path_builds_a_real_verifier_packet()
shutil.rmtree(str(BASE), ignore_errors=True)
print(f"verifier: {N} checks pass")

# L-spec-1504: declared scripts use real ledger appends and never model seats.
import unittest
from test_provision import ProvisionTests
_lane_result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
    ProvisionTests("test_script_lanes_emit_real_verdicts_without_dispatch")]))
assert _lane_result.wasSuccessful(), "shared check-runner lane regression"
