del os.environ["DOIT_SEAT_CLAIM_SEC"]
MT.unlink()
N += 1

# ── AC6 · scripts/seat/claim.sh: O_EXCL semantics, all three sub-cases ─────────
claim_root = TMP / "claimtest"
(claim_root / "seat").mkdir(parents=True)
spawn_c = "L-research-claimtest1"
(claim_root / "seat" / f"{spawn_c}.packet.md").write_text("packet\n")
env_c = {**os.environ, "DOIT_ROOT": str(claim_root)}
r1 = subprocess.run(["bash", "scripts/seat/claim.sh", spawn_c], cwd=str(REPO_ROOT), env=env_c,
                    capture_output=True, text=True)
assert r1.returncode == 0, r1.stderr
claimed_p = claim_root / "seat" / f"{spawn_c}.claimed"
assert claimed_p.is_file(), "the first claim must create the file"
before_stat = claimed_p.stat()
r2 = subprocess.run(["bash", "scripts/seat/claim.sh", spawn_c], cwd=str(REPO_ROOT), env=env_c,
                    capture_output=True, text=True)
assert r2.returncode != 0, "a second claim on the same spawn must be refused"
after_stat = claimed_p.stat()
assert before_stat.st_mtime_ns == after_stat.st_mtime_ns and claimed_p.read_text() == "", \
    "the second call must not touch the first invocation's file"
spawn_nopkt = "L-research-claimtest-nopacket"
r3 = subprocess.run(["bash", "scripts/seat/claim.sh", spawn_nopkt], cwd=str(REPO_ROOT), env=env_c,
                    capture_output=True, text=True)
assert r3.returncode != 0
assert not (claim_root / "seat" / f"{spawn_nopkt}.claimed").is_file()
N += 1

# ── AC7 · all three serving-pattern texts name claim.sh as a first/before step ─
def _claim_first(text):
    lines = text.splitlines()
    low = [l.lower() for l in lines]
    for i, l in enumerate(low):
        if "claim.sh" in l:
            window = low[max(0, i - 1):i + 2]
            if any(("first" in w or "before" in w) for w in window):
                return True
    return False


for relpath in ("scripts/seat/README.md", "agents/planner.md", "agents/thinker.md"):
    fp = REPO_ROOT / relpath
    assert _claim_first(fp.read_text()), f"{relpath} must name claim.sh as its first/before serving step"
N += 1

# ── AC8 · the builder case, driven for real (not fabricated) ───────────────────
os.environ["DOIT_SEAT"] = "1"          # no models.toml now (AC5 unlinked it) — the flag forces the seat backend
os.environ["DOIT_SEAT_CLAIM_SEC"] = "1"
# L-spec-0269: `Unserved` now waits out `window` (the builder's own 90-min cap,
# at minimum), not DOIT_SEAT_CLAIM_SEC — shrunk to 0 so this pre-existing
# fixture (whose own point is the terminal shape, not window timing) still
# raises `Unserved` before its own claim-window log would ever fire, keeping
# the event list exactly `["build-started", "spawn-failed"]`, no `seat-stale`.
_real_window_min_ac8 = dispatch.window_min
dispatch.window_min = lambda *a_, **k_: 0
a = argparse.Namespace(role="builder", subject="L-spec-0001", packet=str(PK), path=None, cwd=str(REPO),
                       charter=None, project="t", mcp_config=None, timeout=60, max_usd=None, seat=False)
try:
    dispatch.main(a)
    code = 0
except SystemExit as e:
    code = e.code
finally:
    dispatch.window_min = _real_window_min_ac8
bev = [json.loads(l) for l in max((TMP / "events").glob("L-builder-*.jsonl")).read_text().splitlines()]
assert code == 1 and [e["type"] for e in bev] == ["build-started", "spawn-failed"], bev
assert bev[0]["backend"] == "seat", bev[0]
assert bev[-1]["reason"] == "unserved", bev[-1]
_rm_seat(bev[0]["spawn"])
del os.environ["DOIT_SEAT_CLAIM_SEC"]
del os.environ["DOIT_SEAT"]

import relay  # noqa: E402
assert relay.unserved(bev, TMP) == [], \
    "the real pair (build-started + spawn-failed{reason:unserved}) is already terminal, not pending — " \
    "and its own event timestamps postdate fold.NOW (a snapshot taken once, earlier in this run), so it " \
    "has not yet 'aged into' the failed-unserved window either — see AC10 for the synthetic in-window case"
N += 1

# ── L-spec-0269 (rewrite of the old AC12i block, §5) · a clock frozen forever
# past DOIT_SEAT_CLAIM_SEC no longer terminates the wait on its own (window
# defaults to the unreached timeout=6000) — a BOUNDED sequence advancing past
# an explicit small `window=330` is what must raise `Unserved`, in a bounded
# call count, never an unbounded loop against a frozen clock ─────────────────
os.environ["DOIT_SEAT_CLAIM_SEC"] = "1"
_ac12i_spawn = "L-research-r6ac12i"
_ac12i_seq = [0.0, 301.0, 301.0, 340.0]
_ac12i_idx = {"i": 0}


def _fake_time_301():
    i = min(_ac12i_idx["i"], len(_ac12i_seq) - 1)
    _ac12i_idx["i"] += 1
    return _ac12i_seq[i]


real_wall_t0 = time.perf_counter()
with _mock.patch("time.time", _fake_time_301), _mock.patch("time.sleep", lambda s: None):
    try:
        dispatch.run_seat(_ac12i_spawn, ["claude", "-p"], "packet", str(REPO), 6000, window=330)
        raise AssertionError("expected Unserved once the sequence passes the 330s window")
    except dispatch.Unserved:
        pass
real_wall_301 = time.perf_counter() - real_wall_t0
assert real_wall_301 < 5, f"the fake clock must have decided it, not real time: {real_wall_301}s"
assert _ac12i_idx["i"] <= len(_ac12i_seq) + 1, f"AC12i: bounded call count expected, got {_ac12i_idx['i']}"
_rm_seat(_ac12i_spawn)
del os.environ["DOIT_SEAT_CLAIM_SEC"]
N += 1

_ac12ii_spawn = "L-research-r6ac12ii"
_want2 = TMP / "seat" / f"{_ac12ii_spawn}.result.json"


def _write_result_fast():
    for _ in range(2000):
        if (TMP / "seat" / f"{_ac12ii_spawn}.packet.md").is_file():
            break
        _real_sleep(0.001)          # the genuine sleep — time.sleep is mocked to a no-op during this scenario
    _want2.write_text(json.dumps(
        {"is_error": False, "structured_output": {**research, "path": "content/L-research-r6ac12ii.md"},
         "num_turns": 1, "usage": {}, "total_cost_usd": None, "modelUsage": {"m": {}},
         "session_id": "fc2", "permission_denials": []}))


def _fake_time_299():
    _fake_time_299.n += 1
    return 0.0 if _fake_time_299.n == 1 else 299.0


_fake_time_299.n = 0
_th_fast = threading.Thread(target=_write_result_fast, daemon=True)
_th_fast.start()
real_wall_t0 = time.perf_counter()
with _mock.patch("time.time", _fake_time_299), _mock.patch("time.sleep", lambda s: None):
    r = dispatch.run_seat(_ac12ii_spawn, ["claude", "-p"], "packet", str(REPO), 6000)
real_wall_299 = time.perf_counter() - real_wall_t0
_th_fast.join(5)
assert real_wall_299 < 1.0, f"the boundary held at 299s the whole time: returned in {real_wall_299}s real time"
assert isinstance(r, dispatch.SeatResult), r
_rm_seat(_ac12ii_spawn)
N += 1

# the source-text supplement: DOIT_SEAT_CLAIM_SEC's default (300) appears in BOTH files
for relpath in ("src/dispatch.py", "src/relay.py"):
    text = (REPO_ROOT / relpath).read_text()
    assert 'os.environ.get("DOIT_SEAT_CLAIM_SEC", 300)' in text, \
        f"{relpath} is missing the DOIT_SEAT_CLAIM_SEC default reader, or its default diverged"
N += 1

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0269 · seat-stays-offered (L-charter-0033) — AC4, AC5, AC7
# ══════════════════════════════════════════════════════════════════════════════
import socket  # noqa: E402 — waiter_host comparisons below

# ── AC5 · dispatch.window_min: all five cases, pasted with their computed
# number (or, for (e), the no-exception result) ───────────────────────────────
(TMP / "content").mkdir(parents=True, exist_ok=True)
assert dispatch.window_min("builder", "L-spec-0269a5-nonseat", [], backend="claude-p") == 90, \
    "a non-seat backend always gets the flat, unbumped role cap"
assert dispatch.window_min("grader", "L-spec-0269a5a", [], backend="seat") == 30, \
    "(a) plain grader, no observed-data event -> the role's own cap"
ev_a5b = [{"type": "spec-written", "spec": "L-spec-0269a5b", "ac_types": ["observed-data"], "ts": dispatch.now()}]
assert dispatch.window_min("grader", "L-spec-0269a5b", ev_a5b, backend="seat") == 45, \
    "(b) observed-data grader -> 45"
(TMP / "content" / "L-spec-0269a5c.md").write_text("# fixture\nwindow_min: 500\n\n## 1. Goal\n")
ev_a5c = [{"type": "spec-written", "spec": "L-spec-0269a5c", "ac_types": ["observed-data"], "ts": dispatch.now()}]
assert dispatch.window_min("grader", "L-spec-0269a5c", ev_a5c, backend="seat") == 240, \
    "(c) window_min: 500, base 45 -> raised to 500, then capped at 240"
(TMP / "content" / "L-spec-0269a5d.md").write_text("# fixture\nwindow_min: 10\n\n## 1. Goal\n")
assert dispatch.window_min("builder", "L-spec-0269a5d", [], backend="seat") == 90, \
    "(d) window_min: 10 must never LOWER a 90-min builder window"
assert dispatch.window_min("reviewer", "L-spec-0269a5e-nonexistent", [], backend="seat") == 30, \
    "(e) no spec-written event and no readable content file -> the plain role cap, no exception"
N += 1

# ── AC4 · a spawn-started (grader, non-builder) carries window_min/waiter_pid/
# waiter_host/waiter_proc_start under BOTH backends, differing correctly on an
# observed-data subject ────────────────────────────────────────────────────────
ac4_subj = "L-spec-0269ac4"
# L-spec-0437: role=grader + backend=seat now needs a build-done with a ready_sha
# on the ledger before it builds a view (AC2) — a synthetic one, same convention
# as the spec-written line right below it, so this pre-existing window_min/waiter
# fixture keeps proving what it always proved, unaffected by the new gate.
(TMP / "events" / "L-fixture-0269ac4.jsonl").write_text(
    json.dumps({"v": 1, "ts": dispatch.now(), "type": "spec-written", "subject": ac4_subj, "spec": ac4_subj,
                "path": str(TMP / "content" / f"{ac4_subj}.md"), "ac_types": ["observed-data"], "ac_count": 1,
                "requirement_ids": ["R1"], "owed_ac_count": 0, "unknown_count": 0, "footprint": ["x.py"]}) + "\n" +
    json.dumps({"v": 1, "ts": dispatch.now(), "type": "build-done", "subject": ac4_subj,
                "base_sha": "ac4base", "ready_sha": "ac4ready"}) + "\n")

os.environ.pop("DOIT_SEAT", None)
PK.write_text("a packet for AC4 claude-p\n")
code, types, evs, _ = spawn("grader", out=grade([met]), subject=ac4_subj)
PK.write_text(PK_DEFAULT)
ss_cp = spawn.raw[0]
assert ss_cp["type"] == "spawn-started" and ss_cp["backend"] == "claude-p", ss_cp
assert ss_cp["window_min"] == 30, "AC4/AC6: claude-p never gets the 45-min seat bump (grader cap is 30 since 2026-10-04)"
assert ss_cp["waiter_pid"] == os.getpid() and ss_cp["waiter_host"] == socket.gethostname(), ss_cp
assert "waiter_proc_start" in ss_cp, ss_cp

os.environ["DOIT_SEAT"] = "1"
os.environ["DOIT_SEAT_CLAIM_SEC"] = "1"


def _ac4_serve():
    for _ in range(400):
        pk = [q for q in (list((TMP / "seat").glob("*.packet.md")) if (TMP / "seat").is_dir() else [])
              if not (TMP / "seat" / (q.name.split(".")[0] + ".result.json")).exists()
              and not (TMP / "seat" / (q.name.split(".")[0] + ".output.json")).exists()]
        if pk:
            sid = max(pk, key=lambda q: q.stat().st_mtime).name.split(".")[0]
            (TMP / "seat" / f"{sid}.result.json").write_text(json.dumps(
                {"is_error": False, "structured_output": grade([met]), "num_turns": 1, "usage": {},
                 "total_cost_usd": None, "modelUsage": {"m": {}}, "session_id": sid, "permission_denials": []}))
            return
        time.sleep(0.05)


threading.Thread(target=_ac4_serve, daemon=True).start()
PK.write_text("a packet for AC4 seat\n")
a4 = argparse.Namespace(role="grader", subject=ac4_subj, packet=str(PK), path=None, cwd=str(REPO),
                        charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=True)
# L-spec-0437: this fixture's own point is window_min/waiter fields, not the view
# build — stubbed to a bare temp dir so it never touches real content files.
_real_gv_build_ac4, grader_view.build = grader_view.build, lambda *a_, **k_: TMP / "grader-view-ac4"
try:
    dispatch.main(a4)
except SystemExit:
    pass
finally:
    grader_view.build = _real_gv_build_ac4
PK.write_text(PK_DEFAULT)
raw4 = [json.loads(l) for l in max((TMP / "events").glob("L-grader-*.jsonl")).read_text().splitlines()]
ss_seat = raw4[0]
assert ss_seat["type"] == "grader-view-built", ss_seat
ss_seat = raw4[1]
assert ss_seat["type"] == "spawn-started" and ss_seat["backend"] == "seat", ss_seat
assert ss_seat["window_min"] == 45, "AC4/AC5(b): the seat backend gets the observed-data bump"
assert ss_seat["waiter_pid"] == os.getpid() and ss_seat["waiter_host"] == socket.gethostname(), ss_seat
assert "waiter_proc_start" in ss_seat, ss_seat
_rm_seat(raw4[0]["spawn"])
del os.environ["DOIT_SEAT_CLAIM_SEC"]
del os.environ["DOIT_SEAT"]
N += 1

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0437 · grader-dispatch-view (L-charter-0042 R8) — AC1-AC12, AC14
# No models.toml exists on this root from here on (L-spec-0269 AC5 unlinked it
# above and nothing rewrites it before this section) — `a.seat=True` alone
# resolves role=grader onto the seat backend, exactly the combination the gate
# is scoped to.
# ══════════════════════════════════════════════════════════════════════════════


def _gv_build_done(subject, base_sha=None, ready_sha=None):
    kv = {}
    if base_sha is not None:
        kv["base_sha"] = base_sha
    if ready_sha is not None:
        kv["ready_sha"] = ready_sha
    (TMP / "events" / f"L-fixture-{subject}.jsonl").write_text(json.dumps(
        {"v": 1, "ts": dispatch.now(), "type": "build-done", "subject": subject, **kv}) + "\n")


def _gv_serve_with(out):
    def _serve():
        for _ in range(400):
            pk = [q for q in (list((TMP / "seat").glob("*.packet.md")) if (TMP / "seat").is_dir() else [])
                  if not (TMP / "seat" / (q.name.split(".")[0] + ".result.json")).exists()
                  and not (TMP / "seat" / (q.name.split(".")[0] + ".output.json")).exists()]
            if pk:
                sid = max(pk, key=lambda q: q.stat().st_mtime).name.split(".")[0]
                (TMP / "seat" / f"{sid}.result.json").write_text(json.dumps(
                    {"is_error": False, "structured_output": out, "num_turns": 1, "usage": {},
                     "total_cost_usd": None, "modelUsage": {"m": {}}, "session_id": sid, "permission_denials": []}))
                return
            time.sleep(0.05)
    return _serve


def _gv_drive(subject, project="t", view_build=None, serve_out=None, timeout=1):
    """Drives dispatch.main() with role=grader forced onto the seat backend.
    `view_build`, when given, replaces `grader_view.build` for this one call only.
    `serve_out`, when given, starts a background thread answering the seat packet
    — omitted for a fixture that must fail before any spend (nothing to serve)."""
    _real_build = grader_view.build
    if view_build is not None:
        grader_view.build = view_build
    if serve_out is not None:
        threading.Thread(target=_gv_serve_with(serve_out), daemon=True).start()
    a = argparse.Namespace(role="grader", subject=subject, packet=str(PK), path=None, cwd=str(REPO),
                           charter=None, project=project, mcp_config=None, timeout=timeout, max_usd=None,
                           seat=True)
    try:
        dispatch.main(a)
        code = 0
    except SystemExit as e:
        code = e.code
    finally:
        grader_view.build = _real_build
    files = sorted((TMP / "events").glob("L-grader-*.jsonl"), key=lambda p: p.stat().st_mtime)
    raw = [json.loads(l) for l in files[-1].read_text().splitlines()]
    return code, raw


def _boom_build(*_a, **_k):
    raise AssertionError("grader_view.build must not be called here")


# ── AC1 · a.project falsy refuses before any spend, before a spawn id spends
# anything under $R/seat ────────────────────────────────────────────────────
code, raw = _gv_drive("L-spec-0437ac1", project="")
assert code == 1 and [e["type"] for e in raw] == ["spawn-failed"], raw
assert "needs --project" in raw[0]["why"], raw[0]
sid1 = raw[0]["spawn"]
assert not (TMP / "seat" / f"{sid1}.packet.md").exists() and not (TMP / "seat" / f"{sid1}.cmd.json").exists()
N += 1

# ── AC2 · no build-done anywhere on the ledger for the subject ──────────────
code, raw = _gv_drive("L-spec-0437ac2")
assert code == 1 and [e["type"] for e in raw] == ["spawn-failed"], raw
assert "no build-done" in raw[0]["why"], raw[0]
sid2 = raw[0]["spawn"]
assert not (TMP / "seat" / f"{sid2}.packet.md").exists()
N += 1

# ── AC3 · the newest build-done carries no ready_sha ────────────────────────
_gv_build_done("L-spec-0437ac3", base_sha="B0")
code, raw = _gv_drive("L-spec-0437ac3")
assert code == 1 and [e["type"] for e in raw] == ["spawn-failed"], raw
assert "ready_sha" in raw[0]["why"], raw[0]
N += 1

# ── AC4/AC5/AC6/AC7 · grader_view.build called once with the right args, the
# packet copy lands byte-identical under view/seat/, cmd.json's cwd becomes
# view/tree, and the event order is grader-view-built, spawn-started, ...,
# spawn-done ─────────────────────────────────────────────────────────────────
_gv_build_done("L-spec-0437ac4", base_sha="B1", ready_sha="R1")
_ac4_calls = []
_ac4_view = pathlib.Path(tempfile.mkdtemp())


def _ac4_build(*args):
    _ac4_calls.append(args)
    return _ac4_view


code, raw = _gv_drive("L-spec-0437ac4", view_build=_ac4_build, serve_out=grade([met]))
assert code == 0, raw
assert [e["type"] for e in raw][:2] == ["grader-view-built", "spawn-started"] and raw[-1]["type"] == "spawn-done", raw
sid4 = raw[0]["spawn"]
assert len(_ac4_calls) == 1, _ac4_calls
assert _ac4_calls[0] == ("L-spec-0437ac4", dispatch.ROOT / "repos" / "t", "B1", "R1", sid4), _ac4_calls[0]
assert raw[0]["view"] == str(_ac4_view) and raw[0]["ready_sha"] == "R1", raw[0]
assert (_ac4_view / "seat" / f"{sid4}.packet.md").read_text() == (TMP / "seat" / f"{sid4}.packet.md").read_text(), \
    "AC5: the view copy must be byte-identical to the real seat packet"
cj4 = json.loads((TMP / "seat" / f"{sid4}.cmd.json").read_text())
assert cj4["cwd"] == str(_ac4_view / "tree") and cj4["cwd"] != str(REPO), cj4
_rm_seat(sid4)
N += 1

# ── AC8 · (a) a non-grader role on seat: grader_view.build is never invoked
# (patched to raise), no grader-view-built appears, cmd.json's cwd is the
# caller's --cwd unchanged. (b) grader resolving to claude-p, its verify text
# naming a real capability (`npm` -> node_modules): L-spec-0481 AC12(iii)
# governs over AC21 for this one fixture (Thinker ruling) — refused
# preflight:no-view before any spend, grader_view.build still never invoked.
# (c) grader resolving to codex, empty spec/verify text (nothing required):
# unaffected, grader_view.build still never invoked ───────────────────────────
_real_build_ac8 = grader_view.build
grader_view.build = _boom_build
try:
    threading.Thread(target=_gv_serve_with(card), daemon=True).start()
    a8a = argparse.Namespace(role="builder", subject="L-spec-0437ac8a", packet=str(PK), path=None, cwd=str(REPO),
                             charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=True)
    try:
        dispatch.main(a8a)
        code8a = 0
    except SystemExit as e:
        code8a = e.code
    raw8a = [json.loads(l) for l in
             max((TMP / "events").glob("L-builder-*.jsonl"), key=lambda p: p.stat().st_mtime).read_text().splitlines()]
    assert code8a == 0 and not any(e["type"] == "grader-view-built" for e in raw8a), raw8a
    sid8a = raw8a[0]["spawn"]
    cj8a = json.loads((TMP / "seat" / f"{sid8a}.cmd.json").read_text())
    assert cj8a["cwd"] == str(REPO), cj8a
    _rm_seat(sid8a)

    os.environ.pop("DOIT_SEAT", None)
    _gv_build_done("L-spec-0437ac8b", base_sha="B1", ready_sha="R1")
    dispatch.CONTENT.mkdir(parents=True, exist_ok=True)
    (dispatch.CONTENT / "verify-L-spec-0437ac8b-grader.sh").write_text("npm test\n")
    code, types8b, evs8b, _ = spawn("grader", out=grade([met]), subject="L-spec-0437ac8b")
    assert code == 1 and types8b == ["spawn-failed"] and not any(t == "grader-view-built" for t in types8b), types8b
    assert spawn.raw[0]["backend"] == "claude-p" and spawn.raw[0]["reason"] == "preflight:no-view", spawn.raw[0]

    MT.write_text('[contracts.grader]\nbackend = "codex"\nmodel = "gpt-6-astra"\n')
    _real_codex_exec_ac8 = dispatch.run_codex_exec

    def _codex_fake_grader(cmd, packet, cwd, timeout):
        pathlib.Path(cmd[cmd.index("-o") + 1]).write_text(json.dumps(grade([met])))
        return argparse.Namespace(returncode=0, stderr="", stdout=json.dumps(
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
    dispatch.run_codex_exec = _codex_fake_grader
    code, types8c, evs8c, _ = spawn("grader", out=grade([met]), subject="L-spec-0437ac8c")
    dispatch.run_codex_exec = _real_codex_exec_ac8
    MT.unlink()
    assert code == 0 and not any(t == "grader-view-built" for t in types8c), types8c
    assert spawn.raw[0]["backend"] == "codex", spawn.raw[0]
finally:
    grader_view.build = _real_build_ac8
N += 1

# ── AC9 · run_seat(view=) direct-call: the SAME polling file set under $R/seat
# in both runs, differing only in the view/seat/ copy and cmd.json's cwd ─────
os.environ["DOIT_SEAT_CLAIM_SEC"] = "1"


def _ac9_serve(spawn_id):
    def go():
        time.sleep(0.05)
        (TMP / "seat" / f"{spawn_id}.result.json").write_text(json.dumps(
            {"is_error": False, "structured_output": grade([met]), "num_turns": 1, "usage": {},
             "total_cost_usd": None, "modelUsage": {"m": {}}, "session_id": "s", "permission_denials": []}))
    threading.Thread(target=go, daemon=True).start()


ac9a, ac9b = "L-run-seat-ac9a", "L-run-seat-ac9b"
_ac9_serve(ac9a)
dispatch.run_seat(ac9a, ["claude", "-p"], "packet", str(REPO), 6)
suffixes_a = sorted(p.name.split(".", 1)[1] for p in (TMP / "seat").glob(f"{ac9a}.*"))
cjA = json.loads((TMP / "seat" / f"{ac9a}.cmd.json").read_text())
_rm_seat(ac9a)

ac9_view = pathlib.Path(tempfile.mkdtemp())
_ac9_serve(ac9b)
dispatch.run_seat(ac9b, ["claude", "-p"], "packet", str(REPO), 6, view=ac9_view)
suffixes_b = sorted(p.name.split(".", 1)[1] for p in (TMP / "seat").glob(f"{ac9b}.*"))
cjB = json.loads((TMP / "seat" / f"{ac9b}.cmd.json").read_text())
_rm_seat(ac9b)

assert suffixes_a == suffixes_b, (suffixes_a, suffixes_b)
assert cjA["cwd"] == str(REPO) and cjB["cwd"] == str(ac9_view / "tree"), (cjA, cjB)
assert (ac9_view / "seat" / f"{ac9b}.packet.md").is_file(), "the view copy exists only when view= is given"
del os.environ["DOIT_SEAT_CLAIM_SEC"]
N += 1

# ── AC11 · an exception inside grader_view.build is caught, never an unhandled
# traceback ───────────────────────────────────────────────────────────────────
_gv_build_done("L-spec-0437ac11", base_sha="B1", ready_sha="R1")


def _boom_raise(*_a, **_k):
    raise RuntimeError("boom")


code, raw = _gv_drive("L-spec-0437ac11", view_build=_boom_raise)
assert code == 1 and [e["type"] for e in raw] == ["spawn-failed"], raw
assert "boom" in raw[0]["why"], raw[0]
sid11 = raw[0]["spawn"]
assert not (TMP / "seat" / f"{sid11}.packet.md").exists() and not (TMP / "seat" / f"{sid11}.cmd.json").exists()
assert not any(e["type"] == "grader-view-built" for e in raw)
N += 1

# ── AC12 · a codex-initiated grader dispatch falling back to seat runs with
# view=None throughout — the gate only ever fires on the INITIAL backend ─────
MT.write_text('[contracts.grader]\nbackend = "codex"\nmodel = "gpt-6-astra"\n'
              'fallback = { backend = "seat", model = "claude-sonnet-5" }\n')
dispatch.run_codex_exec = codex_dead
grader_view.build = _boom_build
threading.Thread(target=_gv_serve_with(grade([met])), daemon=True).start()
a12 = argparse.Namespace(role="grader", subject="L-spec-0437ac12", packet=str(PK), path=None, cwd=str(REPO),
                         charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=False)
try:
    dispatch.main(a12)
    code12 = 0
except SystemExit as e:
    code12 = e.code
MT.unlink()
raw12 = [json.loads(l) for l in
         max((TMP / "events").glob("L-grader-*.jsonl"), key=lambda p: p.stat().st_mtime).read_text().splitlines()]
assert not any(e["type"] == "grader-view-built" for e in raw12), raw12
sid12 = next(e["spawn"] for e in raw12 if e["type"] == "spawn-started")
cj12 = json.loads((TMP / "seat" / f"{sid12}.cmd.json").read_text())
assert cj12["cwd"] == str(REPO), cj12
_rm_seat(sid12)
N += 1

# ── AC14 · relay.pending_packets (unmodified) already surfaces a stalled
# role=grader seat spawn — a spawn-started with no terminal event and a real
# packet on disk ─────────────────────────────────────────────────────────────
ac14_events = [{"type": "spawn-started", "role": "grader", "subject": "L-spec-0437ac14",
               "spawn": "L-grader-0437ac14", "ts": dispatch.now()}]
(TMP / "seat").mkdir(parents=True, exist_ok=True)
(TMP / "seat" / "L-grader-0437ac14.packet.md").write_text("a packet\n")
# served_by=None: since L-spec-8033 graders are served by "grader-pane", not the
# relay; this assertion is about the stalled spawn surfacing at all.
pend = relay.pending_packets(ac14_events, root=TMP, served_by=None)
assert any(p["spawn"] == "L-grader-0437ac14" for p in pend), pend
(TMP / "seat" / "L-grader-0437ac14.packet.md").unlink()
N += 1

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0481 · grading-capabilities (L-charter-0042 R12) — AC12-AC14, AC16, AC19
# Reuses `_gv_drive`/`_gv_build_done`/`_boom_build` above: role=grader forced
# onto the seat backend, no real Postgres/bwrap needed — `node_modules` (no
# `grading.toml` row for project "t") and `git` (never provisioned, R8) both
# fail preflight cheaply and deterministically.
# ══════════════════════════════════════════════════════════════════════════════
import grading_env  # noqa: E402


def _cap_content(subject, verify_text):
    (dispatch.CONTENT).mkdir(parents=True, exist_ok=True)
    (dispatch.CONTENT / f"{subject}.md").write_text(f"# {subject}\n")
    (dispatch.CONTENT / f"verify-{subject}-grader.sh").write_text(verify_text)


def _real_view_build(*_a, **_k):
    return pathlib.Path(tempfile.mkdtemp(prefix="l0481-view-"))


# ── AC12(ii)/AC16 · no hold yet, `node_modules` unprovable -> one
# grading-preflight-failed, one capability-hold scoped to this spec,
# one escalation-blocking(kind=capability-hold), reason preflight:node_modules,
# NO spawn-started; grading_budget is unmoved by any of it ─────────────────────
ac12ii_subj = "L-spec-0481ac12ii"
_cap_content(ac12ii_subj, "npm test\n")
_gv_build_done(ac12ii_subj, base_sha="B1", ready_sha="R1")
gb_before = dispatch.grading_budget(fold.read_events(), ac12ii_subj)
code, raw = _gv_drive(ac12ii_subj, view_build=_real_view_build)
assert code == 1, raw
types12ii = [e["type"] for e in raw]
assert types12ii == ["grader-view-built", "grading-preflight-failed", "capability-hold",
                     "escalation-blocking", "spawn-failed"], types12ii
assert not any(t == "spawn-started" for t in types12ii), "AC16: a refusal never appends spawn-started"
pf12 = next(e for e in raw if e["type"] == "grading-preflight-failed")
assert pf12["role"] == "grader" and pf12["capability"] == "node_modules", pf12
ch12 = next(e for e in raw if e["type"] == "capability-hold")
assert ch12["subject"] == f"capability:node_modules:{ac12ii_subj}" and ch12["capability"] == "node_modules" \
    and ch12["spec"] == ac12ii_subj, ch12
esc12 = next(e for e in raw if e["type"] == "escalation-blocking")
assert esc12["subject"] == f"capability:node_modules:{ac12ii_subj}" and esc12["kind"] == "capability-hold" \
    and esc12["owner"] == "thinker", esc12
assert esc12["default"] and esc12["deadline"] and esc12["revert"] == f"doit append unblocked capability:node_modules:{ac12ii_subj}", esc12
sf12 = raw[-1]
assert sf12["reason"] == "preflight:node_modules", sf12
gb_after = dispatch.grading_budget(fold.read_events(), ac12ii_subj)
assert gb_before == gb_after, "AC16: a preflight refusal never moves grading_budget"
N += 1

# ── AC13 · the hold really refuses the NEXT dispatch (real write-then-read
# through fold.read_events(), not the writer's in-memory list); no view built ──
ac13_subj = ac12ii_subj
_cap_content(ac13_subj, "npm test\n")
_gv_build_done(ac13_subj, base_sha="B1", ready_sha="R1")
code, raw13 = _gv_drive(ac13_subj, view_build=_boom_build)
assert code == 1 and [e["type"] for e in raw13] == ["spawn-failed"], raw13
assert raw13[0]["reason"] == "held:node_modules", raw13[0]
N += 1

# The same capability gap on another spec gets its own preflight and hold.
ac13_other = "L-spec-0481ac13-other"
_cap_content(ac13_other, "npm test\n")
_gv_build_done(ac13_other, base_sha="B1", ready_sha="R1")
code, raw13_other = _gv_drive(ac13_other, view_build=_real_view_build)
assert code == 1 and raw13_other[-1]["reason"] == "preflight:node_modules", raw13_other
assert next(e for e in raw13_other if e["type"] == "capability-hold")["subject"] == \
    f"capability:node_modules:{ac13_other}", raw13_other
N += 1

# ── AC14(i) · a spec-local hold (on subject A) refuses only A; a clean
# subject B, unrelated to it, dispatches normally through to spawn-started ─────
# L-spec-8034/AC6: a bare `git` need (unattributed to any criterion) now
# ROUTES to owed instead of holding the dispatch — git is a GRADER_NEVER
# capability, excluded from `required()` before the held-capability/preflight
# checks ever run. `view-paths` (the one remaining SPEC_LOCAL capability
# routing never touches) preserves this fixture's original intent: a
# spec-local hold that isolates one subject without touching a sibling's.
ac14a_subj, ac14b_subj = "L-spec-0481ac14a", "L-spec-0481ac14b"
_cap_content(ac14a_subj, "/some/foreign/absolute/path\n")
_cap_content(ac14b_subj, "echo hi\n")
_gv_build_done(ac14a_subj, base_sha="B1", ready_sha="R1")
_gv_build_done(ac14b_subj, base_sha="B1", ready_sha="R1")


def _view_build_foreign_path(*_a, **_k):
    """`_real_view_build`'s stub carries no `verify.sh` of its own, so
    `_prove_view_paths` (which reads THIS view's `verify.sh`, not the
    content-dir's verify script) trivially passed with an empty file — this
    populates it with the same foreign path `required()` already saw, so the
    proof genuinely fails the way a real grader_view.build() would."""
    v = pathlib.Path(tempfile.mkdtemp(prefix="l0481-view-"))
    (v / "verify.sh").write_text("#!/bin/bash\n/some/foreign/absolute/path\n")
    return v


code, raw14a = _gv_drive(ac14a_subj, view_build=_view_build_foreign_path)
assert code == 1, raw14a
ch14a = next(e for e in raw14a if e["type"] == "capability-hold")
assert ch14a["subject"] == f"capability:view-paths:{ac14a_subj}", ch14a
code, raw14b = _gv_drive(ac14b_subj, view_build=_real_view_build, serve_out=grade([met]))
assert code == 0 and raw14b[-1]["type"] == "spawn-done", raw14b
assert not any(e["type"] in ("grading-preflight-failed", "capability-hold") for e in raw14b), \
    "AC14(i): subject B is not touched by A's spec-local hold"
_rm_seat(next(e["spawn"] for e in raw14b if e["type"] == "spawn-started"))
code, raw14a2 = _gv_drive(ac14a_subj, view_build=_boom_build)
assert code == 1 and raw14a2[0]["reason"] == "held:view-paths", raw14a2
N += 1

# ── AC14(ii) · reviewer preflight is read-only: porcelain(cwd) of a fixture
# git worktree is byte-identical before and after preflight(), and it writes
# no .env/grading.env/pg under it ───────────────────────────────────────────
rv_wt = harness.test_root("l0481-reviewer-wt")
subprocess.run(["git", "init", "-q"], cwd=rv_wt, check=True)
before_rv = dispatch.porcelain(rv_wt)
rv_failures = grading_env.preflight("reviewer", "L-spec-0481-nonexistent", rv_wt, "t", repo=dispatch.HERE.parent)
after_rv = dispatch.porcelain(rv_wt)
assert rv_failures == [], rv_failures    # gates-only, no [[prod]] row for "t" -> nothing required
assert before_rv == after_rv, (before_rv, after_rv)
assert not (rv_wt / ".env").exists() and not (rv_wt / "grading.env").exists() and not (rv_wt / "pg").exists()
harness.cleanup(rv_wt)
N += 1

# ── AC19 (dispatch-level) · a grader dispatch on the seat backend, `db` never
# required by its own verify text, gets `sandbox=`+dsn_role="none" on
# spawn-started, and grader-view-built precedes it (AC12(v) shape) ─────────────
ac19_subj = "L-spec-0481ac19"
_cap_content(ac19_subj, "echo hi\n")
_gv_build_done(ac19_subj, base_sha="B1", ready_sha="R1")
code, raw19 = _gv_drive(ac19_subj, view_build=_real_view_build, serve_out=grade([met]))
assert code == 0, raw19
assert [e["type"] for e in raw19][:2] == ["grader-view-built", "spawn-started"], raw19
ss19 = raw19[1]
assert ss19["dsn_role"] == "none" and ss19["sandbox"] in ("bwrap", "host"), ss19
_rm_seat(ss19["spawn"])
N += 1

# ── AC7 (dispatch half) · a REAL wall-clock timeout's `spawn-failed` now
# carries `reason: "timeout"` (mirroring the existing `reason: "unserved"`
# assertion above) — both of main()'s `except subprocess.TimeoutExpired` sites
# now stamp it; this fixture drives the first ─────────────────────────────────
def _timeout_boom(cmd, packet, cwd, timeout):
    raise subprocess.TimeoutExpired(cmd, timeout)


_real_run_claude_to = dispatch.run_claude
dispatch.run_claude = _timeout_boom
os.environ.pop("DOIT_SEAT", None)
PK.write_text("a packet for AC7 timeout\n")
a7 = argparse.Namespace(role="research", subject="L-spec-0001", packet=str(PK),
                        path=str(TMP / "content" / "L-research-0269to.md"), cwd=str(REPO),
                        charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=False)
try:
    dispatch.main(a7)
    code7 = 0
except SystemExit as e:
    code7 = e.code
PK.write_text(PK_DEFAULT)
dispatch.run_claude = _real_run_claude_to
raw7 = [json.loads(l) for l in max((TMP / "events").glob("L-research-*.jsonl")).read_text().splitlines()]
assert code7 == 1 and [e["type"] for e in raw7] == ["spawn-started", "spawn-failed"], raw7
assert raw7[-1]["reason"] == "timeout" and "timeout after" in raw7[-1]["why"], raw7[-1]
N += 1

# ── AC7 (doc-consistency half) · agents/executor.md's spawn-failed/spawn-stale
# row names the counted-attempt shape (the same one carry-failed's own row
# already uses) and the reviewer round-2-cap sentence, for reason in
# {unserved, timeout} — the same idiom `_claim_first` already uses for
# claim.sh, not a second grep step in Verification ────────────────────────────
_exec_text = (REPO_ROOT / "agents" / "executor.md").read_text()
_row7 = next(l for l in _exec_text.splitlines() if "spawn-failed" in l and "spawn-stale" in l and "no later" in l)
assert "{unserved, timeout}" in _row7, "AC7: the row must name reason in {unserved, timeout}"
assert "third" in _row7 and "escalation-blocking" in _row7, \
    "AC7: the counted-attempt (third-failure) wording must be present"
assert "round-2 cap" in _row7, "AC7: the reviewer round-cap sentence must be present"
N += 1


# An empty or non-file --packet is refused before a spawn id exists (pilot "Smaller"; charter 3).
before_files = sorted((TMP / "events").glob("L-research-*.jsonl"))
for bad_packet in ("", str(TMP / "nowhere.md")):
    try:
        dispatch.main(argparse.Namespace(role="research", subject="L-spec-0001", packet=bad_packet, path=None, cwd=str(REPO),
                                         charter=None, project="t", mcp_config=None, timeout=None, max_usd=None, seat=False))
        raise AssertionError("a non-file packet must be refused")
    except SystemExit as e:
        assert "not a file" in str(e.code) and "nothing allocated" in str(e.code), e.code
assert sorted((TMP / "events").glob("L-research-*.jsonl")) == before_files, "refused before allocation: no new spawn file"

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0262 · seat-write-path (L-charter-0031) — SWP1-SWP5
# ══════════════════════════════════════════════════════════════════════════════

# ── AC2 · research/reuse-scout with a.path=None: refused before any spend —
#    exactly one spawn-failed, no start event, no backend ever entered ────────
_ac2_hit = []
_real_run_claude, _real_run_seat, _real_run_codex = dispatch.run_claude, dispatch.run_seat, dispatch.run_codex


def _ac2_boom(name):
    def f(*a_, **k_):
        _ac2_hit.append(name)
        raise AssertionError(f"{name} must not be entered on a pre-spend refusal")
    return f


dispatch.run_claude, dispatch.run_seat, dispatch.run_codex = _ac2_boom("run_claude"), _ac2_boom("run_seat"), _ac2_boom("run_codex")
for _role in ("research", "reuse-scout"):
    # A fresh packet body — the bare default "a packet\n" already carries a
    # standing D120 dedup entry for research (research-0018's codex "weekly
    # limit" failure earlier in this file), which would refuse this dispatch
    # for THAT reason and mask the one this fixture means to prove.
    PK.write_text(f"a packet for AC2 ({_role})\n")
    code, raw, _ = _seat_driven(_role, "L-spec-0001", 1, path=None)
    PK.write_text(PK_DEFAULT)
    assert code == 1 and [e["type"] for e in raw] == ["spawn-failed"], (_role, raw)
    assert raw[0]["why"] == "a writing role needs --path", raw[0]
dispatch.run_claude, dispatch.run_seat, dispatch.run_codex = _real_run_claude, _real_run_seat, _real_run_codex
assert _ac2_hit == [], "AC2: no backend was ever entered on the pre-spend refusal"
N += 1


def _swp_serve(make_ready):
    """Background thread for the fixtures below: waits for exactly one still-open
    seat packet, calls `make_ready()` to create whatever file/dir the structured
    output it returns claims exists, then answers with a fixed completion envelope."""
    for _ in range(400):
        pk = [q for q in (list((TMP / "seat").glob("*.packet.md")) if (TMP / "seat").is_dir() else [])
              if not (TMP / "seat" / (q.name.split(".")[0] + ".result.json")).exists()
              and not (TMP / "seat" / (q.name.split(".")[0] + ".output.json")).exists()]
        if pk:
            sid = max(pk, key=lambda q: q.stat().st_mtime).name.split(".")[0]
            out = make_ready()
            (TMP / "seat" / f"{sid}.result.json").write_text(json.dumps(
                {"is_error": False, "structured_output": out, "num_turns": 1, "usage": {},
                 "total_cost_usd": None, "modelUsage": {"m": {}}, "session_id": sid, "permission_denials": []}))
            return
        time.sleep(0.05)


def _swp_dispatch(role, subject, path, make_ready, cwd=None, packet_text=None):
    """Drives dispatch.main() directly on the seat backend (seat=True — no
    DOIT_SEAT env needed); returns (code, events, spawn_id, cmd_json, packet_text).
    `packet_text`, when given, is written to PK for this call only and restored
    to the file-wide default after — a research dispatch earlier in this file
    already left a standing D120 dedup entry on the bare "a packet\\n" body."""
    if packet_text is not None:
        PK.write_text(packet_text)
    threading.Thread(target=_swp_serve, args=(make_ready,), daemon=True).start()
    a = argparse.Namespace(role=role, subject=subject, packet=str(PK), path=path, cwd=str(cwd or REPO),
                           charter=None, project="t", mcp_config=None, timeout=1, max_usd=None, seat=True)
    try:
        dispatch.main(a)
        code = 0
    except SystemExit as e:
        code = e.code
    if packet_text is not None:
        PK.write_text(PK_DEFAULT)
    raw = [json.loads(l) for l in max((TMP / "events").glob(f"L-{role}-*.jsonl")).read_text().splitlines()]
    sid = raw[0]["spawn"]
    cj = json.loads((TMP / "seat" / f"{sid}.cmd.json").read_text())
    return code, raw, sid, cj, (TMP / "seat" / f"{sid}.packet.md").read_text()


def _mk_spec(dest, subject):
    # spec-writer's own schema carries no "path" property (additionalProperties:
    # false) — its Output never echoes one back; `dispatch.main` reads the file at
    # `a.path` directly, so writing WELL_FORMED_SPEC there is the whole contract.
    def ready():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(WELL_FORMED_SPEC)
        return {**sw, "spec_id": subject, "escalations": []}
    return ready


def _mk_file(dest, base):
    def ready():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("dug")
        return {**base, "path": str(dest)}
    return ready


# ── AC1 (L-spec-0321/R5) · spec_path returns an absolute path even when
#    CONTENT itself is relative — proven directly, not through the
#    environment ─────────────────────────────────────────────────────────────
_content_real = dispatch.CONTENT
dispatch.CONTENT = pathlib.Path("content")
assert dispatch.spec_path("x").is_absolute(), "AC1: spec_path must be absolute even with a relative CONTENT"
dispatch.CONTENT = _content_real
N += 1

# ── AC1·SWP1 · spec-writer, no --path: gets the CONTENT default and reaches
#    spec-written/spawn-done — never "a writing role needs --path" ───────────
swp_sub1 = "L-spec-0261a"
swp_dest1 = dispatch.CONTENT / f"{swp_sub1}.md"
code, raw1, sid1, cj1, pkt1 = _swp_dispatch("spec-writer", swp_sub1, None, _mk_spec(swp_dest1, swp_sub1), cwd=TMP)
assert code == 0 and [e["type"] for e in raw1] == ["spawn-started", "spec-written", "spawn-done"], raw1
assert not any(e["type"] == "spawn-failed" for e in raw1), "AC1: no needs-path failure for a defaulted spec-writer"

# ── AC3·SWP2 · cmd.json["path"] for that same spawn: absolute, the CONTENT
#    default, and (this fixture's cwd IS the ledger root) the review_path's own
#    cwd-relative formula too ──────────────────────────────────────────────────
assert cj1["path"] == str(swp_dest1) == str(pathlib.Path(TMP) / "content" / f"{swp_sub1}.md"), cj1

# ── AC4·SWP2 · every kind-truthy role WITH an explicit --path: cmd.json["path"]
#    present, absolute, equal to the resolved value; a kind=None role (builder)
#    gets null ─────────────────────────────────────────────────────────────────
swp_r = TMP / "content" / "L-swp-ac4-research.md"
_, _, _, cj_r, _ = _swp_dispatch("research", "L-spec-0261b", str(swp_r), _mk_file(swp_r, research),
                                 packet_text="a packet for swp ac4 research\n")
assert cj_r["path"] == str(swp_r) and pathlib.Path(cj_r["path"]).is_absolute(), cj_r

swp_rs = TMP / "content" / "L-swp-ac4-reuse.md"
reuse_out = {"summary": "s", "candidates": [], "nothing_cleared": True, "complete": True,
            "contamination": False, "declarations": []}
_, _, _, cj_rs, _ = _swp_dispatch("reuse-scout", "L-spec-0261c", str(swp_rs), _mk_file(swp_rs, reuse_out))
assert cj_rs["path"] == str(swp_rs) and pathlib.Path(cj_rs["path"]).is_absolute(), cj_rs

swp_pd = TMP / "content" / "L-swp-ac4-probe"


def _swp_probe_ready():
    swp_pd.mkdir(parents=True, exist_ok=True)
    (swp_pd / "run.md").write_text("ran")
    return {**probe_out, "path": str(swp_pd) + "/"}


_, _, _, cj_p, _ = _swp_dispatch("probe", "L-spec-0261d", str(swp_pd), _swp_probe_ready)
assert cj_p["path"] == str(swp_pd) and pathlib.Path(cj_p["path"]).is_absolute(), cj_p

_, _, sid_b, cj_b, pkt_b = _swp_dispatch("builder", "L-spec-0261f", None, lambda: card)
assert cj_b["path"] is None, cj_b
N += 1

# ── AC3 (L-spec-0321/R5) · a RELATIVE --path (spec-writer), dispatched under a
#    cwd that is NOT $DOIT_ROOT (REPO, reproducing the 13:40Z incident's shape),
#    resolves against ROOT — never that cwd — to spec_path(subject) ───────────
swp_rel = "content/L-spec-0261g.md"
_, _, _, cj5, _ = _swp_dispatch("spec-writer", "L-spec-0261g", swp_rel,
                                _mk_spec(dispatch.spec_path("L-spec-0261g"), "L-spec-0261g"))
assert cj5["path"] == str(dispatch.spec_path("L-spec-0261g")), cj5
N += 1

# ── AC4 (L-spec-0321/R5) · spec-writer with a --path that is well-formed and
#    absolute under $DOIT_ROOT/content/, but names a DIFFERENT file than the
#    subject (the exact shape the old code silently honored, formerly
#    swp_sw/"L-spec-0261e") — refused before any seat file exists: exit 1, one
#    spawn-failed(reason=write-path-mismatch), no backend entered. Driven
#    DIRECTLY through dispatch.main — never through _swp_dispatch/_swp_serve,
#    whose background thread would otherwise wait for a seat packet this
#    refusal never writes, then serve the NEXT still-open packet
#    (L-spec-0261f/builder, above) with this call's own make_ready() output,
#    corrupting that already-completed fixture ─────────────────────────────────
swp_sw = TMP / "content" / "L-swp-ac4-spec.md"
_seat_before = sorted((TMP / "seat").glob("*")) if (TMP / "seat").is_dir() else []
dispatch.run_claude, dispatch.run_seat, dispatch.run_codex = _ac2_boom("run_claude"), _ac2_boom("run_seat"), _ac2_boom("run_codex")
a_sw = argparse.Namespace(role="spec-writer", subject="L-spec-0261e", packet=str(PK), path=str(swp_sw),
                          cwd=str(REPO), charter=None, project="t", mcp_config=None, timeout=1,
                          max_usd=None, seat=True)
try:
    dispatch.main(a_sw)
    raise AssertionError("a mismatched spec-writer --path must be refused")
except SystemExit as e:
    code_sw = e.code
dispatch.run_claude, dispatch.run_seat, dispatch.run_codex = _real_run_claude, _real_run_seat, _real_run_codex
raw_sw = [json.loads(l) for l in max((TMP / "events").glob("L-spec-writer-*.jsonl")).read_text().splitlines()]
assert code_sw == 1 and [e["type"] for e in raw_sw] == ["spawn-failed"], raw_sw
assert raw_sw[0]["reason"] == "write-path-mismatch", raw_sw[0]
_seat_after = sorted((TMP / "seat").glob("*")) if (TMP / "seat").is_dir() else []
assert _seat_after == _seat_before, "AC4: no seat/<spawn>.* file for the refused spawn"
assert _ac2_hit == [], "AC4: no backend was ever entered on the write-path-mismatch refusal"
N += 1

# ── AC5 (L-spec-0321/R5) · spec-writer with --path EXPLICITLY equal to
#    spec_path(subject) still succeeds — the mismatch check does not
#    over-refuse the legitimate case (e.g. carry.py's own explicit-absolute
#    call pattern) ───────────────────────────────────────────────────────────
swp_match_subj = "L-spec-0261h"
swp_match_path = str(dispatch.spec_path(swp_match_subj))
code_m, raw_m, _, cj_m, _ = _swp_dispatch("spec-writer", swp_match_subj, swp_match_path,
                                          _mk_spec(dispatch.spec_path(swp_match_subj), swp_match_subj))
assert code_m == 0 and [e["type"] for e in raw_m] == ["spawn-started", "spec-written", "spawn-done"], raw_m
assert cj_m["path"] == swp_match_path, cj_m
N += 1

# ── AC6·SWP3 · the packet text: "WRITE PATH: <path>\n\n" + original, for a
#    writing role; byte-identical (no prefix) for a non-writing role ──────────
assert pkt1.startswith(f"WRITE PATH: {cj1['path']}\n\n"), pkt1[:120]
assert pkt1[len(f"WRITE PATH: {cj1['path']}\n\n"):] == PK.read_text() + f"\n\nspawn_id: {sid1}\n", \
    "AC6: the remainder is the original packet, byte-for-byte"
assert not pkt_b.startswith("WRITE PATH:"), "AC6: a non-writing role's packet carries no WRITE PATH prefix"
assert pkt_b == PK.read_text() + f"\n\nspawn_id: {sid_b}\n", \
    "AC6: a non-writing role's packet is byte-identical to the one passed in"
N += 1

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0195 · spec-shape-checked-before-build (L-charter-0028) — AC2, AC4, AC6
#
# Placed BEFORE the L-spec-0192 section below on purpose: that section hand-writes
# spec-writer ledger files with non-numeric spawn-id suffixes ("...-stale9192",
# "...-open9192", ...), and this file's own `spawn()` helper finds "the latest"
# role file with a bare `max()` over a glob — lexicographically, not by alloc
# order — so a "spec-writer" spawn issued AFTER those exist would silently read
# one of THEM back instead of its own freshly allocated file.
# ══════════════════════════════════════════════════════════════════════════════

# ── AC6 · fold.EMITS["verify-waiver"] is executor/operator only ────────────────
assert fold.EMITS["verify-waiver"] == {"executor", "operator"}, fold.EMITS["verify-waiver"]

# ── AC2 · a malformed spec: spec-written, then spec-shape-failed, never
#          spawn-failed; fold state stays "written", never "void" ─────────────
BAD_SHAPE_SPEC = "# L-spec-0093\nno verification, no acceptance criteria, no writes grant\n"
sp93 = TMP / "content" / "L-spec-0093.md"
code, types, evs, cmd = spawn("spec-writer", out={**sw, "spec_id": "L-spec-0093"}, path=sp93,
                              side=lambda: sp93.write_text(BAD_SHAPE_SPEC), subject="L-spec-0093")
assert code == 0 and types == ["spec-written", "spec-shape-failed", "question", "spawn-done"], types
assert evs[1]["findings"], "spec-shape-failed carries what validate.spec_shape returned"
specs, *_ = fold.fold(fold.read_events())
assert specs["L-spec-0093"]["state"] == "written", \
    f"AC2: fold state stays 'written', never 'void': {specs['L-spec-0093']['state']}"

# ── AC4 · a builder dispatch against it is refused before any spend, and clears
#          the instant a fresh well-formed spec-written lands ─────────────────
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-0093")
assert code == 1 and types == ["spawn-failed"] and "spec-shape" in evs[0]["why"], (types, evs)
assert evs[0].get("reason") == "spec-shape", evs[0]
assert cmd is None, "run_claude must never be called on a spec-shape refusal"

code, types, evs, cmd = spawn("spec-writer", out={**sw, "spec_id": "L-spec-0093"}, path=sp93,
                              side=lambda: sp93.write_text(WELL_FORMED_SPEC), subject="L-spec-0093")
assert code == 0 and types == ["spec-written", "question", "spawn-done"], types
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-0093")
assert code == 0 and cmd is not None, (code, types, evs)

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0192 · fold-states-owed-due-and-killed (L-charter-0028) — R3
# ══════════════════════════════════════════════════════════════════════════════

# ── AC10 · role=builder is refused against a killed subject, before any spend ─
code, types, evs, cmd = spawn("spec-writer", out={**sw, "status": "killed", "killed_by_check": 2,
                                                   "escalations": []}, subject="L-spec-0099")
assert code == 0 and types == ["spec-killed", "spawn-done"], (types, evs)
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-0099")
assert code == 1 and types == ["spawn-failed"] and "killed" in evs[0]["why"], (types, evs)
assert cmd is None, "run_claude must never be called on a killed-subject refusal"

# ── AC12 · role=builder is refused while a spec-writer spawn is still open ────
OPEN_SW = TMP / "events" / "L-spec-writer-open9192.jsonl"
dispatch.emit(OPEN_SW, {}, "spawn-started", subject="L-spec-0097", role="spec-writer",
             spawn="L-spec-writer-open9192")
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-0097")
assert code == 1 and types == ["spawn-failed"] and "spec-writer spawn" in evs[0]["why"], (types, evs)
assert cmd is None, "run_claude must never be called on an open-spec-writer-spawn refusal"
# L-spec-0276/AC13: the open-spec-writer-spawn refusal now names its reason.
assert evs[0].get("reason") == "rework-open", evs[0]

# ── AC13a · a spawn-done for the EXACT spawn id lets the builder proceed ──────
DONE_SW = TMP / "events" / "L-spec-writer-done9192.jsonl"
dispatch.emit(DONE_SW, {}, "spawn-started", subject="L-spec-0096", role="spec-writer",
             spawn="L-spec-writer-done9192")
dispatch.emit(DONE_SW, {}, "spawn-done", subject="L-spec-0096", spawn="L-spec-writer-done9192")
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-0096")
assert code == 0 and cmd is not None, (code, types, evs)

# ── AC13b · a spawn-stale for the EXACT spawn id proceeds too ─────────────────
STALE_SW = TMP / "events" / "L-spec-writer-stale9192.jsonl"
dispatch.emit(STALE_SW, {}, "spawn-started", subject="L-spec-0095", role="spec-writer",
             spawn="L-spec-writer-stale9192")
dispatch.emit(STALE_SW, {}, "spawn-stale", subject="L-spec-0095", spawn="L-spec-writer-stale9192")
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-0095")
assert code == 0 and cmd is not None, (code, types, evs)

# ── AC13c · an unmatchable start (no spawn id) aged past 2x the spec-writer cap
# also proceeds — mirroring tick.in_flight's own handling of one.
_cap = dispatch.ROLES["spec-writer"][1]
_old_ts = (_dt.now(_timezone.utc) - _timedelta(minutes=2 * _cap + 5)).isoformat(timespec="seconds")
OLD_SW = TMP / "events" / "L-spec-writer-nospawn9192.jsonl"
OLD_SW.write_text(json.dumps({"v": 1, "ts": _old_ts, "type": "spawn-started",
                              "subject": "L-spec-0094", "role": "spec-writer"}) + "\n")
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-0094")
assert code == 0 and cmd is not None, (code, types, evs)

# ── AC14 · dispatch.emit() gates on the required-fields door ONLY ─────────────
EMIT_ESC = TMP / "events" / "L-emit-esc-test.jsonl"
before_lines = EMIT_ESC.read_text().splitlines() if EMIT_ESC.is_file() else []
reason = dispatch.emit(EMIT_ESC, {}, "escalation-blocking", subject="x", why="y")
after_lines = EMIT_ESC.read_text().splitlines() if EMIT_ESC.is_file() else []
assert reason is not None and "default" in reason and "deadline" in reason and "revert" in reason, reason
assert after_lines == before_lines, "a field-refused emit() writes nothing"

EMIT_VERDICT = TMP / "events" / "L-builder-notgrader-test.jsonl"   # actor "builder" is not in EMITS["verdict"]
r2 = dispatch.emit(EMIT_VERDICT, {}, "verdict", subject="x", confirmed=True, n=1,
                   matches_intent="yes", card_ok="yes", cannot_assess=[])
assert r2 is None, r2
assert any(json.loads(l)["type"] == "verdict" for l in EMIT_VERDICT.read_text().splitlines()), \
    "an actor/type mismatch alone is never refused at the emit() door — only recorded and ignored at fold time"

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0194 · worktree-readonly-dsn (L-charter-0028) — R3
# ══════════════════════════════════════════════════════════════════════════════
_H = []


def _root(name):
    """A fresh, private directory this file is responsible for cleaning up —
    never the real HOME/DOIT_ROOT (harness.py, L-spec-0183)."""
    p = harness.test_root(name)
    _H.append(p)
    return p


def _checkout(name, env_text=None):
    d = _root(f"checkout-{name}")
    if env_text is not None:
        (d / ".env").write_text(env_text)
    return d


def _worktree(name, git=False, gitignore=None, existing_env=None):
    d = _root(f"worktree-{name}")
    if git:
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    if gitignore is not None:
        (d / ".gitignore").write_text(gitignore)
    if existing_env is not None:
        (d / ".env").write_text(existing_env)
    return d


# ── AC1 · no .env at all -> None ──────────────────────────────────────────────
co1 = _checkout("ac1")
assert dispatch.readonly_dsn(co1) is None, "AC1"

# ── AC2 · .env with SUPABASE_DB_URL / SUPABASE_DB_URL_DIRECT only -> None ─────
co2 = _checkout("ac2", "SUPABASE_DB_URL=rw1\nSUPABASE_DB_URL_DIRECT=rw2\n")
assert dispatch.readonly_dsn(co2) is None, "AC2"

# ── AC3 · four .env shapes ─────────────────────────────────────────────────────
assert dispatch.readonly_dsn(_checkout("ac3-1", "SUPABASE_DB_URL_RO=plain-value\n")) == "plain-value", \
    "AC3.1: unquoted value verbatim"
assert dispatch.readonly_dsn(_checkout("ac3-2", 'SUPABASE_DB_URL_RO="quoted-value"\n')) == "quoted-value", \
    "AC3.2: double-quoted value, quotes stripped"
assert dispatch.readonly_dsn(_checkout("ac3-3", "# a comment\nSUPABASE_DB_URL_RO=real-value\n")) == "real-value", \
    "AC3.3: a comment line is not a declaration"
assert dispatch.readonly_dsn(_checkout("ac3-4", "SUPABASE_DB_URL_RO=first\nSUPABASE_DB_URL_RO=second\n")) == "second", \
    "AC3.4: the key declared twice -> the LAST value"

# ── AC4 · no RO key -> "absent", nothing written, worktree need not be a repo ──
wt4 = _worktree("ac4")
r = dispatch.provision_worktree_env(wt4, co2)
assert r == "absent" and not (wt4 / ".env").exists(), ("AC4", r)

# ── AC5 · RO byte-equals SUPABASE_DB_URL -> "refused" ─────────────────────────
co5 = _checkout("ac5", "SUPABASE_DB_URL=same-value\nSUPABASE_DB_URL_RO=same-value\n")
wt5 = _worktree("ac5")
r = dispatch.provision_worktree_env(wt5, co5)
assert r == "refused" and not (wt5 / ".env").exists(), ("AC5", r)

# ── AC6 · RO byte-equals SUPABASE_DB_URL_DIRECT (distinct from SUPABASE_DB_URL)
#         -> "refused" — the widened, not-just-SUPABASE_DB_URL match ──────────
co6 = _checkout("ac6", "SUPABASE_DB_URL=rw-value\nSUPABASE_DB_URL_DIRECT=direct-value\n"
                       "SUPABASE_DB_URL_RO=direct-value\n")
wt6 = _worktree("ac6")
r = dispatch.provision_worktree_env(wt6, co6)
assert r == "refused" and not (wt6 / ".env").exists(), ("AC6", r)

# A checkout with a valid RO DSN distinct from every OTHER DB_URL-named key,
# reused by AC7-AC10.
CO_VALID = _checkout("valid", "SUPABASE_DB_URL=rw-value\nSUPABASE_DB_URL_DIRECT=direct-value\n"
                              "PG_PARITY_DB_URL=parity-value\nSUPABASE_DB_URL_RO=ro-distinct-value\n")
RO_LINE = b"SUPABASE_DB_URL=ro-distinct-value\n"

# ── AC7 · worktree IS a git repo, .gitignore does not list .env -> "refused",
#         proving the ignore-gate fires independently of the equality gate ────
wt7 = _worktree("ac7", git=True)   # no .gitignore at all
r = dispatch.provision_worktree_env(wt7, CO_VALID)
assert r == "refused" and not (wt7 / ".env").exists(), ("AC7", r)

# ── AC8 · fresh git worktree, .gitignore lists .env, no pre-existing .env ─────
wt8 = _worktree("ac8", git=True, gitignore=".env\n")
r = dispatch.provision_worktree_env(wt8, CO_VALID)
env8 = wt8 / ".env"
assert r == "readonly" and env8.read_bytes() == RO_LINE, ("AC8", r, env8.read_bytes() if env8.exists() else None)
assert (env8.stat().st_mode & 0o777) == 0o600, oct(env8.stat().st_mode)

# ── AC9 · repeated on AC8's already-provisioned pair -> "readonly" again,
#         content unchanged (idempotent) ──────────────────────────────────────
r2 = dispatch.provision_worktree_env(wt8, CO_VALID)
assert r2 == "readonly" and env8.read_bytes() == RO_LINE, ("AC9", r2)

# ── AC10 · a pre-existing, unrelated .env -> "refused", bytes unchanged,
#          worktree need not be a git repo ────────────────────────────────────
wt10 = _worktree("ac10", existing_env="SOME_OTHER_VAR=x\n")
before10 = (wt10 / ".env").read_bytes()
r = dispatch.provision_worktree_env(wt10, CO_VALID)
assert r == "refused" and (wt10 / ".env").read_bytes() == before10, ("AC10", r)

for p in _H:
    harness.cleanup(p)

# ── AC11-AC15 · dispatch.main() wires dsn_role end to end ────────────────────
DSN_PROJECT = "dsnproj"
DSN_CHECKOUT = TMP / "repos" / DSN_PROJECT
DSN_CHECKOUT.mkdir(parents=True)
(DSN_CHECKOUT / ".env").write_text("SUPABASE_DB_URL=rw-value\nSUPABASE_DB_URL_DIRECT=direct-value\n"
                                   "SUPABASE_DB_URL_RO=e2e-ro-value\n")
E2E_LINE = b"SUPABASE_DB_URL=e2e-ro-value\n"
_H2 = []


def _dsn_worktree(name):
    d = harness.test_root(f"dsn-wt-{name}")
    _H2.append(d)
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    (d / ".gitignore").write_text(".env\n")
    return d


def drive(role, subject, cwd, project, out):
    """Like spawn() above, but drives dispatch.main() with a caller-chosen
    project/cwd — spawn() itself is pinned to REPO/project="t" for every
    other case in this file, which is exactly the case dsn_role=="absent"
    ends up exercising anyway (no repos/t/.env ever exists here)."""
    res = {"is_error": False, "terminal_reason": "completed", "structured_output": out, "num_turns": 1,
           "usage": {"input_tokens": 1, "output_tokens": 2}, "total_cost_usd": 0.01,
           "modelUsage": {"m": {}}, "permission_denials": []}
    seen = {}

    def fake(cmd, packet, cwd_, timeout):
        p = pathlib.Path(cwd_) / ".env"
        seen["env_exists_at_call"] = p.exists()
        seen["env_bytes_at_call"] = p.read_bytes() if p.exists() else None
        fake.cmd = cmd
        return argparse.Namespace(stdout=json.dumps(res), returncode=0, stderr="")
    dispatch.run_claude = fake
    a = argparse.Namespace(role=role, subject=subject, packet=str(PK), path=None,
                           cwd=str(cwd), charter=None, project=project, mcp_config=None,
                           timeout=None, max_usd=None)
    try:
        dispatch.main(a)
        code = 0
    except SystemExit as e:
        code = e.code
    # [0-9]*, not *: L-builder-notgrader-test.jsonl (EMIT_VERDICT, above) also
    # matches a bare "L-builder-*.jsonl" and, being non-numeric, sorts after
    # every real 4-digit spawn id — max() would silently pick IT instead.
    raw = [json.loads(l) for l in
           max((TMP / "events").glob(f"L-{role}-[0-9]*.jsonl")).read_text().splitlines()]
    return code, raw, seen


# AC11: build-started carries dsn_role=="readonly", and <cwd>/.env exists with
# the exact one line already at the moment the mocked spawn is invoked.
wt11 = _dsn_worktree("ac11")
code, raw11, seen11 = drive("builder", "L-spec-0111", wt11, DSN_PROJECT, card)
bs11 = next(e for e in raw11 if e["type"] == "build-started")
assert bs11["dsn_role"] == "readonly", bs11
assert seen11["env_exists_at_call"] and seen11["env_bytes_at_call"] == E2E_LINE, \
    ("AC11: not provisioned before the mocked spawn ran", seen11)
assert (wt11 / ".env").read_bytes() == E2E_LINE, "AC11: final state"

# AC12: the grader's own spawn-started carries role AND dsn_role together.
# L-spec-0481/AC19/AC21 (sanctioned edit): a grader receives NO production DSN
# of any kind (SD-R12-3b) — provision_worktree_env is never called for it, so
# this claude-p grader (no view, no `db` capability computed) is "none", never
# the L-spec-0194 "readonly" this used to assert.
wt12 = _dsn_worktree("ac12")
code, raw12, seen12 = drive("grader", "L-spec-0112", wt12, DSN_PROJECT, grade([met]))
ss12 = next(e for e in raw12 if e["type"] == "spawn-started")
assert ss12["role"] == "grader" and ss12["dsn_role"] == "none", ss12
assert not (wt12 / ".env").exists(), "AC19: no production DSN reaches a grader's cwd"

# AC13: every OTHER role's spawn-started carries no dsn_role key at all.
# A fresh packet body, not the file-wide "a packet\n": an identical
# (packet, contract) pair already failed as L-research-0018 (weekly limit)
# above, and D120 refuses to re-spend on that exact combination.
PK.write_text("a packet for AC13\n")
code, types, evs, _ = spawn("research", out=research, path=rp)
PK.write_text(PK_DEFAULT)
assert code == 0, (code, types, evs)
assert spawn.raw[0]["type"] == "spawn-started" and spawn.raw[0]["role"] == "research", spawn.raw[0]
assert "dsn_role" not in spawn.raw[0], spawn.raw[0]

# AC14: a.project falsy -> dsn_role=="absent", no .env written, and the
# git-check-ignore subprocess is never invoked at all.
wt14 = harness.test_root("dsn-wt-ac14")   # deliberately not even a git repo
_H2.append(wt14)
_ignore_calls = {"n": 0}
_real_subprocess_run = subprocess.run


def _counting_run(cmd, *a_, **kw):
    if len(cmd) > 1 and cmd[0] == "git" and "check-ignore" in cmd:
        _ignore_calls["n"] += 1
    return _real_subprocess_run(cmd, *a_, **kw)


subprocess.run = _counting_run
try:
    code, raw14, seen14 = drive("builder", "L-spec-0114", wt14, None, card)
finally:
    subprocess.run = _real_subprocess_run
bs14 = next(e for e in raw14 if e["type"] == "build-started")
assert bs14["dsn_role"] == "absent" and not (wt14 / ".env").exists() and _ignore_calls["n"] == 0, \
    (bs14, _ignore_calls)

# AC15: the RO DSN literal appears in neither captured stdout/stderr nor any
# field of any ledger event appended on the subject.
wt15 = _dsn_worktree("ac15")
_buf_out, _buf_err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(_buf_out), contextlib.redirect_stderr(_buf_err):
    code, raw15, seen15 = drive("builder", "L-spec-0115", wt15, DSN_PROJECT, card)
_captured = _buf_out.getvalue() + _buf_err.getvalue()
assert "e2e-ro-value" not in _captured, "AC15: the DSN literal leaked into stdout/stderr"
for e in raw15:
    assert "e2e-ro-value" not in json.dumps(e), ("AC15: the DSN literal leaked into a ledger event", e)

for p in _H2:
    harness.cleanup(p)

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0387 · owed-sweeper-role (L-charter-0038 R1) — AC6-9, AC12-16
# ══════════════════════════════════════════════════════════════════════════════
import jsonschema as _js387  # noqa: E402

OS_SCHEMA = json.loads((REPO_ROOT / "agents" / "owed-sweeper.schema.json").read_text())


def _sweep_manifest(sweep_id, rows, project="t", cwd=None):
    cwd = cwd or str(REPO)
    (TMP / "content" / f"{sweep_id}.md").write_text(
        f"# {sweep_id}\n\nproject: {project}\ncwd: {cwd}\n\n## Rows\n```json\n{json.dumps(rows)}\n```\n")


def _os_row(spec, criterion, declared_src="owed-ac", line="1"):
    return {"spec": spec, "criterion": criterion, "declared_src": declared_src, "line": line,
            "spec_path": str(dispatch.spec_path(spec))}


# ── AC6 · schema requires top-level batch/results; each result requires spec/
#         criterion/verdict; a met/failed row missing evidence, a failed row
#         missing kind, or a cannot-observe row missing capability/why raises ──
_os_good = {"batch": "L-owed-sweeper-schema", "contamination": False, "escalations": [], "declarations": [],
           "results": [{"spec": "L-spec-9001", "criterion": "AC1", "verdict": "met", "evidence": "e"},
                       {"spec": "L-spec-9002", "criterion": "AC2", "verdict": "failed", "evidence": "e",
                        "kind": "stale"},
                       {"spec": "L-spec-9003", "criterion": "AC3", "verdict": "cannot-observe",
                        "capability": "ro-dsn", "why": "w"}]}
_js387.validate(_os_good, OS_SCHEMA)      # every valid sample validates
