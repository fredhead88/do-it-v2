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
import json, os, pathlib, subprocess, sys, tempfile, types
from datetime import datetime, timedelta, timezone

TMP = pathlib.Path(tempfile.mkdtemp(prefix="install-sync-test-"))
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

# the link-agents CLI, invoked with none of that — run() and main() both
# default to HERE.parent as source, which THIS worktree checkout is not, so
# it must follow AC5: refuse, print why, exit non-zero, write nothing
_cli_env = {k: v for k, v in os.environ.items() if k != "DOIT_CANONICAL_REPO"}
proc_cli8027 = subprocess.run(
    [sys.executable, str(REPO_ROOT / "src" / "install_sync.py"), "link-agents"],
    capture_output=True, text=True, env=_cli_env)
ok(proc_cli8027.returncode == 1,
   f"L-spec-8027 AC6: the CLI, run from this non-canonical worktree, refuses: "
   f"rc={proc_cli8027.returncode} stderr={proc_cli8027.stderr!r}")
ok("not the canonical" in proc_cli8027.stderr,
   f"L-spec-8027 AC6: and names why on stderr: {proc_cli8027.stderr!r}")
print("L-spec-8027 AC6 ok")


print(f"install_sync: {N} checks pass")
