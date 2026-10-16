#!/usr/bin/env bash
set -euo pipefail
BASE=9fd2a0547c3d56306d5edf471264221c71f4b000
if [ -f ./.env ]; then set -a; . ./.env; set +a; fi
set -euo pipefail && cd "$(git rev-parse --show-toplevel)" && git rev-parse --verify -q origin/master >/dev/null && A=docs/architecture && C=docs/amazon-coverage && B="$BASE" &&
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
