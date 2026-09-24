#!/usr/bin/env python3
"""crons.py — the builder-box cron manifest. Run: python3 test_crons.py

Hermetic throughout (Plan SD11): never invokes the real `crontab` binary
(`crons._RUN` is patched, never `crons._live_crontab_text` itself), never reads
or writes the real `/etc/cron.d`, and never touches the real `$DOIT_ROOT` —
every fixture is a temp file/dir passed via `manifest=`/`crontab_path=`/
`crond_dir=`.
"""
import contextlib, io, os, pathlib, re, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp(prefix="crons-test-"))
os.environ["DOIT_ROOT"] = str(TMP / "doit-root-unused")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import backup, crons, fold, up  # noqa: E402

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


REAL_MANIFEST = pathlib.Path(__file__).resolve().parent.parent / "crons.toml"
NAMES = {"tick", "look", "tmp-reaper", "lessons-digest", "backup-watch",
         "ledger-snapshot", "auto-deploy"}


def _write_manifest(path, rows_):
    """Serialize `rows_` (a list of the same 8-field dicts `crons.rows()`
    returns) as a `[[row]]`-table TOML file, `sig` as a literal (single-quoted)
    string so a backslash regex round-trips with no escaping."""
    lines = []
    for r in rows_:
        lines.append("[[row]]")
        for k in crons.FIELDS:
            v = r[k]
            if k == "sig":
                lines.append(f"{k} = '{v}'")
            else:
                lines.append(f"{k} = {v!r}")
        lines.append("")
    pathlib.Path(path).write_text("\n".join(lines))


def _exec_manifest(tmpdir, rows_=None, mode=0o755):
    """A temp copy of `rows_` (default: the real manifest's own rows) with
    every `path` rewritten to a fresh temp file at `mode` — AC5/AC9's "every
    row's execute bit pre-satisfied" seam. Returns the manifest path."""
    rows_ = list(rows_ if rows_ is not None else crons.rows(str(REAL_MANIFEST)))
    out = []
    for i, r in enumerate(rows_):
        exe = pathlib.Path(tmpdir) / f"exe-{i}-{r['name']}"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(mode)
        out.append({**r, "path": str(exe)})
    p = pathlib.Path(tmpdir) / "manifest.toml"
    _write_manifest(p, out)
    return p, out


# ══ AC1 · rows() — 7 rows, all 8 keys, where/project split ══════════════════
d1 = TMP / "ac1"
d1.mkdir()
r1 = crons.rows(str(REAL_MANIFEST))
check(len(r1) == 7, f"AC1: exactly 7 rows: {len(r1)}")
check({row["name"] for row in r1} == NAMES, f"AC1: name set: {[row['name'] for row in r1]}")
for row in r1:
    check(set(row.keys()) == set(crons.FIELDS), f"AC1: all 8 keys on {row['name']}: {row.keys()}")
    for k in crons.FIELDS:
        check(isinstance(row[k], str) and row[k], f"AC1: {row['name']}.{k} is a non-empty string: {row[k]!r}")
auto = [row for row in r1 if row["name"] == "auto-deploy"][0]
check(auto["where"] == "cron.d" and auto["project"] == "albert-scott",
      f"AC1: auto-deploy is cron.d/albert-scott: {auto}")
for row in r1:
    if row["name"] == "auto-deploy":
        continue
    check(row["where"] == "user" and row["project"] == "do-it-v2",
          f"AC1: {row['name']} is user/do-it-v2: {row}")

# ══ AC2 · tick matches up.cron_line() byte for byte, default AND monkeypatched ═
tick = [row for row in crons.rows(str(REAL_MANIFEST)) if row["name"] == "tick"][0]
check(f"{tick['schedule']} {tick['command']}" == up.cron_line(),
      f"AC2(a): tick's rendered line == up.cron_line() at defaults:\n"
      f"{tick['schedule']} {tick['command']}\nvs\n{up.cron_line()}")
real_root, real_tick_min = fold.ROOT, os.environ.get("DOIT_TICK_MIN")
try:
    patched_root = TMP / "patched-root"
    fold.ROOT = patched_root
    os.environ["DOIT_TICK_MIN"] = "13"
    tick2 = [row for row in crons.rows(str(REAL_MANIFEST)) if row["name"] == "tick"][0]
    line2 = f"{tick2['schedule']} {tick2['command']}"
    check(line2 == up.cron_line(), f"AC2(b): patched fold.ROOT/DOIT_TICK_MIN, still byte-identical to "
                                   f"up.cron_line():\n{line2}\nvs\n{up.cron_line()}")
    check(str(patched_root) in line2, f"AC2(b): the patched temp path actually appears in the line "
                                      f"(the non-default arm is exercised): {line2}")
finally:
    fold.ROOT = real_root
    if real_tick_min is None:
        os.environ.pop("DOIT_TICK_MIN", None)
    else:
        os.environ["DOIT_TICK_MIN"] = real_tick_min

# ══ AC3 · backup-watch matches backup.cron_line() byte for byte ═════════════
bw = [row for row in crons.rows(str(REAL_MANIFEST)) if row["name"] == "backup-watch"][0]
check(f"{bw['schedule']} {bw['command']}" == backup.cron_line(),
      f"AC3: backup-watch's rendered line == backup.cron_line():\n"
      f"{bw['schedule']} {bw['command']}\nvs\n{backup.cron_line()}")

# ══ AC4 · every row's sig matches its own print_line() ══════════════════════
for row in crons.rows(str(REAL_MANIFEST)):
    line = crons.print_line(row["name"], str(REAL_MANIFEST))
    check(bool(re.search(row["sig"], line)), f"AC4: {row['name']}'s sig matches its own print_line: "
                                             f"{row['sig']!r} vs {line!r}")

# ══ AC5 · check() against the measured-live fixture — 3 absent, other 4 satisfied ══
d5 = TMP / "ac5"
d5.mkdir()
manifest5, rows5 = _exec_manifest(d5)
FIXTURE_5 = (
    "# PAUSED-2026-09-14 */5 * * * * ROLE=orc /opt/albert-scott/scripts/orc-idle-watch.sh "
    ">> /tmp/orc-idle-watch.log 2>&1\n"
    "# */5 * * * * doit tick (Q9 immediate pickup) >> /home/albert/.do-it/logs/tick.log 2>&1\n"
    "# * * * * * doit backup watch (L-spec-0186) --for 60 >> /home/albert/.do-it/backup.log 2>&1\n"
    "*/10 * * * * /var/backups/do-it/snapshot.sh >/dev/null 2>&1\n"
    "7 * * * * /opt/albert-scott/.venv/bin/python3 $HOME/do-it-v2/scripts/lessons_digest.py "
    ">> $HOME/.do-it/lessons-digest.log 2>&1\n"
    "*/5 * * * * DOIT_ROOT=/home/albert/.do-it /home/albert/do-it-v2/doit tick "
    ">> /home/albert/.do-it/logs/tick.log 2>&1\n"
    "7,37 * * * * /home/albert/.do-it/tmp-reaper.sh\n"
    "* * * * * /home/albert/do-it-v2/doit backup watch --for 60 >> /home/albert/.do-it/backup.log 2>&1\n"
)
crond5_empty = d5 / "crond-empty"
crond5_empty.mkdir()
# The fixture's sigs must match against the fixture's own literal command text
# (not the temp-path-substituted one), so rewrite each non-auto-deploy row's sig
# is left as-is (sig is command-shape, not path-shape) — but the LIVE fixture's
# `tick`/`backup-watch` lines carry the real `/home/albert/do-it-v2/doit`, which
# still matches `\bdoit tick\b` / `\bdoit backup watch\b` regardless of path.
res5 = crons.check(manifest=str(manifest5), crontab_text=FIXTURE_5, crond_dir=str(crond5_empty))
check({r["name"] for r in res5} == {"look", "tmp-reaper", "auto-deploy"},
      f"AC5: exactly {{look, tmp-reaper, auto-deploy}} missing: {[r['name'] for r in res5]}")
check(all(r["reason"] == "absent" for r in res5), f"AC5: every entry is absent: {res5}")
check(len(res5) == 3, f"AC5: no extra rows: {res5}")

# AC5's second, minimal fixture: the only matching line is commented.
d5b = TMP / "ac5b"
d5b.mkdir()
solo_exe = d5b / "solo.exe"
solo_exe.write_text("#!/bin/sh\n")
solo_exe.chmod(0o755)
solo_row = {"name": "solo", "where": "user", "schedule": "*/5 * * * *", "command": "doit solo",
            "sig": r"\bdoit solo\b", "path": str(solo_exe), "owner": "o", "project": "p"}
solo_manifest = d5b / "solo.toml"
_write_manifest(solo_manifest, [solo_row])
res5b = crons.check(manifest=str(solo_manifest),
                    crontab_text="# */5 * * * * doit solo >> /tmp/solo.log 2>&1\n",
                    crond_dir=str(d5b / "unused-crond"))
check(len(res5b) == 1 and res5b[0]["name"] == "solo" and res5b[0]["reason"] == "absent",
      f"AC5(minimal): a commented-only match is still absent: {res5b}")

# ══ AC6 · a matching, non-executable path -> not-executable ═════════════════
d6 = TMP / "ac6"
d6.mkdir()
noexec = d6 / "noexec.sh"
noexec.write_text("#!/bin/sh\n")
noexec.chmod(0o644)
row6 = {"name": "solo6", "where": "user", "schedule": "* * * * *", "command": "doit solo6",
        "sig": r"\bdoit solo6\b", "path": str(noexec), "owner": "o", "project": "p"}
manifest6 = d6 / "m.toml"
_write_manifest(manifest6, [row6])
res6 = crons.check(manifest=str(manifest6), crontab_text="* * * * * doit solo6\n",
                   crond_dir=str(d6 / "unused-crond"))
check(len(res6) == 1 and res6[0]["name"] == "solo6" and res6[0]["reason"] == "not-executable",
      f"AC6: a present-but-non-executable row reports not-executable: {res6}")

# ══ AC7 · a needed crond_dir that cannot be read raises Undetermined; an ══════
# ══ unneeded one is never touched and never raises ══════════════════════════
d7 = TMP / "ac7"
d7.mkdir()
crond_row = {"name": "cd7", "where": "cron.d", "schedule": "* * * * *", "command": "echo hi",
            "sig": "echo hi", "path": "/bin/echo", "owner": "o", "project": "p"}
manifest7_crond = d7 / "crond.toml"
_write_manifest(manifest7_crond, [crond_row])
try:
    crons.check(manifest=str(manifest7_crond), crontab_text="", crond_dir=str(d7 / "does-not-exist"))
    check(False, "AC7: a missing-but-needed crond_dir must raise Undetermined")
except crons.Undetermined:
    check(True, "AC7: missing crond_dir raises Undetermined when a cron.d row exists")

user_row = {"name": "u7", "where": "user", "schedule": "* * * * *", "command": "doit u7",
           "sig": r"\bdoit u7\b", "path": "/bin/echo", "owner": "o", "project": "p"}
manifest7_user = d7 / "user.toml"
_write_manifest(manifest7_user, [user_row])
try:
    res7 = crons.check(manifest=str(manifest7_user), crontab_text="* * * * * doit u7\n",
                       crond_dir=str(d7 / "also-does-not-exist"))
    check(res7 == [], f"AC7: no cron.d row -> crond_dir never touched, no raise, row satisfied: {res7}")
except crons.Undetermined as e:
    check(False, f"AC7: crond_dir must not be consulted when no row needs it: {e}")

# ══ AC8 · crons._RUN patched (never _live_crontab_text itself) ══════════════
real_run = crons._RUN


class _CP:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


try:
    crons._RUN = lambda *a, **k: _CP(1, "", "no crontab for testuser")
    check(crons._live_crontab_text() == "", "AC8: 'no crontab' stderr -> empty string, no raise")
    d8 = TMP / "ac8"
    d8.mkdir()
    m8, _ = _exec_manifest(d8, rows_=[row6])  # a plain user row, reused shape
    res8 = crons.check(manifest=str(m8), crontab_text=None, crond_dir=str(d8 / "unused"))
    check(isinstance(res8, list), f"AC8: check(crontab_text=None) proceeds as if given '' (no raise): {res8}")

    crons._RUN = lambda *a, **k: _CP(1, "", "permission denied")
    try:
        crons._live_crontab_text()
        check(False, "AC8: unrelated non-zero exit must raise Undetermined")
    except crons.Undetermined:
        check(True, "AC8: unrelated non-zero exit raises Undetermined (_live_crontab_text)")
    try:
        crons.check(manifest=str(m8), crontab_text=None, crond_dir=str(d8 / "unused"))
        check(False, "AC8: check() must propagate Undetermined from an unrelated exit")
    except crons.Undetermined:
        check(True, "AC8: unrelated non-zero exit raises Undetermined (check())")

    def _raiser(*a, **k):
        raise FileNotFoundError("no crontab binary on this box")
    crons._RUN = _raiser
    try:
        crons._live_crontab_text()
        check(False, "AC8: _RUN raising must raise Undetermined")
    except crons.Undetermined:
        check(True, "AC8: _RUN raising FileNotFoundError raises Undetermined (_live_crontab_text)")
    try:
        crons.check(manifest=str(m8), crontab_text=None, crond_dir=str(d8 / "unused"))
        check(False, "AC8: check() must propagate Undetermined when _RUN raises")
    except crons.Undetermined:
        check(True, "AC8: _RUN raising raises Undetermined (check())")
finally:
    crons._RUN = real_run

# ══ AC9 · main(["check", ...]) exit codes and output, all in-process ════════
d9 = TMP / "ac9"
d9.mkdir()
manifest9, rows9 = _exec_manifest(d9)
crontab9 = "\n".join(crons.print_line(r["name"], str(manifest9)) for r in rows9 if r["where"] == "user")
f9_all = d9 / "crontab-all.txt"
f9_all.write_text(crontab9 + "\n")
crond9 = d9 / "crond"
crond9.mkdir()
auto9 = [r for r in rows9 if r["where"] == "cron.d"][0]
(crond9 / "auto-deploy").write_text(crons.print_line("auto-deploy", str(manifest9)) + "\n")

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc9a = crons.main(["check", "--manifest", str(manifest9), "--crontab-file", str(f9_all),
                       "--crond-dir", str(crond9)])
check(rc9a == 0, f"AC9: exit 0 when everything is satisfied: {rc9a} / {buf.getvalue()!r}")

f9_empty = d9 / "crontab-empty.txt"
f9_empty.write_text("")
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    rc9b = crons.main(["check", "--manifest", str(manifest9), "--crontab-file", str(f9_empty),
                       "--crond-dir", str(crond9)])
out9b = buf2.getvalue()
check(rc9b == 1, f"AC9: exit 1 when some rows are missing: {rc9b}")
for r in rows9:
    if r["where"] == "user":
        check(r["name"] in out9b and "absent" in out9b, f"AC9: {r['name']}/absent named in stdout:\n{out9b}")

buf3, errbuf3 = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(buf3), contextlib.redirect_stderr(errbuf3):
    rc9c = crons.main(["check", "--manifest", str(manifest9), "--crontab-file", str(f9_all),
                       "--crond-dir", str(d9 / "no-such-crond-dir")])
check(rc9c == 2, f"AC9: exit 2 on Undetermined: {rc9c}")
check(errbuf3.getvalue() != "", "AC9: the Undetermined message goes to stderr")
check(buf3.getvalue() == "", f"AC9: nothing printed to stdout on the Undetermined path: {buf3.getvalue()!r}")

# ══ AC10 · install() — refuse cron.d, append, then replace idempotently ═════
d10 = TMP / "ac10"
d10.mkdir()
F10 = d10 / "crontab.txt"
F10.write_text("* * * * * some unrelated line\n")
before10 = F10.read_text()
try:
    crons.install("auto-deploy", crontab_path=str(F10))
    check(False, "AC10(a): install() on a cron.d row must raise ValueError")
except ValueError:
    check(True, "AC10(a): install('auto-deploy', ...) raises ValueError")
check(F10.read_text() == before10, "AC10(a): F is byte-unchanged after the refusal")

r10a = crons.install("tmp-reaper", crontab_path=str(F10))
check(r10a == {"ok": True, "action": "appended", "previous_line": None},
      f"AC10(b): first install appends: {r10a}")
text10a = F10.read_text()
check(before10.rstrip("\n") in text10a, "AC10(b): the original unrelated line survives")
new_line10 = [ln for ln in text10a.splitlines() if ln.endswith("# doit-cron:tmp-reaper")]
check(len(new_line10) == 1, f"AC10(b): exactly one new tagged line: {text10a!r}")
check(bool(re.search(r"\bdoit reap-tmp\b", new_line10[0])),
      f"AC10(b): the new line matches tmp-reaper's own sig: {new_line10[0]!r}")

r10b = crons.install("tmp-reaper", crontab_path=str(F10))
check(r10b["ok"] is True and r10b["action"] == "replaced" and r10b["previous_line"] == new_line10[0],
      f"AC10(c): a rerun replaces, naming (b)'s exact line as previous_line: {r10b}")
text10b = F10.read_text()
tagged10b = [ln for ln in text10b.splitlines() if ln.endswith("# doit-cron:tmp-reaper")]
check(len(tagged10b) == 1, f"AC10(c): still exactly one tagged line: {text10b!r}")
check(before10.rstrip("\n") in text10b, "AC10(c): the unrelated line is still there, byte-identical")

# ══ AC11 · doit's own rewiring ═══════════════════════════════════════════════
DOIT = (pathlib.Path(__file__).resolve().parent.parent / "doit").read_text()
case_lines = [ln for ln in DOIT.splitlines()
             if re.match(r'^\s*crons\)\s+shift;\s+exec python3 "\$SRC/crons\.py"', ln)]
check(len(case_lines) == 1, f"AC11: exactly one crons) case line: {case_lines}")
check("doit crons" in DOIT, "AC11: at least one 'doit crons' help mention")

print(f"crons: {N} checks pass")
