---
spec_id: L-spec-1036
charter: null
project: albert-scott
author: "albert"
source_brief: null
base_sha: "9fd2a0547c3d56306d5edf471264221c71f4b000"
requires_live_run: false
live_run_owner: builder
---

# spec: Product and data-model docs: correct the tables, routes, paths and status labels that no longer match

Carried over into v2 form from the v4 spec (fredhead88/albert-scott-platform PR 726); substance unchanged. Free-standing, charter: null (D15).

## Goal

Thirteen architecture and Amazon-coverage docs change from naming tables, routes, files, scripts and statuses that do not exist (or calling finished work "not yet built") to naming what exists as of 2026-10-15.

In plain English: several architecture and Amazon-coverage docs name database tables, dashboard pages, files and scripts that do not exist, or call finished work "not yet built"; this spec fixes those lines and nothing else.

A cross-check of the product/data-model docs against the repo and the production database (read-only SELECTs, 2026-10-15, at base `65e5d5a3f`) found the statements below wrong. Each one sends a reader to something that is not there. The fix is text-only.

Evidence, by requirement (all 2026-10-15):

- `architecture_dashboard.md` (Flow section, lines 174-175) says the autonomy flow writes `flow_findings`, `flow_recommendations`, `flow_wiki`, `flow_autonomy_ledger`. Production has no `flow_*` table in `public`. The code uses `findings` (`agents/flow_common_findings.py`: `INSERT INTO findings`), `recommendations` (inserted by `agents/recommendation_extractor.py`; updated by `agents/triage_engine.py` and `agents/autonomy_manager.py`), `wiki_pages` and `wiki_sections` (`wiki_pages` is read by `agents/prioritizer.py`, `agents/wiki_validator.py` and `agents/wiki_editor.py`; `wiki_sections` is joined in `agents/prioritizer.py`), and `autonomy_rules` (read and updated by `agents/autonomy_manager.py`); those tables exist in prod. No `flow_autonomy_ledger` or ledger table is referenced in `agents/autonomy_manager.py`. Only the helper name `write_flow_findings` carries the prefix. The same doc lists a `/profit-v2/console` route (no `console` directory exists under `dashboard/src/app/(dashboard)/dashboard/clients/[name]/profit-v2/`), says `schemas.ts` is 719 lines (it is 2,269), calls migration `o5p6q7r8s9t0` "head" (its file header says Create Date 2026-06-01; it lives in `api/alembic_supabase/versions/_archive/`, and `p6q7r8s9t0u1` in the same folder has `down_revision = "o5p6q7r8s9t0"`, so it cannot be a head), and cites `pipelines/amazon/intel/ingest_email.py` (the file is `pipelines/amazon/intel/scrapers/ingest_email.py`).
- `question-registry.md` (line 44, the only occurrence in the file and in the non-archived docs) names the Settlement Coverage page `/profit-v2/coverage`. No such route exists; the sidebar item `nav-client-settlement-coverage` in `dashboard/src/components/app-sidebar.tsx` links to `pv2("/settlement")` and `.../profit-v2/settlement/page.tsx` exists.
- `listing-l3-output-format.md` links `scripts/listing_l3/validate.py` (line 148) and shows a command line for it (line 188). `scripts/listing_l3/` does not exist. The validator is `pipelines/amazon/l3_deck/validate.py`, which exposes `validate_bundle(bundle: dict) -> list[str]` and has no `__main__` or argparse entry point.
- `poe_source_contract.md` opens (lines 5-7) with "it does not implement R1 (`poe_source_status`) or R2 (`catalog_poe_competitor_source_link`)", labels sections 4 and 5 "not yet built", marks seven rows of its section 7 table "R1, not yet built" (lines 232-237 and 239) and one "R2, not yet built" (line 238) - ten lines in all contain "not yet built" - and says the link table is "verified absent" and "no backing table exists today". Further stale future-tense lines: "once R1 ships" (144), "once R1 lands" (154), "Until R1 ships" (155), "Until R2 ships" (193), "No backing column exists for this today" (193), "n/a until R2 ships" (238); the row 238 cell reads "(R2, **not yet built** — no backing table exists today)". A banner on line 12 of the same doc says both were implemented. Both exist: `poe_source_status` at `pipelines/amazon/jobs/poe_freshness.py:455`, the table created by `api/alembic_supabase/versions/poe_competitor_source_link_563.py` (insert at `pipelines/amazon/jobs/poe_csv_ingest.py:866`) and present in prod. Rows 109-113 of the doc already say "R1, implemented".
- `profit_v2_foundations.md` says "Four parsers" in `api/app/lib/profit_v2/parsers/`; the folder holds five modules (`settlement`, `all_orders`, `storage_fee`, `advertised_product`, `ads_monthly_statement`). `ads_monthly_statement.py` parses Amazon Ads monthly billing statement PDFs into one invoice line per row (campaign charges excluding tax) and is imported by `api/app/lib/profit_v2/ingestion/pipeline.py` (which defines `ingest_ads_monthly_statement_file`); the doc does not mention it anywhere (grep `ads_monthly` finds nothing in the doc).
- `profit_v2_schema_snapshot_plan_4_5.md` says any divergence between the file and code "is a bug in the code, not the snapshot". It is a 2026-05-26 capture, its own note (2026-10-09) lists differences from the live schema, and code follows the live database.
- `listing-spine-strategic-improvement-backlog.md` (dated 2026-09-11, updated 2026-09-14) has a "Current weakness" table whose row "Program run" (line 137) says run orders do not persist the frozen program manifest, node lifecycle or output authority, and whose row "Server-authoritative start plan" (line 140) describes a missing start-plan read/persist seam. Since then the tables `listing_program_runs`, `listing_program_nodes`, `listing_program_node_events`, `listing_program_start_plans_v4`, `listing_program_money_reservations` and `listing_program_human_authority_receipts` have been created in prod and each holds 0 rows (exact `count(*)`, as of 2026-10-15 14:24 UTC, re-run read-only). Mapping: "Program run" corresponds to `listing_program_runs`, `listing_program_nodes` and `listing_program_node_events`; "Server-authoritative start plan" corresponds to `listing_program_start_plans_v4`. No "Current weakness" row (grep for "receipt" and "reservation" in the file) says that money reservations or human-authority receipts lack persistence: the "Exact money-gate invocation boundary" row (144) already assumes a persisted reservation draft. The other rows are not re-verified.
- `docs/amazon-coverage/04-sp-api-endpoints.md` (dated 2026-04-25) says its 12 endpoint groups are "not yet used in this codebase". All 12 now have a collector module in `pipelines/amazon/collectors/sp_endpoints/` (group number in the doc's own headings -> module): 1 Listings Items `listings_items.py`, 2 A+ Content `aplus.py`, 3 Product Fees `product_fees.py`, 4 Sales `sales_metrics.py`, 5 Sellers `sellers.py`, 6 Solicitations `solicitations.py`, 7 Messaging `messaging.py`, 8 Tokens/RDT `tokens.py`, 9 Replenishment `replenishment.py`, 10 AWD `awd.py`, 11 Listings Restrictions `listings_restrictions.py`, 12 Product Type Definitions `product_type_definitions.py`. The doc's own "Build Status - 2026-04-25" section already lists these modules. Its title line reads "Unused Endpoints" and its Scope line says "not yet used in this codebase". It also says to enumerate "from existing `amazon_sku_catalog`"; no such table exists in prod, and `amazon_listings` does (it has `seller_sku` and `asin`; the sibling collectors `listings_restrictions.py` and `replenishment.py` read it).
- Dead paths in the coverage docs: `docs/amazon-platform-master.md` (now `docs/strategy/amazon-platform-master.md`; cited in `00-MASTER-PLAN.md:43`, `00-MORNING-PICKUP.md:77`, `02-brand-analytics-extras.md:462`), `docs/seller-central-reauth-checklist.md` (now `docs/playbooks/seller-central-reauth-checklist.md`; `00-MORNING-PICKUP.md:86`), `api/../ads_gated.py` (real file `pipelines/amazon/collectors/ads_gated.py`; `COVERAGE-MATRIX.md:698`; the other mentions at 754, 788 and 810 are bare file names and stay), and the command `python scripts/discover_marketplace_participations.py` in `08-notifications-and-marketplaces.md:355`, which names a script that does not exist.

## Requirements

R1. `docs/architecture/architecture_dashboard.md`: (a) in the Flow paragraph and its **Tables** line (the "The Flow (agents)" section), replace the `flow_*` names with `findings`, `recommendations`, `wiki_pages` (with `wiki_sections`) and `autonomy_rules`; say that `write_flow_findings` in `agents/flow_common_findings.py` writes `findings`, that `agents/recommendation_extractor.py` inserts `recommendations` rows, and that `agents/autonomy_manager.py` reads and updates `autonomy_rules`; do not name a table for an "autonomy ledger" and do not say which agent writes `wiki_pages`; (b) delete the `/profit-v2/console` route line and add a sentence that the route list is not exhaustive and the live set is the directory listing of `dashboard/src/app/(dashboard)/dashboard/clients/[name]/profit-v2/`; (c) replace "719 lines" with a statement that the file is large and the reader should run `wc -l`, no number; (d) replace "(head; down_revision `n4o5p6q7r8s9`)" with "(Create Date 2026-06-01; since archived under `api/alembic_supabase/versions/_archive/`; `p6q7r8s9t0u1` revises it, so it is not a head)"; (e) use the `pipelines/amazon/intel/scrapers/ingest_email.py` path.

R2. `docs/architecture/question-registry.md`: change the Settlement Coverage route to `/profit-v2/settlement`. Touch no other line.

R3. `docs/architecture/listing-l3-output-format.md`: replace both `scripts/listing_l3/validate.py` references (the link at line 148 and the command at line 188) with `pipelines/amazon/l3_deck/validate.py` (keep the relative-link form on line 148: `../../pipelines/amazon/l3_deck/validate.py`), and replace the "Validate offline with `python ...`" sentence with one that says to call `validate_bundle(bundle)` from `pipelines.amazon.l3_deck.validate` (it returns a list of diagnostics; an empty list means valid) and that the module has no command-line entry point. Do not touch the "eight ratified names" sentence. The next sentences ("The validator uses Draft 2020-12 plus format checking ...") stay as written: `validate.py` imports `Draft202012Validator, FormatChecker`, so they remain correct.

R4. `docs/architecture/poe_source_contract.md`: relabel stale status text; do not rewrite the design prose. Specifically: (a) rewrite lines 5-7 so they say this doc does not itself build R1 or R2 and that "both are implemented (spec 563)"; (b) rewrite the line-12 banner so it no longer says "not-yet-built" or "historical": it states that spec 563 R1 and R2 are implemented, and that sections 4 and 5 below are the original design text, "kept as design history"; (c) change the section 4 and 5 headings to end "(implemented; section text is the original design)" in place of "(target contract, not yet built)", and add directly under each heading one line beginning "Status 2026-10-15:" that points at `pipelines/amazon/jobs/poe_freshness.py` (R1) or `api/alembic_supabase/versions/poe_competitor_source_link_563.py` (R2), says future-tense wording in the section ("intended", "will add", "planned") describes what was built, and, for R2, says the table exists in production "as of 2026-10-15 live read"; (d) reword "once R1 ships" (line 144) and "once R1 lands" (line 154) to "now that R1 is implemented"; reword the "Until R1 ships, ... remain the two live primitives ... not a column that exists yet" sentence (155) so it says `poe_freshness()` and `poe_coverage()` remain in `poe_freshness.py` alongside `poe_source_status` and that `poe_source_status` is a function, with no "until R1 ships"; (e) replace the "verified absent ... as of this doc" paragraph under section 5 with a sentence that the table was added by that migration; replace the "No backing column exists for this today. Until R2 ships ..." paragraph (193) with a sentence that before spec 563 R2 `catalog_competitor_asin` carried no own-ASIN linkage and the link table now holds it; (f) in the section 7 table change the seven "(R1, not yet built)" cells (lines 232-237 and 239) to "(R1, implemented)" and the whole row-238 parenthetical "(R2, **not yet built** — no backing table exists today)" to "(R2, implemented)" (drop the bold "not yet built", the em dash and "no backing table exists today"), and change "n/a until R2 ships" to "n/a". Leave section 8 ("What this doc does not do") unchanged: it describes this doc's scope.

R5. `docs/architecture/profit_v2_foundations.md`: change "Four parsers" to "Five parsers" and add a bullet for `ads_monthly_statement.py` saying it parses the Amazon Ads monthly billing statement (PDF) into one invoice line per row (campaign charges excluding tax); read the wording from the module's docstring. Do not label it "archive-only" or any status the module does not state. Do not change the batching sentence: `_chunks`/`_BATCH_SIZE` are in `ingestion/pipeline.py` and it is correct.

R6. `docs/architecture/profit_v2_schema_snapshot_plan_4_5.md`: replace the "This snapshot is the contract ..." sentence pair with a statement that this is a historical baseline captured 2026-05-26 and that where it differs from code or from the live catalog, the live catalog wins.

R7. `docs/architecture/listing-spine-strategic-improvement-backlog.md`: add one status paragraph after the header block (the Date/Updated/Status/Flow lines), separated from it by a blank line, beginning "As of 2026-10-15 live read", stating: the six tables named in the Goal above exist in production and each held 0 rows (exact `count(*)`, as of 2026-10-15 14:24 UTC); the "Current weakness" rows "Program run" and "Server-authoritative start plan" describe tables that now exist (`listing_program_runs`, `listing_program_nodes`, `listing_program_node_events` and `listing_program_start_plans_v4`), and row persistence is not shown (0 rows); this mapping covers those two rows and is not exhaustive (prod holds further `listing_program_*` tables that were not mapped); `listing_program_money_reservations` and `listing_program_human_authority_receipts` also exist but no row of the table claims they are missing; the other rows were not re-verified in this pass. Do not edit any table row.

R8. `docs/amazon-coverage/04-sp-api-endpoints.md`: change the Scope line to say the 12 groups were unused on 2026-04-25; add a paragraph beginning "Status 2026-10-15:" that says all 12 groups have a collector module under `pipelines/amazon/collectors/sp_endpoints/`, lists the 12 module file names from the Goal, notes that the title and Scope describe the 2026-04-25 state and the "Build Status - 2026-04-25" section records the probe results, and says "modules exist; not claimed to run in production" (this states nothing about whether the collectors run) (also change the title line "Unused Endpoints" to "Unused Endpoints (as of 2026-04-25)"; keep the file-name list in one paragraph; the phrase "12 of 12 groups" must appear on the first line of it); replace `amazon_sku_catalog` with `amazon_listings` (line 61).

R9. Fix the three dead doc paths, the one dead code path and the one script reference listed in the Goal, in `00-MASTER-PLAN.md`, `00-MORNING-PICKUP.md`, `02-brand-analytics-extras.md`, `COVERAGE-MATRIX.md` (line 698 only) and `08-notifications-and-marketplaces.md`. For the script in `08`, keep the surrounding text and add "(proposed; this script does not exist in the repo as of 2026-10-15)" on the same line as the command. That line (`08-notifications-and-marketplaces.md:355`) is a `# Run once:` comment inside a ```python fence (fence lines 354 and 359): append the note to the end of the comment line, as comment text, and add no backticks or blank line, so the fence is unchanged.

## Acceptance Criteria

AC1 [backend]: `architecture_dashboard.md` no longer names `flow_recommendations`, `flow_wiki`, `flow_autonomy_ledger`, `flow_findings` (as a table), `/profit-v2/console`, "719 lines", "(head; down_revision" or the old `intel/ingest_email.py` path; its "The Flow (agents)" section names `findings`, `recommendations`, `wiki_pages`, `autonomy_rules`, `write_flow_findings`, `agents/flow_common_findings.py` and `agents/recommendation_extractor.py`; the v2 routes list (from "**v2 routes**" to the "How modules map" heading) contains "route list is not exhaustive"; the line naming `schemas.ts` contains "wc -l"; the line naming `o5p6q7r8s9t0` contains "so it is not a head"; the doc contains the `scrapers/ingest_email.py` path. review_path: the verification chain below.

AC2 [backend]: `question-registry.md` no longer contains `/profit-v2/coverage`, contains `/profit-v2/settlement`, and its diff against the merge-base with `origin/master` is exactly one hunk. review_path: the verification chain below.

AC3 [backend]: `listing-l3-output-format.md` no longer contains `scripts/listing_l3`, and contains `pipelines/amazon/l3_deck/validate.py`, `validate_bundle` and "no command-line entry point". review_path: the verification chain below.

AC4 [backend]: `poe_source_contract.md` contains none of "not yet built", "not-yet-built", "verified absent", "no backing table exists today", "once R1 ships", "Until R1 ships", "Until R2 ships", "No backing column exists" or "n/a until R2 ships"; contains none of "once R1 lands"; contains "both are implemented (spec 563)", "kept as design history", `poe_competitor_source_link_563.py` and "R2, implemented"; sections 4 and 5 each carry a "Status 2026-10-15:" line and the heading text "implemented; section text is the original design"; section 5 says "2026-10-15 live read"; in the section 7 table "(R1, implemented)" occurs 7 times and "(R2, implemented)" once, so all eight table cells are relabelled. review_path: the verification chain below.

AC5 [backend]: `profit_v2_foundations.md` says "Five parsers" and no longer "Four parsers, all in", names `ads_monthly_statement.py` and "monthly billing statement", and does not contain "archive-only". The snapshot doc says "historical baseline" and no longer says "is the contract Plan 4/5 services query against". review_path: the verification chain below.

AC6 [backend]: The backlog contains a line starting "As of 2026-10-15 live read" whose paragraph names all six `listing_program_*` tables, "0 rows", "14:24 UTC", "Program run" and "Server-authoritative start plan". review_path: the verification chain below.

AC7 [backend]: `04-sp-api-endpoints.md` no longer says "not yet used in this codebase" or `amazon_sku_catalog`; contains "unused on 2026-04-25"; its "Status 2026-10-15:" paragraph contains "12 of 12 groups", "modules exist; not claimed to run in production" and all 12 module file names, and every module it names exists on disk. The three dead doc paths, the dead code path and the script reference are fixed (old strings absent, new strings present). review_path: the verification chain below.

AC8 [backend]: Each path this spec adds to a doc resolves on disk (or, for the script, is confirmed absent): the `test -f` checks and the loop over the module names listed in the Status paragraph of the verification chain. review_path: the verification chain below.

AC9 [backend]: File scope is mechanical: `git diff --name-only` against the merge-base with `origin/master` lists only files named in `writes:` (guard, true before and after). Line scope is not mechanical beyond AC2's one-hunk check. review_path: reviewer reads `git diff -U0 $(git merge-base HEAD origin/master)` and confirms every hunk is one of those named in R1 to R9; the chain does not prove this.

## Boundaries


In scope: the 13 files in `writes:`, text only. Out of scope: the 6,393-line `listing-spine-l2-l3-handoff-contract.md`, every other architecture doc, any code, migration or config change, and deciding whether the eight (L3 format v1) or twelve (governed program) L2 reasoner count is right.

Charter is null by design (this spec is free-standing, not part of a charter). The footprint check against the spec ledger and open PRs was done on 2026-10-15 and found no overlap with these 13 files.

### Not included

Rows from the audit that were dropped:

- `profit_v2_foundations.md` "`_chunks(rows, _BATCH_SIZE=500)`": claimed unlocatable; false positive, both are defined in `ingestion/pipeline.py` (lines 47 and 100).
- `00-MASTER-PLAN.md:80` "`api/rdt.py`": false positive; the line already reads `sp_api/rdt.py`, which is the real path.
- `COVERAGE-MATRIX.md:649` `docs/y-map/B6-sbv-status.md`: the file is at `docs/_archive/y-map/B6-sbv-status.md`, but the cell quotes a code comment and records that it could not corroborate the citation; left to its author.
- `sku-asin-mapping-MAP.md:285`: an "e.g." suggestion, not a claim.
- `fc_reference_model.md:3` status line: scoped to its own spec, correct as written.
- `listing-spine-l2-nodes-for-shlomo.md` and the handoff contract (missing pointer to `listing-spine-map-v2.md`): an omission, not an error, and the contract is out of scope.
- "Eight ratified names" in `listing-l3-output-format.md` versus "twelve L2 reasoners" elsewhere: the scope each count belongs to could not be proved from the repo (the authority docs it cites sit under `/home/albert/.do-it/content/` on the orchestration box). No note added.
- `amazon_clients_canonical.md` rows: handled by the separate spec `amazon-clients-canonical-stale-spec.md`.
- `wiki_links` as a table name in `architecture_dashboard.md`: it exists in prod (0 rows, 2026-10-15) but nothing in the repo ties it to the Flow agents, so it is not named.
- The rest of the `architecture_dashboard.md` route list: not checked against the directory in this pass; R1(b) says only that the list is not exhaustive.
- The "Planned shape", "intended location" and "will add" wording inside `poe_source_contract.md` sections 4 and 5: left as original design text under the R4(c) status line.
- `poe_source_contract.md` lines 129-131 ("the intended location ... live there today") and 170 ("R2 will add"): covered by the R4(c) Status lines, which say future-tense wording describes what was built. The "today" at lines 69, 84, 92 and 94 describes the ingest path and column state, not R1/R2, and was not re-verified.
- The row-count and other "Current weakness" rows in the backlog beyond persistence: not re-verified (R7 says so).

## Interfaces

Consumes:
- Repo and production facts as dated in Evidence (2026-10-15, base `65e5d5a3f`): `agents/flow_common_findings.py`, `pipelines/amazon/l3_deck/validate.py` `validate_bundle`, `pipelines/amazon/jobs/poe_freshness.py` `poe_source_status`, `api/app/lib/profit_v2/parsers/ads_monthly_statement.py`, `pipelines/amazon/collectors/sp_endpoints/*.py`.

Produces:
- No code signature; thirteen corrected documentation files.

Writes:
- docs/architecture/architecture_dashboard.md
- docs/architecture/question-registry.md
- docs/architecture/listing-l3-output-format.md
- docs/architecture/poe_source_contract.md
- docs/architecture/profit_v2_foundations.md
- docs/architecture/profit_v2_schema_snapshot_plan_4_5.md
- docs/architecture/listing-spine-strategic-improvement-backlog.md
- docs/amazon-coverage/04-sp-api-endpoints.md
- docs/amazon-coverage/00-MASTER-PLAN.md
- docs/amazon-coverage/00-MORNING-PICKUP.md
- docs/amazon-coverage/02-brand-analytics-extras.md
- docs/amazon-coverage/COVERAGE-MATRIX.md
- docs/amazon-coverage/08-notifications-and-marketplaces.md

## Constraints

- Docs only. No code, config, migration or test file is modified.
- Each dated figure carries its date; no undated number is introduced.
- The verification greps match single-line fixed phrases: keep each required phrase on one line (do not wrap it), and keep the old strings fully removed.
- Do not invent line numbers or counts; where R5 needs the parser's input format, read it from the module.
- The production facts in the docs (table existence, 0-row counts) are copied as dated facts with the wording "as of 2026-10-15 live read"; the builder must not alter or re-derive them.

## Decisions

- 04-sp-api-endpoints.md status line lists all 12 module names rather than saying "most": the directory holds exactly the 12 modules matching the 12 headings, so the list is checkable.
- poe_source_contract.md sections 4 and 5 get a status line under each heading instead of a rewrite of the design prose: the prose is a record of the spec 563 design, and a banner is cheaper and safer than rewriting it; only the lines that assert absence or future state ("verified absent", "once R1 ships", "Until R1/R2 ships", "No backing column", "n/a until R2 ships") are reworded.
- The line-12 banner in poe_source_contract.md is reworded rather than kept: after the relabel it would call the now-corrected labels historical.
- `ads_monthly_statement` is described by what the module does (Ads billing-statement PDF parser), with no archive or active label, because the doc has no such label and the module states none.
- `wiki_links` and any autonomy "ledger" table are not named in the Flow section: nothing in `agents/` supports them.
- The backlog paragraph goes after the whole header block, not between the Status and Flow lines, so the Status line's trailing line break is undisturbed.

## Assumptions

The table presence, absence and zero-row counts were read from production with read-only SELECTs (Supabase SQL, 2026-10-15; the six `listing_program_*` counts were re-run later the same day and again returned 0) and from the repo at the commit below; the builder cannot re-query prod, copies them as dated facts and must not alter them. The final read-only `count(*)` of the six tables (all 0) was taken 2026-10-15 14:24 UTC. The verification chain assumes `origin/master` has been fetched; it fails at its first step if that ref is missing, and uses `git merge-base HEAD origin/master` as the base, so the checks hold after the builder commits. The section-7 count in AC4 is scoped to section 7 only (the "(R1, implemented)" cells in section 3, lines 109-113, are outside it and are not counted).

Audited against: `65e5d5a3f2e33fdf07ed1615e22f9990653a7d91` (origin/master, 2026-10-15).

## Unknowns

Whether the 12 SP-API collectors run in production is not established; the docs the spec produces say so explicitly. Otherwise none.

## Verification

```bash
set -euo pipefail && cd "$(git rev-parse --show-toplevel)" && git rev-parse --verify -q origin/master >/dev/null && A=docs/architecture && C=docs/amazon-coverage && B="$(git merge-base HEAD origin/master)" &&
#P: python check `FILE START END PHRASE...` : every PHRASE is inside the span from START to the next END (END empty = to EOF)
P='import sys;a=sys.argv;t=open(a[1]).read();i=t.index(a[2]);s=t[i:t.find(a[3],i+9) if a[3] else None];sys.exit(not all(p in s for p in a[4:]))' &&
#L: python check `FILE PHRASE...` : some single line of FILE contains every PHRASE
L='import sys;a=sys.argv;sys.exit(not any(all(p in l for p in a[2:]) for l in open(a[1])))' &&
#R1 architecture_dashboard
! grep -qF 'flow_recommendations' $A/architecture_dashboard.md &&
! grep -qF 'flow_wiki' $A/architecture_dashboard.md &&
! grep -qF 'flow_autonomy_ledger' $A/architecture_dashboard.md &&
! grep -qF '`flow_findings`' $A/architecture_dashboard.md &&
! grep -qF '/profit-v2/console' $A/architecture_dashboard.md &&
! grep -qF '719 lines' $A/architecture_dashboard.md &&
! grep -qF '(head; down_revision' $A/architecture_dashboard.md &&
! grep -qF 'pipelines/amazon/intel/ingest_email.py' $A/architecture_dashboard.md &&
grep -qF 'pipelines/amazon/intel/scrapers/ingest_email.py' $A/architecture_dashboard.md &&
/usr/bin/python3 -c "$P" $A/architecture_dashboard.md '**v2 routes**' '## How modules map' 'route list is not exhaustive' &&
/usr/bin/python3 -c "$L" $A/architecture_dashboard.md 'schemas.ts' 'wc -l' &&
/usr/bin/python3 -c "$L" $A/architecture_dashboard.md 'o5p6q7r8s9t0' 'so it is not a head' &&
/usr/bin/python3 -c "$P" $A/architecture_dashboard.md '### The Flow (agents)' '### Data Trust' '`findings`' '`recommendations`' 'wiki_pages' 'autonomy_rules' 'write_flow_findings' 'agents/flow_common_findings.py' 'agents/recommendation_extractor.py' &&
#R2
! grep -qF '/profit-v2/coverage' $A/question-registry.md &&
grep -qF '/profit-v2/settlement' $A/question-registry.md &&
[ "$(git diff -U0 $B -- $A/question-registry.md | grep -c '^@@' || true)" = 1 ] &&
#R3
! grep -qF 'scripts/listing_l3' $A/listing-l3-output-format.md &&
grep -qF 'pipelines/amazon/l3_deck/validate.py' $A/listing-l3-output-format.md &&
grep -qF 'validate_bundle' $A/listing-l3-output-format.md &&
grep -qF 'no command-line entry point' $A/listing-l3-output-format.md &&
#R4 poe_source_contract
! grep -qF 'not yet built' $A/poe_source_contract.md &&
! grep -qF 'not-yet-built' $A/poe_source_contract.md &&
! grep -qF 'verified absent' $A/poe_source_contract.md &&
! grep -qF 'no backing table exists today' $A/poe_source_contract.md &&
! grep -qF 'once R1 ships' $A/poe_source_contract.md &&
! grep -qF 'once R1 lands' $A/poe_source_contract.md &&
! grep -qF 'Until R1 ships' $A/poe_source_contract.md &&
! grep -qF 'Until R2 ships' $A/poe_source_contract.md &&
! grep -qF 'No backing column exists' $A/poe_source_contract.md &&
! grep -qF 'n/a until R2 ships' $A/poe_source_contract.md &&
grep -qF 'both are implemented (spec 563)' $A/poe_source_contract.md &&
grep -qF 'kept as design history' $A/poe_source_contract.md &&
grep -qF 'poe_competitor_source_link_563.py' $A/poe_source_contract.md &&
grep -qF 'R2, implemented' $A/poe_source_contract.md &&
/usr/bin/python3 -c 'import sys;t=open(sys.argv[1]).read();s=t[t.index("## 7. Summary mapping table"):];s=s[:s.index("## 8.")];sys.exit(not (s.count("(R1, implemented)")==7 and s.count("(R2, implemented)")==1))' $A/poe_source_contract.md &&
/usr/bin/python3 -c "$P" $A/poe_source_contract.md '## 4. R1' '## 5. R2' 'implemented; section text is the original design' 'Status 2026-10-15:' 'poe_freshness.py' &&
/usr/bin/python3 -c "$P" $A/poe_source_contract.md '## 5. R2' '## 6. ' 'implemented; section text is the original design' 'Status 2026-10-15:' '2026-10-15 live read' 'poe_competitor_source_link_563.py' &&
#R5 / R6
! grep -qF 'Four parsers, all in' $A/profit_v2_foundations.md &&
grep -qF 'Five parsers' $A/profit_v2_foundations.md &&
grep -qF 'ads_monthly_statement.py' $A/profit_v2_foundations.md &&
grep -qF 'monthly billing statement' $A/profit_v2_foundations.md &&
#guard (true before and after the edit): the parser label is not invented
! grep -qF 'archive-only' $A/profit_v2_foundations.md &&
! grep -qF 'is the contract Plan 4/5 services query against' $A/profit_v2_schema_snapshot_plan_4_5.md &&
grep -qF 'historical baseline' $A/profit_v2_schema_snapshot_plan_4_5.md &&
#R7
grep -q '^As of 2026-10-15 live read' $A/listing-spine-strategic-improvement-backlog.md &&
/usr/bin/python3 -c 'import sys;t=open(sys.argv[1]).read();i=t.index("As of 2026-10-15 live read");s=t[i:t.index("\n\n",i)];sys.exit(0 if all(p in s for p in ["listing_program_runs","listing_program_nodes","listing_program_node_events","listing_program_start_plans_v4","listing_program_money_reservations","listing_program_human_authority_receipts","0 rows","14:24 UTC","Program run","Server-authoritative start plan"]) else 1)' $A/listing-spine-strategic-improvement-backlog.md &&
#R8
! grep -qF 'not yet used in this codebase' $C/04-sp-api-endpoints.md &&
! grep -qF 'amazon_sku_catalog' $C/04-sp-api-endpoints.md &&
grep -qF 'unused on 2026-04-25' $C/04-sp-api-endpoints.md &&
/usr/bin/python3 -c "$L" $C/04-sp-api-endpoints.md 'Unused Endpoints (as of 2026-04-25)' &&
/usr/bin/python3 -c 'import sys;t=open(sys.argv[1]).read();i=t.index("Status 2026-10-15:");s=t[i:t.index("\n\n",i)];m="listings_items aplus product_fees sales_metrics sellers solicitations messaging tokens replenishment awd listings_restrictions product_type_definitions".split();import os;sys.exit(0 if "12 of 12 groups" in s.split("\n")[0] and "modules exist; not claimed to run in production" in s and all(x+".py" in s and os.path.exists("pipelines/amazon/collectors/sp_endpoints/"+x+".py") for x in m) else 1)' $C/04-sp-api-endpoints.md &&
#R9
! grep -qF 'docs/amazon-platform-master.md' $C/00-MASTER-PLAN.md $C/00-MORNING-PICKUP.md $C/02-brand-analytics-extras.md &&
grep -qF 'docs/strategy/amazon-platform-master.md' $C/00-MASTER-PLAN.md &&
grep -qF 'docs/strategy/amazon-platform-master.md' $C/00-MORNING-PICKUP.md &&
grep -qF 'docs/strategy/amazon-platform-master.md' $C/02-brand-analytics-extras.md &&
! grep -qF 'docs/seller-central-reauth-checklist.md' $C/00-MORNING-PICKUP.md &&
grep -qF 'docs/playbooks/seller-central-reauth-checklist.md' $C/00-MORNING-PICKUP.md &&
! grep -qF 'api/../ads_gated.py' $C/COVERAGE-MATRIX.md &&
grep -qF 'pipelines/amazon/collectors/ads_gated.py' $C/COVERAGE-MATRIX.md &&
grep -qF 'this script does not exist in the repo as of 2026-10-15' $C/08-notifications-and-marketplaces.md &&
#guard (true before and after the edit): the target paths exist and the script path does not
test -f docs/strategy/amazon-platform-master.md &&
test -f docs/playbooks/seller-central-reauth-checklist.md &&
test -f pipelines/amazon/collectors/ads_gated.py &&
test -f pipelines/amazon/l3_deck/validate.py &&
test -f pipelines/amazon/intel/scrapers/ingest_email.py &&
test -f pipelines/amazon/jobs/poe_freshness.py &&
test -f api/alembic_supabase/versions/poe_competitor_source_link_563.py &&
test -f api/app/lib/profit_v2/parsers/ads_monthly_statement.py &&
test -f agents/flow_common_findings.py &&
test -f agents/recommendation_extractor.py &&
test -f "dashboard/src/app/(dashboard)/dashboard/clients/[name]/profit-v2/settlement/page.tsx" &&
test ! -e scripts/discover_marketplace_participations.py &&
#AC9 file scope
#guard (true before and after the edit): only writes files differ from the merge-base with origin/master
! git diff --name-only $B | grep -qvxE 'docs/architecture/(architecture_dashboard|question-registry|listing-l3-output-format|poe_source_contract|profit_v2_foundations|profit_v2_schema_snapshot_plan_4_5|listing-spine-strategic-improvement-backlog)\.md|docs/amazon-coverage/(04-sp-api-endpoints|00-MASTER-PLAN|00-MORNING-PICKUP|02-brand-analytics-extras|COVERAGE-MATRIX|08-notifications-and-marketplaces)\.md' &&
echo VERIFY_OK
```

## Dependencies

None. `docs/architecture/amazon_clients_canonical.md` is corrected by the separate spec `amazon-clients-canonical-stale-spec.md`; the two specs share no Writes file.
