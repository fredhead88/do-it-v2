#!/usr/bin/env bash
set -euo pipefail
BASE=9fd2a0547c3d56306d5edf471264221c71f4b000
if [ -f ./.env ]; then set -a; . ./.env; set +a; fi
cd /home/albert/.do-it/repos/albert-scott &&
R=docs/runbooks && P=$R/profit-v2-report-ingestion.md && L=$R/laptop-mirror-setup.md && C=$R/amazon-connect.md && D=$R/dev-onboarding.md &&
A='import sys;t=open(sys.argv[1],encoding="utf-8").read();a=t.index(sys.argv[2]);b=t.find(sys.argv[3],a+len(sys.argv[2])) if sys.argv[3] else -1;b=len(t) if b<0 else b;assert sys.argv[4] in t[a:b],sys.argv[4]' &&
N='import sys;t=open(sys.argv[1],encoding="utf-8").read();a=t.index(sys.argv[2]);b=t.find(sys.argv[3],a+len(sys.argv[2])) if sys.argv[3] else -1;b=len(t) if b<0 else b;assert sys.argv[4] not in t[a:b],sys.argv[4]' &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' 'HTTP 202' &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' 'raw_storage_path' &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' 'file_sha256' &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' 'curl "$DROPLET/api/v2/upload/status/{report_ingestion_id}?agency_id=albert_scott&client_id=qur_life"' &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' 'until `ingest_status` is `done` or `failed`' &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' 'read `lines_inserted` from the status reply' &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' '`report_ingestion_id` is an integer' &&
/usr/bin/python3 -I -c "$N" $P '### Method 2' '## Deduplication behavior' '"error": null' &&
/usr/bin/python3 -I -c "$N" $P '### Method 2' '## Deduplication behavior' '"lines_inserted": 1234' &&
/usr/bin/python3 -I -c "$A" $P '## Deduplication behavior' '## Recommended download cadence' 'HTTP 202' &&
/usr/bin/python3 -I -c "$A" $P '## Deduplication behavior' '## Recommended download cadence' 'existing `report_ingestion_id`' &&
/usr/bin/python3 -I -c "$A" $P '## Deduplication behavior' '## Recommended download cadence' 'File already registered' &&
/usr/bin/python3 -I -c "$A" $P '## Deduplication behavior' '## Recommended download cadence' 'raw_storage_path' &&
/usr/bin/python3 -I -c "$A" $P '## Deduplication behavior' '## Recommended download cadence' 're-run' &&
/usr/bin/python3 -I -c "$A" $P '## Deduplication behavior' '## Recommended download cadence' 'report_ingestion.file_sha256' &&
/usr/bin/python3 -I -c "$A" $P '## Deduplication behavior' '## Recommended download cadence' 're-ingest procedure' &&
/usr/bin/python3 -I -c "$A" $P '## Deduplication behavior' '## Recommended download cadence' 'current behaviour' &&
! grep -qF 'report_ingestion.sha256' $P &&
! grep -q 'skipped_duplicate' $P &&
! grep -q 'Safe to retry' $P &&
! grep -q 'Expected — already uploaded' $P &&
! grep -q 'duplicate file (SHA matches)' $P &&
/usr/bin/python3 -I -c "$A" $P '## Troubleshooting' '## What if' 'repeat of an earlier file' &&
/usr/bin/python3 -I -c "$A" $P '## Troubleshooting' '## What if' '`queued` or `ingesting`' &&
! grep -q 'duplicates skipped' $P &&
/usr/bin/python3 -I -c "$A" $P '### Method 1' '### Method 2' 'Ingest complete' &&
/usr/bin/python3 -I -c "$A" $P '### Method 1' '### Method 2' 'Ingest failed' &&
/usr/bin/python3 -I -c "$A" $P '## Order of upload matters' '## Verifying the upload landed' 'HTTP 410' &&
/usr/bin/python3 -I -c "$A" $P '## Order of upload matters' '## Verifying the upload landed' 'step 2 is skipped' &&
! grep -q 'lines_inserted": 1234' $P &&
! grep -q '"report_ingestion_id": "uuid"' $P &&
! grep -q 'you can fix by re-uploading the same' $P &&
! grep -q 'then re-upload settlement' $P &&
! grep -q 'export DROPLET="http://167.71.46.51:8000"' $P &&
grep -q 'export DROPLET="http://167.71.46.51"' $P &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' 'bound to localhost' &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' 'as of 2026-10-15 live read' &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' '`config/nginx-api-internal.conf`' &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' 'HTTP 413' &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' '50M' &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' 'Vercel' &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' 'not established' &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' 'not supported by the documented path' &&
/usr/bin/python3 -I -c "$A" $P '### Method 2' '## Deduplication behavior' 'plain HTTP' &&
grep -q 'client_max_body_size 50M' config/nginx-api-internal.conf &&
grep -q 'proxy_pass https://frontend-iota-seven-84.vercel.app' deploy/nginx-bluedot.conf &&
! grep -q 'client_max_body_size 500M' config/nginx-bluedot.conf &&
grep -q '^## The report types (four ingested types plus one archive-only type)$' $P &&
/usr/bin/python3 -I -c "$A" $P '## The report types' '## Upload methods' '`ads_monthly_statement`' &&
/usr/bin/python3 -I -c "$A" $P '## The report types' '## Upload methods' 'PDF only' &&
/usr/bin/python3 -I -c "$A" $P '## The report types' '## Upload methods' 'HTTP 410' &&
/usr/bin/python3 -I -c "$A" $P '## The report types' '## Upload methods' 'cut over' &&
/usr/bin/python3 -I -c "$A" $P '## The report types' '## Upload methods' '`settlement_spine_cutover`' &&
/usr/bin/python3 -I -c "$A" $P '## The report types' '## Upload methods' $'`state = \'cutover\'`' &&
/usr/bin/python3 -I -c "$A" $P '## Troubleshooting' '## What if' '| HTTP 410 on settlement' &&
/usr/bin/python3 -I -c "$A" $P '## Troubleshooting' '## What if' 'Use one of: settlement, all_orders, monthly_storage, advertised_product, ads_monthly_statement' &&
/usr/bin/python3 -I -c "$A" $P '## Active client onboarding checklist' '' '2. Upload all four ingested report types' &&
! grep -q 'The 4 report types' $P &&
! grep -q 'all four\*\*' $P &&
! grep -q 'Upload all 4 report types' $P &&
! grep -q 'Active client roster' $P &&
! grep -q 'extending this table' $P &&
/usr/bin/python3 -I -c "$A" $P '## The report types' '## Upload methods' 'does not validate `client_id` against a roster' &&
/usr/bin/python3 -I -c "$A" $P '## The report types' '## Upload methods' '`config/clients.json`' &&
/usr/bin/python3 -I -c "$A" $P '## The report types' '## Upload methods' '`clients` list' &&
/usr/bin/python3 -I -c "$A" $P '## The report types' '## Upload methods' '`status`' &&
/usr/bin/python3 -I -c "$A" $P '## The report types' '## Upload methods' '`active`' &&
grep -q '^1. Add the `client_id` to the project.s brand-mapping' $P &&
! grep -q 'unknown_bucket_count' $P &&
! grep -qF '`unknown_bucket`' $P &&
! grep -q 'recent_unknowns' $P &&
/usr/bin/python3 -I -c "$A" $P '## Troubleshooting' '## What if' 'fee_bucket_unknown_log' &&
/usr/bin/python3 -I -c "$A" $P '## Troubleshooting' '## What if' 'list_unknown_buckets' &&
/usr/bin/python3 -I -c "$A" $P '## Troubleshooting' '## What if' "fee_bucket = 'unallocated'" &&
/usr/bin/python3 -I -c "$A" $P '## What if' '## Active client onboarding checklist' '`unallocated`' &&
/usr/bin/python3 -I -c "$A" $P '## Troubleshooting' '## What if' 'also receives' &&
grep -q '_ARCHIVE_ONLY_TYPES = {"ads_monthly_statement"}' api/app/routers/upload_v2.py &&
grep -q '^def list_unknown_buckets' api/app/lib/profit_v2/services/fee_bucket_unknowns.py &&
grep -q 'UNALLOCATED = "unallocated"' api/app/lib/profit_v2/fee_buckets.py &&
grep -q 'INSERT INTO fee_bucket_unknown_log' api/app/lib/profit_v2/ingestion/pipeline.py &&
! grep -rq 'def recent_unknowns' api &&
grep -q '^## Prerequisites (box side)$' $L &&
/usr/bin/python3 -I -c "$A" $L '## Prerequisites' '## Step 1' 'The Syncthing host is unconfirmed' &&
/usr/bin/python3 -I -c "$A" $L '## Prerequisites' '## Step 1' 'as of 2026-10-15 live read' &&
/usr/bin/python3 -I -c "$A" $L '## Prerequisites' '## Step 1' 'a system-scope Syncthing unit was inactive' &&
/usr/bin/python3 -I -c "$A" $L '## Prerequisites' '## Step 1' 'user-scope Syncthing runs on either host was not established' &&
/usr/bin/python3 -I -c "$A" $L '## Prerequisites' '## Step 1' 'systemctl --user is-active syncthing' &&
/usr/bin/python3 -I -c "$A" $L '## Prerequisites' '## Step 1' 'ls /home/albert/md-mirror/RECENT.md' &&
/usr/bin/python3 -I -c "$A" $L '## Prerequisites' '## Step 1' 'the droplet (`167.71.46.51`)' &&
/usr/bin/python3 -I -c "$A" $L '## Prerequisites' '## Step 1' 'read it on the confirmed Syncthing host to get it' &&
/usr/bin/python3 -I -c "$A" $L '## Prerequisites' '## Step 1' 'Show ID' &&
/usr/bin/python3 -I -c "$A" $L '## Prerequisites' '## Step 1' 'it installs and starts a Syncthing user service' &&
! grep -q 'bash /opt/albert-scott/scripts/setup-syncthing.sh' $L &&
/usr/bin/python3 -I -c "$A" $L '## Step 3' '## Step 4' 'On the Syncthing host' &&
/usr/bin/python3 -I -c "$A" $L '## Step 4' '## Step 5' 'The Syncthing host folder shows "Send Only"' &&
/usr/bin/python3 -I -c "$A" $L '## Security note' '' 'The Syncthing GUI on the Syncthing host' &&
! grep -q 'Syncthing is running on the box as a systemd user service' $L &&
! grep -q 'run this on the box to get it' $L &&
! grep -q 'On the \*\*box\*\*' $L &&
! grep -q 'The box folder shows "Send Only"' $L &&
! grep -q 'The Syncthing GUI on the box' $L &&
! grep -q 'already done by orc' $L &&
! grep -q 'albert-scott-box' $L &&
grep -q 'albert-scott-mirror-host' $L &&
/usr/bin/python3 -I -c 'import re,sys;bad=[l for l in open(sys.argv[1],encoding="utf-8").read().split("\n") if re.search("box",re.sub("orchestration box|each box|\\(box side\\)","",l),re.I)];assert not bad,bad' $L &&
/usr/bin/python3 -I -c "$A" $L '## Troubleshooting' '## Security note' '`md_mirror` row in the `cron_runs` table' &&
/usr/bin/python3 -I -c "$A" $L '## Troubleshooting' '## Security note' 'Check the mirror cron health' &&
/usr/bin/python3 -I -c "$A" $L '## Troubleshooting' '## Security note' 'the droplet (`167.71.46.51`) or on the orchestration box' &&
/usr/bin/python3 -I -c "$A" $L '## Troubleshooting' '## Security note' 'each box keeps its own `md-mirror.log`' &&
/usr/bin/python3 -I -c "$A" $L '## Troubleshooting' '## Security note' 'deploy path' &&
grep -q 'bash /opt/albert-scott/scripts/md-mirror-assemble.sh' $L &&
grep -q '/opt/albert-scott/scripts/md-mirror-assemble.sh' deploy/cron/md_mirror.cron &&
test -f scripts/md-mirror-assemble.sh &&
! git grep -n 'recent_unknowns' -- . ':!docs/runbooks/profit-v2-report-ingestion.md' &&
/usr/bin/python3 -I -c "$A" $L '## Troubleshooting' '## Security note' 'Host that assembles the mirror: `tail -f' &&
! grep -q 'spec_ledger.py --check' $L &&
! grep -q 'ssh albert@167.71.46.51' $L &&
! grep -q 'Box: `tail -f' $L &&
! grep -q 'Check box cron health' $L &&
! grep -q 'Box cron running' $L &&
grep -q 'Mirror cron running?' $L &&
/usr/bin/python3 -I -c "$N" $C '## Invite and callback backend' '### How to check it' 'sections 3.2 and 4' &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' 'may need entries in' &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' 'After-connect steps' &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' '`first_backfill`' &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' '`cogs_seed`' &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' '`portal_login`' &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' '`config/amazon_client_identity.json`' &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' '`config/amazon_seller_accounts.json`' &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' '`config/amazon_financial_owners.json`' &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' '`config/amazon_client_registry_bridge.json`' &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' '`dashboard/src/lib/amazon-roster.ts`' &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' '`/api/amazon-roster`' &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' 'a hand edit of `client-switcher.tsx` is not needed' &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' 'hardcoded `AMAZON_CLIENTS` fallback' &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' 'non-staff' &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' 'does not claim to be exhaustive' &&
/usr/bin/python3 -I -c 'import re,sys;t=open(sys.argv[1],encoding="utf-8").read();a=t.index("## Invite and callback backend");b=t.index("### How to check it",a);assert not re.search(r"\bT[12]\b",t[a:b])' $C &&
/usr/bin/python3 -I -c "$A" $C '## Invite and callback backend' '### How to check it' 'Do not create another client name for a seller already present' &&
test -f config/amazon_client_identity.json && test -f config/amazon_seller_accounts.json && test -f config/amazon_financial_owners.json && test -f config/amazon_client_registry_bridge.json && test -f dashboard/src/lib/amazon-roster.ts && test -f dashboard/src/app/api/amazon-roster/route.ts &&
! grep -q 'do-it-v2/scripts/sweep/ro-sql' $C &&
/usr/bin/python3 -I -c "$A" $C '## After-connect steps' '### Rollback' 'any read-only SQL session against the production database' &&
/usr/bin/python3 -I -c "$A" $C '## After-connect steps' '### Rollback' 'the sweep tool `ro-sql` exists only on the orchestration box' &&
/usr/bin/python3 -I -c "$A" $C '## After-connect steps' '### Rollback' 'as of 2026-10-15 live read' &&
/usr/bin/python3 -I -c "$A" $C '## Niftyone token sync' '### How to check it' $'### Operator steps\n\nStatus as of 2026-10-15 live read:' &&
/usr/bin/python3 -I -c "$A" $C '## Niftyone token sync' '### How to check it' '`/root/as-export` does not exist' &&
/usr/bin/python3 -I -c "$A" $C '## Niftyone token sync' '### How to check it' 'no `NIFTYONE_*` keys' &&
/usr/bin/python3 -I -c "$A" $C '## Niftyone token sync' '### How to check it' 'missing NIFTYONE_SSH_TARGET' &&
/usr/bin/python3 -I -c "$A" $C '## Niftyone token sync' '### How to check it' 'installed but not operating' &&
/usr/bin/python3 -I -c "$A" $D '### 3a.' '### 3b.' '`SUPABASE_DB_URL_READONLY`' &&
/usr/bin/python3 -I -c "$A" $D '### 3a.' '### 3b.' 'bw://Albert Scott — Dev/SUPABASE_DB_URL_READONLY' &&
/usr/bin/python3 -I -c "$A" $D '### 3a.' '### 3b.' 'one item named exactly `Albert Scott — Dev`' &&
/usr/bin/python3 -I -c "$A" $D '### 3a.' '### 3b.' '"Albert Scott — Dev <name>"' &&
/usr/bin/python3 -I -c "$A" $D '### 3c.' '### 3d.' '`SUPABASE_DB_URL_READONLY`' &&
/usr/bin/python3 -I -c "$A" $D '### 3a.' '### 3b.' 'does not read the collection name' &&
/usr/bin/python3 -I -c "$A" $D '### 3a.' '### 3b.' '`SCRAPINGBEE_API_KEY`' &&
/usr/bin/python3 -I -c "$A" $D '### 3a.' '### 3b.' '`APIFY_API_TOKEN`' &&
/usr/bin/python3 -I -c "$A" $D '### 3a.' '### 3b.' '`KEEPA_API_KEY`' &&
/usr/bin/python3 -I -c "$A" $D '### 3a.' '### 3b.' '`IMPORTGENIUS_API_KEY`' &&
! grep -qP '(?<!SUPABASE_DB_URL_)READONLY' $D &&
/usr/bin/python3 -I -c "$A" $D '### 3d.' '## 4.' '2026-07-19 snapshot' &&
/usr/bin/python3 -I -c "$A" $D '### 3d.' '## 4.' 'the current list of credentials to rotate is `docs/architecture/credential_rotation_register.yml`' &&
/usr/bin/python3 -I -c "$A" $D '### 3d.' '## 4.' '**atlas, atlas_olivie, mouthwatchers, qur_life, supersmile,' &&
grep -q 'bw://Albert Scott — Dev/SUPABASE_DB_URL_READONLY' .env.dev.tpl &&
grep -q 'SCRAPINGBEE_API_KEY="bw://Albert Scott — Dev/SCRAPINGBEE_API_KEY"' .env.dev.tpl &&
grep -q 'APIFY_API_TOKEN="bw://Albert Scott — Dev/APIFY_API_TOKEN"' .env.dev.tpl &&
grep -q 'KEEPA_API_KEY="bw://Albert Scott — Dev/KEEPA_API_KEY"' .env.dev.tpl &&
grep -q 'IMPORTGENIUS_API_KEY="bw://Albert Scott — Dev/IMPORTGENIUS_API_KEY"' .env.dev.tpl &&
/usr/bin/python3 -I -c 'import sys,subprocess as s;r=s.run(["git","merge-base","--is-ancestor",sys.argv[1],"origin/master"]);r.returncode==0 or sys.exit("origin/master missing or lacks audited commit "+sys.argv[1]+"; run git fetch origin")' 65e5d5a3f2e33fdf07ed1615e22f9990653a7d91 &&
B="$BASE" &&
/usr/bin/python3 -I -c 'import sys,subprocess as s;o=set(s.check_output(["git","diff","--name-only",sys.argv[1]],text=True).split());assert o<=set(sys.argv[2:]),o' "$B" $P $L $C $D &&
H='import sys,re,subprocess as s,functools as F;b,f=sys.argv[1:3];L=open(f,encoding="utf-8").read().split("\n");fd=lambda p,c:next(k for k in range(c,len(L)) if L[k].startswith(p));rg=lambda sp:(lambda st,en:(lambda c:(c+1,fd(en,c+1) if en else len(L)))(F.reduce(lambda c,p:fd(p,c),st.split(">>"),0)))(*sp.split("|"));R=[rg(x) for x in sys.argv[3:]];d=s.check_output(["git","diff","-U0",b,"--",f],text=True);ck=lambda m:(lambda lo,hi:any(x<=lo and hi<=y for x,y in R) or sys.exit("hunk outside: %s %d-%d"%(f,lo,hi)))(int(m.group(1)),int(m.group(1))+max(int(m.group(2) if m.group(2) else 1),1)-1);[ck(m) for m in re.finditer(r"(?m)^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@",d)]' &&
/usr/bin/python3 -I -c "$H" "$B" $P '## The|## Upload methods' '### Method 1|### Method 2' '### Method 2|## Recommended download cadence' '## Order of upload matters|## Verifying the upload landed' '## Troubleshooting|## What if' '## What if|## Active client onboarding checklist' '## Active client onboarding checklist|' &&
/usr/bin/python3 -I -c "$H" "$B" $L '|## Step 1' '## Step 3|## Step 5' '## Step 6|' &&
/usr/bin/python3 -I -c "$H" "$B" $C '## Invite and callback backend|### How to check it' '## Niftyone token sync>>### Operator steps|### How to check it' '## After-connect steps>>### How to check it|### Rollback' &&
/usr/bin/python3 -I -c "$H" "$B" $D '### 3a.|### 3b.' '### 3c.|### 3d.' '### 3d.|## 4.' &&
echo VERIFY_OK
