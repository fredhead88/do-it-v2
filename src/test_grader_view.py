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
# L-spec-0481/R12.2 (minor deviation, declared — see the output card): build()
# now ALWAYS inserts one static grading.env-sourcing line right after the
# shebang (AC9) and rewrites any `REPO`-absolute path (AC6) — this fixture's
# own verify script names no such path, so only the inserted line differs;
# the rest of the body is still byte-identical to the input. L-spec-8027/R8.5
# extends that SAME one line with an unconditional HOME/DOIT_ROOT export
# (AC9 of THIS spec) — still one line, still right after the shebang.
orig = (FIXTURE_ROOT / "content" / f"verify-{SPEC}-grader.sh").read_text()
got = verify_sh.read_text()
orig_shebang, orig_rest = orig.split("\n", 1)
got_lines = got.split("\n")
assert got_lines[0] == orig_shebang, got_lines[0]
assert got_lines[1] == (f'[ -s "{view}/grading.env" ] && . "{view}/grading.env"; '
                        f'export HOME="{view}/home" DOIT_ROOT="{view}/.doit"'), got_lines[1]
assert "\n".join(got_lines[2:]) == orig_rest, got
assert (view / "home").is_dir(), "L-spec-8027 AC9: build() creates <view>/home"
assert (view / ".doit").is_dir(), "L-spec-8027 AC9: build() creates <view>/.doit"
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
    "/run/systemd/resolve",  # DNS: /etc/resolv.conf links here (read-only)
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


# ── L-spec-0481 AC9 · bwrap_argv(..., binds=...) renders ro, never rw ───────
extra_src = str(view / "extra-bind-src")
pathlib.Path(extra_src).mkdir(parents=True, exist_ok=True)
argv_binds = grader_view.bwrap_argv(view, doit_src=DOIT_SRC, venvs=(), binds=[(extra_src, extra_src)])
bind_flags = [t[0] for t in bind_triples(argv_binds) if t[1] == extra_src]
assert bind_flags == ["--ro-bind"], bind_flags
print("L-spec-0481 AC9 ok")


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


# ── L-spec-0481 AC7 · view-paths rewrite: a REPO-absolute path present in the
# archived tree is rewritten; /usr/bin/python3 and the project's own venv
# python stay byte-identical; a symlinked `repo` argument naming the resolved
# target still rewrites; a checkout path missing from view/tree (and a
# foreign /tmp path) are left un-rewritten and fail view-paths ─────────────
AC7_REPO = pathlib.Path(tempfile.mkdtemp(prefix="grader-view-ac7-repo-"))
(AC7_REPO / "x").write_text("echo hi\n")
(AC7_REPO / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
(AC7_REPO / ".venv" / "bin" / "python").write_text("#!/bin/sh\necho fixture-python\n")
(AC7_REPO / ".venv" / "bin" / "python").chmod(0o755)
subprocess.run(["git", "init", "-q", str(AC7_REPO)], check=True)
subprocess.run(["git", "-C", str(AC7_REPO), "config", "user.email", "t@example.com"], check=True)
subprocess.run(["git", "-C", str(AC7_REPO), "config", "user.name", "t"], check=True)
subprocess.run(["git", "-C", str(AC7_REPO), "add", "-A"], check=True)
subprocess.run(["git", "-C", str(AC7_REPO), "commit", "-q", "-m", "fixture"], check=True)
AC7_SHA = subprocess.run(["git", "-C", str(AC7_REPO), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
AC7_PROJECT = AC7_REPO.name
(FIXTURE_ROOT / "grading.toml").write_text(f'[project.{AC7_PROJECT}]\nvenv = ".venv"\n')
os.environ["DOIT_GRADING_TOML"] = str(FIXTURE_ROOT / "grading.toml")

# (i)/(ii): a checkout path present in the tree rewrites; /usr/bin/python3 and
# the venv python (present but never in the tree) do not.
AC7_SPEC = "L-spec-9428"
(FIXTURE_ROOT / "content" / f"{AC7_SPEC}.md").write_text("# fixture\n")
ac7_verify = f"#!/usr/bin/env bash\nset -euo pipefail\n{AC7_REPO}/x /usr/bin/python3 {AC7_REPO}/.venv/bin/python\n"
(FIXTURE_ROOT / "content" / f"verify-{AC7_SPEC}-grader.sh").write_text(ac7_verify)
ac7_view = grader_view.build(AC7_SPEC, AC7_REPO, AC7_SHA, AC7_SHA, SPAWN + "-ac7")
ac7_text = (ac7_view / "verify.sh").read_text()
assert f"{ac7_view}/tree/x" in ac7_text, ac7_text
assert "/usr/bin/python3" in ac7_text, ac7_text
assert f"{AC7_REPO}/.venv/bin/python" in ac7_text, ac7_text
if shutil.which("bwrap"):
    binds = [(str(AC7_REPO / ".venv"), str(AC7_REPO / ".venv"))]
    wrap = grader_view.bwrap_argv(ac7_view, doit_src=REPO, binds=binds)
    r = subprocess.run(wrap + ["--", str(AC7_REPO / ".venv" / "bin" / "python")],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "fixture-python" in r.stdout, r
else:
    print("SKIP bwrap")  # never a pass
print("L-spec-0481 AC7 ok (i,ii)")

# (iii): repo argument is a symlink (ROOT/repos/<project> form); verify names
# the RESOLVED target — the same rewrite still fires.
AC7_LINK_DIR = FIXTURE_ROOT / "repos"
AC7_LINK_DIR.mkdir(parents=True, exist_ok=True)
AC7_LINK = AC7_LINK_DIR / AC7_PROJECT
if AC7_LINK.exists() or AC7_LINK.is_symlink():
    AC7_LINK.unlink()
AC7_LINK.symlink_to(AC7_REPO)
AC7_SPEC2 = "L-spec-9429"
(FIXTURE_ROOT / "content" / f"{AC7_SPEC2}.md").write_text("# fixture\n")
ac7b_verify = f"#!/usr/bin/env bash\nset -euo pipefail\n{AC7_REPO.resolve()}/x\n"
(FIXTURE_ROOT / "content" / f"verify-{AC7_SPEC2}-grader.sh").write_text(ac7b_verify)
ac7b_view = grader_view.build(AC7_SPEC2, AC7_LINK, AC7_SHA, AC7_SHA, SPAWN + "-ac7b")
ac7b_text = (ac7b_view / "verify.sh").read_text()
assert f"{ac7b_view}/tree/x" in ac7b_text, ac7b_text
print("L-spec-0481 AC7 ok (iii)")

# (iv): a checkout path missing from view/tree, and /tmp/foreign, are both
# left un-rewritten and both fail `_prove_view_paths`.
AC7_SPEC3 = "L-spec-9430"
(FIXTURE_ROOT / "content" / f"{AC7_SPEC3}.md").write_text("# fixture\n")
ac7c_verify = f"#!/usr/bin/env bash\nset -euo pipefail\n{AC7_REPO}/does-not-exist /tmp/foreign\n"
(FIXTURE_ROOT / "content" / f"verify-{AC7_SPEC3}-grader.sh").write_text(ac7c_verify)
ac7c_view = grader_view.build(AC7_SPEC3, AC7_REPO, AC7_SHA, AC7_SHA, SPAWN + "-ac7c")
ac7c_text = (ac7c_view / "verify.sh").read_text()
assert f"{AC7_REPO}/does-not-exist" in ac7c_text, ac7c_text
assert "/tmp/foreign" in ac7c_text, ac7c_text
import grading_env as _ge
ok7c, reason7c = _ge._prove_view_paths(ac7c_view)
assert ok7c is False, (ok7c, reason7c)
assert "does-not-exist" in reason7c or "foreign" in reason7c, reason7c
shutil.rmtree(AC7_REPO, ignore_errors=True)
print("L-spec-0481 AC7 ok (iv)")


# ── L-spec-0653/0667 · view-paths false positives fixed 2026-10-05 ─────────
# (v) a PYTHONPATH-style colon list of two checkout-absolute paths in one
#     token rewrites each element — the bug 752c2ba fixed, left uncovered.
# (vi) a checkout-absolute path immediately followed by shell syntax the path
#     regex does not stop at (`$(wc -l < PATH)PATH)"`) still rewrites, with
#     the trailing `)` reattached outside the rewritten path — the bug this
#     session fixed (L-spec-0667's grader preflight refusal).
AC_PUNCT_REPO = pathlib.Path(tempfile.mkdtemp(prefix="grader-view-punct-repo-"))
(AC_PUNCT_REPO / "api" / "lib").mkdir(parents=True, exist_ok=True)
(AC_PUNCT_REPO / "api" / "lib" / "cross_validation.py").write_text("x = 1\n")
subprocess.run(["git", "init", "-q", str(AC_PUNCT_REPO)], check=True)
subprocess.run(["git", "-C", str(AC_PUNCT_REPO), "config", "user.email", "t@example.com"], check=True)
subprocess.run(["git", "-C", str(AC_PUNCT_REPO), "config", "user.name", "t"], check=True)
subprocess.run(["git", "-C", str(AC_PUNCT_REPO), "add", "-A"], check=True)
subprocess.run(["git", "-C", str(AC_PUNCT_REPO), "commit", "-q", "-m", "fixture"], check=True)
AC_PUNCT_SHA = subprocess.run(["git", "-C", str(AC_PUNCT_REPO), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()

AC_COLON_SPEC = "L-spec-9432"
(FIXTURE_ROOT / "content" / f"{AC_COLON_SPEC}.md").write_text("# fixture\n")
colon_verify = (f"#!/usr/bin/env bash\nset -euo pipefail\n"
                f"PYTHONPATH={AC_PUNCT_REPO}:{AC_PUNCT_REPO}/api echo hi\n")
(FIXTURE_ROOT / "content" / f"verify-{AC_COLON_SPEC}-grader.sh").write_text(colon_verify)
colon_view = grader_view.build(AC_COLON_SPEC, AC_PUNCT_REPO, AC_PUNCT_SHA, AC_PUNCT_SHA, SPAWN + "-colon")
colon_text = (colon_view / "verify.sh").read_text()
assert f"PYTHONPATH={colon_view}/tree:{colon_view}/tree/api" in colon_text, colon_text
ok_colon, reason_colon = _ge._prove_view_paths(colon_view)
assert ok_colon is True, (ok_colon, reason_colon)
print("L-spec-0653 AC(colon-list) ok")

AC_PAREN_SPEC = "L-spec-9433"
(FIXTURE_ROOT / "content" / f"{AC_PAREN_SPEC}.md").write_text("# fixture\n")
paren_verify = (f'#!/usr/bin/env bash\nset -euo pipefail\n'
                f'test "$(wc -l < {AC_PUNCT_REPO}/api/lib/cross_validation.py)" -le 5 && echo VERIFY_OK\n')
(FIXTURE_ROOT / "content" / f"verify-{AC_PAREN_SPEC}-grader.sh").write_text(paren_verify)
paren_view = grader_view.build(AC_PAREN_SPEC, AC_PUNCT_REPO, AC_PUNCT_SHA, AC_PUNCT_SHA, SPAWN + "-paren")
paren_text = (paren_view / "verify.sh").read_text()
assert f"{paren_view}/tree/api/lib/cross_validation.py)" in paren_text, paren_text
# the path itself, without the trailing shell paren, must have actually moved into the tree
assert (paren_view / "tree" / "api" / "lib" / "cross_validation.py").is_file()
ok_paren, reason_paren = _ge._prove_view_paths(paren_view)
assert ok_paren is True, (ok_paren, reason_paren)
print("L-spec-0667 AC(trailing-closer) ok")

# A balanced bracket in a path (a route dir like `[name]`) is never trimmed.
assert grader_view._trim_unmatched_closer("/x/[name]") == "/x/[name]"
assert grader_view._trim_unmatched_closer("/x/cross_validation.py)") == "/x/cross_validation.py"
assert grader_view._trim_unmatched_closer("/x/file(2).txt") == "/x/file(2).txt"
print("L-spec-0667 _trim_unmatched_closer ok")

shutil.rmtree(AC_PUNCT_REPO, ignore_errors=True)


# ── L-spec-8027 AC9 · verify.sh always sees the VIEW's own HOME/DOIT_ROOT — ──
# ── the invoker's, and NOT with a grading.env present either ────────────────
AC9_8027_SPEC = "L-spec-9431"
(FIXTURE_ROOT / "content" / f"{AC9_8027_SPEC}.md").write_text("# fixture\n")
ac9_8027_verify = '#!/usr/bin/env bash\nset -euo pipefail\necho "HOME=$HOME"\necho "DOIT_ROOT=$DOIT_ROOT"\n'
(FIXTURE_ROOT / "content" / f"verify-{AC9_8027_SPEC}-grader.sh").write_text(ac9_8027_verify)
ac9_8027_view = grader_view.build(AC9_8027_SPEC, REPO, BASE_SHA, READY_SHA, SPAWN + "-8027ac9")
assert (ac9_8027_view / "home").is_dir(), "L-spec-8027 AC9: build() creates <view>/home"
assert (ac9_8027_view / ".doit").is_dir(), "L-spec-8027 AC9: build() creates <view>/.doit"

CALLER_ENV_8027 = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "HOME": "/not-the-view/caller-home", "DOIT_ROOT": "/not-the-view/caller-root"}

# (a) no grading.env present at all
assert not (ac9_8027_view / "grading.env").exists()
r9a = subprocess.run([str(ac9_8027_view / "verify.sh")], capture_output=True, text=True,
                     env=CALLER_ENV_8027, check=True)
assert f"HOME={ac9_8027_view}/home" in r9a.stdout.splitlines(), r9a.stdout
assert f"DOIT_ROOT={ac9_8027_view}/.doit" in r9a.stdout.splitlines(), r9a.stdout
print("L-spec-8027 AC9 ok (a: no grading.env)")

# (b) a grading.env present, itself setting DIFFERENT HOME/DOIT_ROOT values —
# the unconditional export runs AFTER the conditional source, so it still wins
(ac9_8027_view / "grading.env").write_text(
    'export HOME="/from-grading-env/home"\nexport DOIT_ROOT="/from-grading-env/root"\n')
r9b = subprocess.run([str(ac9_8027_view / "verify.sh")], capture_output=True, text=True,
                     env=CALLER_ENV_8027, check=True)
assert f"HOME={ac9_8027_view}/home" in r9b.stdout.splitlines(), r9b.stdout
assert f"DOIT_ROOT={ac9_8027_view}/.doit" in r9b.stdout.splitlines(), r9b.stdout
print("L-spec-8027 AC9 ok (b: grading.env present, still overridden)")


shutil.rmtree(FIXTURE_ROOT, ignore_errors=True)
shutil.rmtree(FIXTURE_SCRATCH, ignore_errors=True)
shutil.rmtree(FIXTURE_HOME, ignore_errors=True)
print("ALL OK")
