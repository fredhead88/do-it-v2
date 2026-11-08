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
    # These are the same failure epoch, independent of a wall-clock second boundary.
    import dispatch
    from unittest.mock import patch
    with patch.object(dispatch, 'now', return_value=NOW.isoformat()):
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



def test_2238_shared_admission_reservations_and_concurrency():
    import hashlib, threading
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import patch
    import dispatch, sweep_owed
    quiet = dict(mem_avail_gb=20, swap_free_gb=8, load1_per_core=.5,
                 cpu_some10=0, mem_some10=0, mem_full10=0)
    hard = quiet | dict(mem_avail_gb=4, swap_free_gb=0, load1_per_core=3.5,
                        cpu_some10=90, mem_some10=6, mem_full10=2)
    summaries = []
    for order in ('verifier-first', 'sweep-first', 'concurrent', 'drain'):
        with tempfile.TemporaryDirectory(prefix='owed-reservations-', dir=BASE) as tmp, contextlib.ExitStack() as stack:
            root = pathlib.Path(tmp)
            for name in ('events','content','home'):
                (root/name).mkdir()
            stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(root), HOME=str(root/'home')))
            for module in (dispatch, fold, sweep_owed.packet):
                for attr in ('ROOT','CONTENT','EVENTS','PACKETS'):
                    if hasattr(module, attr):
                        stack.enter_context(patch.object(module, attr, root if attr=='ROOT' else root/attr.lower()))
            stack.enter_context(patch.object(fold, 'PROJECT', None))
            clock = stack.enter_context(patch.object(dispatch, 'now', return_value=iso(hours=-2)))
            telemetry = stack.enter_context(patch.object(sweep_owed.loadgate, 'readings', return_value=quiet))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            seeds = root/'events/L-spec-writer-seed.jsonl'
            for i in range(25):
                sid=f'L-spec-{7200+i:04d}'
                dispatch.spec_path(sid).write_text('## Acceptance criteria\nAC1 [backend]: scratch observation\n')
                for kind, fields in (('spec-written', dict(project='demo')),
                    ('owed-ac', dict(criterion='AC1',wake_at=iso(hours=-1),line='scratch')),
                    ('shipped', {})):
                    target = root/'events/L-executor-seed.jsonl' if kind == 'shipped' else seeds
                    assert dispatch.emit(target, dict(subject=sid), kind, **fields) is None
            clock.return_value = NOW.isoformat()
            batches, calls = [], []
            class Worker:
                def run(self, argv):
                    calls.append(argv)
                    role, subject=argv[2:4]
                    manifest=sweep_owed.packet.parse_sweep_manifest(root/'content'/(subject+'.md'))
                    if argv[1]=='dispatch':
                        # Enlarge the race window before spawn-started is committed.
                        time.sleep(.03)
                        reserved=sweep_owed.reserved_rows(fold.read_events(),NOW)
                        keys={(r['spec'],r['criterion']) for r in manifest['rows']}
                        assert not reserved & keys
                        spawn=f'L-{role}-{len(batches)+1:04d}'
                        event_path=root/'events'/(spawn+'.jsonl')
                        assert dispatch.emit(event_path, dict(subject=subject), 'spawn-started',
                            role=role,spawn=spawn,project='demo') is None
                        batches.append(dict(role=role,subject=subject,spawn=spawn,rows=manifest['rows'],path=event_path))
                    return subprocess.CompletedProcess(argv,0,stdout='scratch-packet.md\n',stderr='')
            worker=Worker()
            def finish(batch, observe=False):
                if observe:
                    for row in batch['rows']:
                        assert verifier.record(row,dict(verdict='met',evidence='scratch worker read')) == 'owed-met'
                assert dispatch.emit(batch['path'],dict(subject=batch['subject']), 'spawn-done',spawn=batch['spawn']) is None
            def verify():
                return verifier.run(now=NOW,runner=worker)
            def sweep():
                return sweep_owed.run(now=NOW,runner=worker)
            if order=='drain':
                telemetry.return_value=hard
                verify()
                assert not calls and len(verifier.candidates(fold.fold(fold.read_events())[3],NOW))==25
                telemetry.return_value=quiet | dict(load1_per_core=1)
                verify()
                assert len(batches)==1 and len(batches[0]['rows'])==8
                verify()
                assert len(batches)==1, 'open verifier batch must block another dispatch'
                finish(batches[0],observe=True)
                telemetry.return_value=quiet
                verify()
                assert len(batches)==2 and len(batches[1]['rows'])==17
                finish(batches[1],observe=True)
                verify()
                assert not verifier.candidates(fold.fold(fold.read_events())[3],NOW)
                # Observer seam remains governed by the same policy, including --check selection.
                before_calls=len(calls)
                verifier.run(observe=lambda row: dict(verdict='met',evidence='forced scratch read'), now=NOW,
                             only=['L-spec-7200/AC1'])
                assert before_calls==len(calls)
            else:
                if order=='verifier-first':
                    verify(); sweep()
                elif order=='sweep-first':
                    sweep(); verify()
                else:
                    # Standalone verifier overlaps a sweep and its nested verifier.
                    barrier=threading.Barrier(2)
                    def entered(fn):
                        barrier.wait(timeout=5)
                        return fn()
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        futures=[pool.submit(entered,fn) for fn in (sweep,verify)]
                        for future in futures:
                            future.result(timeout=20)
                assert len(batches)==1 and len(batches[0]['rows'])==25, (order,batches)
                finish(batches[0])  # No verdict: released reservations remain eligible.
                assert len(verifier.candidates(fold.fold(fold.read_events())[3],NOW))==25
                verify()
                assert len(batches)==2 and len(batches[1]['rows'])==25
                finish(batches[1],observe=True)
                assert not verifier.candidates(fold.fold(fold.read_events())[3],NOW)
            summaries.append(dict(order=order,batches=[dict(role=b['role'],subject=b['subject'],
                keys=[(r['spec'],r['criterion']) for r in b['rows']]) for b in batches], remaining=0))
    body=json.dumps(summaries,sort_keys=True)
    print(json.dumps(dict(url='cli://verify-owed/2238-AC2',status=0,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),body_excerpt=body)))


def test_2238_schedule_and_store():
    """Full schedule/store scenario, also invoked by the sweep AC1 script."""
    import hashlib, crons, dispatch, sweep_owed, math
    from unittest.mock import patch
    with tempfile.TemporaryDirectory(prefix='schedule-', dir=BASE) as tmp, contextlib.ExitStack() as stack:
        root = pathlib.Path(tmp) / 'root'
        EV, CONTENT = root / 'events', root / 'content'
        EV.mkdir(parents=True); CONTENT.mkdir()
        REPO_ROOT = HERE.parent
        NORMAL2238 = dict(mem_avail_gb=20, swap_free_gb=8, load1_per_core=1,
                          cpu_some10=0, mem_some10=0, mem_full10=0)
        REAL_VERIFIER2238 = verifier.run
        stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(root), HOME=str(pathlib.Path(tmp)/'home'),
                                       DOIT_SCRATCH=str(pathlib.Path(tmp)/'scratch')))
        stack.enter_context(patch.object(fold, 'PROJECT', None))
        for module in (dispatch, fold, sweep_owed.packet):
            for attr in ('ROOT', 'CONTENT', 'EVENTS', 'PACKETS'):
                if hasattr(module, attr):
                    stack.enter_context(patch.object(module, attr, root if attr == 'ROOT' else root/attr.lower()))
        def write(name, *events):
            for event in events:
                if event.get('type') == 'spec-written':
                    dispatch.spec_path(event['subject']).write_text('## Acceptance criteria\nAC1 [backend]: scratch plain\n')
            (EV/name).write_text(''.join(json.dumps(dict(v=1, **e))+'\n' for e in events))
        start = datetime(2026, 11, 9, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        current = [start]
        inventory, pending, manifests, passes = {}, [], [], []
        quiet = NORMAL2238 | dict(load1_per_core=.5)
        hard = dict(mem_avail_gb=4, swap_free_gb=0, load1_per_core=3.5,
                    cpu_some10=90, mem_some10=6, mem_full10=2)

        def seed(first, count, at):
            for i in range(first, first + count):
                sid = f'L-spec-{3200 + i:04d}'
                project = 'proj-a' if i % 2 == 0 else 'proj-b'
                inventory[(sid, 'AC1')] = at
                write('L-spec-writer-' + str(i) + '.jsonl',
                    dict(type='spec-written', subject=sid, project=project, ts=(start - timedelta(days=3)).isoformat()),
                    dict(type='owed-ac', subject=sid, criterion='AC1', line='scratch plain',
                         wake_at=at.isoformat(), ts=(start - timedelta(days=1)).isoformat()))
                write('L-executor-' + str(i) + '.jsonl',
                    dict(type='shipped', subject=sid, ts=(start - timedelta(days=2)).isoformat()))

        class Workers:
            def __init__(self):
                self.calls = []
            def run(self, argv):
                self.calls.append(argv)
                role, subject = argv[2:4]
                path = CONTENT / (subject + '.md')
                manifest = sweep_owed.packet.parse_sweep_manifest(path)
                rows = manifest['rows']
                assert 0 < len(rows) <= 32
                if argv[1] == 'dispatch':
                    keys = {(r['spec'], r['criterion']) for r in rows}
                    reserved = sweep_owed.reserved_rows(fold.read_events(), current[0])
                    assert not keys & reserved, (role, keys & reserved)
                    spawn = f'L-{role}-{len(manifests) + 1:04d}'
                    event_path = EV / (spawn + '.jsonl')
                    assert dispatch.emit(event_path, dict(subject=subject), 'spawn-started',
                        role=role, project=manifest['project'], spawn=spawn) is None
                    pending.append((current[0] + timedelta(minutes=6), rows, spawn, event_path, subject))
                    manifests.append(dict(role=role, subject=subject, keys=sorted(keys), at=current[0].isoformat()))
                return subprocess.CompletedProcess(argv, 0, stdout='scratch-packet.md\n', stderr='')

        def complete(at):
            current[0] = at
            for due, rows, spawn, event_path, subject in list(pending):
                if due > at:
                    continue
                current[0] = due
                for row in rows:
                    assert sweep_owed.verifier.record(row, dict(verdict='met', evidence='scratch external worker observation')) == 'owed-met'
                assert dispatch.emit(event_path, dict(subject=subject), 'spawn-done', spawn=spawn) is None
                pending.remove((due, rows, spawn, event_path, subject))
            current[0] = at

        rows = {r['name']: r for r in crons.rows() if r['name'] in ('sweep-owed', 'verify-owed')}
        starts = []
        # Expand the parsed minute/hour fields; these manifest schedules have no day restriction.
        for name, row in rows.items():
            minute, hour, day, month, weekday = row['schedule'].split()
            assert (day, month, weekday) == ('*', '*', '*')
            hours = range(24) if hour == '*' else [int(h) for h in hour.split(',')]
            for h in hours:
                for m in map(int, minute.split(',')):
                    starts.append((start + timedelta(hours=h, minutes=m), name))
        starts.sort()
        assert len([x for x in starts if x[1] == 'verify-owed' and x[0].hour < 16]) == 64
        assert len([x for x in starts if x[1] == 'verify-owed' and x[0].hour >= 16]) == 32
        workers = Workers()
        seed(0, 180, start)
        assert dispatch.emit(EV / 'L-operator-route.jsonl', dict(subject='L-spec-3200'),
                             'owed-routed', criterion='AC1', route='recheck') is None
        hold_path = EV / 'L-builder-pressure.jsonl'
        with patch.object(sweep_owed.verifier, 'run', REAL_VERIFIER2238), \
             patch.object(dispatch, 'now', side_effect=lambda: current[0].isoformat()), \
             patch.object(sweep_owed.loadgate, 'readings', side_effect=lambda: hard if current[0].hour < 16 else quiet), \
             contextlib.redirect_stdout(io.StringIO()):
            assert dispatch.emit(hold_path, dict(subject='L-spec-pressure'), 'spawn-started',
                                 role='builder', spawn='L-builder-pressure') is None
            noon_seeded = False
            released = False
            for at, name in starts:
                if at.hour >= 12 and not noon_seeded:
                    seed(180, 65, start + timedelta(hours=12))
                    noon_seeded = True
                complete(at)  # Completion wins a same-instant race with dispatch.
                if at.hour >= 16 and not released:
                    assert dispatch.emit(hold_path, dict(subject='L-spec-pressure'), 'spawn-done', spawn='L-builder-pressure') is None
                    released = True
                before = len(workers.calls)
                lines = (sweep_owed.run if name == 'sweep-owed' else REAL_VERIFIER2238)(now=at, runner=workers)
                expected = 0 if at.hour < 16 else 32
                reports = [json.loads(l.split(': pass ', 1)[1]) for l in lines if ': pass ' in l]
                assert len(reports) == (2 if name == 'sweep-owed' else 1), reports
                assert all(r['budget'] == expected for r in reports), (at, reports)
                if not expected:
                    assert len(workers.calls) == before
                for r in reports:
                    for key in ('elapsed_s', 'self_cpu_s', 'synchronous_child_cpu_s'):
                        assert math.isfinite(r[key]) and r[key] >= 0
                    assert 'seconds' in r['cpu_source'] and r['detached_worker_cpu_s'] == 'unknown'
                passes.append(dict(at=at.isoformat(), name=name, expected=expected,
                                   actual=[r['budget'] for r in reports]))
            complete(end)
        assert not pending
        events = fold.read_events()
        verdicts = [e for e in events if e.get('type') == 'owed-met']
        assert len(verdicts) == len(inventory) == 245
        firsts = {(e['subject'], e['criterion']): fold.ts(e['ts']) for e in verdicts}
        assert set(firsts) == set(inventory)
        assert all(max(start, arrival) <= firsts[key] < end for key, arrival in inventory.items())
        assert min(firsts.values()) >= start + timedelta(hours=16, minutes=14)
        assert any(len(m['keys']) > 8 for m in manifests)
        records = [e for e in events if e.get('type') == 'sweep-run']
        assert len(records) == 4 and [e['outcome'] for e in records[:3]] == ['yielded']*3
        assert all(e['reason'] == 'owed-yield' for e in records[:3])
        # Independently reopen/fold twice; reads cannot create observations.
        code = """import sys,json; sys.path.insert(0,sys.argv[1]); import fold,owed
from datetime import datetime,timezone
now=datetime(2026,11,10,tzinfo=timezone.utc)
events=fold.read_events(); by=fold.fold(events)[3]
print(json.dumps(dict(verdicts=sum(e.get('type')=='owed-met' for e in events),
met=sum(r['status']=='met' for evs in by.values() for r in owed.checks(evs,now)))))"""
        reopened = []
        for _ in range(2):
            result = subprocess.run([sys.executable, '-c', code, str(REPO_ROOT / 'src')], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            reopened.append(json.loads(result.stdout))
        assert reopened == [dict(verdicts=245, met=245)]*2, reopened
        # Dry runs leave the ledger, manifests and local crontab byte-identical.
        files = [*EV.glob('*.jsonl'), *CONTENT.glob('*')]
        before = {str(p): p.read_bytes() for p in files if p.is_file()}
        calls = len(workers.calls)
        with patch.object(sweep_owed.verifier, 'run', REAL_VERIFIER2238), \
             patch.object(sweep_owed.loadgate, 'readings', return_value=quiet), contextlib.redirect_stdout(io.StringIO()):
            sweep_owed.run(dry_run=True, now=end, runner=workers)
            REAL_VERIFIER2238(dry_run=True, now=end, runner=workers)
        after = {str(p): p.read_bytes() for p in [*EV.glob('*.jsonl'), *CONTENT.glob('*')] if p.is_file()}
        assert before == after and calls == len(workers.calls)
        body = dict(window=[start.isoformat(),end.isoformat()], parsed_starts=[(t.isoformat(), n) for t,n in starts],
            passes=passes, manifests=manifests, first_observations=[dict(spec=k[0], criterion=k[1], at=firsts[k].isoformat()) for k in sorted(firsts)],
            independent_readback=reopened, sweep_records=records, dry_run_unchanged=True)
        payload = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url='cli://sweep-owed/2238-AC1-AC5', status=0,
            body_sha256=hashlib.sha256(payload.encode()).hexdigest(), body_excerpt=payload)))

        # Exercise the script entry in a fresh interpreter: importing sweep_owed from
        # its nested verifier must reuse the CLI's lock and cannot deadlock.
        with tempfile.TemporaryDirectory(prefix='owed-cli-2238-') as cli_tmp:
            cli_root = pathlib.Path(cli_tmp) / 'root'
            (cli_root / 'events').mkdir(parents=True)
            (cli_root / 'content').mkdir()
            cli_env = dict(os.environ, DOIT_ROOT=str(cli_root), HOME=str(pathlib.Path(cli_tmp) / 'home'),
                           DOIT_SCRATCH=str(pathlib.Path(cli_tmp) / 'scratch'))
            cli_result = subprocess.run([sys.executable, str(REPO_ROOT / 'src/sweep_owed.py')],
                                        env=cli_env, capture_output=True, text=True, timeout=10)
            assert cli_result.returncode == 0, cli_result.stderr
            assert 'sweep-owed: pass ' in cli_result.stdout and 'verify-owed: pass ' in cli_result.stdout
            print('2238 fresh-process sweep CLI and nested verifier complete without deadlock')


if sys.argv[1:] == ['--schedule-scenario']:
    try:
        test_2238_schedule_and_store()
    finally:
        shutil.rmtree(str(BASE), ignore_errors=True)
    raise SystemExit(0)


test_2238_shared_admission_reservations_and_concurrency()
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
