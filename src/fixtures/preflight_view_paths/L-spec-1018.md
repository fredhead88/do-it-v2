# L-spec-1018 — claude-md-corrections

charter: null
requirements: R1, R2, R3, R4, R5, R6, R7, R8
carried_from: v4 spec https://github.com/fredhead88/albert-scott-platform/pull/718 (free-standing, D15; carried into v2 form, substance unchanged)

# CLAUDE.md: correct the claims that no longer match the repo or the boxes

## Goal

CLAUDE.md changes from carrying statements that live checks on 2026-10-15 showed wrong or stale to stating what the repo and the boxes show.

In plain English: CLAUDE.md still says the Hetzner box was never resized, names a venv path that does not exist on the droplet, lists a cron that is really a service, points builders at a skill that does not exist, and describes the old v4 pipeline as the live one; this spec fixes those lines and nothing else.

A cross-check of the docs against the code, the production database, and live reads of both servers (2026-10-15) found these CLAUDE.md statements wrong. CLAUDE.md is the first file every builder reads, so each wrong line misleads every later session. The fix is text-only and touches one file.

Evidence, by requirement (all measured 2026-10-15 unless stated):

- Hetzner box `Albert-Scott-Cloud` (live read 2026-10-15, see Assumptions): 8 vCPU (AMD EPYC-Milan), 31,330 MB RAM, 16 GB swap, `/dev/sda1` 225G with 135G used (63%) at 13:23 UTC; `/var/tmp` and `/home` are on the same `/dev/sda1`; last boot 2026-10-15 07:16 UTC; a comment in `/home/albert/.do-it/models.toml` (the `[seats]` section, near line 118) records a CCX33 resize dated 2026-10-15, and the builder seat cap there moves over time. CLAUDE.md says "never resized", 150G, 89% (2026-08-30).
- Droplet venv is `/opt/albert-scott/venv` (live `ls`: `.venv` does not exist there; `deploy/droplet_venv_path.env` declares it). The orchestration box uses `/opt/albert-scott/.venv`. CLAUDE.md names `/opt/albert-scott/api && source ../.venv` at the "Start API locally" command (same absolute prefix, different venv per host) and `/opt/albert-scott/.venv/bin/python` for the SessionEnd hook (that hook runs on the orchestration box; it stays as written).
- The notifications listener is the systemd unit `deploy/systemd/albert-scott-notifications.service` (`--loop`, `Restart=always`), not a 5-minute cron. Its weekly subscription reconcile is the `notify_reconcile` cron in `deploy.sh`. `t2_pricing` (`deploy/cron/amazon_tier2_pricing.cron`, runner `--tier 2 --tier2-unit pricing`, own lock) and Tier 3 (`deploy.sh`, job `t3`) are missing from the cron list. (Other `deploy.sh` jobs such as `ams_reconcile`, Mon 02:30, are also absent from that list; see Decisions.) The existing "Competitive pricing: 03:30 UTC nightly" line is a different job (`--jobs competitive_pricing`, `deploy.sh`, own lock) and stays.
- `pipelines/amazon/runner.py:216` no longer holds the cited comment (`grep -n "4 GB" pipelines/amazon/runner.py` finds nothing).
- `scripts/lint_metered_claude.sh` exists (spec 572 R5) with `scripts/tests/test_lint_metered_claude.sh`. Repo-wide, `grep -rln lint_metered_claude . --exclude-dir=.git` finds it only in the script itself, `scripts/.pane_poker_allowlist`, `docs/do-it/skills-snapshot/grader.SKILL.md`, specs and session docs, and test fixtures; nothing in `.githooks/`, `.github/`, `scripts/ci/` or a pre-commit config calls it.
- No `aaaudit` skill exists; the user-level skills on the thinker's box are `audit` and `xaudit` (whether they are installed on a builder's box is not verified). `grader` exists in the repo as `do-it-starter/skills/grader/SKILL.md` and `docs/do-it/skills-snapshot/grader.SKILL.md`, not as a user-installed skill.
- `tests/` and `api/tests/` hold 2,674 `test_*.py` files (`find tests api/tests -name 'test_*.py' | wc -l`) and 30,516 `def test_` lines (`grep -rn 'def test_' tests api/tests | wc -l`, at the audited SHA), not "199+". `agents/*.py` holds 47 modules besides `__init__.py`, not "~25".
- Since 2026-09-20 merges run on the DO-IT V2 pilot (ids `L-spec-NNNN`, state in `~/.do-it` on the orchestration box, mirrored to `github.com/fredhead88/do-it-ledger`): `git log --since=2026-09-20 --format=%s | grep -c '^merge(L-spec-'` gives 519 at the audited SHA `65e5d5a3f`, and no `merge(` subject in that window carries a bare numeric spec id (the five non-`L-spec` ones are branch merges). So V2 has been the sole merge engine since 2026-09-20, as evidenced by those subjects. CLAUDE.md still describes the v4 `/orc` flow and the `~/.claude/ledger/<NNN>-*.yml` ledger as current.

## Requirements

R1. Hetzner paragraph (the "Server (orchestration box ...)" bullet under Constraints). Replace the sizing sentences with the figures above, worded "as of 2026-10-15 live read" so the doc carries its own date and source: 8 vCPU, 31,330 MB RAM, 16 GB swap, `/dev/sda1` 225G, 135G used (63%) at 13:23 UTC, last booted 2026-10-15 07:16 UTC. State, as a fact with its source, that a comment in `models.toml` records a resize dated 2026-10-15; do not say the box was "upgraded on" a date. Remove "was never resized". Keep the warning that the droplet's figures do not apply to this box, and add that `/var/tmp` and `/home` share `/dev/sda1` (as of the same read). State in the doc that the disk figure goes stale within hours (the box has a disk watch) and is a snapshot, not a limit. Tell the reader to re-measure with `df -h /; nproc; free -m` before sizing against any figure. Do not state the builder seat cap as a number; point to `[seats] builder` in `/home/albert/.do-it/models.toml`.

R2. Venv path. At the "Start API locally" command (inside a code fence, so the added line must be a `#` comment), add a comment line making the host explicit: `../.venv` resolves to `/opt/albert-scott/.venv`, the orchestration box's venv; on the droplet the venv is `/opt/albert-scott/venv` (declared in `deploy/droplet_venv_path.env`). In the Deployment "Droplet" bullet replace "single venv" with "single venv at `/opt/albert-scott/venv`". Leave the SessionEnd hook line (`/opt/albert-scott/.venv/bin/python ...`) as it is.

R3. Cron list. Replace "Notifications listener: every 5 min" with the long-running systemd unit `albert-scott-notifications` and its weekly subscription reconcile (`notify_reconcile`). Add `t2_pricing` and Tier 3 (job `t3`) entries. Read every schedule (reconcile weekday and time, `t2_pricing`, Tier 3) from `deploy.sh` or `deploy/cron/amazon_tier2_pricing.cron`; do not copy a schedule from this spec. The `t2_pricing` line must also say it is a separate job from the 03:30 "Competitive pricing" line. Keep the "Competitive pricing" line unchanged. Each new entry stays on one line. Scope: only the three named gaps (notifications, `t2_pricing`, Tier 3) are fixed; other omitted jobs are not added.

R4. Dead reference. In the Deployment "Droplet" bullet delete the clause naming "the `~4 GB RAM + 2 GB swap` comment at `pipelines/amazon/runner.py:216`" and repair the sentence so it reads: `The long-standing "4GB RAM, 1 vCPU" here was stale; so is spec 196's "1 GB box" premise.` The following instruction to check `free -m` and `dmesg -T` stays.

R5. Metered-`claude -p` lint. Replace "`status: planned` — so the CI lint ... is **not yet shipped**" with: the lint script `scripts/lint_metered_claude.sh` exists but is not wired into pre-commit or CI, so nothing but this rule stops it. Also, next to the line that forbids setting `ANTHROPIC_API_KEY`, add that the SessionEnd hook (`.claude/hooks/session-end.py`) is a known exception: it makes one small metered SDK call and reads the key from `.env` (see "Session handoffs"). Do not call it the "sole" exception; other scripts in the repo also construct an Anthropic SDK client and this rule does not audit them.

R6. Skills. Replace "the **`aaaudit` skill**" with the `audit` and `xaudit` skills (worded "if installed": they are user-level skills whose install varies per box), and replace "(the `grader` skill)" (the phrase wraps across two lines in CLAUDE.md) with the statement that graders take their own interactive turn in their own pane, as described in `docs/do-it/skills-snapshot/grader.SKILL.md`.

R7. Counts. In "What's in here", replace "199+ tests" with "about 2,700 test files and about 30,000 test functions (counted 2026-10-15)", and replace "~25 intelligence agents" with a description that names `agents/flow_orchestrator.py` and gives no number. Keep the "What's in here" bullets otherwise as they are.

R8. Pipeline notice (mark and point; do not rewrite the pipeline text). Add one short notice to the "DO-IT pipeline" bullet and one to "Diagnosis discipline", saying that since 2026-09-20 merges run on the DO-IT V2 pilot (ids `L-spec-NNNN`; ledger state at `~/.do-it`, mirrored to `github.com/fredhead88/do-it-ledger`; read it with the `do-it-ledger` skill, if installed), and that the `/orc` description and the build-status ledger (`~/.claude/ledger/<NNN>-*.yml`) text that follows are the v4 flow, which no longer performs the merges. Say nothing stronger than that (not "retired" or "paused"). The notice in the DO-IT bullet covers the build-status ledger sentences in that bullet, which are left in place. Change the first Diagnosis rule from "Read `~/.claude/ledger/<NNN>-*.yml`" to "read the ledger card for the spec (the `do-it-ledger` skill, if installed), not the prose". Leave the remaining v4 text in place. Do not describe V2 internals beyond the facts in this spec.

## Acceptance Criteria

AC1 [backend]: In the orchestration-box bullet, CLAUDE.md no longer says "was never resized", and carries `as of 2026-10-15 live read`, `8 vCPU`, `31,330 MB`, `225G`, `2026-10-15 07:16 UTC`, `/var/tmp`, `[seats] builder` and the `df -h /; nproc; free -m` instruction. review_path: the verification chain below.

AC2 [backend]: The Deployment "Droplet" bullet and the "Start API locally" command both name `/opt/albert-scott/venv`, "single venv" no longer stands without a path, and the SessionEnd hook line is unchanged. review_path: the verification chain below.

AC3 [backend]: The cron list no longer contains "Notifications listener: every 5 min", names `albert-scott-notifications`, `t2_pricing` and Tier 3, and the reconcile, `t2_pricing` and Tier 3 times in the doc agree with `deploy.sh` and the cron file. review_path: the verification chain below.

AC4 [backend]: CLAUDE.md no longer references `runner.py:216`, `aaaudit` or "199+ tests", and the Droplet bullet still carries "1 GB box" and `free -m`. review_path: the verification chain below.

AC5 [backend]: The metered-`claude -p` section says `scripts/lint_metered_claude.sh` exists and is not wired in, no longer says "not yet shipped", and names the SessionEnd hook as "a known exception" and does not use the phrase "sole exception" (CLAUDE.md legitimately keeps "sole production path" elsewhere, so the check is on the phrase, not the word). review_path: the verification chain below.

AC6 [backend]: The skills text names `audit`, `xaudit` and the grader snapshot path; the counts text names `agents/flow_orchestrator.py` and the count date. review_path: the verification chain below.

AC7 [backend]: The "DO-IT pipeline" bullet and "Diagnosis discipline" each carry a V2 notice (`DO-IT V2`, `2026-09-20`, `do-it-ledger`), and the first Diagnosis rule no longer tells the reader to read `~/.claude/ledger/<NNN>-*.yml`. review_path: the verification chain below.

AC8 [backend]: Every diff hunk against `origin/master` falls inside a region named in R1 to R8. The chain's hunk-window check proves that mechanically for the regions; that no unwanted wording change happens inside a region is not provable by script. review_path: reviewer reads `git diff origin/master -- CLAUDE.md` (the chain's window check is a floor, not a proof).

## Boundaries

In scope: CLAUDE.md text only. Out of scope: DO-IT.md and the other docs (separate specs), rewriting the v4 section, describing V2 internals, changing any code, and the droplet's own figures (re-measured on 2026-08-18; a live read on 2026-10-15 matched them: 4 vCPU, 7,941 MB).

Not included:

- The SessionEnd hook line's venv path (`/opt/albert-scott/.venv/bin/python`, near "Session handoffs"): correct for the orchestration box, where the hook runs; left unchanged on purpose.
- The "Competitive pricing: 03:30 UTC nightly" line: verified to be a different job from `t2_pricing`, so it stays.
- The stale v4 text in the DO-IT bullet (`/orc` as integrator, ledger masters in `~/.claude/ledger/`): not rewritten; R8's notice marks it as v4 (a decision already made).
- Other `deploy.sh` jobs missing from the CLAUDE.md cron list (for example `ams_reconcile`): a full cron-list reconciliation is a separate spec.
- Any CLAUDE.md claim not listed in R1 to R8: not examined here.

## Interfaces

Consumes: nothing (a text-only change to one file).

Produces: nothing (no other spec reads this file's new wording).

Writes:
- CLAUDE.md

## Constraints

- Text-only change. No code, config or test file is modified.
- Dated figures carry their date; undated figures are not introduced. Figures from the live read carry the words "as of 2026-10-15 live read".
- Keep each phrase the checks look for on a single line (docs wrap, and the checks match single lines).
- Where R3 asks for a schedule, read it from `deploy.sh` or the cron file. Do not copy a schedule from this spec.
- Do not use git commands that change state while working or verifying.

## Assumptions

The Hetzner figures in the Goal (vCPU, RAM, swap, disk, boot time, the `models.toml` comment) come from a read-only live session on the orchestration box (`Albert-Scott-Cloud`), 2026-10-15 about 13:00-13:25Z; the thinker kept a transcript summary. The builder cannot re-read the box: copy these as dated facts and do not alter them. Everything else in the Goal was measured in the repo (counts, greps, `git log`) or from `deploy.sh` and the cron files, and the builder can re-run it.

Audited against: `65e5d5a3f2e33fdf07ed1615e22f9990653a7d91` (origin/master, 2026-10-15).

## Decisions

- Pricing lines: keep "Competitive pricing 03:30" and add `t2_pricing` separately, because `deploy.sh` runs them as different jobs with different locks.
- SessionEnd venv line: unchanged, because the hook runs on the orchestration box where `/opt/albert-scott/.venv` is correct.
- Cron list scope: fix only the notifications entry, `t2_pricing` and Tier 3. `deploy.sh` has further jobs absent from the list (for example `ams_reconcile`, Mon 02:30); completing the list is a separate audit, listed in Not included.
- Seat cap: pointer only, no number, because it changes (the `models.toml` comment shows three values in four days).
- Exception wording: "a known exception", not "sole", because other scripts also create an SDK client.
- Schedules (reconcile, `t2_pricing`, Tier 3) are read from source, not hard-coded in this spec, and the chain compares the doc to the source.
- Grader: pointer to the snapshot copy in `docs/do-it/skills-snapshot/`, because the repo copy is the only one a reader can open.

## Unknowns

- [NEEDS CLARIFICATION: the Hetzner figures cannot be re-measured by the builder; they are dated facts from one read.]
- [NEEDS CLARIFICATION: whether any v4 path still merges outside the `merge(` subjects is not verified; the doc says only what the Goal evidences (merges since 2026-09-20 are all V2 ids).]

## Verification

```bash
set -euo pipefail && cd "$(/usr/bin/git rev-parse --show-toplevel)" &&
#guard (true before and after the edit): the SessionEnd hook venv line stays as written
grep -q '/opt/albert-scott/.venv/bin/python .claude/hooks/session-end.py' CLAUDE.md &&
#guard (true before and after the edit): the competitive-pricing line stays
grep -q 'Competitive pricing: 03:30 UTC nightly' CLAUDE.md &&
#guard (true before and after the edit): the word sole must not describe the SessionEnd exception
! grep -q 'sole exception' CLAUDE.md &&
#guard (true before and after the edit): the Droplet bullet keeps the 1 GB box premise
grep -q '1 GB box' CLAUDE.md &&
#R1 orchestration-box bullet
/usr/bin/python3 -I -c 'import re,sys;L=open("CLAUDE.md").read().split("\n");a=[i for i,l in enumerate(L) if "Server (orchestration box" in l][0];b=[i for i,l in enumerate(L) if i>a and "**AI costs**" in l][0];S=L[a:b];T="\n".join(S);bad=[p for p in ["was never resized","150G capacity"] if p in T];miss=[p for p in ["as of 2026-10-15 live read","8 vCPU","225G","31,330 MB","/var/tmp","df -h /; nproc; free -m","[seats] builder","models.toml","2026-10-15 07:16 UTC"] if not any(p in l for l in S)];sys.exit("R1 bad=%s miss=%s"%(bad,miss) if bad or miss else 0)' &&
#R2 and R4 Deployment Droplet bullet plus Start API command
/usr/bin/python3 -I -c 'import sys;L=open("CLAUDE.md").read().split("\n");a=[i for i,l in enumerate(L) if l.startswith("- **Droplet**: 167.71.46.51")][0];b=[i for i,l in enumerate(L) if i>a and "Disk resize 2026-08-18" in l][0];S=L[a:b];T="\n".join(S);c=[i for i,l in enumerate(L) if "uvicorn app.main:app --reload" in l][0];N="\n".join(L[max(0,c-3):c+6]);errs=[];errs+=["single venv unpathed"] if "single venv at `/opt/albert-scott/venv`" not in T else [];errs+=["runner.py:216"] if "runner.py:216" in T else [];errs+=["free -m"] if "free -m" not in T else [];errs+=["start-api no droplet venv"] if "/opt/albert-scott/venv" not in N or "droplet" not in N.lower() else [];sys.exit(";".join(errs) if errs else 0)' &&
#R3 cron list, compared with deploy.sh and the t2_pricing cron file
/usr/bin/python3 -I -c 'import re,sys;L=open("CLAUDE.md").read().split("\n");a=[i for i,l in enumerate(L) if "Tier 1 Amazon: every 4h" in l][0];b=[i for i,l in enumerate(L) if i>a and "On startup, the API does NOT" in l][0];S=L[a:b];D=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];F=lambda t:"%02d:%02d"%(int(t[1]),int(t[0]));P=lambda txt,key:re.search(r"^(\d+) (\d+) \* \* (\d) .*"+key,txt,re.M);dep=open("deploy.sh").read();cr=open("deploy/cron/amazon_tier2_pricing.cron").read();t3=P(dep,r"--job t3 ");nr=P(dep,r"--job notify_reconcile ");tp=re.search(r"^(\d+) (\d+) \* \* \* .*--tier2-unit pricing",cr,re.M);errs=[];errs+=["old 5 min line"] if any("Notifications listener: every 5 min" in l for l in S) else [];ok=lambda k,*req:any(k in l and all(r in l for r in req) for l in S);errs+=[] if ok("Tier 3",F((t3.group(1),t3.group(2))),D[(int(t3.group(3))-1)%7]) else ["missing or wrong Tier 3 line"];errs+=[] if ok("albert-scott-notifications",F((nr.group(1),nr.group(2))),D[(int(nr.group(3))-1)%7]) else ["missing or wrong reconcile line"];errs+=[] if ok("t2_pricing",F((tp.group(1),tp.group(2))),"separate job") else ["missing or wrong t2_pricing line"];sys.exit(";".join(errs) if errs else 0)' &&
#R5 and R6 metered section
/usr/bin/python3 -I -c 'import sys;L=open("CLAUDE.md").read().split("\n");a=[i for i,l in enumerate(L) if l.startswith("### NEVER use `claude -p`")][0];b=[i for i,l in enumerate(L) if i>a and l.startswith("### Constraints")][0];S=L[a:b];T="\n".join(S);miss=[p for p in ["scripts/lint_metered_claude.sh","not wired","session-end.py","`audit`","`xaudit`","docs/do-it/skills-snapshot/grader.SKILL.md","graders take their own interactive turn"] if not any(p in l for l in S)];U=" ".join(T.split());bad=[p for p in ["not yet shipped","aaaudit","(the `grader` skill)"] if p in U];sys.exit("R56 miss=%s bad=%s"%(miss,bad) if miss or bad else 0)' &&
#R7 counts in What is in here
/usr/bin/python3 -I -c 'import sys;L=open("CLAUDE.md").read().split("\n");ag=[l for l in L if l.startswith("- **agents/**")];ts=[l for l in L if l.startswith("- **tests/**")];errs=[];errs+=["agents line"] if not ag or "agents/flow_orchestrator.py" not in ag[0] or "~25" in ag[0] else [];errs+=["tests line"] if not ts or "199+" in ts[0] or "about 2,700" not in ts[0] or "2026-10-15" not in ts[0] else [];sys.exit(";".join(errs) if errs else 0)' &&
#R8 DO-IT bullet and Diagnosis discipline
/usr/bin/python3 -I -c 'import sys;L=open("CLAUDE.md").read().split("\n");a=[i for i,l in enumerate(L) if l.startswith("- **DO-IT pipeline")][0];b=[i for i,l in enumerate(L) if i>a and l.startswith("## Diagnosis discipline")][0];c=[i for i,l in enumerate(L) if i>b and "**Reproduce before you fix" in l][0];A=L[a:b];B=L[b:c];errs=[];[errs.append("bullet missing "+p) for p in ["DO-IT V2","2026-09-20","do-it-ledger","fredhead88/do-it-ledger","v4"] if not any(p in l for l in A)];[errs.append("diag missing "+p) for p in ["DO-IT V2","2026-09-20","do-it-ledger"] if not any(p in l for l in B)];errs+=["old first rule"] if any("Read `~/.claude/ledger/<NNN>-*.yml`" in l for l in B) else [];sys.exit(";".join(errs) if errs else 0)' &&
#R4/R6 whole-file residue
! grep -q 'runner.py:216' CLAUDE.md &&
! grep -q 'aaaudit' CLAUDE.md &&
! grep -q '199+ tests' CLAUDE.md &&
#AC8 floor: every hunk of the read-only diff lies in an allowed region of the base file
/usr/bin/python3 -I -c 'import re,subprocess,sys;g=lambda *a:subprocess.check_output(["git"]+list(a),text=True);B=g("show","origin/master:CLAUDE.md").split("\n");f=lambda s:[i+1 for i,l in enumerate(B) if s in l][0];W=[(f("- **agents/**"),f("- **agents/**")),(f("- **tests/**"),f("- **tests/**")),(f("- **Droplet**: 167.71.46.51"),f("Disk resize 2026-08-18")-1),(f("Tier 1 Amazon: every 4h"),f("On startup, the API does NOT")-1),(f("### NEVER use `claude -p`"),f("### Constraints")-1),(f("Server (orchestration box"),f("**AI costs**")-1),(f("uvicorn app.main:app --reload"),f("uvicorn app.main:app --reload")),(f("- **DO-IT pipeline"),f("**Reproduce before you fix")-1)];bad=[];[bad.append(h) for h in re.findall(r"^@@ -(\d+)(?:,(\d+))? ",g("diff","-U0","origin/master","--","CLAUDE.md"),re.M) if not any(lo-1<=int(h[0])<=hi+1 and lo-1<=int(h[0])+max(int(h[1] or 1)-1,0)<=hi+1 for lo,hi in W)];sys.exit("hunks outside regions: %s"%bad if bad else 0)' &&
echo VERIFY_OK
```

## rollback_path

Not required: a one-file text change, reversible by `git revert` of the merge commit; no data layer is touched.

## cost_path

Not required: no paid external call is reachable from this footprint.

## security_path

Not required: the footprint touches no untrusted input, authentication or authorization, and no client data.

## Dependencies

None. A separate spec corrects `docs/do-it/DO-IT.md`; this one does not edit that file.