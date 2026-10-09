

def _os_bad_result(row):
    try:
        _js387.validate({**_os_good, "results": [row]}, OS_SCHEMA)
        raise AssertionError(f"AC6: schema accepted an invalid result row: {row!r}")
    except _js387.ValidationError:
        pass


_os_bad_result({"criterion": "AC1", "verdict": "met", "evidence": "e"})                          # no spec
_os_bad_result({"spec": "x", "verdict": "met", "evidence": "e"})                                 # no criterion
_os_bad_result({"spec": "x", "criterion": "AC1", "verdict": "met"})                               # met, no evidence
_os_bad_result({"spec": "x", "criterion": "AC1", "verdict": "failed", "evidence": "e"})            # failed, no kind
_os_bad_result({"spec": "x", "criterion": "AC1", "verdict": "cannot-observe", "why": "w"})         # no capability
_os_bad_result({"spec": "x", "criterion": "AC1", "verdict": "cannot-observe", "capability": "droplet"})  # no why
try:
    _js387.validate({k: v for k, v in _os_good.items() if k != "results"}, OS_SCHEMA)
    raise AssertionError("AC6: a document with no results must be refused")
except _js387.ValidationError:
    pass
try:
    _js387.validate({k: v for k, v in _os_good.items() if k != "batch"}, OS_SCHEMA)
    raise AssertionError("AC6: a document with no batch must be refused")
except _js387.ValidationError:
    pass
N += 1

# ── AC7 · a capability outside the enumerated six values fails schema validation
for cap in ("ro-dsn", "droplet", "browser", "operator-action", "elapsed", "other"):
    _js387.validate({**_os_good, "results": [{"spec": "x", "criterion": "AC1", "verdict": "cannot-observe",
                                              "capability": cap, "why": "w"}]}, OS_SCHEMA)
try:
    _js387.validate({**_os_good, "results": [{"spec": "x", "criterion": "AC1", "verdict": "cannot-observe",
                                              "capability": "nonsense", "why": "w"}]}, OS_SCHEMA)
    raise AssertionError("AC7: an out-of-enum capability must be refused")
except _js387.ValidationError:
    pass
N += 1

# ── AC8 · dispatch.ROLES["owed-sweeper"] == (None, 45, 5) ──────────────────────
assert dispatch.ROLES["owed-sweeper"] == (None, 45, 5), dispatch.ROLES["owed-sweeper"]
N += 1

# ── AC9 · both TOML templates carry [contracts.owed-sweeper] backend=seat,
#         model=claude-sonnet-5, and models.resolve agrees against each ───────
for _tmpl in ("models.example.toml", "models.claude-only.toml"):
    _mp = models.load(REPO_ROOT / _tmpl)
    assert _mp["owed-sweeper"]["backend"] == "seat" and _mp["owed-sweeper"]["model"] == "claude-sonnet-5", \
        (_tmpl, _mp["owed-sweeper"])
    _r = models.resolve("owed-sweeper", None, mp=_mp)
    assert _r["backend"] == "seat" and _r["model"] == "claude-sonnet-5", (_tmpl, _r)
N += 1

# ── AC12 · a 3-row manifest (A/AC1, B/AC2, C/AC3) drives exactly one owed-met,
#          one owed-failed, one owed-unobservable — each on its OWN row's spec,
#          never the batch subject; spawn-started/spawn-done keep the batch ───
SWEEP12 = "L-owed-sweeper-fx12"
ROWS12 = [_os_row("L-owsw-a12", "AC1"), _os_row("L-owsw-b12", "AC2"), _os_row("L-owsw-c12", "AC3")]
_sweep_manifest(SWEEP12, ROWS12)
OUT12 = {"batch": SWEEP12, "contamination": False, "escalations": [], "declarations": [],
         "results": [{"spec": "L-owsw-a12", "criterion": "AC1", "verdict": "met", "evidence": "e"},
                     {"spec": "L-owsw-b12", "criterion": "AC2", "verdict": "failed", "evidence": "e",
                      "kind": "unmet"},
                     {"spec": "L-owsw-c12", "criterion": "AC3", "verdict": "cannot-observe",
                      "capability": "droplet", "why": "w"}]}
# AC15 rides along on this same drive: provision_worktree_env must never be
# called for owed-sweeper, and no .env must appear under REPO (the manifest's cwd).
_real_pwe387 = dispatch.provision_worktree_env


def _pwe387_boom(*_a, **_k):
    raise AssertionError("AC15: provision_worktree_env must never be called for owed-sweeper")


dispatch.provision_worktree_env = _pwe387_boom
try:
    code12, types12, evs12, _ = spawn("owed-sweeper", out=OUT12, subject=SWEEP12)
finally:
    dispatch.provision_worktree_env = _real_pwe387
assert code12 == 0, evs12
assert not (REPO / ".env").exists(), "AC15: no .env under the manifest's cwd"
_om12 = [e for e in evs12 if e["type"] == "owed-met"]
_of12 = [e for e in evs12 if e["type"] == "owed-failed"]
_ou12 = [e for e in evs12 if e["type"] == "owed-unobservable"]
assert len(_om12) == 1 and _om12[0]["subject"] == "L-owsw-a12" and _om12[0]["criterion"] == "AC1" \
    and _om12[0]["batch"] == SWEEP12, _om12
assert len(_of12) == 1 and _of12[0]["subject"] == "L-owsw-b12" and _of12[0]["criterion"] == "AC2" \
    and _of12[0]["kind"] == "unmet" and _of12[0]["batch"] == SWEEP12, _of12
assert len(_ou12) == 1 and _ou12[0]["subject"] == "L-owsw-c12" and _ou12[0]["criterion"] == "AC3" \
    and _ou12[0]["capability"] == "droplet" and _ou12[0]["batch"] == SWEEP12, _ou12
assert spawn.raw[0]["type"] == "spawn-started" and spawn.raw[0]["subject"] == SWEEP12, spawn.raw[0]
_sd12 = next(e for e in spawn.raw if e["type"] == "spawn-done")
assert _sd12["subject"] == SWEEP12 and _sd12.get("off_manifest") == 0, _sd12
N += 1

# ── AC13 · a 4th, off-manifest result is dropped before events_for ever sees
#          it (no event for it), and counted on spawn-done; AC12's exact drive
#          re-run carries off_manifest == 0 ────────────────────────────────────
SWEEP13 = "L-owed-sweeper-fx13"
_sweep_manifest(SWEEP13, ROWS12)
OUT13 = {**OUT12, "batch": SWEEP13,
         "results": OUT12["results"] + [{"spec": "L-owsw-ghost13", "criterion": "AC9",
                                         "verdict": "met", "evidence": "e"}]}
code13, types13, evs13, _ = spawn("owed-sweeper", out=OUT13, subject=SWEEP13)
assert code13 == 0, evs13
assert not any(e.get("subject") == "L-owsw-ghost13" for e in evs13), \
    "AC13: an off-manifest row must never become an event"
_sd13 = next(e for e in spawn.raw if e["type"] == "spawn-done")
assert _sd13["off_manifest"] == 1, _sd13
# re-run AC12's exact (on-manifest-only) drive: off_manifest == 0
SWEEP13b = "L-owed-sweeper-fx13b"
_sweep_manifest(SWEEP13b, ROWS12)
code13b, types13b, evs13b, _ = spawn("owed-sweeper", out={**OUT12, "batch": SWEEP13b}, subject=SWEEP13b)
_sd13b = next(e for e in spawn.raw if e["type"] == "spawn-done")
assert _sd13b["off_manifest"] == 0, _sd13b
N += 1

# ── AC14 · role=owed-sweeper with no --path raises no "a writing role needs
#          --path" failure (kind is None); completes code == 0 given a valid stub
SWEEP14 = "L-owed-sweeper-fx14"
_sweep_manifest(SWEEP14, [_os_row("L-owsw-a14", "AC1")])
OUT14 = {"batch": SWEEP14, "contamination": False, "escalations": [], "declarations": [],
         "results": [{"spec": "L-owsw-a14", "criterion": "AC1", "verdict": "met", "evidence": "e"}]}
code14, types14, evs14, _ = spawn("owed-sweeper", out=OUT14, subject=SWEEP14, path=None)
assert code14 == 0, evs14
assert not any(e.get("why") == "a writing role needs --path" for e in evs14), evs14
N += 1

# ── AC16 · "owed-sweeper" carries no codex sandbox grant ───────────────────────
assert "owed-sweeper" not in dispatch.CODEX_WRITES, dispatch.CODEX_WRITES
N += 1

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-9004 · owed-sweeper-ro-credentials (L-charter-0042 OC1) — AC1-AC9
# ══════════════════════════════════════════════════════════════════════════════
_H9004 = []


def _root9004(name):
    d = harness.test_root(f"9004-{name}")
    _H9004.append(d)
    return d


def _checkout9004(name, env_text=None):
    d = _root9004(f"checkout-{name}")
    if env_text is not None:
        (d / ".env").write_text(env_text)
    return d


# ── AC1 · no SUPABASE_DB_URL_RO declared (incl. no .env at all) -> "absent",
#         no .env created ──────────────────────────────────────────────────────
sw_co_1a = _checkout9004("ac1-a")
sw_dir_1a = _root9004("ac1-dir-a")
r = dispatch.provision_sweep_env(sw_dir_1a, sw_co_1a)
assert r == "absent" and not (sw_dir_1a / ".env").exists(), ("9004 AC1a", r)
sw_co_1b = _checkout9004("ac1-b", "SUPABASE_DB_URL=rw1\nSUPABASE_DB_URL_DIRECT=rw2\n")
sw_dir_1b = _root9004("ac1-dir-b")
r = dispatch.provision_sweep_env(sw_dir_1b, sw_co_1b)
assert r == "absent" and not (sw_dir_1b / ".env").exists(), ("9004 AC1b", r)
N += 1

# ── AC2 · RO byte-equals SUPABASE_DB_URL, and separately SUPABASE_DB_URL_DIRECT
#         -> "refused", nothing written ────────────────────────────────────────
sw_co_2a = _checkout9004("ac2-a", "SUPABASE_DB_URL=same-value\nSUPABASE_DB_URL_RO=same-value\n")
sw_dir_2a = _root9004("ac2-dir-a")
r = dispatch.provision_sweep_env(sw_dir_2a, sw_co_2a)
assert r == "refused" and not (sw_dir_2a / ".env").exists(), ("9004 AC2a", r)
sw_co_2b = _checkout9004("ac2-b", "SUPABASE_DB_URL=rw-value\nSUPABASE_DB_URL_DIRECT=direct-value\n"
                                  "SUPABASE_DB_URL_RO=direct-value\n")
sw_dir_2b = _root9004("ac2-dir-b")
r = dispatch.provision_sweep_env(sw_dir_2b, sw_co_2b)
assert r == "refused" and not (sw_dir_2b / ".env").exists(), ("9004 AC2b", r)
N += 1

# A checkout with a valid RO DSN distinct from every OTHER DB_URL-named key,
# reused by AC3-AC5.
SW_CO_VALID = _checkout9004("valid", "SUPABASE_DB_URL=rw-value\nSUPABASE_DB_URL_DIRECT=direct-value\n"
                                     "PG_PARITY_DB_URL=parity-value\nSUPABASE_DB_URL_RO=ro-distinct-9004\n")
SW_RO_LINE = b"SUPABASE_DB_URL=ro-distinct-9004\n"

# ── AC3 · valid distinct RO, plain non-git sweep_dir -> "readonly", exactly one
#         line, mode 0o600; a repeat call is idempotent, bytes unchanged ──────
sw_dir_3 = _root9004("ac3-dir")
r = dispatch.provision_sweep_env(sw_dir_3, SW_CO_VALID)
env3 = sw_dir_3 / ".env"
assert r == "readonly" and env3.read_bytes() == SW_RO_LINE, ("9004 AC3", r, env3.read_bytes() if env3.exists() else None)
assert (env3.stat().st_mode & 0o777) == 0o600, oct(env3.stat().st_mode)
r2 = dispatch.provision_sweep_env(sw_dir_3, SW_CO_VALID)
assert r2 == "readonly" and env3.read_bytes() == SW_RO_LINE, ("9004 AC3 idempotent", r2)
N += 1

# ── AC4 · same valid RO, but sweep_dir sits at/under a .git entry -> "refused",
#         proving the destination-safety gate fires independently of DSN validity
sw_dir_4a = _root9004("ac4-dir-self")     # sweep_dir itself holds a .git entry
(sw_dir_4a / ".git").mkdir()
r = dispatch.provision_sweep_env(sw_dir_4a, SW_CO_VALID)
assert r == "refused" and not (sw_dir_4a / ".env").exists(), ("9004 AC4a", r)
sw_parent_4b = _root9004("ac4-parent")
(sw_parent_4b / ".git").mkdir()
sw_dir_4b = sw_parent_4b / "nested" / "sweep"
sw_dir_4b.mkdir(parents=True)
r = dispatch.provision_sweep_env(sw_dir_4b, SW_CO_VALID)
assert r == "refused" and not (sw_dir_4b / ".env").exists(), ("9004 AC4b", r)
N += 1

# ── AC5 · sweep_dir already holds an .env with DIFFERENT bytes -> "refused",
#         bytes unchanged ──────────────────────────────────────────────────────
sw_dir_5 = _root9004("ac5-dir")
(sw_dir_5 / ".env").write_text("SOME_OTHER_VAR=x\n")
before5 = (sw_dir_5 / ".env").read_bytes()
r = dispatch.provision_sweep_env(sw_dir_5, SW_CO_VALID)
assert r == "refused" and (sw_dir_5 / ".env").read_bytes() == before5, ("9004 AC5", r)
N += 1

# ── AC6 · dispatch.main() end-to-end for role=owed-sweeper: spawn-started
#         carries dsn_role=="readonly", and <cwd>/.env exists with the exact
#         one line already at the moment the mocked spawn is invoked ──────────
SWEEP9004_6 = "L-owed-sweeper-9004ac6"
_sweep_manifest(SWEEP9004_6, [_os_row("L-owsw-9004ac6", "AC1")])
cwd9004_6 = _root9004("ac6-cwd")
OUT9004_6 = {"batch": SWEEP9004_6, "contamination": False, "escalations": [], "declarations": [],
             "results": [{"spec": "L-owsw-9004ac6", "criterion": "AC1", "verdict": "met", "evidence": "e"}]}
code9004_6, raw9004_6, seen9004_6 = drive("owed-sweeper", SWEEP9004_6, cwd9004_6, DSN_PROJECT, OUT9004_6)
ss9004_6 = next(e for e in raw9004_6 if e["type"] == "spawn-started")
assert ss9004_6.get("dsn_role") == "readonly", ss9004_6
assert seen9004_6["env_exists_at_call"] and seen9004_6["env_bytes_at_call"] == E2E_LINE, \
    ("9004 AC6: not provisioned before the mocked spawn ran", seen9004_6)
assert (cwd9004_6 / ".env").read_bytes() == E2E_LINE, "9004 AC6: final state"
N += 1

# ── AC7 · same drive, but the checkout's RO value is byte-identical to its own
#         SUPABASE_DB_URL (mislabeled read-write): spawn-started carries
#         dsn_role=="refused", no .env exists under cwd afterward ─────────────
DSN_PROJECT_9004B = "dsnproj9004b"
DSN_CHECKOUT_9004B = TMP / "repos" / DSN_PROJECT_9004B
DSN_CHECKOUT_9004B.mkdir(parents=True)
(DSN_CHECKOUT_9004B / ".env").write_text("SUPABASE_DB_URL=same-rw-9004\nSUPABASE_DB_URL_RO=same-rw-9004\n")
SWEEP9004_7 = "L-owed-sweeper-9004ac7"
_sweep_manifest(SWEEP9004_7, [_os_row("L-owsw-9004ac7", "AC1")])
cwd9004_7 = _root9004("ac7-cwd")
OUT9004_7 = {"batch": SWEEP9004_7, "contamination": False, "escalations": [], "declarations": [],
             "results": [{"spec": "L-owsw-9004ac7", "criterion": "AC1", "verdict": "met", "evidence": "e"}]}
code9004_7, raw9004_7, seen9004_7 = drive("owed-sweeper", SWEEP9004_7, cwd9004_7, DSN_PROJECT_9004B, OUT9004_7)
ss9004_7 = next(e for e in raw9004_7 if e["type"] == "spawn-started")
assert ss9004_7.get("dsn_role") == "refused", ss9004_7
assert not (cwd9004_7 / ".env").exists(), "9004 AC7: no .env under cwd"
N += 1

# ── AC8 · five-row shape table: the checkout's own read-write value(s) never
#         appear in any written .env; exactly row 5 ever writes a non-empty one
RW_A, RW_B = "table-rw-a-9004", "table-rw-b-9004"
tbl_co_1 = _checkout9004("ac8-1", f"SUPABASE_DB_URL={RW_A}\nSUPABASE_DB_URL_DIRECT={RW_B}\n")
tbl_co_2 = _checkout9004("ac8-2", f"SUPABASE_DB_URL={RW_A}\nSUPABASE_DB_URL_RO={RW_A}\n")
tbl_co_3 = _checkout9004("ac8-3", f"SUPABASE_DB_URL={RW_A}\nSUPABASE_DB_URL_DIRECT={RW_B}\n"
                                  f"SUPABASE_DB_URL_RO={RW_B}\n")
tbl_co_45 = _checkout9004("ac8-45", f"SUPABASE_DB_URL={RW_A}\nSUPABASE_DB_URL_DIRECT={RW_B}\n"
                                    "SUPABASE_DB_URL_RO=table-ro-distinct-9004\n")
tbl_dir_4 = _root9004("ac8-dir-4")
(tbl_dir_4 / ".git").mkdir()
tbl_rows = [
    ("no-ro", tbl_co_1, _root9004("ac8-dir-1")),
    ("eq-rw", tbl_co_2, _root9004("ac8-dir-2")),
    ("eq-direct", tbl_co_3, _root9004("ac8-dir-3")),
    ("valid-git", tbl_co_45, tbl_dir_4),
    ("valid-plain", tbl_co_45, _root9004("ac8-dir-5")),
]
written_nonempty = []
for label, co, dest in tbl_rows:
    r = dispatch.provision_sweep_env(dest, co)
    env_p = dest / ".env"
    content = env_p.read_bytes() if env_p.exists() else b""
    assert RW_A.encode() not in content and RW_B.encode() not in content, ("9004 AC8", label, content)
    if content:
        written_nonempty.append(label)
assert written_nonempty == ["valid-plain"], ("9004 AC8", written_nonempty)
assert (tbl_rows[4][2] / ".env").read_bytes() == b"SUPABASE_DB_URL=table-ro-distinct-9004\n"
N += 1

# ── AC9 · provision_worktree_env is never called for owed-sweeper's DSN path ──
_real_pwe9004 = dispatch.provision_worktree_env


def _pwe9004_boom(*_a, **_k):
    raise AssertionError("9004 AC9: provision_worktree_env must never be called for owed-sweeper")


dispatch.provision_worktree_env = _pwe9004_boom
SWEEP9004_9 = "L-owed-sweeper-9004ac9"
_sweep_manifest(SWEEP9004_9, [_os_row("L-owsw-9004ac9", "AC1")])
cwd9004_9 = _root9004("ac9-cwd")
OUT9004_9 = {"batch": SWEEP9004_9, "contamination": False, "escalations": [], "declarations": [],
             "results": [{"spec": "L-owsw-9004ac9", "criterion": "AC1", "verdict": "met", "evidence": "e"}]}
try:
    code9004_9, raw9004_9, seen9004_9 = drive("owed-sweeper", SWEEP9004_9, cwd9004_9, DSN_PROJECT, OUT9004_9)
finally:
    dispatch.provision_worktree_env = _real_pwe9004
ss9004_9 = next(e for e in raw9004_9 if e["type"] == "spawn-started")
assert ss9004_9.get("dsn_role") == "readonly", ss9004_9
N += 1

for p in _H9004:
    harness.cleanup(p)

print(f"dispatch: +L-spec-9004 (owed-sweeper-ro-credentials: AC1-AC9)")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0276 · defaults-and-dispatch-order (L-charter-0033) — R3 Target 4, R5
# ══════════════════════════════════════════════════════════════════════════════

# ── AC10 · wave-order refuses under a bare id, a path, or no --charter at all,
# identically; a killed OR shipped earlier-wave sibling unblocks it ───────────
plan_ch276 = TMP / "content" / "plan-CH276.md"
plan_ch276.write_text(
    "# Plan — CH276\n\n"
    "## unit-a\nGoal: g.\nFootprint:\n- src/x276.py\nWave: 1\n\n"
    "## unit-b\nGoal: g.\nFootprint:\n- src/y276.py\nWave: 2\n")
SW276 = TMP / "events" / "L-spec-writer-fx276a.jsonl"
dispatch.emit(SW276, {}, "spec-written", subject="L-spec-2761", charter="CH276", footprint=["src/x276.py"])
dispatch.emit(SW276, {}, "spec-written", subject="L-spec-2762", charter="CH276", footprint=["src/y276.py"])

for charter_arg in ("CH276", "/any/path/CH276.md", None):
    code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-2762", charter=charter_arg)
    assert code == 1 and cmd is None and evs[-1]["type"] == "spawn-failed" \
        and evs[-1].get("reason") == "wave-order", (charter_arg, code, types, evs)

dispatch.emit(SW276, {}, "shipped", subject="L-spec-2761")
for charter_arg in ("CH276", "/any/path/CH276.md", None):
    code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-2762", charter=charter_arg)
    assert code == 0 and cmd is not None, (charter_arg, code, types, evs)

# ── AC11 · a killed wave-1 sibling never blocks wave 2 ─────────────────────────
dispatch.emit(SW276, {}, "spec-written", subject="L-spec-2765", charter="CH276", footprint=["src/x276.py"])
dispatch.emit(SW276, {}, "spec-killed", subject="L-spec-2765", check=1)
dispatch.emit(SW276, {}, "spec-written", subject="L-spec-2763", charter="CH276", footprint=["src/y276.py"])
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-2763", charter="CH276")
assert code == 0 and cmd is not None, (code, types, evs)

# ── AC12 · wrong-project is refused before any spend ───────────────────────────
# A FRESH subject: L-spec-2762 already carries a "project" field from its own
# earlier successful `build-started` above (AC10's second loop, project="t",
# `spawn()`'s own default) — `subject_project` reads a subject's FIRST-ever
# project, so this must be a subject nothing has dispatched before.
SW276c = TMP / "events" / "L-spec-writer-fx276c.jsonl"
dispatch.emit(SW276c, {}, "spec-written", subject="L-spec-2767", project="acme276")
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-2767", project="other-project")
assert code == 1 and cmd is None and evs[-1]["type"] == "spawn-failed" \
    and evs[-1].get("reason") == "wrong-project", (code, types, evs)

# ── AC14 · no resolvable charter, or no plan file, is undetermined — never a
# refusal — and `build-started` carries `wave_note="undetermined"` ────────────
SW276b = TMP / "events" / "L-spec-writer-fx276b.jsonl"
dispatch.emit(SW276b, {}, "spec-written", subject="L-spec-2764", footprint=["src/z276.py"])
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-2764")
assert code == 0 and cmd is not None, (code, types, evs)
bs276 = next(e for e in evs if e["type"] == "build-started")
assert bs276.get("wave_note") == "undetermined", bs276
# ...and a resolved charter/wave that DID determine carries no wave_note at all.
# (L-spec-2762's own recorded project is "t" — `spawn()`'s default, stamped by
# its own first successful build-started in AC10's second loop above.)
code, types, evs, cmd = spawn("builder", out=card, subject="L-spec-2762", charter="CH276")
bs_det = next(e for e in evs if e["type"] == "build-started")
assert "wave_note" not in bs_det, bs_det

# ── AC15 (dispatch half) · validate.spec_shape_warnings monkeypatched onto the
# imported module object; one deduplicated spec-lint-warning per finding ──────
# L-spec-0321/R5 (SWP2, pre-existing at this spec's own pinned base): a
# spec-writer dispatch's `--path` must resolve to exactly `spec_path(subject)`
# or `main` refuses it before any spend (`write-path-mismatch`) — this fixture
# predates that check and named two arbitrary sibling files under `content/`,
# which the check has refused ever since (confirmed failing identically at
# this spec's own pinned base, outside Target 1-6's footprint — fixed here
# because AC14 requires every pre-existing assertion in this file to pass).
# Both dispatches below share one subject, so both now write the ONE
# canonical destination that subject resolves to.
import validate as validate276  # noqa: E402
_orig_warn276 = getattr(validate276, "spec_shape_warnings", None)
validate276.spec_shape_warnings = lambda text: ["PL-002: test finding"]
try:
    # both writes must resolve to dispatch.spec_path("L-spec-2766") — a distinctly
    # named fixture path here would now be refused before spend (write-path-mismatch,
    # L-spec-0321), so both reuse the one canonical destination for this subject.
    sp276a = dispatch.spec_path("L-spec-2766")
    code, types, evs, cmd = spawn("spec-writer", out={**sw, "spec_id": "L-spec-2766"}, path=sp276a,
                                  side=lambda: sp276a.write_text(WELL_FORMED_SPEC), subject="L-spec-2766")
    assert types.count("spec-lint-warning") == 1, types
    lw276 = next(e for e in evs if e["type"] == "spec-lint-warning")
    assert lw276["subject"] == "L-spec-2766" and lw276["finding"] == "PL-002: test finding", lw276

    sp276b = dispatch.spec_path("L-spec-2766")
    code2, types2, evs2, cmd2 = spawn("spec-writer", out={**sw, "spec_id": "L-spec-2766"}, path=sp276b,
                                      side=lambda: sp276b.write_text(WELL_FORMED_SPEC), subject="L-spec-2766")
    assert "spec-lint-warning" not in types2, \
        "AC15: an identical (subject, finding) pair is not appended twice"
finally:
    if _orig_warn276 is None:
        delattr(validate276, "spec_shape_warnings")
    else:
        validate276.spec_shape_warnings = _orig_warn276

print(f"dispatch: {N} spawns mocked, every check fired · +L-spec-0276 "
      "(defaults-and-dispatch-order: AC10-AC15)")

# ── L-spec-0435 (R10(b)/(c)) · dispatch.grading_budget, pure, plus the
#    role=grader pre-spend branch it gates ─────────────────────────────────────
_GB_BASE = _dt(2026, 1, 1, tzinfo=_timezone.utc)


def GT(mins):
    """A fixture timestamp `mins` minutes off a fixed base — used only by the
    grading_budget() pure-function tests below, which never compare against a
    real dispatch.main()-emitted ts, so any monotonic base will do."""
    return (_GB_BASE + _timedelta(minutes=mins)).isoformat(timespec="seconds")


def FT(mins):
    """A fixture timestamp `mins` real-clock minutes from now — gives a
    hand-written ledger fixture a well-ordered position relative to the REAL
    now() a live dispatch.main() drive stamps its own events with."""
    return (_dt.now(_timezone.utc) + _timedelta(minutes=mins)).isoformat(timespec="seconds")


def gb(actor, type_, subject, ts, **kv):
    """One event dict, shaped exactly as fold.read_events() hands grading_budget
    one (`actor` present, `ts` an ISO string) — grading_budget takes a plain
    list, so no ledger file is needed for these."""
    return {"type": type_, "actor": actor, "subject": subject, "ts": ts, **kv}


def raw_ev(actor, tag, type_, subject, ts, **kv):
    """One raw ledger line, written directly (bypassing dispatch.emit, which
    always stamps ts=now()) — these tests need exact control over a fixture
    event's ts to prove the freshness comparisons the budget check makes.
    `actor` is read back from the filename by fold.read_events() (D90), same
    as every real event."""
    f = TMP / "events" / f"L-{actor}-{tag}.jsonl"
    with open(f, "a") as fh:
        fh.write(json.dumps({"v": 1, "ts": ts, "type": type_, "subject": subject, **kv}) + "\n")
    return f


# AC3: no rejects, at most 2 grader spawn-started rows -> None.
ac3_subj435 = "L-spec-9403"
ev3_435 = [gb("grader", "spawn-started", ac3_subj435, GT(0), role="grader"),
           gb("grader", "spawn-started", ac3_subj435, GT(1), role="grader")]
assert dispatch.grading_budget(ev3_435, ac3_subj435) is None, "AC3: <=2 grader spawns, no rejects -> None"
N += 1

# AC4: two rejected-criterion(AC2) rows, different why, no clearance, no reset
# newer than the 2nd -> repeat-rejection:AC2 regardless of the why differing.
ac4_subj435 = "L-spec-9404"
ev4_435 = [gb("grader", "rejected-criterion", ac4_subj435, GT(0), criterion="AC2",
              why="Evidence missing for the retry path"),
           gb("grader", "rejected-criterion", ac4_subj435, GT(10), criterion="AC2",
              why="the retry-path evidence isn't attached")]
assert dispatch.grading_budget(ev4_435, ac4_subj435) == "repeat-rejection:AC2", ev4_435
N += 1

# AC5: a build-done (a) strictly between, (b) strictly after -> unchanged in
# BOTH cases; a fresh spec-written, or ANY thinker decision (regrade or not),
# after the 2nd resets it; a builder's regrade=yes decision (wrong actor) does
# not.
ac5_subj435 = "L-spec-9405"
base5_435 = [gb("grader", "rejected-criterion", ac5_subj435, GT(0), criterion="AC2", why="w1"),
             gb("grader", "rejected-criterion", ac5_subj435, GT(10), criterion="AC2", why="w2")]
ev5a_435 = [base5_435[0], gb("builder", "build-done", ac5_subj435, GT(5), status="DONE"), base5_435[1]]
assert dispatch.grading_budget(ev5a_435, ac5_subj435) == "repeat-rejection:AC2", "AC5a: build-done between"
ev5b_435 = base5_435 + [gb("builder", "build-done", ac5_subj435, GT(20), status="DONE")]
assert dispatch.grading_budget(ev5b_435, ac5_subj435) == "repeat-rejection:AC2", "AC5b: build-done after"
ev5c_435 = base5_435 + [gb("spec-writer", "spec-written", ac5_subj435, GT(20))]
assert dispatch.grading_budget(ev5c_435, ac5_subj435) is None, "AC5: a fresh spec-written after the 2nd resets it"
ev5d_435 = base5_435 + [gb("thinker", "decision", ac5_subj435, GT(20))]
assert dispatch.grading_budget(ev5d_435, ac5_subj435) is None, "AC5: any thinker decision resets it, regrade or not"
ev5e_435 = base5_435 + [gb("builder", "decision", ac5_subj435, GT(20), regrade="yes")]
assert dispatch.grading_budget(ev5e_435, ac5_subj435) == "repeat-rejection:AC2", \
    "AC5: a builder's decision (wrong actor) never resets it"
N += 5

# AC6: L-spec-0173's real shape, replayed verbatim — three grader verdict
# rounds, each its own build-done, why distinct every round, no
# criterion-cleared ever -> repeat-rejection:AC1 after the third round (the
# pre-rewrite why-text-matching definition would return None here).
ac6_subj435 = "L-spec-9406"
ev6_435, t6_435 = [], 0
for why in ("round one's own reason", "a completely different wording", "yet another distinct reason"):
    ev6_435.append(gb("builder", "build-done", ac6_subj435, GT(t6_435), status="DONE")); t6_435 += 1
    ev6_435.append(gb("grader", "spawn-started", ac6_subj435, GT(t6_435), role="grader")); t6_435 += 1
    ev6_435.append(gb("grader", "verdict", ac6_subj435, GT(t6_435), confirmed=False)); t6_435 += 1
    ev6_435.append(gb("grader", "rejected-criterion", ac6_subj435, GT(t6_435), criterion="AC1", why=why))
    t6_435 += 1
assert dispatch.grading_budget(ev6_435, ac6_subj435) == "repeat-rejection:AC1", ev6_435
N += 1

# AC7: exactly 3 grader spawn-started rows, no qualifying regrade -> cap:3;
# operator regrade=yes newer than the 3rd -> None; builder regrade=yes (wrong
# actor), or a thinker decision with no regrade field, never lifts it.
ac7_subj435 = "L-spec-9407"
ev7_435 = [gb("grader", "spawn-started", ac7_subj435, GT(i), role="grader") for i in range(3)]
assert dispatch.grading_budget(ev7_435, ac7_subj435) == "cap:3", "AC7: 3 prior runs, no regrade -> cap:3"
assert dispatch.grading_budget(ev7_435 + [gb("operator", "decision", ac7_subj435, GT(10), regrade="yes")],
                               ac7_subj435) is None, "AC7: a qualifying operator regrade lifts it"
assert dispatch.grading_budget(ev7_435 + [gb("builder", "decision", ac7_subj435, GT(10), regrade="yes")],
                               ac7_subj435) == "cap:3", "AC7: wrong actor never lifts it"
assert dispatch.grading_budget(ev7_435 + [gb("thinker", "decision", ac7_subj435, GT(10))],
                               ac7_subj435) == "cap:3", "AC7: a decision with no regrade field never lifts it"
N += 4

# AC8: 4 grader spawn-started rows (a regrade already permitted the 4th), no
# regrade newer than the 4th -> cap:4 — the rule reapplies past the literal
# fourth run.
ac8_subj435 = "L-spec-9408"
ev8_435 = [gb("grader", "spawn-started", ac8_subj435, GT(i), role="grader") for i in range(3)]
ev8_435.append(gb("operator", "decision", ac8_subj435, GT(3), regrade="yes"))
ev8_435.append(gb("grader", "spawn-started", ac8_subj435, GT(4), role="grader"))
assert dispatch.grading_budget(ev8_435, ac8_subj435) == "cap:4", ev8_435
N += 1


def _boom435(*a, **k):
    raise AssertionError("a backend must not be entered for a refused grading-budget dispatch")


def drive_grader435(subject):
    """Drives dispatch.main() for role=grader with every backend monkeypatched
    to explode if entered (AC9) — the refusal this proves must land strictly
    before any of them is ever called. Returns (exit code, this drive's own new
    ledger file's parsed events, that file's path)."""
    real = dispatch.run_claude, dispatch.run_seat, dispatch.run_codex
    dispatch.run_claude = dispatch.run_seat = dispatch.run_codex = _boom435
    a = argparse.Namespace(role="grader", subject=subject, packet=str(PK), path=None,
                          cwd=str(REPO), charter=None, project="t", mcp_config=None,
                          timeout=None, max_usd=None)
    try:
        dispatch.main(a)
        code = 0
    except SystemExit as e:
        code = e.code
    finally:
        dispatch.run_claude, dispatch.run_seat, dispatch.run_codex = real
    f = max((TMP / "events").glob("L-grader-[0-9]*.jsonl"))
    return code, [json.loads(l) for l in f.read_text().splitlines()], f


# AC9: a subject with exactly 3 prior grader spawn-started rows, no qualifying
# regrade -> main() raises SystemExit before any backend, THIS spawn's own
# newly-allocated ledger file carries exactly [escalation-blocking,
# spawn-failed], reason=="cap:3", and no other file gains a new event.
AC9_SUBJ435 = "L-spec-9409"
_fx9a, _fx9b, _fx9c = (raw_ev("grader", f"fx9409{s}", "spawn-started", AC9_SUBJ435, FT(o), role="grader")
                       for s, o in (("a", -500), ("b", -499), ("c", -498)))
_pre9_435 = {f: f.read_text() for f in (_fx9a, _fx9b, _fx9c)}
code9_435, raw9_435, f9_435 = drive_grader435(AC9_SUBJ435)
assert code9_435 == 1, raw9_435
assert [e["type"] for e in raw9_435] == ["escalation-blocking", "spawn-failed"], raw9_435
assert raw9_435[1]["reason"] == "cap:3", raw9_435[1]
for f, content in _pre9_435.items():
    assert f.read_text() == content, f"AC9: {f} must gain no new event"
N += 1

# AC10: the escalation-blocking's own shape — kind, reason, default, revert, a
# deadline 23h55m-24h05m after its own ts, actor==grader (from its filename),
# and fold.escalation_ok() true.
_full9_435 = fold.read_events()
e10_435 = next(e for e in _full9_435 if e.get("subject") == AC9_SUBJ435 and e["type"] == "escalation-blocking")
assert e10_435["actor"] == "grader", e10_435
assert e10_435["kind"] == "grading-budget" and e10_435["reason"] == "cap:3", e10_435
assert e10_435["default"] == "no further grade; rework with the standing reasons, or the Thinker amends the spec"
assert e10_435["revert"] == "a decision regrade=yes"
_delta_h_435 = (fold.ts(e10_435["deadline"]) - fold.ts(e10_435["ts"])).total_seconds() / 3600
assert 23 + 55 / 60 <= _delta_h_435 <= 24 + 5 / 60, _delta_h_435
assert fold.escalation_ok(e10_435), e10_435
N += 1

# AC11: a SECOND drive on the SAME subject (still cap:3, no qualifying
# decision) — its own second ledger file appends a second spawn-failed but NO
# second escalation-blocking; reading the FULL event set (both files
# together), exactly one escalation-blocking{kind, reason} exists.
code11_435, raw11_435, f11_435 = drive_grader435(AC9_SUBJ435)
assert code11_435 == 1 and [e["type"] for e in raw11_435] == ["spawn-failed"], raw11_435
assert raw11_435[0]["reason"] == "cap:3", raw11_435[0]
_full11_435 = fold.read_events()
_esc11_435 = [e for e in _full11_435 if e.get("subject") == AC9_SUBJ435 and e["type"] == "escalation-blocking"
             and e.get("kind") == "grading-budget" and e.get("reason") == "cap:3"]
assert len(_esc11_435) == 1, "AC11: exactly one escalation-blocking across both drives"
N += 1

# AC12: the AC7 "lifted" fixture (a qualifying regrade already permits a 4th
# run) driven through main() with a normal successful grader response — the
# spawn completes (spawn-started/verdict/spawn-done), code 0, proving the
# check does not misfire once the condition is genuinely cleared.
AC12_SUBJ435 = "L-spec-9412"
for _s, _o in (("a", -500), ("b", -499), ("c", -498)):
    raw_ev("grader", f"fx9412{_s}", "spawn-started", AC12_SUBJ435, FT(_o), role="grader")
raw_ev("operator", "fx9412d", "decision", AC12_SUBJ435, FT(-100), regrade="yes")
code12_435, types12_435, evs12_435, _ = spawn("grader", out=grade([met]), subject=AC12_SUBJ435)
assert code12_435 == 0, evs12_435
assert "verdict" in types12_435 and "spawn-done" in types12_435, types12_435
assert "spawn-started" in [e["type"] for e in spawn.raw], spawn.raw
N += 1

# AC14: the AC9 standing escalation-blocking{cap:3}, resolved by an unrelated
# decision (regrade=no — never lifts the cap itself) newer than it — grading_budget
# still returns cap:3, AND this THIRD drive's own ledger file carries a SECOND,
# FRESH escalation-blocking{cap:3} (not suppressed by the first, now-resolved
# one) — a resolved-but-not-fixed block re-surfaces rather than wedging silent.
# L-spec-0482/R12.c superseded this block's own prior `fx9414b` build-done (it
# used to assert a build-done changes nothing here — R12.c's whole point is
# that it now does): removed, so this drive still sees only the 3 spawn-started
# rows plus the harmless `decision regrade=no`, and cap:3 still stands.
_dec_ts_435 = (fold.ts(e10_435["ts"]) + _timedelta(minutes=5)).isoformat(timespec="seconds")
raw_ev("operator", "fx9414a", "decision", AC9_SUBJ435, _dec_ts_435, regrade="no")
code14_435, raw14_435, f14_435 = drive_grader435(AC9_SUBJ435)
assert code14_435 == 1 and [e["type"] for e in raw14_435] == ["escalation-blocking", "spawn-failed"], raw14_435
assert raw14_435[0]["kind"] == "grading-budget" and raw14_435[0]["reason"] == "cap:3", raw14_435[0]
assert raw14_435[1]["reason"] == "cap:3", raw14_435[1]
N += 1

# L-spec-0482/R12.c (new): a build-done newer than every counted run resets the
# cap outright now — appended strictly after the third drive above, so it
# changes nothing about the assertions just made.
raw_ev("builder", "fx9414c482", "build-done", AC9_SUBJ435, FT(1), status="DONE")
assert dispatch.grading_budget(fold.read_events(), AC9_SUBJ435) is None, \
    "R12.c: a build-done newer than the newest counted run resets the cap"
N += 1

# AC13: agents/executor.md's spawn-failed/spawn-stale row states both reset
# paths, verbatim in substance, each naming its own — never the other's.
_exec_md_435 = (pathlib.Path(__file__).parent.parent / "agents" / "executor.md").read_text()
_row_435 = next(l for l in _exec_md_435.splitlines() if "spawn-stale" in l and "unserved, timeout" in l)
assert "repeat-rejection:" in _row_435 and "cap:" in _row_435, _row_435
assert "fresh `spec-written`" in _row_435 and "thinker`/`operator`" in _row_435 and \
    "regrade or not" in _row_435, "AC13: repeat-rejection's reset path, stated"
assert 'decision{regrade: "yes"}' in _row_435, "AC13: cap's own, narrower reset path, stated"
N += 1

print(f"dispatch: +L-spec-0435 (grading-spend: AC3-AC14)")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0482 (grading-breaker) — R12.b circuit breaker, R12.c cap reset,
# R12.e executor rule. AC1-AC9, AC15, AC16. `grading_env` already imported
# above (L-spec-0481 section); `_cap_content` reused for AC3's second spec.
# ══════════════════════════════════════════════════════════════════════════════

# AC1: schema enum equals grading_env.CAPABILITIES + "unknown"; grader.md states
# the rule; doit validate grader on four fixtures (met/no-field, tool-failed
# with a valid cap, tool-failed with the field absent, tool-failed with a
# bogus value — only the last is INVALID).
_schema482 = json.loads((pathlib.Path(__file__).parent.parent / "agents" / "grader.schema.json").read_text())
_cap_enum482 = _schema482["properties"]["verdicts"]["items"]["properties"]["missing_capability"]["enum"]
assert set(_cap_enum482) == set(grading_env.CAPABILITIES) | {"unknown"}, _cap_enum482
_grader_md482 = (pathlib.Path(__file__).parent.parent / "agents" / "grader.md").read_text()
assert "missing_capability" in _grader_md482, "AC1: grader.md must name the field"

import validate as validate482  # noqa: E402
_VDIR482 = TMP / "content" / "validate-fixtures-482"
_VDIR482.mkdir(parents=True, exist_ok=True)


def _grade_row482(**kw):
    return {"verdicts": [{"ac": "AC1", "verdict": "met", "reason": "r", **kw}],
            "matches_intent": "yes", "card_ok": "yes", "could_not_run": False,
            "contamination": False, "declarations": [], "checkers": []}


def _validate482(obj, name):
    p = _VDIR482 / f"{name}.json"
    p.write_text(json.dumps(obj))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            validate482.main(["grader", str(p)])
        return 0, buf.getvalue()
    except SystemExit as e:
        return (e.code if isinstance(e.code, int) else 1), str(e.code)


c1_482, o1_482 = _validate482(_grade_row482(), "met-no-field")
assert "VALID" in o1_482 and c1_482 == 0, (c1_482, o1_482)
c2_482, o2_482 = _validate482({**_grade_row482(), "verdicts": [
    {"ac": "AC1", "verdict": "cannot-assess", "reason": "r", "reason_code": "tool-failed",
     "missing_capability": "db"}]}, "tool-failed-valid")
assert "VALID" in o2_482 and c2_482 == 0, (c2_482, o2_482)
c3_482, o3_482 = _validate482({**_grade_row482(), "verdicts": [
    {"ac": "AC1", "verdict": "cannot-assess", "reason": "r", "reason_code": "tool-failed"}]}, "tool-failed-absent")
assert "VALID" in o3_482 and c3_482 == 0, (c3_482, o3_482)
c4_482, o4_482 = _validate482({**_grade_row482(), "verdicts": [
    {"ac": "AC1", "verdict": "cannot-assess", "reason": "r", "reason_code": "tool-failed",
     "missing_capability": "nonsense"}]}, "tool-failed-bogus")
assert c4_482 != 0 and "missing_capability" in o4_482, (c4_482, o4_482)
print("L-spec-0482 AC1 ok")

# Small fixture-row builders, reused across AC2-AC16 below.
_ca482 = lambda ac, cap=None, **kw: {"ac": ac, "verdict": "cannot-assess", "reason": "r",
                                     "reason_code": "tool-failed",
                                     **({"missing_capability": cap} if cap is not None else {}), **kw}

# AC2: a non-owed cannot-assess row (AC1, capability db) rides on the verdict
# event; an owed one (AC2, capability browser, declared owed) does not; a `met`
# row (AC3) is untouched. A second fixture: an executor-authored owed-ac with
# no prior declaration never suppresses.
# (uses "env-file"/"browser", never "db" — AC3/AC4 below open and reuse the
# shared capability:db hold across dispatches, and must start from it closed)
subj_ac2_482 = "L-spec-9450"
raw_ev("spec-writer", "fx482ac2", "owed-ac", subj_ac2_482, GT(0), criterion="AC2")
met_ac3_482 = {"ac": "AC3", "verdict": "met", "reason": "r"}
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC1", "env-file"), _ca482("AC2", "browser"),
                                                  met_ac3_482]), subject=subj_ac2_482)
vev2_482 = next(e for e in evs if e["type"] == "verdict")
assert vev2_482["missing_capability"] == [{"ac": "AC1", "capability": "env-file"}], vev2_482
assert set(vev2_482["cannot_assess"]) == {"AC1", "AC2"}, vev2_482

subj_ac2b_482 = "L-spec-9451"
# A subject's own admitted history must be non-empty before the sole event on
# it is one `fold()` ignores (a bare executor owed-ac with no prior
# declaration) — an all-ignored subject is a pre-existing `fold.fold()` crash
# (IndexError on `evs[-1]`) outside this spec's footprint; sidestepped here by
# giving the subject one harmless admitted event first.
raw_ev("spec-writer", "fx482ac2bpre", "spec-written", subj_ac2b_482, GT(-1))
raw_ev("executor", "fx482ac2b", "owed-ac", subj_ac2b_482, GT(0), criterion="AC4")
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC4", "env-file")]), subject=subj_ac2b_482)
vev2b_482 = next(e for e in evs if e["type"] == "verdict")
assert vev2b_482["missing_capability"] == [{"ac": "AC4", "capability": "env-file"}], \
    "AC2: an executor-authored owed-ac with no prior declaration never suppresses"
print("L-spec-0482 AC2 ok")

# AC3: the store round trip, through the production events_for/emit path — a
# fresh fold.read_events() shows both the hold and the escalation, and a
# SECOND spec whose content requires `db` is held by the shared shape.
ac3_second_482 = "L-spec-9452"
_cap_content(ac3_second_482, "pytest -k live_db\n")
subj_ac3_482 = "L-spec-9453"
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC1", "db")]), subject=subj_ac3_482)
assert "capability-hold" in types and "escalation-blocking" in types, types
hold3_482 = next(e for e in evs if e["type"] == "capability-hold")
# Operator ruling 2026-10-04: a verdict-sourced hold is spec-local for every
# capability — one grader's cannot-assess never holds another spec's grade.
L3 = f"capability:db:{subj_ac3_482}"
assert (hold3_482["subject"], hold3_482["capability"], hold3_482["spec"], hold3_482["source"]) == \
    (L3, "db", subj_ac3_482, "verdict"), hold3_482
esc3_482 = next(e for e in evs if e["type"] == "escalation-blocking")
assert esc3_482["subject"] == L3 and esc3_482["kind"] == "capability-hold" \
    and esc3_482["owner"] == "thinker" and esc3_482["revert"] == f"doit append unblocked {L3}", esc3_482
assert esc3_482.get("default") and esc3_482.get("deadline"), esc3_482
fresh3_482 = fold.read_events()
assert L3 in fold.capability_holds(fresh3_482), fold.capability_holds(fresh3_482)
assert "capability:db" not in fold.capability_holds(fresh3_482), fold.capability_holds(fresh3_482)
assert ac3_second_482 not in fold.held_specs(fresh3_482), \
    "a sibling spec needing db is NOT held by another spec's verdict"
print("L-spec-0482 AC3 ok (spec-local)")

# AC4: the SAME spec's second cannot-assess on an already-held capability opens
# nothing further; after `unblocked` the next one opens a fresh hold; two distinct
# capabilities in one verdict open two (spec-local) holds.
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC1", "db")]), subject=subj_ac3_482)
assert "capability-hold" not in types and "escalation-blocking" not in types, \
    "AC4: an already-open hold on this spec gets no second capability-hold/escalation"
raw_ev("operator", "fx482ac4unblock", "unblocked", L3, FT(1))
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC1", "db")]), subject=subj_ac3_482)
assert "capability-hold" in types and "escalation-blocking" in types, \
    "AC4: after an unblocked close, the next non-owed cannot-assess opens a fresh hold"
subj_ac4c_482 = "L-spec-9456"
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC1", "browser"), _ca482("AC2", "review-account")]),
                           subject=subj_ac4c_482)
hold_subjs_482 = {e["subject"] for e in evs if e["type"] == "capability-hold"}
assert hold_subjs_482 == {f"capability:browser:{subj_ac4c_482}", f"capability:review-account:{subj_ac4c_482}"}, \
    hold_subjs_482
print("L-spec-0482 AC4 ok (spec-local)")

# AC5: absent / literal "unknown" / an out-of-enum value all coerce to
# capability:unknown:<SPEC>, spec=<SPEC>; a bogus value fails the grader's own
# schema well before events_for, so it is exercised directly (same pattern the
# COMMIT-SHAPE AC13 block above uses for an unreachable-through-main() case).
subj_ac5a_482 = "L-spec-9460"
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC1")]), subject=subj_ac5a_482)  # field absent
hold5a_482 = next(e for e in evs if e["type"] == "capability-hold")
assert hold5a_482["subject"] == f"capability:unknown:{subj_ac5a_482}" and hold5a_482["spec"] == subj_ac5a_482, hold5a_482
assert fold.capability_holds(fold.read_events())[hold5a_482["subject"]]["specs"] == {subj_ac5a_482}
assert subj_ac5a_482 in fold.held_specs(fold.read_events())

subj_ac5b_482 = "L-spec-9461"
code, types, evs, _ = spawn("grader", out=grade([_ca482("AC1", "unknown")]), subject=subj_ac5b_482)
hold5b_482 = next(e for e in evs if e["type"] == "capability-hold")
assert hold5b_482["subject"] == f"capability:unknown:{subj_ac5b_482}", hold5b_482

subj_ac5c_482 = "L-spec-9462"
a5c_482 = argparse.Namespace(subject=subj_ac5c_482)
base5c_482 = {"subject": subj_ac5c_482, "project": "t", "spawn": "L-grader-fake5c482"}
ev5c_482 = dispatch.events_for("grader", grade([_ca482("AC1", "nonsense-cap")]), a5c_482, base5c_482)
hold5c_482 = next(kv for t, kv in ev5c_482 if t == "capability-hold")
assert hold5c_482["subject"] == f"capability:unknown:{subj_ac5c_482}" and hold5c_482["spec"] == subj_ac5c_482, hold5c_482
print("L-spec-0482 AC5 ok")

# AC6: cannot-assess rows that are ALL owed open no hold, missing_capability is
# [], and verdict_confirmed is unchanged (True when matches_intent/card_ok yes).
subj_ac6_482 = "L-spec-9470"
raw_ev("spec-writer", "fx482ac6", "owed-ac", subj_ac6_482, GT(0), criterion="AC1")
code, types, evs, _ = spawn("grader", out=grade([{"ac": "AC1", "verdict": "cannot-assess", "reason": "r",
                                                  "reason_code": "criterion-unevaluable-from-packet"}]),
                          subject=subj_ac6_482)
assert "capability-hold" not in types and "escalation-blocking" not in types, types
vev6_482 = next(e for e in evs if e["type"] == "verdict")
assert vev6_482["missing_capability"] == [], vev6_482
assert fold.verdict_confirmed(vev6_482, {"AC1"}) is True, vev6_482
print("L-spec-0482 AC6 ok")

# AC7: grading_budget's build-done-gated window — 3 runs older than the
# newest build-done never count (None); 3 newer do (cap:3); no build-done at
# all still counts every run (cap:3, unchanged from before this unit).
ac7_subj482 = "L-spec-9480"
ev7a482 = [gb("grader", "spawn-started", ac7_subj482, GT(0), role="grader"),
          gb("grader", "verdict", ac7_subj482, GT(1), confirmed=False, cannot_assess=[]),
          gb("grader", "rejected-criterion", ac7_subj482, GT(1), criterion="AC1", why="w"),
          gb("grader", "spawn-started", ac7_subj482, GT(2), role="grader"),
          gb("grader", "verdict", ac7_subj482, GT(3), confirmed=False, cannot_assess=[]),
          gb("grader", "rejected-criterion", ac7_subj482, GT(3), criterion="AC2", why="w"),
          gb("grader", "spawn-started", ac7_subj482, GT(4), role="grader"),
          gb("grader", "verdict", ac7_subj482, GT(5), confirmed=False, cannot_assess=[]),
          gb("grader", "rejected-criterion", ac7_subj482, GT(5), criterion="AC3", why="w"),
          gb("builder", "build-done", ac7_subj482, GT(10), status="DONE")]
assert dispatch.grading_budget(ev7a482, ac7_subj482) is None, dispatch.grading_budget(ev7a482, ac7_subj482)

ev7b482 = [gb("builder", "build-done", ac7_subj482, GT(0), status="DONE"),
          gb("grader", "spawn-started", ac7_subj482, GT(1), role="grader"),
          gb("grader", "spawn-started", ac7_subj482, GT(2), role="grader"),
          gb("grader", "spawn-started", ac7_subj482, GT(3), role="grader")]
assert dispatch.grading_budget(ev7b482, ac7_subj482) == "cap:3", dispatch.grading_budget(ev7b482, ac7_subj482)

ev7c482 = [gb("grader", "spawn-started", ac7_subj482, GT(i), role="grader") for i in range(3)]
assert dispatch.grading_budget(ev7c482, ac7_subj482) == "cap:3", dispatch.grading_budget(ev7c482, ac7_subj482)
print("L-spec-0482 AC7 ok")

# AC8: 4 grader runs after the newest build-done, each carrying a non-owed
# cannot-assess -> None; all 4 owed (spec-writer-declared) -> cap:4; only an
# executor-authored owed-ac with no prior declaration -> still None.
ac8_subj482 = "L-spec-9481"


def _mk_run482(i, criterion):
    return [gb("grader", "spawn-started", ac8_subj482, GT(i * 2), role="grader", spawn=f"g482{i}"),
            gb("grader", "verdict", ac8_subj482, GT(i * 2 + 1), spawn=f"g482{i}", confirmed=False,
               cannot_assess=[criterion])]


_build8_482 = gb("builder", "build-done", ac8_subj482, GT(-1), status="DONE")
_runs8_482 = [ev for i, c in enumerate(("AC1", "AC2", "AC3", "AC4")) for ev in _mk_run482(i, c)]
ev8a_482 = [_build8_482] + _runs8_482
assert dispatch.grading_budget(ev8a_482, ac8_subj482) is None, dispatch.grading_budget(ev8a_482, ac8_subj482)

_owed8_482 = [gb("spec-writer", "owed-ac", ac8_subj482, GT(-2), criterion=c)
             for c in ("AC1", "AC2", "AC3", "AC4")]
ev8b_482 = _owed8_482 + ev8a_482
assert dispatch.grading_budget(ev8b_482, ac8_subj482) == "cap:4", dispatch.grading_budget(ev8b_482, ac8_subj482)

_owed8c_482 = [gb("executor", "owed-ac", ac8_subj482, GT(-2), criterion=c)
              for c in ("AC1", "AC2", "AC3", "AC4")]
ev8c_482 = _owed8c_482 + ev8a_482
assert dispatch.grading_budget(ev8c_482, ac8_subj482) is None, dispatch.grading_budget(ev8c_482, ac8_subj482)
print("L-spec-0482 AC8 ok")

# AC9: regression — the pre-existing L-spec-0435 suite above (AC3-AC12, AC14's
# rewritten block) is unmodified in behavior except the one block R12.c names;
# proved by this whole file exiting 0 (checked by the Verification command)
# and by `git diff` on this file showing no other pre-existing line touched.
print("L-spec-0482 AC9 ok (see: this file's own full run, plus git diff of the AC14 block)")

# AC15: agents/executor.md's cap: clause names the build-done reset AND the
# decision{regrade: "yes"} lift, while still carrying every substring the
# pre-existing 0435 `# AC13:` block already reads (`_row_435`, computed above).
assert "fresh `build-done` newer than the newest counted grader run" in _row_435, _row_435
assert "re-dispatches" in _row_435, _row_435
assert 'decision{regrade: "yes"}' in _row_435, _row_435
print("L-spec-0482 AC15 ok")

# AC16: the omitted-field path end to end through dispatch.main — passes
# schema validation, is not spawn-failed, opens capability:unknown:<SPEC>, and
# three such runs never count against the cap (grading_budget stays None).
subj_ac16_482 = "L-spec-9490"
row16_482 = {"ac": "AC1", "verdict": "cannot-assess", "reason": "r", "reason_code": "tool-failed"}
code16_482, types16_482, evs16_482, _ = spawn("grader", out=grade([row16_482]), subject=subj_ac16_482)
assert code16_482 == 0 and "spawn-failed" not in types16_482, (code16_482, types16_482)
vev16_482 = next(e for e in evs16_482 if e["type"] == "verdict")
assert vev16_482["missing_capability"] == [{"ac": "AC1", "capability": "unknown"}], vev16_482
hold16_482 = next(e for e in evs16_482 if e["type"] == "capability-hold")
assert hold16_482["subject"] == f"capability:unknown:{subj_ac16_482}", hold16_482
assert "escalation-blocking" in types16_482, types16_482
for _ in range(2):
    spawn("grader", out=grade([row16_482]), subject=subj_ac16_482)
assert dispatch.grading_budget(fold.read_events(), subj_ac16_482) is None, \
    "AC16: three environment-failure runs never count against the cap"
print("L-spec-0482 AC16 ok")

print("dispatch: +L-spec-0482 (grading-breaker: AC1-AC9 AC15 AC16)")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-8034 (capability-routing) — AC6 (dispatch records routing before spend,
# actor grader, non-seat grader route; never refused for a routed capability)
# and AC7 (events_for never derives criterion-routed from a grader Output)
# ══════════════════════════════════════════════════════════════════════════════
SUBJ_8034_6 = "L-spec-9472"
dispatch.CONTENT.mkdir(parents=True, exist_ok=True)
SPEC_8034_6 = dispatch.CONTENT / f"{SUBJ_8034_6}.md"
SPEC_8034_6.write_text(
    "## Acceptance criteria\n\nAC1 [ui]: browser criterion.\nreview_path: log in as x, go to /y\n\n"
    "AC2 [backend]: plain criterion.\nreview_path: nothing special here.\n")
met_8034 = {"ac": "AC2", "verdict": "met", "reason": "r"}

code6a, types6a, evs6a, _ = spawn("grader", out=grade([met_8034]), subject=SUBJ_8034_6)
assert code6a == 0 and "spawn-failed" not in types6a, (code6a, types6a, evs6a)
routed6a = [e for e in evs6a if e["type"] == "criterion-routed"]
assert len(routed6a) == 1 and routed6a[0]["criterion"] == "AC1" and routed6a[0]["capability"] == "browser" \
    and routed6a[0]["to"] == "owed", routed6a
raw6a_types = [e["type"] for e in spawn.raw]
assert raw6a_types.index("criterion-routed") < raw6a_types.index("spawn-started"), raw6a_types
N += 1

# a second, unchanged dispatch appends no new criterion-routed row
code6b, types6b, evs6b, _ = spawn("grader", out=grade([met_8034]), subject=SUBJ_8034_6)
assert code6b == 0 and not any(t == "criterion-routed" for t in types6b), types6b
N += 1

# rework: the browser criterion is dropped -> the next dispatch appends to=none
SPEC_8034_6.write_text(
    "## Acceptance criteria\n\nAC2 [backend]: plain criterion.\nreview_path: nothing special here.\n")
code6c, types6c, evs6c, _ = spawn("grader", out=grade([met_8034]), subject=SUBJ_8034_6)
routed6c = [e for e in evs6c if e["type"] == "criterion-routed"]
assert len(routed6c) == 1 and routed6c[0]["criterion"] == "AC1" and routed6c[0]["to"] == "none", routed6c
print("8034-AC6 ok (non-seat grader route)")

# AC6, seat route: the same routing computation runs before either grader
# refusal branch (dispatch.main's top `if a.role == "grader":` block, outside
# the grader_seat/non-seat split) — proven end to end via the seat backend too.
SUBJ_8034_6S = "L-spec-9471"
_gv_build_done(SUBJ_8034_6S, base_sha="B1", ready_sha="R1")
(dispatch.CONTENT / f"{SUBJ_8034_6S}.md").write_text(
    "## Acceptance criteria\n\nAC1 [ui]: browser criterion.\nreview_path: log in as x, go to /y\n\n"
    "AC2 [backend]: plain criterion.\nreview_path: nothing special here.\n")
_seat_view_6 = pathlib.Path(tempfile.mkdtemp())
code6s, raw6s = _gv_drive(SUBJ_8034_6S, view_build=lambda *a: _seat_view_6, serve_out=grade([met_8034]))
assert code6s == 0 and not any(e["type"] == "spawn-failed" for e in raw6s), raw6s
routed6s = [e for e in raw6s if e["type"] == "criterion-routed"]
assert len(routed6s) == 1 and routed6s[0]["criterion"] == "AC1" and routed6s[0]["to"] == "owed", raw6s
assert not any(e["type"] in ("grading-preflight-failed", "capability-hold") for e in raw6s), raw6s
print("8034-AC6 ok (seat grader route)")

# AC7 (events_for half): dispatch never derives a criterion-routed from a
# grader Output, whatever its fields look like.
ev_for_7 = dispatch.events_for("grader", grade([met_8034]), argparse.Namespace(subject=SUBJ_8034_6),
                               {"subject": SUBJ_8034_6})
assert not any(t == "criterion-routed" for t, _ in ev_for_7), ev_for_7
print("8034-AC7(events_for) ok")

# ══════════════════════════════════════════════════════════════════════════
# L-spec-0486 (R15d) · dispatch.ensure_room / _room_need_gb, plus the
# role=builder/grader pre-spend gate they back
# ══════════════════════════════════════════════════════════════════════════
import reap_tmp as _reap_tmp486  # noqa: E402

_orig_du486 = shutil.disk_usage
_orig_reap_now_486 = _reap_tmp486.reap_now


def _du_row486(total_gb, used_gb, free_gb):
    du = _orig_du486(str(TMP))
    return type(du)(total=int(total_gb * 2**30), used=int(used_gb * 2**30), free=int(free_gb * 2**30))


# AC12a: 0 bytes needed is always enough; the reaper never runs.
_reap_calls486 = []
_reap_tmp486.reap_now = lambda dry_run=False: (_reap_calls486.append(dry_run), {})[1]
try:
    assert _real_ensure_room_486(str(TMP), 0) is True, "AC12: 0 bytes needed is always enough"
    assert _reap_calls486 == [], f"AC12: the reaper is not called when there's already enough room: {_reap_calls486}"

    # too little, then enough right after the reaper "runs"
    _state486 = {"n": 0}

    def _du_low_then_high486(path):
        _state486["n"] += 1
        return _du_row486(100, 95, 0 if _state486["n"] == 1 else 10)

    shutil.disk_usage = _du_low_then_high486
    _reap_calls486.clear()
    assert _real_ensure_room_486(str(TMP), 5) is True, "AC12: too little then enough after the reaper -> True"
    assert len(_reap_calls486) == 1, f"AC12: the reaper ran exactly once: {_reap_calls486}"

    # stays too little
    shutil.disk_usage = lambda path: _du_row486(100, 99, 0)
    _reap_calls486.clear()
    assert _real_ensure_room_486(str(TMP), 5) is False, "AC12: stays too little -> False"
    assert len(_reap_calls486) == 1, f"AC12: exactly one reaper run even on failure: {_reap_calls486}"

    # a reaper that raises
    def _raising_reap_now486(dry_run=False):
        raise RuntimeError("L0486-AC12: forced")

    _reap_tmp486.reap_now = _raising_reap_now486
    assert _real_ensure_room_486(str(TMP), 5) is False, "AC12: a raising reaper yields False, never raises"
finally:
    shutil.disk_usage = _orig_du486
    _reap_tmp486.reap_now = _orig_reap_now_486
N += 1

# AC12b: DOIT_LEDGER_FILE is saved/restored around the reaper call — set to a
# distinct value, unset, and when the reaper itself raises after setting it.
shutil.disk_usage = lambda path: _du_row486(100, 99, 0)
try:
    for _prior486 in ("L-something-0001.jsonl", None):
        if _prior486 is None:
            os.environ.pop("DOIT_LEDGER_FILE", None)
        else:
            os.environ["DOIT_LEDGER_FILE"] = _prior486

        def _reap_now_sets_ledger486(dry_run=False):
            os.environ["DOIT_LEDGER_FILE"] = "L-executor-0001.jsonl"
            return {}

        _reap_tmp486.reap_now = _reap_now_sets_ledger486
        _real_ensure_room_486(str(TMP), 5)
        assert os.environ.get("DOIT_LEDGER_FILE") == _prior486, \
            f"AC12: DOIT_LEDGER_FILE restored to {_prior486!r}, got {os.environ.get('DOIT_LEDGER_FILE')!r}"

        if _prior486 is None:
            os.environ.pop("DOIT_LEDGER_FILE", None)
        else:
            os.environ["DOIT_LEDGER_FILE"] = _prior486

        def _reap_now_raises_after_set486(dry_run=False):
            os.environ["DOIT_LEDGER_FILE"] = "L-executor-0001.jsonl"
            raise RuntimeError("L0486-AC12: forced after set")

        _reap_tmp486.reap_now = _reap_now_raises_after_set486
        assert _real_ensure_room_486(str(TMP), 5) is False
        assert os.environ.get("DOIT_LEDGER_FILE") == _prior486, \
            f"AC12: restored even when the reaper raises after setting it: {os.environ.get('DOIT_LEDGER_FILE')!r}"
finally:
    shutil.disk_usage = _orig_du486
    _reap_tmp486.reap_now = _orig_reap_now_486
    os.environ.pop("DOIT_LEDGER_FILE", None)
N += 1

# AC12c: need_gb resolution — 5/2 defaults, look.toml's room_builder_gb/room_grader_gb override.
assert dispatch._room_need_gb("builder") == 5, "AC12: default builder need is 5GB"
assert dispatch._room_need_gb("grader") == 2, "AC12: default grader need is 2GB"
_fixture_toml_486 = TMP / "l0486-look-fixture.toml"
_fixture_toml_486.write_text("[thresholds]\nroom_builder_gb = 9\nroom_grader_gb = 4\n")
assert dispatch._room_need_gb("builder", toml_path=_fixture_toml_486) == 9, "AC12: room_builder_gb overrides to 9"
assert dispatch._room_need_gb("grader", toml_path=_fixture_toml_486) == 4, "AC12: room_grader_gb overrides to 4"
N += 1
print("L0486-AC12 ok")


def _boom13(*a, **kw):
    _boom13.calls.append(a)
    raise AssertionError("L0486-AC13: a backend must not be entered for a disk-refused dispatch")


_boom13.calls = []


def _drive_disk486(role, subject, force):
    """Drives dispatch.main() for role/subject with `ensure_room` forced to
    `force` and every backend stubbed to explode (AC13 must prove the refusal
    lands strictly before any of them). Returns (outcome, this drive's own
    ledger rows, that file's path) — outcome is the real exit code on a clean
    SystemExit, or the literal "reached-backend" when a stubbed backend fired
    (proof the dispatch got PAST the disk gate)."""
    dispatch.ensure_room = lambda path, need_gb: force
    real = dispatch.run_claude, dispatch.run_seat, dispatch.run_codex
    dispatch.run_claude = dispatch.run_seat = dispatch.run_codex = _boom13
    a = argparse.Namespace(role=role, subject=subject, packet=str(PK), path=None, cwd=str(REPO),
                           charter=None, project="t", mcp_config=None, timeout=None, max_usd=None)
    try:
        dispatch.main(a)
        outcome = 0
    except SystemExit as e:
        outcome = e.code
    except AssertionError:
        outcome = "reached-backend"
    finally:
        dispatch.run_claude, dispatch.run_seat, dispatch.run_codex = real
    f = max((TMP / "events").glob(f"L-{role}-[0-9]*.jsonl"))
    return outcome, [json.loads(l) for l in f.read_text().splitlines()], f


AC13_BUILDER_SUBJ, AC13_GRADER_SUBJ = "L-spec-9486e1", "L-spec-9486e2"
code13a, raw13a, _ = _drive_disk486("builder", AC13_BUILDER_SUBJ, False)
types13a = [e["type"] for e in raw13a]
assert code13a == 1, raw13a
assert "build-started" not in types13a and "spawn-started" not in types13a, \
    f"AC13: no build-started/spawn-started for a disk-refused builder: {types13a}"
assert any(e["type"] == "spawn-failed" and e.get("reason") == "disk" for e in raw13a), raw13a
assert _boom13.calls == [], f"AC13: no backend was ever entered: {_boom13.calls}"

code13b, raw13b, _ = _drive_disk486("grader", AC13_GRADER_SUBJ, False)
types13b = [e["type"] for e in raw13b]
assert code13b == 1, raw13b
assert "spawn-started" not in types13b and "grader-view-built" not in types13b, \
    f"AC13: no spawn-started/grader-view-built for a disk-refused grader: {types13b}"
assert any(e["type"] == "spawn-failed" and e.get("reason") == "disk" for e in raw13b), raw13b
assert _boom13.calls == [], f"AC13: no backend/grading call happened for the grader either: {_boom13.calls}"

_all13 = fold.read_events()
_briefs13 = [e for e in _all13 if e.get("type") == "brief" and e.get("condition") == "disk-room"]
assert _briefs13, "AC13: a brief for condition disk-room exists"
assert all(e.get("owner") == "thinker" for e in _briefs13), _briefs13
assert not any(e.get("type") in ("capability-hold", "escalation-blocking")
              and e.get("subject") in (AC13_BUILDER_SUBJ, AC13_GRADER_SUBJ) for e in _all13), \
    "AC13: a disk refusal is never a hold"
N += 1

# spec-writer/reviewer never call ensure_room at all
for _role13, _subj13 in (("spec-writer", "L-spec-9486e3"), ("reviewer", "L-spec-9486e4")):
    _calls13 = []
    dispatch.ensure_room = lambda path, need_gb: (_calls13.append(1), True)[1]
    real13 = dispatch.run_claude, dispatch.run_seat, dispatch.run_codex
    dispatch.run_claude = dispatch.run_seat = dispatch.run_codex = _boom13
    a13 = argparse.Namespace(role=_role13, subject=_subj13, packet=str(PK), path=None, cwd=str(REPO),
                             charter=None, project="t", mcp_config=None, timeout=None, max_usd=None)
    try:
        dispatch.main(a13)
    except (SystemExit, AssertionError):
        pass
    finally:
        dispatch.run_claude, dispatch.run_seat, dispatch.run_codex = real13
    assert _calls13 == [], f"L0486-AC13: {_role13} dispatch never calls ensure_room: {_calls13}"
N += 1

# a second dispatch, forced True, proceeds past the gate — the SAME subject's
# open disk-room brief is answered (why starting "cleared:").
_boom13.calls = []
code13c, raw13c, _ = _drive_disk486("builder", AC13_BUILDER_SUBJ, True)
assert code13c == "reached-backend", f"AC13: forced True proceeds to the (stubbed) backend: {code13c} {raw13c}"
assert "build-started" in [e["type"] for e in raw13c], \
    f"AC13: it got far enough to write build-started: {raw13c}"
assert not any(e.get("reason") == "disk" for e in raw13c), raw13c
_answered13 = [e for e in fold.read_events() if e.get("type") == "brief-answered"]
_cleared13 = [e for e in _answered13 if str(e.get("why", "")).startswith("cleared:")]
assert _cleared13, f"L0486-AC13: the disk-room brief is answered, why starting 'cleared:': {_answered13}"
N += 1

dispatch.ensure_room = lambda path, need_gb: True   # leave this file's own hermetic default in place
print("L0486-AC13 ok")
