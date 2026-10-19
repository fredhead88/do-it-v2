#!/usr/bin/env python3
"""Verifier-pass timed readings and portal traffic (L-spec-0682 R2).
Ship-time capture is deferred by the binding scope correction.
Run: python3 test_owed_timed.py
"""
import contextlib, importlib.util, io, json, os, pathlib, subprocess, sys, tempfile, threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

TMP = pathlib.Path(tempfile.mkdtemp(prefix="owed-timed-test-"))
os.environ["HOME"], os.environ["DOIT_ROOT"] = str(TMP / "home"), str(TMP / "root")
os.environ["DOIT_SCRATCH"] = str(TMP / "scratch")
for k in ("DOIT_PROJECT", "DOIT_LEDGER_FILE"):
    os.environ.pop(k, None)
ROOT = pathlib.Path(os.environ["DOIT_ROOT"])
EV = ROOT / "events"
EV.mkdir(parents=True, exist_ok=True)
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fold, owed, owed_timed  # noqa: E402

N = 0
NOW = datetime.now(timezone.utc).replace(microsecond=0)


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


def test_first_reading_on_verifier_pass_second_scheduled():
    sid = "L-spec-6101"
    write("spec-writer", [{"ts": iso(hours=-3), "type": "owed-ac", "subject": sid, "criterion": "AC5",
                           "wake_at": iso(hours=21), "line": "AC5 OWED two snapshots 24h apart",
                           "readings": 2, "interval_h": 24, "reading": "pg_stat_statements", "project": "albert-scott"}])
    write("executor", [{"ts": iso(hours=-1), "type": "shipped", "subject": sid}])
    by = fold.fold(fold.read_events())[3]
    check(owed_timed.load(sid, "AC5") is None,
          "ship-time capture is deferred: the ship event has not stored a reading")
    check(owed_timed.run_pass(by, NOW, lambda s, c, d: None, events=fold.read_events()) == [f"{sid} AC5: first reading not takeable this pass"],
          "a reader that cannot read stores nothing and schedules nothing")
    check(owed_timed.load(sid, "AC5") is None, "nothing stored")
    lines = owed_timed.run_pass(by, NOW, lambda s, c, d: {"calls": 7, "form": "date"}, events=fold.read_events())
    check(len(lines) == 2 and "first reading stored" in lines[0] and "second reading due" in lines[1], f"pass lines: {lines}")
    # a FRESH process reads the stored first reading back
    out = subprocess.run([sys.executable, "-c",
                          "import sys, json; sys.path.insert(0, sys.argv[1]); import owed_timed; "
                          "print(json.dumps(owed_timed.load('L-spec-6101', 'AC5')))", str(HERE)],
                         capture_output=True, text=True, env=dict(os.environ))
    rec = json.loads(out.stdout)
    check(rec["reading"] == {"calls": 7, "form": "date"} and rec["captured_at"] == iso(), f"first reading round-trips: {rec}")
    evs = fold.read_events()
    redated = [e for e in evs if e.get("type") == "owed-ac" and e.get("second_reading_of")]
    check(len(redated) == 1, f"exactly one re-dated owed-ac: {redated}")
    first = datetime.fromisoformat(rec["captured_at"])
    check(first > datetime.fromisoformat(iso(hours=-1)),
          "a delayed verifier pass records its actual capture time, never the ship time")
    wake = datetime.fromisoformat(redated[0]["wake_at"])
    check(wake - first >= timedelta(hours=24), f"the second is dated at least first + 24h: {wake - first}")
    spec_evs = fold.fold(evs)[3][sid]
    check(any(e.get("second_reading_of") for e in spec_evs), "the verifier's re-date survives the fold's admission")
    row = lambda t: owed.checks(spec_evs, t)[0]
    check(row(NOW + timedelta(hours=1))["status"] == "waiting", f"before the instant: waiting, not expired: {row(NOW + timedelta(hours=1))['status']}")
    check(row(NOW + timedelta(hours=23, minutes=59))["status"] == "waiting", "still waiting a minute before")
    check(row(NOW + timedelta(hours=24, minutes=1))["status"] == "due", "due once the interval has passed")
    again = owed_timed.run_pass(fold.fold(evs)[3], NOW, lambda s, c, d: {"calls": 99}, events=evs)
    check(len([e for e in fold.read_events() if e.get("second_reading_of")]) == 1, f"idempotent — no second re-date: {again}")
    check(owed_timed.load(sid, "AC5")["reading"] == {"calls": 7, "form": "date"}, "the first reading is never overwritten")


SECRET = "FAKE-VERIFIER-SECRET-0123456789"


def _portal():
    spec = importlib.util.spec_from_file_location("portal_traffic", HERE.parent / "scripts" / "portal-traffic.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MIN_GAP_S = 0
    return mod


def test_portal_traffic_get_only_host_pinned_no_secret():
    pt = _portal()
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(("GET", self.headers.get("Authorization")))
            body = f"hello {self.headers.get('Authorization')}".encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            seen.append(("POST", None)); self.send_response(200); self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{srv.server_address[1]}"
    env = {pt.HOST_ENV: host, pt.TOKEN_ENV: SECRET, pt.SCHEME_ENV: "http"}
    sent = []
    spy = lambda req, timeout=None: sent.append(req) or (_ for _ in ()).throw(AssertionError("a socket send happened"))
    for label, call in (
            ("POST", lambda: pt.fetch(f"http://{host}/x", method="POST", env=env, opener=spy)),
            ("DELETE", lambda: pt.fetch(f"http://{host}/x", method="DELETE", env=env, opener=spy)),
            ("foreign host", lambda: pt.fetch("http://example.org/x", env=env, opener=spy)),
            ("host spoof", lambda: pt.fetch(f"http://{host.split(':')[0]}.example.org/x", env=env, opener=spy)),
            ("no host configured", lambda: pt.fetch(f"http://{host}/x", env={pt.TOKEN_ENV: SECRET}, opener=spy)),
            ("no credential", lambda: pt.fetch(f"http://{host}/x", env={pt.HOST_ENV: host}, opener=spy))):
        try:
            call()
            check(False, f"{label} must raise")
        except pt.Refused:
            check(True, label)
    check(sent == [] and seen == [], f"every refusal came before any send: {sent} {seen}")
    r = pt.fetch(f"http://{host}/ok", env=env)
    check(r["status"] == 200 and seen == [("GET", f"Bearer {SECRET}")], f"a GET to the pinned host goes out: {r} {seen}")
    check(SECRET not in json.dumps(r) and "[redacted]" in r["body_excerpt"], f"the credential is not in the evidence: {r}")
    out, err = io.StringIO(), io.StringIO()
    old = dict(os.environ)
    os.environ.update(env)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pt.main(["/a", "/b", "--count", "2"])
    finally:
        os.environ.clear(); os.environ.update(old)
    check(rc == 0 and out.getvalue().count("200 http") == 4, f"main issued 4 GETs: {out.getvalue()!r}")
    check(SECRET not in out.getvalue() + err.getvalue(), "the secret is in neither stdout nor stderr")
    check(all(m == "GET" for m, _ in seen), f"only GET reached the server: {seen}")
    srv.shutdown()
    # the throttle sleeps when asked to go faster than MIN_GAP_S
    pt.MIN_GAP_S, naps, t = 5.0, [], [100.0]
    pt._last_sent[0] = 0.0
    srv2 = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    env["DOIT_PORTAL_HOST"] = f"127.0.0.1:{srv2.server_address[1]}"
    for _ in range(2):
        pt.fetch(f"http://{env['DOIT_PORTAL_HOST']}/t", env=env, sleep=naps.append, clock=lambda: t[0])
    check(len(naps) == 1 and naps[0] > 0, f"the second request was throttled: {naps}")
    srv2.shutdown()


test_first_reading_on_verifier_pass_second_scheduled()
test_portal_traffic_get_only_host_pinned_no_secret()
import shutil
shutil.rmtree(str(TMP), ignore_errors=True)
print(f"owed_timed: {N} checks pass")
