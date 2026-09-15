#!/usr/bin/env python3
"""Round-one spec-writer slot packet (pilot S6: the Planner's own packet, hand-built).
usage: mkslot.py <charter> <unit-name> <spec-id> <out>"""
import pathlib, re, sys, subprocess
R = pathlib.Path.home() / ".do-it"
cid, unit, sid, out = sys.argv[1], sys.argv[2], sys.argv[3], pathlib.Path(sys.argv[4])
ch = (R / "content" / f"{cid}.md").read_text()
cut = (R / "content" / f"cut-{cid}.md").read_text()
plan = (R / "content" / f"plan-{cid}.md").read_text()
def sec(text, title_re):
    m = re.search(rf"^## {title_re}[^\n]*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return m.group(1).strip("\n")
blocks = {m.group(1): m.group(0) for m in re.finditer(r"^## (\S+)\n(?:(?!^## ).*\n?)*", cut, re.M)}
ub = blocks[unit]
delivers = re.search(r"^Delivers: (.*)$", ub, re.M).group(1).split(", ")
footprint = re.search(r"^Footprint: (.*)$", ub, re.M).group(1)
goal = re.search(r"^Goal: (.*)$", ub, re.M).group(1)
wave = re.search(r"^Wave: (.*)$", ub, re.M).group(1)
consumes = re.search(r"^Consumes:(.*)$", ub, re.M).group(1).strip() or "nothing"
produces = re.search(r"^Produces:(.*)$", ub, re.M).group(1).strip() or "nothing"
reqs = [l for l in sec(ch, r"2\.").splitlines() if any(l.startswith(f"- {r}:") for r in delivers)]
sib = [(n, re.search(r"^Produces:(.*)$", b, re.M).group(1).strip()) for n, b in blocks.items() if n != unit]
sib_lines = [f"- `{n}` produces: {p or 'nothing'}" for n, p in sib]
head = subprocess.run(["git", "-C", str(R / "repos" / "albert-scott"), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
env = {"flag-detail-on-the-health-surface":
       "a builder alone in a worktree of this repository can run `PYTHONPATH=. /opt/albert-scott/.venv/bin/pytest tests/health/<file> tests/test_482_analyzers.py -q` (no `.venv` inside the worktree — use the absolute interpreter), run `ruff check`, run `wc -l scripts/health/process_health/analyzers.py` (the spec-548 ratchet: the file is 1,496 lines with a hard cap of 1,500 and the pre-commit guard blocks a commit that grows it past the cap), render the process-health digest on a fixture liveness dir, and read git history. It cannot observe the live `~/.claude/ledger/liveness/` surface changing (that is the reaper's, the other unit) — R2's surface half is proven on fixtures.",
       "reaper-three-way-lock-test":
       "a builder alone in a worktree of this repository can run `PYTHONPATH=. /opt/albert-scott/.venv/bin/pytest tests/test_611_liveness_reaper.py -q` (absolute interpreter; the test is hermetic via LIVENESS_DIR/LANE_DIR/REAP_* env seams and a fake HOME), run `bash -n` and `shellcheck` on the script, run the reaper `--dry-run` against a fixture dir, chmod fixtures to 0444/0000 as uid 1000 (non-root), and read git history. It can READ the live lock `~/.claude/ledger/liveness/.grading-957-marketplace-scoped-ads-profiles-fanout.lock` (owner yitzy, mode 664, mtime 2026-09-09T11:03:12Z) but must not run the real reaper against the live liveness dir and must not touch anything under /home/yitzy. R4 is therefore an owed `observed-data` criterion (wake_at 2026-09-16T11:04:00Z, the moment the lock passes REAP_INFRA_ALARM_MAX_AGE_SECS), proven by the reviewer's run on merged code after that instant."}
parts = [f"## Plan slot · unit `{unit}` · charter {cid} · wave {wave} of 1", "",
 "1. Charter extract — the requirement ids this slot covers, and the constraints and product decisions that bind it, verbatim:", "",
 f"Requirements delivered by this unit: {', '.join(delivers)}.", ""] + reqs + ["",
 "Constraints and product decisions (charter §3), verbatim:", sec(ch, r"3\."), "",
 "2. The plan slot:",
 f"- Unit: `{unit}`", f"- Goal: {goal}", f"- Footprint (= the merge grant, `Writes:`): {footprint}", f"- Wave: {wave}. Seams: Consumes {consumes}; Produces {produces}.",
 "- The Plan's Seams and Shared decisions this spec must honour, verbatim:", "", "### Seams", sec(plan, "Seams"), "", "### Shared decisions", sec(plan, "Shared decisions"), "",
 "- Repro class: `reproducible` — see the Plan's Research findings (measured on this box 2026-09-15): " + ("the 957 lock is unopenable for write and unheld; the reaper today never lists it." if unit.startswith("reaper") else "`_liveness_findings` renders a fixed detail string and reads no `detail:` line; a fixture flag with a `detail:` line renders without it."),
 "", f"3. Read access to the repository: your cwd is `/home/albert/.do-it/repos/albert-scott` (main branch `master`, HEAD `{head}`). Read-only.",
 "", f"4. Builder capability envelope: {env[unit]}",
 "", "5. Cost-path inventory: none. Nothing in this footprint fires a paid external call.",
 "", "6. Sibling units' `Produces:`:"] + sib_lines + ["",
 "7. Acquisition ADRs for this footprint: none. Architecture ADR binding the footprint: `L-adr-0005` (the flag-body `detail:` line grammar, Plan SD1).",
 "", "8. Probe residue: none — no probe was commissioned; the Plan's Research findings are the measurements.",
 "", f"9. Write the spec to: `/home/albert/.do-it/content/{sid}.md`. That path is the ONLY file you may write.",
 "", f"10. The template: the eleven slots of your contract. Every acceptance criterion is `AC<n> [type]:` with type from `ui` · `backend` · `observed-data` · `financial`, and carries a `review_path` (log in as / go to / do / worked if / failed if). Name the charter in the spec header as `charter: {cid}` and cite `Writes:` exactly as the footprint above (a bare space-separated list of paths on one line, no prose in the list). **The spec is at most 400 lines** — a longer spec is the cost the pilot measured (S29); say less, cite more. Its Verification block (§9) is turned verbatim into the checker script: one `&&` chain, absolute interpreter, no bare `;` or `||` inside the gated chain.", ""]
out.write_text("\n".join(parts)); print(out)
