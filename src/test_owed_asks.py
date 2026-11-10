#!/usr/bin/env python3
"""owed_asks — the operator-act queue (L-spec-0682 R4: AC11, AC12). Run: python3 test_owed_asks.py

One throwaway DOIT_ROOT for the file; fixtures are real ledger files read back
through `fold.read_events`, never a mocked write."""
import json, os, pathlib, re, sys, tempfile
from datetime import datetime, timedelta, timezone

TMP = pathlib.Path(tempfile.mkdtemp(prefix="owed-asks-test-"))
os.environ["HOME"], os.environ["DOIT_ROOT"] = str(TMP / "home"), str(TMP / "root")
os.environ["DOIT_SCRATCH"] = str(TMP / "scratch")
for k in ("DOIT_PROJECT", "DOIT_LEDGER_FILE"):
    os.environ.pop(k, None)
EV = pathlib.Path(os.environ["DOIT_ROOT"]) / "events"
EV.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold, owed_asks, sweep_owed, verifier  # noqa: E402

N = 0
NOW = datetime.now(timezone.utc)
HERE = pathlib.Path(__file__).resolve().parent


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


def iso(**kw):
    return (NOW + timedelta(**kw)).isoformat(timespec="seconds")


def write(actor, rows):
    p = EV / f"L-{actor}-0001.jsonl"
    with p.open("a") as fh:
        for r in rows:
            fh.write(json.dumps({"v": 1, **r}) + "\n")


def spec(sid, crit, *, shipped_ago_d, cap=None, why=None, verdict=None):
    """A shipped spec with one owed criterion due one day after ship, and optionally a later word."""
    t0 = shipped_ago_d + 1
    write("spec-writer", [{"ts": iso(days=-t0), "type": "owed-ac", "subject": sid, "criterion": crit,
                           "wake_at": iso(days=-t0 + 1), "line": f"{crit} OWED prod read"}])
    write("executor", [{"ts": iso(days=-shipped_ago_d), "type": "shipped", "subject": sid}])
    if cap:
        write("owed-sweeper", [{"ts": iso(hours=-1), "type": "owed-unobservable", "subject": sid,
                                "criterion": crit, "capability": cap, "why": why or "x"}])
    if verdict == "met":
        write("owed-sweeper", [{"ts": iso(minutes=-5), "type": "owed-met", "subject": sid,
                                "criterion": crit, "evidence": "saw it"}])


def test_one_ask_per_waiting_check_and_no_silent_expiry():
    spec("L-spec-7001", "AC1", shipped_ago_d=3, cap="operator-action",
         why="Approve the refund batch in L-spec-9999 ledger event, then tell the verifier")
    spec("L-spec-7002", "AC2", shipped_ago_d=3, cap="browser", why="the browser tool is not started")
    spec("L-spec-7003", "AC3", shipped_ago_d=3, cap="operator-action", verdict="met")   # answered: no ask
    spec("L-spec-7004", "AC4", shipped_ago_d=3, cap="elapsed", why="the second reading is not yet due")
    spec("L-spec-7005", "AC5", shipped_ago_d=9)                                          # expired by age, no ask
    evs = fold.read_events()
    q = owed_asks.queue(evs, NOW)
    check([(r["spec"], r["criterion"]) for r in q] == [("L-spec-7001", "AC1"), ("L-spec-7002", "AC2")],
          f"exactly one ask per waiting check, none for met / elapsed / plain-expired: {q}")
    for r in q:
        check(set(r) == {"spec", "criterion", "ask", "unblock_command"}, f"ask shape: {r}")
        check(r["unblock_command"] == f"doit verify-owed --check {r['spec']}/{r['criterion']}",
              f"a concrete unblock command: {r}")
        check(not re.search(r"owed-|escalation|ledger|capability|operator-action|\bevent\b", r["ask"], re.I),
              f"the ask carries no ledger jargon: {r['ask']}")
        others = set(re.findall(r"L-[a-z]+-\d+", r["ask"])) - {r["spec"]}
        check(not others, f"no id beyond the spec id: {others} in {r['ask']}")
        check(r["ask"].startswith(r["spec"]) and "waiting on you" in r["ask"], f"plain sentence: {r['ask']}")
    # Current owed.checks keeps operator/browser waits blocked rather than aging them out.
    # Exercise the production append guard directly too: an expiry producer may still
    # attempt an append using an older snapshot of these rows.
    specs = fold.fold(evs)[0]
    import dispatch, owed
    check(all(owed.checks(specs[s]["evs"], NOW)[0]["status"] == "blocked"
              for s in ("L-spec-7001", "L-spec-7002")), "waiting acts remain blocked")
    lines = sweep_owed.sweep_expired(specs, NOW)
    for sid, crit in (("L-spec-7001", "AC1"), ("L-spec-7002", "AC2"), ("L-spec-7005", "AC5")):
        refusal = dispatch.emit(sweep_owed.ledger_path(), {"subject": sid}, "escalation-blocking",
                                kind="owed-expired", criteria=json.dumps([crit]), evidence=json.dumps(["still waiting"]),
                                default="ask the operator", deadline=iso(hours=72), revert="recheck")
        check(bool(refusal) == (sid != "L-spec-7005"), f"only an open ask suppresses expiry: {sid}: {refusal}")
    expired = [e for e in fold.read_events() if e.get("type") == "escalation-blocking" and e.get("kind") == "owed-expired"]
    check({e["subject"] for e in expired} == {"L-spec-7005"},
          f"only the ask-less control escalates: {[e['subject'] for e in expired]} / {lines}")



def test_verifier_never_waives():
    allowed = {"owed-met", "owed-failed", "owed-unobservable", "brief"}
    check(set(verifier.ALLOWED_TYPES) == allowed, f"the allowed set is exactly the four: {verifier.ALLOWED_TYPES}")
    for name in ("verifier.py", "owed_asks.py"):
        src = (HERE / name).read_text()
        emitted = set(re.findall(r"""_emit\(\s*["']([a-z-]+)["']""", src))
        check(emitted <= allowed, f"{name} appends only allowed types: {emitted}")
        code = re.sub(r'"""[\s\S]*?"""', "", src)
        literals = set(re.findall(r"""["'](owed-[a-z]+|spec-[a-z]+|shipped|decision|unblocked)["']""", code))
        check(literals - {"owed-verifier", "owed-sweeper"} <= allowed, f"{name} names no waiving type: {literals}")
        check("owed-waived" not in code, f"{name} never names owed-waived in code")
    check(not re.search(r"\bemit\(|\.write\(|\bopen\(", re.sub(r'"""[\s\S]*?"""', "", (HERE / "owed_asks.py").read_text())),
          "owed_asks appends and writes nothing at all")
    try:
        verifier._emit("owed-waived", "L-spec-7001", criterion="AC1", reason="x")
        check(False, "a waiving append must be refused")
    except AssertionError as x:
        check("may not append" in str(x), f"refused at the append: {x}")
    # behavioural: a pass over fresh fixtures drops nothing
    spec("L-spec-7101", "AC1", shipped_ago_d=2, cap="browser", why="no browser")
    spec("L-spec-7102", "AC1", shipped_ago_d=2)
    before = {(r["spec"], r["criterion"]) for s in fold.fold(fold.read_events())[3].values()
              for r in owed.checks(s, NOW) if r["spec"] in ("L-spec-7101", "L-spec-7102")}
    n0 = len(fold.read_events())
    verifier.run(observe=lambda row: {"verdict": "blocked", "capability": "browser", "why": "still none"}, now=NOW,
                 only=["L-spec-7101/AC1", "L-spec-7102/AC1"])
    new = fold.read_events()[n0:]
    check(new and {e["type"] for e in new} <= allowed, f"only allowed types appended: {[e['type'] for e in new]}")
    after = {(r["spec"], r["criterion"]): r["status"] for s in fold.fold(fold.read_events())[3].values()
             for r in owed.checks(s, NOW) if r["spec"] in ("L-spec-7101", "L-spec-7102")}
    check(set(after) == before and not {"waived", "dropped", "met"} & set(after.values()),
          f"no check was waived, dropped or closed by a blocked pass: {after}")


def test_exhausted_route_escalates_once():
    import owed_routes
    sid = "L-spec-7369"
    spec(sid, "AC1", shipped_ago_d=3, cap="browser")
    write("sweep", [{"ts": iso(hours=-20 + n), "type": "owed-routed", "subject": sid,
                     "criterion": "AC1", "route": "browser", "capability": "browser", "epoch": ""}
                    for n in range(owed_routes.MAX_ATTEMPTS)])
    evs = fold.fold(fold.read_events())[0][sid]["evs"]
    check(owed.checks(evs, NOW)[0]["status"] == "blocked", "route exhaustion remains a Thinker-owned hold")
    check(owed.checks(evs, NOW)[0]["next_owner"] == "thinker", "capability gap is owned")
    check(not owed_asks.open_ask_criteria(evs, NOW), "an exhausted route has no open ask")
    check(not any(r["spec"] == sid for r in owed_asks.queue(fold.read_events(), NOW)),
          "an exhausted route is absent from the operator queue")
    lines = sweep_owed.sweep_expired({sid: {"evs": evs}}, NOW)
    check(lines == [], f"capability exhaustion never creates expiry: {lines}")
    specs = fold.fold(fold.read_events())[0]
    check(not sweep_owed.sweep_expired({sid: specs[sid]}, NOW), "exhaustion never escalates twice")
    escalations = [e for e in fold.read_events() if e.get("subject") == sid
                   and e.get("type") == "escalation-blocking" and e.get("kind") == "owed-expired"]
    check(escalations == [], "no capability expiry escalation was written")


def test_non_exhausted_open_ask_still_stripped():
    import owed_routes
    sid = "L-spec-7370"
    spec(sid, "AC1", shipped_ago_d=3, cap="browser")
    write("sweep", [{"ts": iso(hours=-20 + n), "type": "owed-routed", "subject": sid,
                     "criterion": "AC1", "route": "browser", "capability": "browser", "epoch": ""}
                    for n in range(owed_routes.MAX_ATTEMPTS - 1)])
    evs = fold.fold(fold.read_events())[0][sid]["evs"]
    check(owed_asks.open_ask_criteria(evs, NOW) == {"AC1"}, "a non-exhausted route keeps its ask")
    kv = {"criteria": json.dumps(["AC1", "AC2"]), "evidence": json.dumps(["open", "expired"])}
    kept, refusal = owed_asks.guard_expiry(sid, kv, now=NOW, events=fold.read_events())
    check(refusal is None and json.loads(kept["criteria"]) == ["AC2"]
          and json.loads(kept["evidence"]) == ["expired"], "open criterion and aligned evidence still stripped")
    _, refusal = owed_asks.guard_expiry(sid, {"criteria": '["AC1"]'}, now=NOW, events=fold.read_events())
    check(bool(refusal), "an escalation naming only the live ask is still refused")
    operator_sid = "L-spec-7371"
    spec(operator_sid, "AC1", shipped_ago_d=3, cap="operator-action")
    write("sweep", [{"ts": iso(hours=-20 + n), "type": "owed-routed", "subject": operator_sid,
                     "criterion": "AC1", "route": "operator-action", "capability": "operator-action", "epoch": ""}
                    for n in range(owed_routes.MAX_ATTEMPTS)])
    evs = fold.fold(fold.read_events())[0][operator_sid]["evs"]
    check(owed.checks(evs, NOW)[0]["status"] == "blocked"
          and owed_asks.open_ask_criteria(evs, NOW) == {"AC1"}, "operator asks never expire on attempts")


import owed  # noqa: E402

test_one_ask_per_waiting_check_and_no_silent_expiry()
test_verifier_never_waives()
test_exhausted_route_escalates_once()
test_non_exhausted_open_ask_still_stripped()
import shutil
shutil.rmtree(str(TMP), ignore_errors=True)
print(f"owed_asks: {N} checks pass")


def test_inventories():
    import dispatch
    import subprocess
    from unittest.mock import patch
    cases = [
        ('credential', 'Read production SQL', 'Operator: grant SELECT on fixture', 'due'),
        ('api-token', 'Requires an approved API token to observe the endpoint', '', 'due'),
        ('missing-account', 'Log in as staff at https://fixture.invalid', 'no review account', 'due'),
        ('present-account', 'Log in as staff at https://fixture.invalid', 'no review account', 'due'),
        ('decision', 'Read repository output', 'Operator ruling needed', 'failed'),
        ('approval', 'Read repository output', 'Operator: approve this change', 'correcting'),
        ('real-world', 'Read repository output', 'Operator: purchase this item', 'due'),
        ('sql-wrapper', 'Read production SQL through existing read-only wrapper', 'no browser here', 'due'),
        ('local-dependency', 'Run repository tests', 'missing node_modules', 'due'),
        ('closed', 'Log in as staff at https://fixture.invalid', 'no review account', 'met'),
        ('waived', 'Read production SQL', 'Operator: grant SELECT on fixture', 'waived'),
        ('dropped', 'Read production SQL', 'Operator: grant SELECT on fixture', 'dropped'),
        ('unshipped', 'Read production SQL', 'Operator: grant SELECT on fixture', 'unshipped'),
        ('callback-fault', 'Log in as staff at https://fixture.invalid', 'no review account', 'due'),
        ('browser-executable', 'Run Playwright against an anonymous local fixture', 'missing chromium', 'due'),
    ]
    with tempfile.TemporaryDirectory(prefix='credential-inventory-') as tmp:
        root = pathlib.Path(tmp)
        (root/'content').mkdir()
        (root/'events').mkdir()
        evs = []
        expected_credentials, expected_operator = set(), set()
        for i, (name, observation, why, state) in enumerate(cases):
            sid = f'L-spec-{9950+i}'
            (root/'content'/f'{sid}.md').write_text('AC1 [backend]: '+observation)
            base = dict(subject=sid, project=name)
            def event(event_type, actor, seconds, **fields):
                return dict(base, type=event_type, actor=actor, ts=(NOW+timedelta(seconds=seconds)).isoformat(), **fields)
            evs.append(event('owed-ac','spec-writer',-100,criterion='AC1',wake_at=iso(days=-1)))
            if state != 'unshipped':
                evs.append(event('shipped','executor',-90))
            evs.append(event('owed-unobservable','owed-sweeper',-80,criterion='AC1',capability='operator-action',why=why))
            evs += [event('owed-routed','sweep',-70+j,criterion='AC1',route='operator-action') for j in range(2)]
            if state in ('failed','correcting','met','waived'):
                evs.append(event('owed-'+('failed' if state=='correcting' else state),'operator',-60,
                                 criterion='AC1',evidence='$ fixture\nobserved',reason='fixture',kind='unmet'))
            if state == 'correcting':
                evs.append(event('owed-corrective','operator',-50,criterion='AC1',corrective='L-spec-9999'))
            if state == 'dropped':
                evs.append(event('spec-killed','thinker',-40))
            if name in ('credential','api-token','missing-account','callback-fault'):
                expected_credentials.add(sid)
            if name in ('credential','decision','approval','real-world'):
                expected_operator.add(sid)
        evs.append(dict(evs[0]))
        evs.append(dict(evs[0],subject='L-spec-9998',actor='builder'))
        calls = []
        def accounts(project):
            calls.append(project)
            if project == 'callback-fault':
                raise OSError('fixture stat failure')
            return project == 'present-account'
        with patch.object(dispatch,'CONTENT',root/'content'):
            creds = owed_asks.credential_list(evs,NOW,accounts=accounts)
            assert len(calls) == len(set(calls))
            assert {r['spec'] for r in creds} == expected_credentials, creds
            assert next(r for r in creds if r['spec'] == 'L-spec-9951')['credential'] == 'API token'
            assert all(r['why']=='no review account' for r in creds if r['credential']=='review account')
            ops = owed_asks.operator_open(evs,NOW)
            assert {r['spec'] for r in ops} == expected_operator, ops
            assert creds == owed_asks.credential_list(evs,NOW,accounts=accounts)
            assert ops == owed_asks.operator_open(evs,NOW)
            print('credential inventory',[(r['spec'],r['criterion'],r['credential'],r['why']) for r in creds])
            print('operator inventory',[(r['spec'],r['criterion']) for r in ops])
        # The existing CLI emits exactly one JSON aggregate, with no row messages.
        for event in evs:
            data=dict(event)
            actor=data.pop('actor')
            with (root/'events'/f'L-{actor}-fixture.jsonl').open('a') as f:
                f.write(json.dumps(data)+'\n')
        env=dict(os.environ,DOIT_ROOT=str(root),HOME=str(root/'home'),DOIT_READ_CACHE='0')
        result=subprocess.run(['/usr/bin/python3',str(HERE/'owed_asks.py')],env=env,capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        assert len(result.stdout.splitlines())==1
        assert isinstance(json.loads(result.stdout),list)
        print('credential CLI: one aggregate; no writes or notifications')


test_inventories()
