#!/usr/bin/env python3
"""One runnable check on grader_view.py's AC1-AC6. Run: python3 test_grader_view.py

Before importing `grader_view`, this sets DOIT_ROOT, DOIT_SCRATCH and HOME to
fresh temp-directory fixtures — never the live `$R/content`, never the real
`~/.claude/.credentials.json`, and never the live shared `grader-claude`
config dir (Boundaries) — and writes a fixture `content/<spec>.md` and
`content/verify-<spec>-grader.sh` under the fixture DOIT_ROOT, plus a fixture
`.claude/.credentials.json` under the fixture HOME. `git archive`/`git diff`
still target THIS real do-it-v2 checkout as `repo` (Assumption 8) — only the
spec/verify/credentials/scratch side of the test is fixture-isolated.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent  # this checkout's own src/
REPO = HERE.parent  # this checkout's own root — the real `repo` for AC1/AC5

REAL_HOME = os.environ.get("HOME", "")  # captured BEFORE the override below

FIXTURE_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="grader-view-root-"))
FIXTURE_SCRATCH = pathlib.Path(tempfile.mkdtemp(prefix="grader-view-scratch-"))
FIXTURE_HOME = pathlib.Path(tempfile.mkdtemp(prefix="grader-view-home-"))

os.environ["DOIT_ROOT"] = str(FIXTURE_ROOT)
os.environ["DOIT_SCRATCH"] = str(FIXTURE_SCRATCH)
os.environ["HOME"] = str(FIXTURE_HOME)

SPEC = "L-spec-9426"

(FIXTURE_ROOT / "content").mkdir(parents=True, exist_ok=True)
(FIXTURE_ROOT / "content" / f"{SPEC}.md").write_text("# fixture spec\n\nnothing real.\n")

# The fixture's own verify.sh: prints a marker line, THEN fails — exercising
# AC5's "started despite a later non-zero exit" case (the advisory note about
# a real grader verify script failing inside this sandbox as built today).
VERIFY_FIXTURE = "#!/usr/bin/env bash\nset -euo pipefail\necho SMOKE_MARKER\nfalse\n"
(FIXTURE_ROOT / "content" / f"verify-{SPEC}-grader.sh").write_text(VERIFY_FIXTURE)

# A real, minimal git repo at FIXTURE_ROOT so AC5's OUTSIDE-sandbox baseline
# (`git -C $DOIT_ROOT log -1`) has something to succeed against.
subprocess.run(["git", "init", "-q", str(FIXTURE_ROOT)], check=True)
subprocess.run(["git", "-C", str(FIXTURE_ROOT), "config", "user.email", "t@example.com"], check=True)
subprocess.run(["git", "-C", str(FIXTURE_ROOT), "config", "user.name", "t"], check=True)
subprocess.run(["git", "-C", str(FIXTURE_ROOT), "add", "-A"], check=True)
subprocess.run(["git", "-C", str(FIXTURE_ROOT), "commit", "-q", "-m", "fixture"], check=True)

(FIXTURE_HOME / ".claude").mkdir(parents=True, exist_ok=True)
CREDS_FIXTURE = json.dumps({"token": "fixture-not-a-real-secret"})
(FIXTURE_HOME / ".claude" / ".credentials.json").write_text(CREDS_FIXTURE)
(FIXTURE_HOME / ".claude" / ".credentials.json").chmod(0o600)

sys.path.insert(0, str(HERE))
import grader_view  # noqa: E402


def snapshot(*roots):
    """path -> mtime_ns for every file under each root (AC6's before/after)."""
    out = {}
    for root in roots:
        for p in pathlib.Path(root).rglob("*"):
            if p.is_file():
                out[str(p)] = p.stat().st_mtime_ns
    return out


def bind_triples(argv):
    """(flag, src, dst) for every --bind/--ro-bind pair — never scanned as one
    flat substring (fix-list item 3)."""
    out, i = [], 0
    while i < len(argv):
        if argv[i] in ("--bind", "--ro-bind"):
            out.append((argv[i], argv[i + 1], argv[i + 2]))
            i += 3
        else:
            i += 1
    return out


def under(path, root):
    try:
        pathlib.Path(path).resolve().relative_to(pathlib.Path(root).resolve())
        return True
    except ValueError:
        return False


BASE_SHA = subprocess.run(
    ["git", "-C", str(REPO), "rev-parse", "HEAD~1"], check=True, capture_output=True, text=True
).stdout.strip()
READY_SHA = subprocess.run(
    ["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
).stdout.strip()
SPAWN = "L-builder-9426-selftest"


# ── AC1 · build() populates the exact layout, no .git anywhere under tree/ ──
view = grader_view.build(SPEC, REPO, BASE_SHA, READY_SHA, SPAWN)
assert view.is_dir(), view
assert (view / "tree").is_dir()
found_git = subprocess.run(
    ["find", str(view / "tree"), "-name", ".git"], capture_output=True, text=True
).stdout.strip()
assert found_git == "", found_git
expect_diff = subprocess.run(
    ["git", "-C", str(REPO), "diff", f"{BASE_SHA}..{READY_SHA}"], check=True, stdout=subprocess.PIPE
).stdout
assert (view / "diff.patch").read_bytes() == expect_diff
assert (view / "spec.md").read_bytes() == (FIXTURE_ROOT / "content" / f"{SPEC}.md").read_bytes()
verify_sh = view / "verify.sh"
assert verify_sh.read_bytes() == (FIXTURE_ROOT / "content" / f"verify-{SPEC}-grader.sh").read_bytes()
assert verify_sh.stat().st_mode & 0o111, oct(verify_sh.stat().st_mode)
assert (view / "seat").is_dir()
assert (view / "view.json").is_file()
print("AC1 ok")


# ── AC2 · view.json's keys are exactly the five; build() raises on a missing
# spec instead of writing a placeholder ─────────────────────────────────────
keys = set(json.loads((view / "view.json").read_text()).keys())
assert keys == {"spec", "base_sha", "ready_sha", "spawn", "built_at"}, keys
raised = False
try:
    grader_view.build("L-spec-0000000-does-not-exist", REPO, BASE_SHA, READY_SHA, "nope")
except FileNotFoundError:
    raised = True
assert raised
assert not (grader_view._scratch_sub("grade") / "nope").exists()
print("AC2 ok")


# ── AC3 · config_dir(): agents/grader.md symlink, credentials copy mode 0600,
# idempotent — a sentinel dropped into projects/ between two calls survives ──
cfg1 = grader_view.config_dir()
link = cfg1 / "agents" / "grader.md"
assert link.is_symlink(), link
assert link.resolve() == (REPO / "agents" / "grader.md").resolve()
creds = cfg1 / ".credentials.json"
assert creds.is_file()
assert creds.read_text() == CREDS_FIXTURE
assert oct(creds.stat().st_mode & 0o777) == "0o600", oct(creds.stat().st_mode)

sentinel_dir = cfg1 / "projects"
sentinel_dir.mkdir(parents=True, exist_ok=True)
sentinel = sentinel_dir / "sentinel.txt"
sentinel.write_text("still here\n")

cfg2 = grader_view.config_dir()
assert cfg2 == cfg1
assert sentinel.is_file() and sentinel.read_text() == "still here\n"
print("AC3 ok")


# ── AC4 · bwrap_argv()'s bind allowlist ─────────────────────────────────────
DOIT_SRC = REPO
argv = grader_view.bwrap_argv(view, doit_src=DOIT_SRC, venvs=())
triples = bind_triples(argv)
assert triples, "no bind triples found"

cfg_dir_path = grader_view.config_dir()
claude_launcher, claude_resolved = grader_view._claude_launcher()
allow_exact = {
    "/usr", "/bin", "/lib", "/lib64", "/etc",
    str(DOIT_SRC / "src"), str(DOIT_SRC / "agents"),
    str(DOIT_SRC / "scripts"), str(DOIT_SRC / "doit"),
    claude_launcher, claude_resolved,
}

seen_allow = set()
for flag, src, dst in triples:
    assert src != REAL_HOME, (flag, src, dst)
    assert src != str(FIXTURE_ROOT), (flag, src, dst)  # $DOIT_ROOT, current value
    assert not src.startswith(str(FIXTURE_ROOT) + "/"), (flag, src, dst)  # $R/
    assert not src.startswith(str(FIXTURE_HOME) + "/.claude/"), (flag, src, dst)
    assert not (REAL_HOME and src.startswith(REAL_HOME + "/.claude/")), (flag, src, dst)
    assert not src.endswith(".git"), (flag, src, dst)
    if under(src, view) or under(src, cfg_dir_path):
        continue
    assert src in allow_exact, (flag, src, dst)
    seen_allow.add(src)

for must in allow_exact:
    assert must in seen_allow, must

srcs = {t[1] for t in triples}
assert str(view) in srcs
assert str(cfg_dir_path) in srcs

assert "--dev" in argv and argv[argv.index("--dev") + 1] == "/dev"
assert "--proc" in argv and argv[argv.index("--proc") + 1] == "/proc"
assert "--tmpfs" in argv and argv[argv.index("--tmpfs") + 1] == "/tmp"
print("AC4 ok")


# ── AC5 · smoke() — real bwrap, discriminating checks ───────────────────────
outside_doit_src = subprocess.run(["git", "-C", str(REPO), "log", "-1"], capture_output=True, text=True)
outside_doit_root = subprocess.run(
    ["git", "-C", str(FIXTURE_ROOT), "log", "-1"], capture_output=True, text=True
)
assert outside_doit_src.returncode == 0, outside_doit_src.stderr
assert outside_doit_root.returncode == 0, outside_doit_root.stderr

smoke_result = grader_view.smoke(view)
skipped = smoke_result.get("skipped", [])
for key in ("card_path_unreadable", "git_unreachable", "verify_started", "claude_version_ok"):
    if key in skipped:
        print(f"SKIP: {key} (unavailable in this environment)")
        continue
    assert smoke_result[key] is True, (key, smoke_result)
print("AC5 ok")


# ── AC6 · no ledger event; writes confined to the two scratch subtrees ──────
before = snapshot(FIXTURE_ROOT, FIXTURE_SCRATCH, FIXTURE_HOME)
grader_view.build(SPEC, REPO, BASE_SHA, READY_SHA, SPAWN + "-ac6")
grader_view.config_dir()
after = snapshot(FIXTURE_ROOT, FIXTURE_SCRATCH, FIXTURE_HOME)

new_paths = set(after) - set(before)
changed_mtime = {p for p in before if p in after and after[p] != before[p]}

grade_root, claude_root = str(FIXTURE_SCRATCH / "grade"), str(FIXTURE_SCRATCH / "grader-claude")
for p in new_paths | changed_mtime:
    assert p.startswith(grade_root + os.sep) or p.startswith(claude_root + os.sep), p

# Cheap first pass only (fix-list item 6), not the proof (the snapshot above is):
src_text = (HERE / "grader_view.py").read_text()
assert "fold" not in src_text and "import carry" not in src_text
assert not (FIXTURE_ROOT / "events").exists()
print("AC6 ok")


shutil.rmtree(FIXTURE_ROOT, ignore_errors=True)
shutil.rmtree(FIXTURE_SCRATCH, ignore_errors=True)
shutil.rmtree(FIXTURE_HOME, ignore_errors=True)
print("ALL OK")
