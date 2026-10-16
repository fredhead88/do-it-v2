#!/usr/bin/env bash
set -euo pipefail
if [ -f ./.env ]; then set -a; . ./.env; set +a; fi
cd /opt/albert-scott && P=docs/playbooks &&
/usr/bin/python3 -c 'import os,re,sys;t=open("docs/playbooks/onboard-new-client.md").read();bad=re.findall(r"scripts/(?!amazon/)(?:backfill_amazon_settlements|backfill_amazon_orders_by_date|backfill_sales_traffic_60d|seed_goya_cogs_from_pnl|seed_default_cogs_25pct)",t);miss=[n for n in re.findall(r"scripts/amazon/[A-Za-z0-9_]+\.py",t) if not os.path.exists(n)];sys.exit(0 if not bad and not miss else "wrong-path or missing scripts: %s %s"%(bad,miss))' &&
! grep -q 'seed_goya_cogs_from_pnl' $P/onboard-new-client.md &&
! grep -q 'applies automatically' $P/onboard-new-client.md &&
! grep -q 'run_all' $P/onboard-new-client.md &&
! grep -q 'TODO: build' $P/onboard-new-client.md &&
! grep -q 'client-switcher.tsx' $P/onboard-new-client.md &&
! grep -q 'AMAZON_CLIENTS' $P/onboard-new-client.md &&
grep -q '/dashboard/amazon-connect' $P/onboard-new-client.md &&
grep -q 'config/amazon_client_identity.json' $P/onboard-new-client.md &&
grep -q -e '--tier 1 --client' $P/onboard-new-client.md &&
! grep -qF -e '--days 730' $P/onboard-new-client.md &&
grep -q 'albert-scott-t1.lock' $P/onboard-new-client.md &&
! grep -qF 'only FastAPI changes' $P/ops-playbook.md &&
! grep -qF 'only pipeline/cron changes' $P/ops-playbook.md &&
! grep -q '24GB disk' $P/ops-playbook.md &&
! grep -q '~52% used' $P/ops-playbook.md &&
! grep -q 'maxconn=6' $P/ops-playbook.md &&
! grep -q '6 clients' $P/ops-playbook.md &&
! grep -q 'albert-scott-t2.lock' $P/ops-playbook.md &&
/usr/bin/python3 -c 'import sys;L=open("docs/playbooks/ops-playbook.md").read().splitlines();sys.exit(1 if any("amazon-tier2.log" in l and (l.startswith("ssh ") or l.startswith("| T2 log") or ">> /var/log/albert-scott/amazon-tier2.log" in l) for l in L) else 0)' &&
/usr/bin/python3 -c 'import sys;L=open("docs/playbooks/ops-playbook.md").read().splitlines();sys.exit(0 if any("t2.heartbeat" in l and "whichever" in l and "cron_runs" in l for l in L) else 1)' &&
! grep -q 'rsync -a /opt/albert-scott-prev/ /opt/albert-scott/' $P/ops-playbook.md &&
! grep -q 'amazon_feed_health_rollup.py' $P/ops-playbook.md &&
! grep -q 'Memory-guard cron runs every 5 minutes' $P/ops-playbook.md &&
! grep -q 'All 15 jobs have flock' $P/ops-playbook.md &&
grep -q '7,941 MB' $P/ops-playbook.md &&
/usr/bin/python3 -c 'import re,sys;t=open("docs/playbooks/ops-playbook.md").read();m=re.search(r"^### T2 nightly run was skipped or failed.*?(?=^### |^---$|\Z)",t,re.S|re.M);S=m.group(0) if m else "";need=["albert-scott-t2-seller.lock","--tier2-unit seller","cron_attest --job t2_seller","flock -n -E 99","7200","t2_ads","t2_pricing","amazon-tier2-ads.log","amazon-tier2-pricing.log"];miss=[n for n in need if n not in S];sys.exit(0 if m and not miss else "T2 section missing: %s"%miss)' &&
grep -q './deploy.sh --rollback --dry-run' $P/ops-playbook.md &&
grep -q 'cron_runs' $P/ops-playbook.md &&
/usr/bin/python3 -c 'import sys;L=open("docs/playbooks/ops-playbook.md").read().splitlines();sys.exit(0 if any("amazon-tier2.log" in l and "combined" in l and "2026-10-15" in l and not l.startswith(("ssh ","| ")) for l in L) else 1)' &&
/usr/bin/python3 -c 'import sys;L=[l for l in open("docs/playbooks/ops-playbook.md") if "memory-guard" in l.lower() or "memory guard" in l.lower()];sys.exit(0 if L and all(("not scheduled" in l or "when run by hand" in l) for l in L) else 1)' &&
/usr/bin/python3 -c 'import re,sys;t=open("docs/playbooks/ops-playbook.md").read().splitlines();hm=lambda n:[("%02d:%02d"%(int(m.group(2)),int(m.group(1)))) for m in (re.match(r"^(\d+) (\d+) ",l) for l in open("deploy/cron/amazon_tier2_%s.cron"%n)) if m][0];ok=len([l for l in t if "./deploy.sh --rollback" in l])>=2 and all(any((hm(n)+" UTC") in l and re.search(r"\b"+w+r"\b",l,re.I) for l in t) for n,w in (("seller","seller"),("ads","ads"),("pricing","pricing")));sys.exit(0 if ok else 1)' &&
! grep -q 'automatically on push' $P/deploy.md &&
! grep -q 'Dashboard auto-deploys on push' $P/deploy.md &&
! grep -qF '**Trap (fixed' $P/deploy.md &&
! grep -qF '| Flag | Rsyncs |' $P/deploy.md &&
! grep -qF 'direct** port (5432)' $P/deploy.md &&
! grep -qF 'per the manifest above' $P/deploy.md &&
! grep -qF 'every deploy mode' $P/data-flows.md &&
! grep -q 'advertised_product' $P/deploy.md &&
! grep -qF 'The webhook API uses the **pooler**' $P/deploy.md &&
grep -q 'whole tree' $P/deploy.md &&
grep -q 'cron install' $P/deploy.md &&
grep -q 'daily_release.py --now' $P/deploy.md &&
grep -q 'release-hold' $P/deploy.md &&
grep -q 'session mode' $P/deploy.md &&
grep -q 'RLS-bypass' $P/deploy.md &&
grep -q 'PIPELINE_POOL_MODE' $P/deploy.md &&
grep -q 'API_STATEMENT_TIMEOUT_MS' $P/deploy.md &&
/usr/bin/python3 -c 'import re,sys;t=open("docs/playbooks/deploy.md").read();m=re.search(r"^## Database connection \(Supavisor session mode\)\s*$.*?(?=^## |\Z)",t,re.S|re.M);m or sys.exit("heading not reworded to: ## Database connection (Supavisor session mode)");L=m.group(0).splitlines();stale=[l for l in L if "6543" in l and "PIPELINE_POOL_MODE" not in l and not re.search(r"\bnot\b",l)];sys.exit(0 if not stale and not re.search(r"\bdirect\b",m.group(0),re.I) and any("PIPELINE_POOL_MODE" in l and "6543" in l for l in L) else "stale pooler claim, or direct, or missing PIPELINE_POOL_MODE+6543 line: %s"%stale)' &&
! grep -qF 'times out at 60s' $P/deploy.md &&
! grep -qF '60s is a band-aid' $P/deploy.md &&
! grep -q 'maxconn=25' $P/postgres-perf.md &&
! grep -q "We're at 60s" $P/postgres-perf.md &&
grep -q 'pipeline_pool_maxconn()' $P/postgres-perf.md &&
grep -q 'API_STATEMENT_TIMEOUT_MS' $P/postgres-perf.md &&
! grep -Eq 'sp_(settlements|settlement_lines|orders_by_date|sales_traffic_daily|listings|inventory_summaries|advertised_product|ads_campaigns|orders|order_items|financial_events|fees_estimate)\b' $P/data-flows.md &&
/usr/bin/python3 -c 'import re,sys;A=set("amazon_ads_campaigns amazon_catalog_extensions amazon_sales_traffic amazon_asin_availability amazon_asin_catalog amazon_cash_pnl_lines amazon_fba_inventory_health amazon_fees_estimated amazon_financials amazon_inventory amazon_listings amazon_order_economics amazon_orders amazon_orders_by_date amazon_orders_flat amazon_settlement_lines amazon_settlements amazon_shipment_sales sp_listing_items".split());R=[l.split("|") for l in open("docs/playbooks/data-flows.md") if l.startswith("| ") and "collectors/" in l];chk=lambda c:("not confirmed" in c[3]) or (lambda ns:bool(ns) and all(n in A for n in ns))(re.findall(r"`([a-z_0-9]+)`",c[3]));sys.exit(0 if len(R)==10 and all(chk(c) for c in R) else 1)' &&
! grep -q 'nightly cron at 23:00 UTC' $P/data-flows.md &&
! grep -qF 'config/clients/<slug>' $P/data-flows.md &&
grep -q 'install_ams_listener_unit' $P/ams-listener-systemd-setup.md &&
grep -q 'enables and restarts' $P/ams-listener-systemd-setup.md &&
git rev-parse --verify origin/master >/dev/null &&
/usr/bin/python3 -c 'import subprocess,sys;W={"docs/playbooks/"+x for x in ("onboard-new-client.md","ops-playbook.md","deploy.md","postgres-perf.md","data-flows.md","ams-listener-systemd-setup.md")};b=subprocess.check_output(["git","merge-base","origin/master","HEAD"],text=True).strip();d=set(subprocess.check_output(["git","diff","--name-only",b],text=True).split());x=d-W;x and sys.exit("files outside the six: %s"%sorted(x));sys.exit(0)' &&
echo VERIFY_OK
