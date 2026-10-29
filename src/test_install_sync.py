#!/usr/bin/env python3
"""install_sync — agent-linking and the once-per-shipped-sha cron sync
(L-charter-0042 R5). Run: python3 test_install_sync.py

Hermetic throughout: every `run()` call below supplies its own `root=`/
`repo=`/`agents_dir=`/`manifest=`/`crontab_path=` fixture (finding 4), so no
test here ever touches the real `~/.claude/agents`, the real crontab, or the
real `$DOIT_ROOT` ledger. `link_agents`'s own AC2 fixtures are the one
exception by design — they exercise the REAL repo's `agents/*.md` as SOURCE,
symlinked into a fresh, throwaway destination dir.
"""
import json, os, pathlib, shutil, subprocess, sys, tempfile, types, unittest
from datetime import datetime, timedelta, timezone

_scratch = tempfile.TemporaryDirectory(prefix="install-sync-test-")
TMP = pathlib.Path(_scratch.name)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import crons, install_sync  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    assert cond, msg
    N += 1


REPO_ROOT = install_sync.HERE.parent
REAL_AGENT_NAMES = sorted(p.name for p in (REPO_ROOT / "agents").glob("*.md"))

FIELDS = ("name", "where", "schedule", "command", "sig", "path", "owner", "project")


def _row(name, where, schedule, command):
    return {"name": name, "where": where, "schedule": schedule, "command": command,
            "sig": name, "path": "/bin/true", "owner": "operator", "project": "do-it-v2"}


def _write_manifest(path, rows_):
    """Serialize `rows_` (the same 8-field shape `crons.rows()` returns) as a
    `[[row]]`-table TOML file — `test_crons.py`'s own `_write_manifest` shape,
    duplicated here rather than imported (a test fixture helper, not product
    code)."""
    lines = []
    for r in rows_:
        lines.append("[[row]]")
        for k in FIELDS:
            lines.append(f"{k} = {r[k]!r}")
        lines.append("")
    pathlib.Path(path).write_text("\n".join(lines))


def _git_repo():
    repo = pathlib.Path(tempfile.mkdtemp(dir=TMP))
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    return repo


def _commit_crons(repo, rows_, msg):
    _write_manifest(repo / "crons.toml", rows_)
    subprocess.run(["git", "add", "crons.toml"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()


def _read_jsonl(path):
    p = pathlib.Path(path)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _newroot():
    d = pathlib.Path(tempfile.mkdtemp(dir=TMP))
    (d / "events").mkdir()
    return d


def _iso(dt):
    return dt.isoformat(timespec="seconds")


# ══════════════════════════════════════════════════════════════════════════════
# AC1 · install.sh: no per-file agent-linking loop remains, install_sync IS called
# ══════════════════════════════════════════════════════════════════════════════
_install_sh = (REPO_ROOT / "install.sh").read_text()
ok("agents/*.md" not in _install_sh,
   "installsync-0431 AC1: no bare agents/*.md glob token remains in install.sh")
ok(not any(("ln -sf" in line and "$AGENTS" in line) for line in _install_sh.splitlines()),
   "installsync-0431 AC1: no bare ln -sf ... \"$AGENTS\" line remains in install.sh")
ok("install_sync.py" in _install_sh and "link-agents" in _install_sh,
   "installsync-0431 AC1: install.sh invokes install_sync's link-agents CLI")
print("installsync-0431 AC1 ok")


# ══════════════════════════════════════════════════════════════════════════════
# AC2 · link_agents: every name sorted+linked; idempotent; repairs a break
# ══════════════════════════════════════════════════════════════════════════════
dest2 = pathlib.Path(tempfile.mkdtemp(dir=TMP))
linked2a = install_sync.link_agents(agents_dir=dest2)
ok(linked2a == REAL_AGENT_NAMES,
   f"installsync-0431 AC2: every agents/*.md name, sorted: {linked2a} vs {REAL_AGENT_NAMES}")
for name in REAL_AGENT_NAMES:
    dest_file = dest2 / name
    ok(dest_file.is_symlink() and dest_file.resolve() == (REPO_ROOT / "agents" / name).resolve(),
       f"installsync-0431 AC2: {name} is correctly symlinked")

linked2b = install_sync.link_agents(agents_dir=dest2)
ok(linked2b == [], f"installsync-0431 AC2: a second call on the same dir relinks nothing: {linked2b}")

broken_name = REAL_AGENT_NAMES[0]
(dest2 / broken_name).unlink()
(dest2 / broken_name).symlink_to(dest2)   # points somewhere wrong now
linked2c = install_sync.link_agents(agents_dir=dest2)
ok(linked2c == [broken_name],
   f"installsync-0431 AC2: a broken destination symlink is repaired and named alone: {linked2c}")
ok((dest2 / broken_name).resolve() == (REPO_ROOT / "agents" / broken_name).resolve(),
   "installsync-0431 AC2: the repaired symlink resolves correctly again")
print("installsync-0431 AC2 ok")


# ══════════════════════════════════════════════════════════════════════════════
# AC3 · run(): no install-synced event anywhere -> exactly one baseline event;
# a second call with still no shipped event appends nothing further
# ══════════════════════════════════════════════════════════════════════════════
repo3 = _git_repo()
row3a = _row("tick", "user", "*/5 * * * *", "doit tick")
sha3a = _commit_crons(repo3, [row3a], "c0")
root3 = _newroot()
agents3 = pathlib.Path(tempfile.mkdtemp(dir=TMP))
res3a = install_sync.run([], root=root3, repo=repo3, agents_dir=agents3)
lines3 = _read_jsonl(root3 / "events" / "L-tick-installsync.jsonl")
ok(len(lines3) == 1, f"installsync-0431 AC3: exactly one install-synced event appended: {lines3}")
ev3 = lines3[0]
ok(ev3["type"] == "install-synced" and ev3["baseline"] is True and ev3["sha"] == sha3a,
   f"installsync-0431 AC3: baseline=true, sha == git rev-parse HEAD: {ev3}")
ok(ev3["installed"] == [] and ev3["print_only"] == [] and ev3["subject"] == "do-it-v2",
   f"installsync-0431 AC3: installed/print_only empty, subject do-it-v2: {ev3}")
ok(sorted(ev3["linked"]) == REAL_AGENT_NAMES and res3a == ev3["linked"],
   f"installsync-0431 AC3: linked == run()'s own return: {ev3['linked']} vs {res3a}")
ok(not (agents3 / "tick").exists(), "installsync-0431 AC3: precondition sanity — a throwaway dir, not real")

# a second call, still no shipped event anywhere: appends nothing further
res3b = install_sync.run(lines3, root=root3, repo=repo3, agents_dir=agents3)
lines3b = _read_jsonl(root3 / "events" / "L-tick-installsync.jsonl")
ok(len(lines3b) == 1, f"installsync-0431 AC3: a second call with no shipped event appends nothing: {lines3b}")
ok(res3b == [], f"installsync-0431 AC3: and its own return is empty: {res3b}")
print("installsync-0431 AC3 ok")


# ══════════════════════════════════════════════════════════════════════════════
# AC4 · a shipped sha changing one where="user" row: crons.install runs against
# the fixture only, an install-synced with installed==[name] lands, and the
# fixture crontab gains a `# doit-cron:<name>` line
# ══════════════════════════════════════════════════════════════════════════════
repo4 = _git_repo()
row4a = _row("A", "user", "0 * * * *", "echo a-v1")
sha4a = _commit_crons(repo4, [row4a], "c0")
root4 = _newroot()
agents4 = pathlib.Path(tempfile.mkdtemp(dir=TMP))
install_sync.run([], root=root4, repo=repo4, agents_dir=agents4)
lines4 = _read_jsonl(root4 / "events" / "L-tick-installsync.jsonl")
baseline4_ts = datetime.fromisoformat(lines4[0]["ts"])

row4b = _row("A", "user", "5 * * * *", "echo a-v2")
sha4b = _commit_crons(repo4, [row4b], "c1")
manifest4 = repo4 / "crons.toml"                      # HEAD's current text, on disk
crontab4 = pathlib.Path(tempfile.mkdtemp(dir=TMP)) / "crontab.txt"
events4 = lines4 + [{"type": "shipped", "subject": "L-spec-9401", "project": "do-it-v2",
                     "sha": sha4b, "ts": _iso(baseline4_ts + timedelta(minutes=1))}]
res4 = install_sync.run(events4, root=root4, repo=repo4, agents_dir=agents4,
                        manifest=str(manifest4), crontab_path=str(crontab4))
lines4b = _read_jsonl(root4 / "events" / "L-tick-installsync.jsonl")
ok(len(lines4b) == 2, f"installsync-0431 AC4: one new install-synced event: {lines4b}")
ev4 = lines4b[-1]
ok(ev4["installed"] == ["A"] and ev4["sha"] == sha4b and ev4["print_only"] == [],
   f"installsync-0431 AC4: installed == [name], keyed by the shipped sha: {ev4}")
ok("failed" not in ev4, f"installsync-0431 AC4: no failures on a clean install: {ev4}")
ok("# doit-cron:A" in crontab4.read_text(),
   f"installsync-0431 AC4: the fixture crontab gains the tagged line: {crontab4.read_text()!r}")
ok(res4 == ["A"], f"installsync-0431 AC4: run()'s own return names the row touched: {res4}")
print("installsync-0431 AC4 ok")


# ══════════════════════════════════════════════════════════════════════════════
# AC5 · a where="cron.d" row instead: crons.install never called, print_only
# names it, a cron-row-needs-root brief lands; a second unresolved pass repeats
# no second brief
# ══════════════════════════════════════════════════════════════════════════════
repo5 = _git_repo()
row5a = _row("B", "cron.d", "0 3 * * *", "echo b-v1")
sha5a = _commit_crons(repo5, [row5a], "c0")
root5 = _newroot()
agents5 = pathlib.Path(tempfile.mkdtemp(dir=TMP))
install_sync.run([], root=root5, repo=repo5, agents_dir=agents5)
lines5 = _read_jsonl(root5 / "events" / "L-tick-installsync.jsonl")
baseline5_ts = datetime.fromisoformat(lines5[0]["ts"])

row5b = _row("B", "cron.d", "0 4 * * *", "echo b-v2")
sha5b = _commit_crons(repo5, [row5b], "c1")
manifest5 = repo5 / "crons.toml"
crontab5 = pathlib.Path(tempfile.mkdtemp(dir=TMP)) / "crontab.txt"


def _crons_install_must_not_be_called(*a, **k):
    raise AssertionError("installsync-0431 AC5: crons.install must never be called for a cron.d row")


real_install = crons.install
crons.install = _crons_install_must_not_be_called
try:
    events5 = lines5 + [{"type": "shipped", "subject": "L-spec-9501", "project": "do-it-v2",
                         "sha": sha5b, "ts": _iso(baseline5_ts + timedelta(minutes=1))}]
    res5 = install_sync.run(events5, root=root5, repo=repo5, agents_dir=agents5,
                            manifest=str(manifest5), crontab_path=str(crontab5))
finally:
    crons.install = real_install
lines5b = _read_jsonl(root5 / "events" / "L-tick-installsync.jsonl")
ev5 = lines5b[-1]
ok(ev5["print_only"] == ["B"] and ev5["installed"] == [],
   f"installsync-0431 AC5: print_only names the row, installed stays empty: {ev5}")
briefs5 = _read_jsonl(root5 / "events" / "L-look-local.jsonl")
crb5 = [b for b in briefs5 if b.get("type") == "brief" and b.get("condition") == "cron-row-needs-root"]
ok(len(crb5) == 1 and crb5[0]["key"] == "B" and crb5[0]["owner"] == "operator",
   f"installsync-0431 AC5: exactly one cron-row-needs-root brief, keyed by name, owner operator: {crb5}")
ok(not crontab5.exists() or "# doit-cron:B" not in crontab5.read_text(),
   "installsync-0431 AC5: the fixture crontab never gains a line for a cron.d row")

# a second, still-unresolved pass (the sha is now already named by an
# install-synced event, so run() re-processes nothing, and no second brief lands)
res5b = install_sync.run(lines5b, root=root5, repo=repo5, agents_dir=agents5,
                         manifest=str(manifest5), crontab_path=str(crontab5))
briefs5b = _read_jsonl(root5 / "events" / "L-look-local.jsonl")
crb5b = [b for b in briefs5b if b.get("type") == "brief" and b.get("condition") == "cron-row-needs-root"]
ok(len(crb5b) == 1, f"installsync-0431 AC5: a second unresolved pass raises no second brief: {crb5b}")
print("installsync-0431 AC5 ok")


# ══════════════════════════════════════════════════════════════════════════════
# AC6 · changed_rows: manifest-order added/changed names; a no-parent sha
# counts every row present there
# ══════════════════════════════════════════════════════════════════════════════
repo6 = _git_repo()
row6_a1 = _row("A", "user", "0 * * * *", "echo a1")
row6_c1 = _row("C", "user", "0 1 * * *", "echo c1")
sha6_1 = _commit_crons(repo6, [row6_a1, row6_c1], "c0")
row6_a2 = _row("A", "user", "5 * * * *", "echo a2")     # changed
row6_b2 = _row("B", "user", "0 2 * * *", "echo b2")     # new
row6_c2 = _row("C", "user", "0 1 * * *", "echo c1")     # unchanged
sha6_2 = _commit_crons(repo6, [row6_a2, row6_b2, row6_c2], "c1")
changed6 = install_sync.changed_rows(repo6, sha6_2)
ok(changed6 == ["A", "B"], f"installsync-0431 AC6: exactly A (changed) and B (new): {changed6}")
first6 = install_sync.changed_rows(repo6, sha6_1)
ok(sorted(first6) == ["A", "C"],
   f"installsync-0431 AC6: a sha with no parent counts every row present there: {first6}")
print("installsync-0431 AC6 ok")


# ══════════════════════════════════════════════════════════════════════════════
# AC7 · two changed where="user" rows in the SAME shipped pass; crons.install
# raises for the FIRST row only: it lands in `failed`, the second still installs
# ══════════════════════════════════════════════════════════════════════════════
repo7 = _git_repo()
row7_a1 = _row("A", "user", "0 * * * *", "echo a1")
row7_d1 = _row("D", "user", "0 5 * * *", "echo d1")
sha7_1 = _commit_crons(repo7, [row7_a1, row7_d1], "c0")
root7 = _newroot()
agents7 = pathlib.Path(tempfile.mkdtemp(dir=TMP))
install_sync.run([], root=root7, repo=repo7, agents_dir=agents7)
lines7 = _read_jsonl(root7 / "events" / "L-tick-installsync.jsonl")
baseline7_ts = datetime.fromisoformat(lines7[0]["ts"])

row7_a2 = _row("A", "user", "10 * * * *", "echo a2")    # changed
row7_d2 = _row("D", "user", "15 * * * *", "echo d2")    # changed
sha7_2 = _commit_crons(repo7, [row7_a2, row7_d2], "c1")
manifest7 = repo7 / "crons.toml"
crontab7 = pathlib.Path(tempfile.mkdtemp(dir=TMP)) / "crontab.txt"

real_install7 = crons.install


def _fail_first_row(name, manifest=None, crontab_path=None):
    if name == "A":
        raise RuntimeError("boom-A")
    return real_install7(name, manifest=manifest, crontab_path=crontab_path)


crons.install = _fail_first_row
try:
    events7 = lines7 + [{"type": "shipped", "subject": "L-spec-9701", "project": "do-it-v2",
                         "sha": sha7_2, "ts": _iso(baseline7_ts + timedelta(minutes=1))}]
    install_sync.run(events7, root=root7, repo=repo7, agents_dir=agents7,
                     manifest=str(manifest7), crontab_path=str(crontab7))
finally:
    crons.install = real_install7
lines7b = _read_jsonl(root7 / "events" / "L-tick-installsync.jsonl")
ev7 = lines7b[-1]
ok(ev7.get("failed") and ev7["failed"][0]["name"] == "A" and "boom-A" in ev7["failed"][0]["error"],
   f"installsync-0431 AC7: failed names the row and its error text: {ev7}")
ok(ev7["installed"] == ["D"], f"installsync-0431 AC7: the second row still installs: {ev7}")
print("installsync-0431 AC7 ok")


# ══════════════════════════════════════════════════════════════════════════════
# L-spec-8027 AC5 · install_sync._canonical: true only for the resolved
# canonical tree (passwd home, never HOME/DOIT_ROOT) or an explicit
# DOIT_CANONICAL_REPO; link_agents(agents_dir=None) refuses anything else
# before creating anything
# ══════════════════════════════════════════════════════════════════════════════
_orig_getpwuid = install_sync.pwd.getpwuid
_fake_passwd_home = pathlib.Path(tempfile.mkdtemp(dir=TMP))
install_sync.pwd.getpwuid = lambda uid: types.SimpleNamespace(pw_dir=str(_fake_passwd_home))
canonical_tree8027 = _fake_passwd_home / ".do-it" / "repos" / "do-it-v2"
(canonical_tree8027 / "agents").mkdir(parents=True)
(canonical_tree8027 / "agents" / "fixture.md").write_text("x")

_real_doit_root = os.environ.get("DOIT_ROOT")
_real_home = os.environ.get("HOME")
try:
    other_checkout8027 = pathlib.Path(tempfile.mkdtemp(dir=TMP))
    ok(install_sync._canonical(other_checkout8027) is False,
       "L-spec-8027 AC5: a temp checkout, unrelated to the canonical tree, is not canonical")

    # a temp DOIT_ROOT that happens to CONTAIN repos/do-it-v2: still false —
    # DOIT_ROOT is never the anchor (SD-R16-1 as amended)
    fake_root8027 = pathlib.Path(tempfile.mkdtemp(dir=TMP))
    fake_root_tree8027 = fake_root8027 / "repos" / "do-it-v2"
    fake_root_tree8027.mkdir(parents=True)
    os.environ["DOIT_ROOT"] = str(fake_root8027)
    ok(install_sync._canonical(fake_root_tree8027) is False,
       "L-spec-8027 AC5: a temp DOIT_ROOT holding repos/do-it-v2 cannot satisfy the check")
    if _real_doit_root is None:
        os.environ.pop("DOIT_ROOT", None)
    else:
        os.environ["DOIT_ROOT"] = _real_doit_root

    # a symlink resolving to the canonical tree: true
    symlink_checkout8027 = TMP / "symlink-to-canonical-8027"
    symlink_checkout8027.symlink_to(canonical_tree8027)
    ok(install_sync._canonical(symlink_checkout8027) is True,
       "L-spec-8027 AC5: a symlink resolving to the canonical tree is true")
    ok(install_sync._canonical(canonical_tree8027) is True,
       "L-spec-8027 AC5: the canonical tree itself, straight from the fake passwd home, is true")

    # DOIT_CANONICAL_REPO pointing at a fixture: true — an explicit override
    override_tree8027 = pathlib.Path(tempfile.mkdtemp(dir=TMP))
    os.environ["DOIT_CANONICAL_REPO"] = str(override_tree8027)
    ok(install_sync._canonical(override_tree8027) is True,
       "L-spec-8027 AC5: an explicit DOIT_CANONICAL_REPO is honoured")
    ok(install_sync._canonical(other_checkout8027) is False,
       "L-spec-8027 AC5: and a non-matching source is still refused with the override set")
    del os.environ["DOIT_CANONICAL_REPO"]

    # HOME set to a temp dir changes no answer — the anchor is passwd, not HOME
    os.environ["HOME"] = str(pathlib.Path(tempfile.mkdtemp(dir=TMP)))
    ok(install_sync._canonical(canonical_tree8027) is True,
       "L-spec-8027 AC5: HOME is never consulted — the canonical tree still qualifies")
    ok(install_sync._canonical(other_checkout8027) is False,
       "L-spec-8027 AC5: HOME is never consulted — a non-canonical tree still doesn't")
    if _real_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _real_home

    # the refusal case: link_agents(agents_dir=None) raises before creating
    # anything, and the sentinel HOME stays empty
    sentinel_home8027 = pathlib.Path(tempfile.mkdtemp(dir=TMP))
    os.environ["HOME"] = str(sentinel_home8027)
    try:
        install_sync.link_agents(agents_dir=None, source=other_checkout8027)
        ok(False, "L-spec-8027 AC5: a non-canonical source must raise LinkRefused")
    except install_sync.LinkRefused as e:
        ok("not the canonical" in str(e), f"L-spec-8027 AC5: the refusal names the reason: {e}")
    ok(not any(sentinel_home8027.iterdir()),
       "L-spec-8027 AC5: the refusal leaves the sentinel HOME empty — nothing created")
    if _real_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _real_home
finally:
    install_sync.pwd.getpwuid = _orig_getpwuid
    if _real_doit_root is None:
        os.environ.pop("DOIT_ROOT", None)
    else:
        os.environ["DOIT_ROOT"] = _real_doit_root
    if _real_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _real_home
    os.environ.pop("DOIT_CANONICAL_REPO", None)
print("L-spec-8027 AC5 ok")


# ══════════════════════════════════════════════════════════════════════════════
# L-spec-8027 AC6 · an explicit agents_dir is never refused, even from a
# non-canonical source, and links/counts/skips exactly as before (AC2 already
# covers this with the real repo as source); run(..., agents_dir=fixture) and
# the link-agents CLI follow AC5 when agents_dir is unset
# ══════════════════════════════════════════════════════════════════════════════
noncanonical_src8027 = pathlib.Path(tempfile.mkdtemp(dir=TMP))
(noncanonical_src8027 / "agents").mkdir()
(noncanonical_src8027 / "agents" / "explicit-fixture.md").write_text("x")
dest8027 = pathlib.Path(tempfile.mkdtemp(dir=TMP))
linked8027 = install_sync.link_agents(agents_dir=dest8027, source=noncanonical_src8027)
ok(linked8027 == ["explicit-fixture.md"],
   f"L-spec-8027 AC6: an explicit agents_dir links even from a non-canonical source: {linked8027}")
ok((dest8027 / "explicit-fixture.md").is_symlink() and
   (dest8027 / "explicit-fixture.md").resolve() == (noncanonical_src8027 / "agents" / "explicit-fixture.md").resolve(),
   "L-spec-8027 AC6: and the link is real, pointing at that source")
linked8027b = install_sync.link_agents(agents_dir=dest8027, source=noncanonical_src8027)
ok(linked8027b == [], f"L-spec-8027 AC6: a second call on the same dir relinks nothing: {linked8027b}")

# `install_sync.run(..., agents_dir=fixture)` is exactly AC3/AC4's own fixture
# shape above — already exercised with an explicit agents_dir throughout this
# file, never refused.

# The CLI derives its source from its own module path. Give it a disposable
# checkout so the refusal is independent of this repo's canonical status.
cli_checkout8027 = pathlib.Path(tempfile.mkdtemp(dir=TMP))
shutil.copytree(REPO_ROOT / "src", cli_checkout8027 / "src",
                ignore=shutil.ignore_patterns("__pycache__"))
(cli_checkout8027 / "agents").mkdir()
(cli_checkout8027 / "agents" / "fixture.md").write_text("x")
cli_module8027 = cli_checkout8027 / "src" / "install_sync.py"
cli_home8027 = pathlib.Path(tempfile.mkdtemp(dir=TMP))
_cli_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(cli_home8027), "DOIT_ROOT": str(cli_home8027 / "root")}
proc_cli8027 = subprocess.run(
    [sys.executable, str(cli_module8027), "link-agents"], cwd=cli_checkout8027,
    capture_output=True, text=True, env=_cli_env)
ok(proc_cli8027.returncode == 1,
   f"L-spec-8027 AC6: the CLI, run from a fixture checkout, refuses: "
   f"rc={proc_cli8027.returncode} stderr={proc_cli8027.stderr!r}")
ok("not the canonical" in proc_cli8027.stderr,
   f"L-spec-8027 AC6: and names why on stderr: {proc_cli8027.stderr!r}")
ok(not any(cli_home8027.iterdir()), "L-spec-8027 AC6: the CLI refusal writes nothing")
print("L-spec-8027 AC6 ok")


print(f"install_sync: {N} checks pass")

# Machine installation: real file writes, isolated destinations, preflight refusal.
from unittest.mock import patch
import hashlib
import machine


def machine_signed(ac, checks):
    body = json.dumps({"criterion": f"AC{ac}", "checks": checks}, sort_keys=True)
    print(json.dumps(dict(url=f"cli://install-machine/AC{ac}", status=0,
                          body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))
    print(f"install_machine AC{ac} ok")


def machine_fixture():
    base = pathlib.Path(tempfile.mkdtemp(dir=TMP))
    repo, root, cron = base / "repo", base / "root", base / "cron"
    (repo / "machine").mkdir(parents=True)
    (root / "machine-last").mkdir(parents=True)
    for name, text in (("models.toml", 'model = "new"\n'), ("crontab.txt", "@reboot echo new\n")):
        (repo / "machine" / name).write_text(text)
        old = text.replace("new", "old")
        (root / name if name == "models.toml" else cron).write_text(old)
        (root / "machine-last" / name).write_text(old)
    return repo, root, cron


repo_m, root_m, cron_m = machine_fixture()
assert install_sync.install_machine(repo_m, root=root_m, crontab_path=cron_m) == ["crontab.txt", "models.toml"]
for name, dest in (("models.toml", root_m / "models.toml"), ("crontab.txt", cron_m)):
    assert dest.read_bytes() == (repo_m / "machine" / name).read_bytes()
    assert (root_m / "machine-last" / name).read_bytes() == dest.read_bytes()
    assert (root_m / (name + ".bak")).read_text() == dest.read_text().replace("new", "old")
backups_m = {p.name: p.read_bytes() for p in root_m.glob("*.bak")}
assert install_sync.install_machine(repo_m, root=root_m, crontab_path=cron_m) == []
assert backups_m == {p.name: p.read_bytes() for p in root_m.glob("*.bak")}
p_m = subprocess.run([str(REPO_ROOT / "doit"), "machine", "check", "--models", str(root_m / "models.toml"),
                      "--crontab", str(cron_m), "--repo", str(repo_m)], capture_output=True)
assert p_m.returncode == 0


def tree_m(path):
    return {str(p.relative_to(path)): machine.read_text(p) for p in path.rglob("*") if p.is_file()}


def refused_m(repo, root, cron):
    before = tree_m(root.parent)
    try:
        install_sync.install_machine(repo, root=root, crontab_path=cron)
    except install_sync.MachineInstallRefused as exc:
        assert exc.name in machine.NAMES and exc.name in str(exc)
    else:
        raise AssertionError("machine installation should refuse")
    assert tree_m(root.parent) == before


for bad in ("missing", "secret", "unreadable"):
    repo_m, root_m, cron_m = machine_fixture()
    path_m = repo_m / "machine/models.toml"
    if bad == "missing": path_m.unlink()
    elif bad == "secret": path_m.write_text('api_key = "' + 'fixture"\n')
    elif os.geteuid() != 0: path_m.chmod(0)
    else: continue
    try: refused_m(repo_m, root_m, cron_m)
    finally:
        if path_m.exists(): path_m.chmod(0o600)
for bad_m in (None, 'api_key = "' + 'fixture"\n'):
    repo_m, root_m, cron_m = machine_fixture()
    if bad_m is None: (root_m / "models.toml").unlink()
    else: (root_m / "models.toml").write_text(bad_m)
    refused_m(repo_m, root_m, cron_m)
repo_m, root_m, cron_m = machine_fixture()
with patch.dict(os.environ, {"DOIT_CANONICAL_REPO": str(root_m / "elsewhere")}):
    import models
    for r, c in ((root_m, None), (models.PATH.parent, cron_m)):
        with patch.object(machine, "read_text", side_effect=AssertionError("guard must precede reads")):
            try: install_sync.install_machine(repo_m, root=r, crontab_path=c)
            except install_sync.MachineInstallRefused: pass
            else: raise AssertionError("noncanonical real destination accepted")
machine_signed(6, "whole-file writes; backup; idempotence; child check; preflight; resolved real guard")
machine_signed(3, "secret repo refuses before writing any file")

# Ledger events are read by a fresh interpreter, rather than the emitter's return.
def reread_m(root):
    code = 'import json,pathlib,sys; print(json.dumps([json.loads(x) for p in (pathlib.Path(sys.argv[1])/"events").glob("*.jsonl") for x in p.read_text().splitlines()]))'
    return json.loads(subprocess.check_output([sys.executable, "-c", code, str(root)]))


def shipped_tick_m(count, refuse=False):
    repo, root, cron = machine_fixture()
    (root / "events").mkdir()
    events = [{"type": "install-synced", "sha": "baseline", "ts": "2026-10-01T00:00:00+00:00"}]
    events += [{"type": "shipped", "project": "do-it-v2", "sha": f"fixture-{i}",
                "ts": f"2026-10-01T00:0{i+1}:00+00:00"} for i in range(count)]
    effect = install_sync.MachineInstallRefused("crontab.txt", "live differs from marker and repo") if refuse else None
    with patch.object(install_sync, "install_machine", return_value=["models.toml"], side_effect=effect) as install, \
         patch.object(install_sync, "changed_rows", return_value=["A"]), \
         patch.object(install_sync, "_row_where", return_value="user"), \
         patch.object(crons, "install") as row_install:
        install_sync.run(events, root=root, repo=repo, crontab_path=cron)
        assert install.call_count == (1 if count else 0)
        assert row_install.call_count == count
    return reread_m(root)


for count_m in (0, 1, 2):
    rows_m = [e for e in shipped_tick_m(count_m) if e["type"] == "install-synced"]
    assert len(rows_m) == count_m
    if count_m:
        assert rows_m[-1]["machine"] == ["models.toml"]
        assert all("machine" not in row for row in rows_m[:-1])
with patch.object(install_sync, "install_machine", side_effect=AssertionError("baseline install")):
    repo_m = _git_repo()
    _commit_crons(repo_m, [], "baseline")
    root_m = _newroot()
    install_sync.run([], root=root_m, repo=repo_m, agents_dir=TMP / "baseline-agents")
    assert "machine" not in reread_m(root_m)[-1]
machine_signed(7, "once per shipped tick; newest sha only; baseline never installs; fresh-process ledger read")

for absent_m in (False, True):
    repo_m, root_m, cron_m = machine_fixture()
    if absent_m: (root_m / "machine-last/crontab.txt").unlink()
    cron_m.write_text("@reboot echo outside\n")
    refused_m(repo_m, root_m, cron_m)
repo_m, root_m, cron_m = machine_fixture()
for name_m, dest_m in (("models.toml", root_m / "models.toml"), ("crontab.txt", cron_m)):
    (root_m / "machine-last" / name_m).unlink()
    dest_m.write_bytes((repo_m / "machine" / name_m).read_bytes())
assert install_sync.install_machine(repo_m, root=root_m, crontab_path=cron_m) == []
assert all((root_m / "machine-last" / name).exists() for name in machine.NAMES)
rows_m = shipped_tick_m(2, refuse=True)
assert all("machine" not in row for row in rows_m if row["type"] == "install-synced")
assert len([row for row in rows_m if row.get("condition") == "machine-install-refused"]) == 1
machine_signed(14, "out-of-band and missing-marker refusal is write-free; bootstrap; operator brief; per-row loop preserved")


def test_realm_restart_on_ship():
    import realm
    with tempfile.TemporaryDirectory(prefix='realm-sync-') as tmp:
        base=pathlib.Path(tmp); repo=base/'repo';repo.mkdir()
        subprocess.run(['git','init','-q',str(repo)],check=True)
        subprocess.run(['git','-C',str(repo),'config','user.email','fixture@example.test'],check=True)
        subprocess.run(['git','-C',str(repo),'config','user.name','Fixture'],check=True)
        (repo/'crons.toml').write_text('')
        def commit(path,text):
            dest=repo/path;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(text)
            subprocess.run(['git','-C',str(repo),'add','.'],check=True)
            subprocess.run(['git','-C',str(repo),'commit','-qm','fixture'],check=True)
            return subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
        baseline=commit('readme','fixture')
        code=commit('src/realm.html','changed')
        unrelated=commit('readme','unrelated')
        unit=commit('ops/doit-realm.service',(REPO_ROOT/'ops/doit-realm.service').read_text())
        def run(sha):
            root=base/('root-'+sha);root.mkdir()
            events=[dict(type='install-synced',sha=baseline,ts='2026-10-01T00:00:00Z'),
                    dict(type='shipped',sha=sha,project='do-it-v2',subject='L-spec-9001',ts='2026-10-02T00:00:00Z')]
            with patch.object(install_sync,'install_machine',return_value=[]):
                install_sync.run(events,root=root,repo=repo,agents_dir=base/'agents',crontab_path=base/'cron')
            return root,_read_jsonl(root/'events/L-tick-installsync.jsonl')[-1],events
        with patch.object(realm,'systemctl_user') as ctl,patch.object(realm,'install_realm') as install, \
             patch.object(realm,'unit_active',return_value=True):
            root,row,events=run(code)
            assert row['realm']=='restarted' and not row.get('failed')
            ctl.assert_called_once_with('try-restart','doit-realm');install.assert_not_called()
            with patch.object(install_sync,'install_machine',return_value=[]):
                install_sync.run(events+[row],root=root,repo=repo,agents_dir=base/'agents',crontab_path=base/'cron')
            assert ctl.call_count==1
            ctl.reset_mock()
            _,row,_=run(unrelated);assert 'realm' not in row
            ctl.assert_not_called();install.assert_not_called()
            _,row,_=run(unit);assert row['realm']=='installed'
            install.assert_called_once_with(repo,restart_running_only=True);ctl.assert_not_called()
        # Real systemctl invocation from the sync under a cron-like environment.
        binary=base/'bin';binary.mkdir();log=base/'calls'
        script=binary/'systemctl';script.write_text('#!/bin/sh\nprintf "%s|%s|%s\\n" "$*" "$XDG_RUNTIME_DIR" "$DBUS_SESSION_BUS_ADDRESS" >> "$CALL_LOG"\necho "fixture sync bus failure" >&2\nexit 1\n');script.chmod(0o755)
        with patch.dict(os.environ,{'PATH':str(binary)+':'+os.environ['PATH'],'CALL_LOG':str(log)}):
            with patch.dict(os.environ):
                os.environ.pop('XDG_RUNTIME_DIR',None);os.environ.pop('DBUS_SESSION_BUS_ADDRESS',None)
                # A new root allows another observation of the same ship.
                other=commit('src/realm.py','changed again')
                _,row,_=run(other)
        assert 'realm' not in row and row['failed'][0]['name']=='doit-realm'
        assert 'fixture sync bus failure' in row['failed'][0]['error']
        runtime=f'/run/user/{os.getuid()}'
        assert log.read_text().strip()==f'--user is-active doit-realm|{runtime}|unix:path={runtime}/bus'
    import hashlib
    body=json.dumps(row,sort_keys=True).encode()
    print(json.dumps(dict(url='install-synced://fixture',status=0,body_sha256=hashlib.sha256(body).hexdigest(),body_excerpt=json.dumps({'failed':row['failed']}))))
    print('ok test_realm_restart_on_ship')


class RealmTryRestartTests(unittest.TestCase):
    """Real git, systemctl subprocesses, unit copies and append-only ledger IO."""

    def setUp(self):
        import realm
        self.realm = realm
        scratch = tempfile.TemporaryDirectory(dir=TMP)
        self.addCleanup(scratch.cleanup)
        self.base = pathlib.Path(scratch.name)
        self.repo = _git_repo()
        self.baseline = _commit_crons(self.repo, [], 'baseline')
        binary = self.base / 'bin'
        binary.mkdir()
        self.state = self.base / 'state.json'
        self.log = self.base / 'commands.jsonl'
        script = binary / 'systemctl'
        script.write_text('''#!/usr/bin/python3
import json, os, pathlib, sys
state_path = pathlib.Path(os.environ['REALM_FIXTURE_STATE'])
log = pathlib.Path(os.environ['REALM_FIXTURE_LOG'])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
with log.open('a') as out:
    out.write(json.dumps(dict(argv=args, runtime=os.environ.get('XDG_RUNTIME_DIR'),
                             bus=os.environ.get('DBUS_SESSION_BUS_ADDRESS'))) + '\\n')
assert args[0] == '--user'
action = args[1]
if action == 'is-active':
    print(state.get('printed', state['state']))
    print(state.get('stderr', ''), file=sys.stderr, end='')
    if state.get('stop_after_status'):
        state['state'] = 'inactive'
        state_path.write_text(json.dumps(state))
    sys.exit(state['status'])
if action == state.get('fail_action'):
    print('fixture ' + action + ' failure', file=sys.stderr)
    sys.exit(1)
if action in ('start', 'restart') or (action == 'try-restart' and state['state'] == 'running'):
    state['state'] = 'running'
    state['restarts'] += 1
if action in ('enable', 'unmask'):
    state['enabled'] = True
state_path.write_text(json.dumps(state))
''')
        script.chmod(0o755)
        self.config = self.base / 'config'
        self.target = self.config / 'systemd/user/doit-realm.service'
        self.drop = self.config / 'systemd/user/doit-realm.service.d/limit.conf'
        self.drop.parent.mkdir(parents=True)
        self.drop.write_text('[Service]\nMemoryMax=4G\n')
        self.env = patch.dict(os.environ, {
            'PATH': str(binary) + ':' + os.environ['PATH'],
            'REALM_FIXTURE_STATE': str(self.state), 'REALM_FIXTURE_LOG': str(self.log),
            'XDG_CONFIG_HOME': str(self.config),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('XDG_RUNTIME_DIR', None)
        os.environ.pop('DBUS_SESSION_BUS_ADDRESS', None)

    def reset(self, enabled=True, state='running', status=0, **extra):
        self.state.write_text(json.dumps(dict(enabled=enabled, state=state, status=status,
                                             restarts=0, **extra)))
        self.log.write_text('')

    def commands(self):
        return _read_jsonl(self.log)

    def actions(self):
        commands = self.commands()
        for row in commands:
            self.assertEqual(row['argv'][0], '--user')
            self.assertEqual(row['argv'][2:], [] if row['argv'][1] == 'daemon-reload' else ['doit-realm'])
        return [row['argv'][1] for row in commands]

    def commit(self, *paths):
        for path in paths:
            dest = self.repo / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            if path == 'ops/doit-realm.service':
                dest.write_bytes((REPO_ROOT / path).read_bytes() + f'\n# fixture {paths!r}\n'.encode())
            else:
                dest.write_text('fixture ' + path + '\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-qm', 'fixture'], check=True)
        return subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], text=True).strip()

    def assert_state(self, enabled, state, restarts):
        saved = json.loads(self.state.read_text())
        self.assertEqual((saved['enabled'], saved['state'], saved['restarts']),
                         (enabled, state, restarts))

    def signed(self, ac, result):
        body = json.dumps(result, sort_keys=True)
        print(json.dumps(dict(url=f'install-sync://scratch/AC{ac}', status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def test_code_only_state_matrix(self):
        results = []
        for path in ('src/realm.py', 'src/realm.html', 'src/realm_ledger.py', 'src/realm_future.py'):
            sha = self.commit(path)
            for enabled in (True, False):
                for state, status in (('running', 0), ('inactive', 3), ('failed', 3)):
                    with self.subTest(path=path, enabled=enabled, state=state):
                        self.reset(enabled, state, status)
                        action = install_sync.sync_realm(self.repo, sha)
                        self.assertEqual(action, 'restarted' if status == 0 else 'not-running')
                        self.assert_state(enabled, state, int(status == 0))
                        self.assertEqual(self.actions(), ['is-active', 'try-restart'])
                        results.append(dict(path=path, enabled=enabled, state=state, action=action,
                                            argv=[r['argv'] for r in self.commands()]))
            for printed in ('activating', 'deactivating', 'reloading'):
                for status in (0, 3, 1):
                    with self.subTest(path=path, printed=printed, status=status):
                        self.reset(printed=printed, status=status)
                        if status == 1:
                            with self.assertRaises(RuntimeError):
                                install_sync.sync_realm(self.repo, sha)
                            self.assertEqual(self.actions(), ['is-active'])
                            action = 'error'
                        else:
                            action = install_sync.sync_realm(self.repo, sha)
                            self.assertEqual(action, 'restarted' if status == 0 else 'not-running')
                            self.assertEqual(self.actions(), ['is-active', 'try-restart'])
                        results.append(dict(path=path, printed=printed, status=status, action=action))
            self.reset(stop_after_status=True)
            self.assertEqual(install_sync.sync_realm(self.repo, sha), 'restarted')
            self.assert_state(True, 'inactive', 0)
            self.assertEqual(self.actions(), ['is-active', 'try-restart'])
            results.append(dict(path=path, intervening_stop='inactive', restarts=0))
        self.signed(1, results)

    def test_unit_sync_state_matrix(self):
        results = []
        for paths in (('ops/doit-realm.service',), ('ops/doit-realm.service', 'src/realm.py')):
            sha = self.commit(*paths)
            for enabled in (True, False):
                for state, status in (('running', 0), ('inactive', 3), ('failed', 3)):
                    with self.subTest(paths=paths, enabled=enabled, state=state):
                        self.reset(enabled, state, status)
                        self.target.write_text('old unit')
                        self.assertEqual(install_sync.sync_realm(self.repo, sha), 'installed')
                        with self.target.open('rb') as installed:
                            self.assertEqual(installed.read(), (self.repo / paths[0]).read_bytes())
                        self.assertEqual(self.drop.read_text(), '[Service]\nMemoryMax=4G\n')
                        self.assert_state(enabled, state, int(status == 0))
                        self.assertEqual(self.actions(), ['daemon-reload', 'try-restart'])
                        results.append(dict(paths=paths, enabled=enabled, state=state, action='installed',
                                            copied=True, drop_in_preserved=True, argv=[r['argv'] for r in self.commands()]))
        self.signed(2, results)

    def run_ship(self, sha):
        root = pathlib.Path(tempfile.mkdtemp(dir=self.base))
        events = [dict(type='install-synced', sha=self.baseline, ts='2026-10-01T00:00:00Z'),
                  dict(type='shipped', sha=sha, project='do-it-v2', ts='2026-10-02T00:00:00Z')]
        kwargs = dict(root=root, repo=self.repo, agents_dir=self.base / 'agents',
                      manifest=self.repo / 'crons.toml', crontab_path=self.base / 'cron')
        # The machine sync is independent; service and ledger seams stay real.
        with patch.object(install_sync, 'install_machine', return_value=[]):
            install_sync.run(events, **kwargs)
        row = next(e for e in reread_m(root) if e['type'] == 'install-synced')
        before = self.log.read_bytes()
        install_sync.run(events + [row], **kwargs)
        self.assertEqual(self.log.read_bytes(), before)
        self.assertEqual(len(reread_m(root)), 1)
        return row

    def test_sync_event_round_trip_and_failures(self):
        results = []
        code = self.commit('src/realm.py')
        for state, status, expected in (('running', 0, 'restarted'), ('inactive', 3, 'not-running')):
            self.reset(state=state, status=status)
            row = self.run_ship(code)
            self.assertEqual(row['realm'], expected)
            self.assertNotIn('failed', row)
            results.append(row)
        unrelated = self.commit('readme')
        self.reset()
        self.assertNotIn('realm', self.run_ship(unrelated))
        self.assertEqual(self.actions(), [])
        root = pathlib.Path(tempfile.mkdtemp(dir=self.base))
        install_sync.run([dict(type='shipped', sha=code, project='do-it-v2')],
                         root=root, repo=self.repo, agents_dir=self.base / 'agents')
        self.assertTrue(reread_m(root)[0]['baseline'])
        self.assertEqual(self.actions(), [])
        unit = self.commit('ops/doit-realm.service')
        self.reset(state='inactive', status=3)
        row = self.run_ship(unit)
        self.assertEqual(row['realm'], 'installed')
        results.append(row)

        for status, printed, stderr in ((0, 'active', ''), (3, 'inactive', ''), (3, 'failed', ''),
                                        (1, 'inactive', 'fixture bus failure'), (1, 'active', ''),
                                        (4, 'unknown', 'fixture missing unit')):
            with self.subTest(status=status, printed=printed, stderr=stderr):
                self.reset(status=status, printed=printed, stderr=stderr)
                if status in (0, 3):
                    self.assertIs(self.realm.unit_active('doit-realm'), status == 0)
                else:
                    with self.assertRaises(RuntimeError) as error:
                        self.realm.unit_active('doit-realm')
                    self.assertIn(f'exit {status}', str(error.exception))
                    self.assertIn('systemctl --user is-active doit-realm', str(error.exception))
                    if stderr:
                        self.assertIn(stderr, str(error.exception))
                self.assertEqual(self.actions(), ['is-active'])
                results.append(dict(status=status, printed=printed, stderr=stderr, classified=True))

        for explicit in (False, True):
            self.reset()
            if explicit:
                os.environ.update(XDG_RUNTIME_DIR='/fixture/runtime', DBUS_SESSION_BUS_ADDRESS='unix:path=/fixture/bus')
            self.assertTrue(self.realm.unit_active('doit-realm'))
            self.realm.systemctl_user('try-restart', 'doit-realm')
            runtime = '/fixture/runtime' if explicit else f'/run/user/{os.getuid()}'
            bus = 'unix:path=/fixture/bus' if explicit else f'unix:path={runtime}/bus'
            for call in self.commands():
                self.assertEqual((call['runtime'], call['bus']), (runtime, bus))
            for call in (lambda: self.realm.unit_active('doit-realm'),
                         lambda: self.realm.systemctl_user('try-restart', 'doit-realm')):
                with patch.object(self.realm.subprocess, 'run', side_effect=subprocess.TimeoutExpired('systemctl', 30)) as runner:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        call()
                    self.assertEqual(runner.call_args.kwargs['timeout'], 30)
                    env = runner.call_args.kwargs['env']
                    self.assertEqual((env['XDG_RUNTIME_DIR'], env['DBUS_SESSION_BUS_ADDRESS']), (runtime, bus))
            results.append(dict(explicit_environment=explicit, runtime=runtime, bus=bus,
                                status_and_action_timeout=30))

        failures = ((code, dict(status=1, stderr='fixture bus failure'), ['is-active'], 'fixture bus failure'),
                    (code, dict(status=1), ['is-active'], 'exit 1'),
                    (code, dict(fail_action='try-restart'), ['is-active', 'try-restart'], 'fixture try-restart failure'),
                    (code, dict(state='inactive', status=3, fail_action='try-restart'),
                     ['is-active', 'try-restart'], 'fixture try-restart failure'),
                    (unit, dict(fail_action='daemon-reload'), ['daemon-reload'], 'fixture daemon-reload failure'),
                    (unit, dict(fail_action='try-restart'), ['daemon-reload', 'try-restart'], 'fixture try-restart failure'))
        for sha, state, actions, diagnostic in failures:
            with self.subTest(failure=diagnostic, sha=sha):
                self.reset(**state)
                row = self.run_ship(sha)
                self.assertNotIn('realm', row)
                self.assertEqual(row['failed'][0]['name'], 'doit-realm')
                self.assertIn(diagnostic, row['failed'][0]['error'])
                self.assertEqual(self.actions(), actions)
                results.append(row)
        self.reset()
        real_run = subprocess.run
        def timeout_status(command, **kwargs):
            if command[:3] == ['systemctl', '--user', 'is-active']:
                self.assertEqual(kwargs['timeout'], 30)
                raise subprocess.TimeoutExpired(command, 30)
            return real_run(command, **kwargs)
        with patch.object(self.realm.subprocess, 'run', side_effect=timeout_status):
            row = self.run_ship(code)
        self.assertNotIn('realm', row)
        self.assertEqual(row['failed'][0]['name'], 'doit-realm')
        self.assertIn('timed out', row['failed'][0]['error'])
        self.assertEqual(self.actions(), [])
        results.append(row)
        self.reset()
        # Fail inside the real copyfile, after the destination parent exists.
        self.target.unlink()
        self.target.mkdir()
        row = self.run_ship(unit)
        self.assertNotIn('realm', row)
        self.assertEqual(row['failed'][0]['name'], 'doit-realm')
        self.assertTrue(row['failed'][0]['error'])
        self.assertEqual(self.actions(), [])
        results.append(row)
        self.signed(3, results)


if __name__ == '__main__':
    test_realm_restart_on_ship()
    unittest.main()
