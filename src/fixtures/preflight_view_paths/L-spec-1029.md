# spec: DO-IT.md: mark the v4 protocol as paused and point to DO-IT V2

Carried over from the v4 spec https://github.com/fredhead88/albert-scott-platform/pull/719 into v2 form. charter: null (free-standing, D15). Author: Yitzchak Greenblatt (cross-box thinker, think-remote; docs audit 2026-10-15). Audited against `65e5d5a3f2e33fdf07ed1615e22f9990653a7d91` (origin/master, 2026-10-15).

## Goal

`docs/do-it/DO-IT.md` changes from describing the v4 pipeline as the live one ("the single source of truth for how the spec pipeline works", "always current", a "4GB box" builder cap) to carrying dated paused-v4 notices that point to the live DO-IT V2 pipeline, with the box-size clause replaced and no other v4 text rewritten.

In plain English: DO-IT.md describes the old pipeline as if it were the live one and says builders are limited by a 4GB box; this spec adds short dated notices saying the old flow has been paused since 2026-09-20 and the new pipeline is the live one, and fixes the box-size line, without touching the rest.

Evidence:

- `docs/do-it/DO-IT.md` (1,826 lines) opens (paragraph on lines 5-8; line 9 is blank) by calling itself "the single source of truth for how the spec pipeline works" and "the *what* (always current)", and describes the v4 flow: the `/orc` integrator, the `~/.claude/ledger/<NNN>-*.yml` ledger, numeric spec ids. The file never mentions V2, `L-spec`, or the pilot.
- On origin/master `65e5d5a3f`, 519 subjects begin `merge(L-spec-` since 2026-09-20 (command: `git log 65e5d5a3f --since=2026-09-20 --format=%s | grep -c '^merge(L-spec-'`; the count depends on the ref and the `--since` bound, so it is quoted only at this SHA) (for example "window win-20261015-tail1"), and no subject begins `merge(` or `merge(spec-` followed by a digit (`git log 65e5d5a3f --since=2026-09-20 --format=%s | grep -cE '^merge\((spec-)?[0-9]'`, result 0). Five other `merge(` subjects exist in that range (`merge(master)`, `merge(docs/...)`, `merge(qur/audit-fixes)`, `merge(burn/integration)`); they are branch merges, not pipeline merges. Merges of specs run on the DO-IT V2 pilot, whose state is `~/.do-it` on the orchestration box, mirrored to `github.com/fredhead88/do-it-ledger` (read-only live session, 2026-10-15).
- The spec author's instruction (2026-10-15, recorded in the relay facts) is "mark and point": label the v4 sections as paused, point to V2 with the verified facts only, do not rewrite the v4 text.
- DO-IT.md section 6 (Prime directives), "Throughput via parallelism" bullet (line 766-767), says to run parallel builders "(≤3 on the 4GB box)" (the file has the single character U+2264, bytes E2 89 A4, on line 767; match on the phrase `4GB box`, not on `<=`). "The orchestration box" is the Hetzner host Albert-Scott-Cloud where the V2 pilot runs; at the live read it had 8 vCPU and 31,330 MB RAM (2026-10-15 ~13:23 UTC). Those box figures are context only and must not go into the doc. The seat cap is `[seats] builder` in `/home/albert/.do-it/models.toml`; it was 12 at the live read, and its comment records changes 6 to 8 (2026-10-12) and 8 to 12 (2026-10-15), so the doc must point to the file, not copy a bare number.

## Requirements

R1. Top banner.
- Current: no V2 notice; the opening paragraph claims the file is the single source of truth.
- Target: directly after the `**Version:**` line (file line 3) and its following blank line, and before the "The single source of truth" paragraph, one blockquote line beginning `> **Status (2026-10-15):**`, on one physical line, followed by one blank line, saying: as of 2026-10-15 live read, since 2026-09-20 merges run on the DO-IT V2 pilot, which is the live pipeline; this file describes the v4 flow (`/orc` integrator, `~/.claude/ledger/<NNN>-*.yml` ledger, numeric spec ids), paused as the merge engine; the "single source of truth" and "always current" wording below, and the v4 operating description in every section below (ledger paths, `/orc`, numeric ids), including sections without their own notice, are superseded for the merge engine and describe the v4 flow only, with one carve-out stated in the banner: sections 7b-7e and 8 (loop telemetry and standing-role reliability) may still run and are not claimed to be paused (the banner uses the literal token `7b-7e`); V2 uses ids `L-spec-NNNN` (also `L-card`, `L-charter`), keeps its state in `~/.do-it` on the orchestration box mirrored to `github.com/fredhead88/do-it-ledger`, and is read with the `do-it-ledger` skill; inbound specs arrive as `spec-pending-review` PRs that a human relabels `inbound-spec`. The banner states these facts and no other V2 facts. The wording of the sentence may vary, but each backticked token and each quoted phrase above must appear verbatim on the banner line, including `` `~/.claude/ledger/<NNN>-*.yml` ``, the literal phrase `` `do-it-ledger` skill `` (backticks included) and the dating phrase `as of 2026-10-15 live read`.
- Acceptance: AC1.

R2. Section notices.
- Current: none.
- Target: one short blockquote line beginning `> **Status (2026-10-15):**`, on one physical line, as the first content after the heading line (one blank line between heading and notice) of each of: `## 0. CONFIG` (must contain `~/.do-it` and `` `do-it-ledger` skill ``: the Bus root, Ledger (master) and lane paths under `~/.claude/` are the v4 locations; the V2 ledger is `~/.do-it`, read through the `do-it-ledger` skill), `## 1. The map` (must contain `/orc` and `2026-09-20`: the integrator and `/orc` alias shown are the v4 roles, paused as the merge engine since 2026-09-20), and `## 3. The index` (must contain `L-spec-NNNN` and `~/.claude/ledger`: the numeric `NNN` ids and the index under `~/.claude/ledger/` are v4; V2 ids are `L-spec-NNNN`). Each is one or two lines, followed by one blank line. The notice in `## 1. The map` goes before the code fence that opens that section.
- Acceptance: AC2.

R3. Builder cap.
- Current: section 6 (Prime directives), "Throughput via parallelism" bullet, says `(≤3 on the 4GB box)`.
- Target: replace that clause (with the real ≤ character U+2264; locate it by the phrase `4GB box`) with a clause, kept on one physical line, saying the builder seat cap is `[seats] builder` in `/home/albert/.do-it/models.toml` on the orchestration box, to be read when needed, and that it was 12 as of 2026-10-15 live read and changes. The phrase `as of 2026-10-15 live read` must appear in that bullet, adjacent to the number 12 (within 40 characters, no sentence end between). Do not state the number without that phrase. Do not add a RAM or vCPU claim.
- Acceptance: AC3.

R4. Nothing else changes.
- Current: v4 text throughout.
- Target: do not edit, reflow or delete any other v4 text, including the opening paragraph (lines 5-8), the `> **v4.0.0` blockquote that starts at line 10 (left alone), and the v4 sections `## 2`, `## 4`, `## 5`, `## 7*`, `## 8`, `## 9`, `## 10`, `## 11`; they are covered by the banner of R1 (which scopes itself to the v4 operating description). The only deleted text is the clause replaced in R3 (one line, or the same line rewritten).
- Acceptance: AC4.

## Acceptance Criteria

AC1 [backend]: DO-IT.md has the top banner of R1 as the first content after the `**Version:**` line and its blank line (file line 5, followed by a blank line 6), starting `> **Status (2026-10-15):**`, with all of these on that one line, in any order: `~/.claude/ledger/<NNN>-*.yml`, `DO-IT V2`, `2026-09-20`, `L-spec-NNNN`, `L-card`, `L-charter`, `github.com/fredhead88/do-it-ledger`, `` `do-it-ledger` skill ``, `~/.do-it`, `spec-pending-review`, `inbound-spec`, `/orc`, `single source of truth`, `always current`, `v4 operating description`, `7b-7e`, `as of 2026-10-15 live read`. review_path: read-only; go to the repository root and run the Verification chain; worked if python check 1 passes; failed if it raises "banner placement" or lists a missing token.

AC2 [backend]: DO-IT.md has exactly four lines starting `> **Status (2026-10-15):**`: the banner and one notice each immediately after (heading, blank line) of `## 0. CONFIG`, `## 1. The map` and `## 3. The index`, each containing the tokens named in R2, and each of the four followed by a blank line. review_path: read-only; run the Verification chain; worked if python check 1 passes; failed if it raises "status line count" or "section notices".

AC3 [backend]: The `4GB box` clause is gone from DO-IT.md, and the "Throughput via parallelism" bullet contains, each on one line, `[seats] builder`, `/home/albert/.do-it/models.toml` and `as of 2026-10-15 live read`, the last within 40 characters of `12`. review_path: read-only; run the Verification chain; worked if the `4GB box` grep and python check 2 pass; failed if either fails or "12 adjacency" is raised.

AC4 [backend]: Only `docs/do-it/DO-IT.md` differs from the merge base, and every `git diff -U0` hunk is one of: an insertion of the banner and its blank line (git may show it directly after line 3 or line 4, the same resulting layout, which check 1 pins to lines 5-6), directly after the line of a section heading in R2 or the blank line after it (notice), or a change of the one or two lines of the "Throughput via parallelism" bullet that hold the R3 clause; there are exactly four insertion hunks and one or two change hunks inside the bullet (lines of the bullet and at most two lines after its first line), with at most two deleted lines in total. review_path: read-only; run the Verification chain; worked if python check 3 passes; failed if the `git diff --name-only` test fails or the hunk assertion raises. That no other wording of v4 text changed beyond these hunks is what the hunk-range check proves; whether the added wording is faithful to R1-R3 is for the reviewer reading `git diff`.

## Boundaries

In scope:
- `docs/do-it/DO-IT.md` text only (banner, three section notices, the R3 clause).

Out of scope:
- Rewriting the v4 text: forbidden by the "mark and point" instruction.
- Describing V2 internals beyond the facts in R1/R2: only those facts were verified.
- CLAUDE.md: a separate spec owns it.
- `do-it-starter/` and `docs/do-it/DESIGN.md`: separate documents.
- Any code: this is a text-only change.
- Audit row ADR-003 droplet sizing: belongs to the decisions/ADR spec, not this file.
- A rewrite of the Crontab lines, `/tmp/orc-active` and relay-baton references: these are v4 mechanics; they stay under the paused-v4 banner.
- Any claim of which v4 scripts still run on the box: not verified by a live read.
- A claim that DO-IT.md has no other dead file or script reference: the thinker's earlier scan is not reproducible by the builder, so no such claim is made or required.
- `do-it-starter/DO-IT.template.md`: a separate kit-canonical copy (`do-it-starter/release-map.yml` lines 397-398 record that the kit is canonical).
- `scripts/orc_footprint_agent.py` lists `docs/do-it/DO-IT.md` in its reference-doc denylist for derived writes; a text-only edit does not change that behaviour (low risk, noted only).
- Separate notices on `## 2`, `## 4`, `## 5`, `## 7*`, `## 8`, `## 9`, `## 10`, `## 11`: see Decisions.

Decisions:
- The opening paragraph (lines 5-8; line 9 is blank) is left unedited and the banner contradicts it explicitly: the banner must quote "single source of truth" and "always current" and say they describe the v4 flow only. Reason: R4 forbids rewriting v4 text, and a banner that names the contradicting wording removes the ambiguity at the cost of one sentence.
- The banner says the v4 operating description is superseded, not that every section below is inert, and it names the carve-out itself (sections 7b-7e and 8). Reason: those sections (loop telemetry and standing-role reliability, specs 503/780/863/864/256) may still run and were not verified, so the banner claims only what the live read supports; banner, R4 and this decision say the same thing.
- Only the banner plus three entry-section notices; no notice on the other v4 sections. Reason: the banner scopes the v4 operating description (ledger paths, `/orc`, numeric ids) as superseded without claiming that every section is dead, the three marked sections are where a reader decides which ledger and ids to use, and a notice per section (about 12) would add clutter without a new fact.
- The seat cap number 12 is stated, dated and attributed to the live read, with the file pointer as the authority. Reason: a bare pointer gives the reader no sense of scale, and the date keeps it honest as it changes.

## Interfaces

Consumes:
- None.

Produces:
- None (a documentation change; no signature other specs depend on).

Writes:
- docs/do-it/DO-IT.md

## Constraints

- Text-only change. No code, config or test file is modified.
- Each dated figure carries its date. The only figure introduced is the seat cap 12, dated `as of 2026-10-15 live read` in the doc text.
- Docs wrap, and the checks match single lines: the builder must keep the banner, each notice and the R3 clause on one physical line each (long lines are allowed in blockquotes; no markdown lint or file-size guard applies to `.md`, whose `file_size_guard.sh` cap covers only `*.py *.ts *.tsx`).
- Do not add V2 facts beyond those in R1/R2.

## Assumptions

- The V2 facts in R1/R2 and the seat cap in R3 were read in a read-only live session on the Hetzner orchestration box, 2026-10-15 ~13:00-13:25Z (transcript summary kept by the thinker, in `_relay-facts.md`). The builder cannot re-read the box; it copies these as dated facts and must not alter or extend them.
- The 2026-09-20 date rests on that live read alone: git history is consistent with it but does not prove it (the last numeric `merge(NNNN)` commit is dated 2026-09-14 and `merge(L-spec-` commits begin the same day, so the repo shows an overlap through about 2026-09-19). "Last booted" and resize details in the relay facts are not used.
- Written against the v4 spec's audited base `65e5d5a3f2e33fdf07ed1615e22f9990653a7d91` (origin/master, 2026-10-15); the intake base_sha was `9fd2a0547c3d56306d5edf471264221c71f4b000`.
- Concurrent work: a separate spec (CLAUDE.md) adds the matching notice there; its footprint (CLAUDE.md) does not intersect this one. Carried over without re-running pre-flight queries 4-5 against the V2 ledger: the instruction was to change nothing substantive.
- Cost path, security path and rollback_path do not apply: no paid external call, no untrusted input or authentication, and the change is a reversible text edit in git.

## Unknowns

Count: 0 `[NEEDS CLARIFICATION]` entries. For the record, the 2026-10-15 live-read facts (V2 merge engine since 2026-09-20, `~/.do-it` mirror, human relabel of `spec-pending-review` PRs, seat cap 12) cannot be re-verified from the repo; the repo-checkable parts (519 `merge(L-spec-` commits since 2026-09-20 at the audited SHA, the `4GB box` clause in DO-IT.md at line 767) were checked at the audited SHA.

## Verification

```bash
cd /opt/albert-scott && git rev-parse --verify origin/master >/dev/null && ! grep -q '4GB box' docs/do-it/DO-IT.md && /usr/bin/python3 -c 'import re;t=open("docs/do-it/DO-IT.md",encoding="utf-8").read();L=t.split("\n");P="> **Status (2026-10-15):**";assert len([l for l in L if l.startswith(P)])==4,"status line count";assert L[2].startswith("**Version:**") and L[3]=="" and L[4].startswith(P) and L[5]=="","banner placement";B=L[4];bad=[p for p in ("DO-IT V2","2026-09-20","L-spec-NNNN","L-card","L-charter","github.com/fredhead88/do-it-ledger","`do-it-ledger` skill","`~/.claude/ledger/<NNN>-*.yml`","~/.do-it","spec-pending-review","inbound-spec","/orc","single source of truth","always current","v4 operating description","7b-7e","as of 2026-10-15 live read") if p not in B];assert not bad,bad;n=lambda h:[k for k,l in enumerate(L) if l.startswith(h)][0];assert all(L[n(h)+1]=="" and L[n(h)+2].startswith(P) and L[n(h)+3]=="" and all(p in L[n(h)+2] for p in ps) for h,ps in (("## 0. CONFIG",("~/.do-it","`do-it-ledger` skill")),("## 1. The map",("/orc","2026-09-20")),("## 3. The index",("L-spec-NNNN","~/.claude/ledger")))),"section notices"' && /usr/bin/python3 -c 'import re;L=open("docs/do-it/DO-IT.md",encoding="utf-8").read().split("\n");i=[k for k,l in enumerate(L) if l.startswith("- **Throughput via parallelism.**")][0];j=next(k for k in range(i+1,len(L)) if L[k].startswith("- **"));bl=L[i:j];bad=[p for p in ("[seats] builder","/home/albert/.do-it/models.toml","as of 2026-10-15 live read") if not any(p in l for l in bl)];assert not bad and not any("4GB box" in l for l in bl),bad;T=" ".join(bl);PH="as of 2026-10-15 live read";ps=T.find(PH);pe=ps+len(PH);se=lambda s:re.search(r"[.!?](\s|$)",s);assert any((pe<=m.start()<=pe+40 and not se(T[pe:m.start()])) or (ps-40<=m.end()<=ps and not se(T[m.end():ps])) for m in re.finditer(r"\b12\b",T)),"12 adjacency"' && base="$(git merge-base origin/master HEAD)" && test "$(git diff --name-only "$base" | tr -d '\n')" = "docs/do-it/DO-IT.md" && /usr/bin/python3 -c 'import re,sys,subprocess as s;b=sys.argv[1];f="docs/do-it/DO-IT.md";o=s.check_output(["git","show",b+":"+f],text=True).split("\n");d=s.check_output(["git","diff","-U0",b,"--",f],text=True);ix=lambda h:[k+1 for k,l in enumerate(o) if l.startswith(h)][0];ok={(3,0),(4,0)}|{(ix(h)+j,0) for h in ("## 0. CONFIG","## 1. The map","## 3. The index") for j in (0,1)};r=ix("- **Throughput via parallelism.**");hs=[(int(m.group(1)),int(m.group(2) or 1)) for m in re.finditer(r"(?m)^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@",d)];ins=[h for h in hs if h[1]==0];ch=[h for h in hs if h[1]>0];assert len(ins)==4 and all(h in ok for h in ins) and 1<=len(ch)<=2 and all(r<=s and s+c-1<=r+2 for s,c in ch) and sum(c for s,c in ch)<=2,hs' "$base" && echo VERIFY_OK
```

Note: the `git rev-parse --verify origin/master` step exits non-zero with git's own fatal message if the ref is missing, so the chain cannot pass silently without it. `(int(m.group(2) or 1))` reads a hunk header without a count as one line; a header with count `,0` is an insertion. No AC or check in this spec runs a mutating git command.

## Dependencies

None. A separate spec (CLAUDE.md) adds the matching notice there; this spec does not edit that file.
