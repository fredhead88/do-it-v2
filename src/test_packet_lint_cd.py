#!/usr/bin/env python3
"""Table-driven PL-010 regressions; all paths are strings, never live reads."""
import builtins, os, pathlib, socket, subprocess, sys, tempfile
from unittest.mock import patch

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="packet-lint-cd-"))
os.environ["HOME"] = str(_TMP / "home")
os.environ["DOIT_ROOT"] = str(_TMP / "root")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import packet_lint

TARGETS = ("/opt/albert-scott", "/opt/albert-scott/dashboard", "/home/albert/do-it-v2",
           "/home/albert/do-it-v2/src", "/home/albert/.do-it/repos/do-it-v2",
           "/home/albert/other-repo", "~/.do-it/repos/do-it-v2",
           "$HOME/.do-it/repos/do-it-v2", "$R/repos/albert-scott",
           "${DOIT_ROOT:-$HOME/.do-it}/repos/albert-scott")
COMMANDS = ("cd {}", "pushd {}", "git -C {} status", "make -C {} test",
            "tar -C {} -tf archive", "tool --root {}", "tool --root={}")
PORTABLE = ('cd "$(git rev-parse --show-toplevel)"',
            'cd "$(git rev-parse --show-toplevel)/dashboard"',
            'cd "$(git rev-parse --show-toplevel)/api"',
            "cd dashboard", "cd ..", "cd src", 'cd "$(mktemp -d)"',
            'cd "$d"', 'cd "$SCRATCH"', "cd /tmp/x", "cd /var/tmp/x",
            '/home/albert/do-it-v2/scripts/sweep/ro-sql "select 1"',
            "/usr/bin/test -f /opt/albert-scott/README.md", "grep -r x /opt/albert-scott/src",
            "ls -C /opt/albert-scott", "grep -C 3 x file", "diff -C 3 a b",
            "abcd /opt/x", 'echo "cd /opt/x"', 'echo "--root=/opt/x"')


def fixture(command="/usr/bin/true", acceptance="AC1 [backend]: works.\nreview_path: do true",
            heading="## Acceptance", repo="repo: do-it-v2\n"):
    return (repo + "Consumes: /opt/albert-scott\n## Verification\n```bash\n" + command
            + "\n```\n" + heading + "\n" + acceptance + "\nWrites: src/x.py\n")


CASES = []
for target in TARGETS:
    for command in COMMANDS:
        for quote in ("", "'", '"'):
            for prefix in ("", "/usr/bin/true && "):
                CASES.append((fixture(prefix + command.format(quote + target + quote)), target))
for command in PORTABLE:
    CASES.append((fixture(command), None))
for command in ('bash -c "cd /opt/albert-scott && x"', "git -C/opt/albert-scott status",
                "cd /tmp/../opt/albert-scott", "cd /srv/fixed", "true; cd /srv/fixed",
                "true\ncd /srv/fixed", "env X=y git -C /srv/fixed status"):
    CASES.append((fixture(command), "/opt/albert-scott" if "albert-scott" in command else "/srv/fixed"))
for heading in ("## Acceptance", "### Acceptance Criteria", "## Other"):
    for marker in (" review_path: ", "\nreview_path:\n"):
        for command, expected in [("cd /opt/albert-scott && /usr/bin/python3 -m pytest", "/opt/albert-scott"),
                                  ('cd "$(git rev-parse --show-toplevel)" && /usr/bin/python3 src/test_x.py', None)]:
            for wrap in ("{}", "`{}`"):
                ac = "AC1 [backend]: mentions /opt/albert-scott." + marker + "go to checkout / do " + wrap.format(command)
                CASES.append((fixture(acceptance=ac, heading=heading), expected))
        CASES.append((fixture(acceptance="AC1 [backend]: cd /opt/albert-scott is described only." + marker + "do true",
                              heading=heading), None))
CASES += [(fixture("cd /opt/albert-scott", repo=""), "/opt/albert-scott"),
          (fixture(acceptance="AC1 [backend]: fine.\nreview_path: do true\n## Notes\ncd /opt/albert-scott"), None),
          (fixture(acceptance="AC1 [backend]: fine.\nreview_path: do true\nAC2 [backend]: fine.\nreview_path: do cd /srv/fixed"), "/srv/fixed")]


def forbidden(*args, **kwargs):
    raise AssertionError("lint must never execute or access a target")


real_open = builtins.open
real_read_text = pathlib.Path.read_text


def checklist_only(path, *args, **kwargs):
    assert path == packet_lint.CHECKLIST, "only the checklist may be read"
    return real_read_text(path, *args, **kwargs)



def read_only(file, mode="r", *args, **kwargs):
    assert not any(c in mode for c in "wax+"), "lint must never write"
    return real_open(file, mode, *args, **kwargs)


with patch.object(subprocess, "run", forbidden), patch.object(subprocess, "Popen", forbidden), \
     patch.object(os, "system", forbidden), patch.object(socket, "socket", forbidden), \
     patch.object(builtins, "open", read_only), patch.object(pathlib.Path, "resolve", forbidden), \
     patch.object(pathlib.Path, "exists", forbidden), \
     patch.object(pathlib.Path, "read_text", checklist_only):
    for text, expected in CASES:
        found = [f for f in packet_lint.run(text, packet_lint.CHECKLIST, "spec") if f["id"] == "PL-010"]
        assert bool(found) == (expected is not None), (text, found, expected)
        if found:
            assert len(found) == 1 and found[0]["severity"] == "block", found
            assert found[0]["text"].startswith("PL-010: ") and expected in found[0]["text"], found
    text = fixture("cd /srv/first", "AC1 [backend]: fine. review_path: do cd /srv/second")
    found = [f for f in packet_lint.run(text, packet_lint.CHECKLIST, "spec") if f["id"] == "PL-010"]
    assert len(found) == 1 and "/srv/first" in found[0]["text"], found
print(f"packet_lint_cd: {len(CASES) + 1} fixture checks pass")
