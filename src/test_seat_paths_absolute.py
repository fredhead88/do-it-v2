"""Thinker 2026-10-07 — seat-script-relative-path (22 lessons, 09-27..10-05).

Serving panes start in the PROJECT repo (e.g. /opt/albert-scott), not in the
do-it-v2 checkout, so a prompt or role contract that says `scripts/seat/claim.sh`
or `scripts/sweep/ro-sql` relative resolves to nothing there (exit 127) and every
seat paid one wasted lookup. Every place a server is told to RUN one of these
scripts must name it absolutely, under this checkout.
"""
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ("scripts/seat/claim.sh", "scripts/sweep/ro-sql", "scripts/sweep/droplet-read")
SURFACES = [ROOT / "scripts" / "seat" / "README.md", *sorted((ROOT / "agents").glob("*.md"))]
# A run instruction: the script path at the start of a backtick span or after "run ".
RUN_RE = re.compile(r"(?:`|\brun\s+)(?P<path>[^\s`]*(?:%s))" % "|".join(re.escape(s) for s in SCRIPTS))

fails = []
for f in SURFACES:
    for n, line in enumerate(f.read_text().splitlines(), 1):
        for m in RUN_RE.finditer(line):
            if not m.group("path").startswith("/"):
                fails.append(f"{f.relative_to(ROOT)}:{n}: relative {m.group('path')!r}")

if fails:
    print("FAIL: seat/sweep scripts named relative to an unknown cwd:")
    print("\n".join(fails))
    sys.exit(1)

readme = (ROOT / "scripts" / "seat" / "README.md").read_text()
assert f"run {ROOT}/scripts/seat/claim.sh <id>" in readme or "run /" in readme, \
    "the serving-pattern prompt must give claim.sh an absolute path"
print(f"seat paths absolute: {len(SURFACES)} surfaces checked, ALL OK")
